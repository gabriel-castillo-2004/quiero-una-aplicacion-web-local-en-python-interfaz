"""Local filesystem adapter implementing FileSystemPort."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import shutil
import uuid

from yt_music_dl.validation import sanitize_filename

AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {".mp3", ".m4a", ".opus", ".ogg", ".wav", ".flac", ".aac"}
)


class LocalFileSystemAdapter:
    """Filesystem adapter backed by pathlib and shutil."""

    def default_music_dir(self) -> Path:
        """Return ~/Music or ~/Música if it exists, defaulting to ~/Music."""
        home = Path.home()
        for candidate_name in ("Music", "Música"):
            candidate = home / candidate_name
            if candidate.exists() and candidate.is_dir():
                return candidate
        return home / "Music"

    def resolve_dir(self, raw_path: str) -> Path:
        """Expand user home (~) and resolve the directory path."""
        expanded = Path(raw_path.strip()).expanduser()
        return expanded.resolve()

    def ensure_writable(self, path: Path) -> None:
        """Create path if needed and verify real write access with a probe file."""
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            raise NotADirectoryError(f"Path is not a directory: {path}")

        probe = path / f".yt_music_dl_probe_{uuid.uuid4().hex}"
        try:
            probe.write_bytes(b"ok")
        finally:
            if probe.exists():
                probe.unlink(missing_ok=True)

    def sanitize_name(self, name: str, max_len: int = 120) -> str:
        """Sanitize a file or folder name."""
        return sanitize_filename(name, max_len=max_len)

    def promote_file(
        self,
        src: Path,
        dest_dir: Path,
        relative_path: Path | None = None,
    ) -> Path:
        """Sanitize target path components and move src into dest_dir."""
        rel = relative_path if relative_path is not None else Path(src.name)
        sanitized_parts: list[str] = []
        for idx, part in enumerate(rel.parts):
            if idx == len(rel.parts) - 1:
                stem = self.sanitize_name(Path(part).stem)
                suffix = Path(part).suffix.lower()
                sanitized_parts.append(f"{stem}{suffix}")
            else:
                sanitized_parts.append(self.sanitize_name(part))

        target = dest_dir.joinpath(*sanitized_parts)
        resolved_dest = dest_dir.resolve()
        resolved_target = target.resolve()
        if resolved_dest not in resolved_target.parents and resolved_target != resolved_dest:
            raise ValueError(f"Refusing to promote file outside destination: {target}")

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(target))
        return target

    def cleanup_dir(self, path: Path) -> None:
        """Remove a staging directory tree safely."""
        shutil.rmtree(path, ignore_errors=True)

    def list_audio_files(
        self,
        base_dir: Path,
        extensions: frozenset[str] = AUDIO_EXTENSIONS,
    ) -> list[dict[str, Any]]:
        """List audio files recursively under base_dir without escaping it."""
        if not base_dir.exists() or not base_dir.is_dir():
            return []

        base_resolved = base_dir.resolve()
        items: list[dict[str, Any]] = []
        for entry in base_resolved.rglob("*"):
            if entry.name.startswith("."):
                continue
            try:
                resolved = entry.resolve()
                rel = resolved.relative_to(base_resolved)
            except (OSError, ValueError):
                continue

            if not resolved.is_file():
                continue
            suffix = resolved.suffix.lower()
            if suffix not in extensions:
                continue

            rel_posix = rel.as_posix()
            folder = rel.parent.as_posix() if rel.parent != Path(".") else ""
            stat = resolved.stat()
            items.append(
                {
                    "name": resolved.name,
                    "title": resolved.stem,
                    "path": rel_posix,
                    "relative_path": rel_posix,
                    "format": suffix.lstrip("."),
                    "folder": folder,
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                }
            )

        items.sort(key=lambda item: item["relative_path"].lower())
        return items
