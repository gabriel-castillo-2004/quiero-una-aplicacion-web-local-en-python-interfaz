"""Defensive validation for YouTube URLs, destination paths, and filenames."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse
import re

from yt_music_dl.ports import FileSystemPort

ALLOWED_YT_HOSTS: frozenset[str] = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
    }
)

WINDOWS_RESERVED_NAMES: frozenset[str] = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)

FORBIDDEN_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class ValidationError(ValueError):
    """Raised when external input fails validation."""


def validate_youtube_url(url: str) -> str:
    """Validate that url is an HTTP/HTTPS YouTube video or playlist URL.

    Returns the stripped URL if valid, or raises ValidationError.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValidationError("URL must be a non-empty string.")

    cleaned = url.strip()
    parsed = urlparse(cleaned)

    if parsed.scheme not in {"http", "https"}:
        raise ValidationError("URL scheme must be http or https.")

    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_YT_HOSTS:
        raise ValidationError(f"Unsupported YouTube host: {host!r}")

    if host == "youtu.be":
        video_id = parsed.path.strip("/")
        if not video_id:
            raise ValidationError("Missing video ID in youtu.be URL.")
        return cleaned

    path = parsed.path.rstrip("/")
    qs = parse_qs(parsed.query)

    if path == "/watch" and qs.get("v", [""])[0].strip():
        return cleaned
    if path == "/playlist" and qs.get("list", [""])[0].strip():
        return cleaned
    if path.startswith("/shorts/") and len(path.split("/")) >= 3 and path.split("/")[2]:
        return cleaned

    raise ValidationError("URL is neither a valid YouTube video nor a playlist.")


def is_playlist_url(url: str) -> bool:
    """Return True if the validated YouTube URL targets a playlist."""
    parsed = urlparse(url.strip())
    path = parsed.path.rstrip("/")
    qs = parse_qs(parsed.query)
    return path == "/playlist" or bool(qs.get("list", [""])[0].strip())


def sanitize_filename(name: str, max_len: int = 120) -> str:
    """Sanitize a filename or folder name for Windows and POSIX filesystems.

    Removes forbidden characters, collapses whitespace, avoids Windows reserved
    device names, and truncates safely to max_len characters.
    """
    if max_len < 1:
        raise ValueError("max_len must be at least 1.")

    cleaned = FORBIDDEN_FILENAME_CHARS_RE.sub("_", name or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = "untitled"

    stem_upper = cleaned.split(".")[0].upper()
    if stem_upper in WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"

    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(" .")
        if not cleaned:
            cleaned = "untitled"

    return cleaned


def validate_destination_dir(raw_path: str, fs: FileSystemPort) -> Path:
    """Resolve raw_path through fs and verify real write permissions."""
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValidationError("Destination directory must be a non-empty path.")

    try:
        resolved = fs.resolve_dir(raw_path)
    except (OSError, ValueError) as exc:
        raise ValidationError(f"Invalid destination directory: {exc}") from exc

    try:
        fs.ensure_writable(resolved)
    except (OSError, PermissionError) as exc:
        raise ValidationError(f"Destination directory is not writable: {exc}") from exc

    return resolved
