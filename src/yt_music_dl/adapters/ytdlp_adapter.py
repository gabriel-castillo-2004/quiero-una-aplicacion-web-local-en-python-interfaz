"""yt-dlp downloader adapter and ffmpeg preflight checker."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
import re
import shutil
import time

from yt_music_dl.models import (
    ALLOWED_AUDIO_FORMATS,
    DownloadManifest,
    DownloadRequest,
    ProgressSnapshot,
    TrackResult,
)
from yt_music_dl.validation import normalize_youtube_url


class CancelledDownloadError(Exception):
    """Raised from progress_hooks when a job is cooperatively cancelled."""


class FatalDownloadError(RuntimeError):
    """Raised on unrecoverable network, ffmpeg, or permission errors."""


_FATAL_ERROR_MARKERS = (
    "ffmpeg",
    "ffprobe",
    "permission denied",
    "read-only file system",
    "no space left",
    "unable to download webpage",
    "name or service not known",
    "temporary failure in name resolution",
    "connection refused",
    "timed out",
    "urlopen error",
)

_TRANSIENT_ERROR_MARKERS = (
    "429",
    "too many requests",
    "rate limit",
    "connection reset",
    "connection aborted",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "temporary failure",
    "http error 502",
    "http error 503",
    "http error 504",
    "broken pipe",
    "incomplete read",
    "network is unreachable",
)


_BOT_OR_403_MARKERS = (
    "403",
    "http error 403",
    "forbidden",
    "not a bot",
    "sign in to confirm",
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_YTDLP_ID_ERR_RE = re.compile(r"\[youtube(?:[:\w]+)?\]\s+([A-Za-z0-9_-]{6,}):\s*(.+)")


def _is_transient_error(msg: str) -> bool:
    """Return True when an error message indicates a transient network or rate-limit issue."""
    lower = msg.lower()
    return any(marker in lower for marker in _TRANSIENT_ERROR_MARKERS)


def _is_bot_or_403_error(msg: str) -> bool:
    """Return True when an error message indicates HTTP 403 or YouTube bot verification."""
    lower = msg.lower()
    return any(marker in lower for marker in _BOT_OR_403_MARKERS)


def _clean_ytdlp_error_line(raw: str) -> str:
    """Extract a concise, human-readable error line from yt-dlp/ffmpeg stderr output."""
    cleaned = _ANSI_RE.sub("", raw or "").strip()
    if cleaned.lower().startswith("error:"):
        cleaned = cleaned[6:].strip()
    match = _YTDLP_ID_ERR_RE.match(cleaned)
    if match:
        return match.group(2).strip()
    return cleaned


class _YtdlpCaptureLogger:
    """yt-dlp logger that captures stderr error lines per video ID and sequentially."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.errors_by_id: dict[str, str] = {}
        self.last_error: str | None = None
        self._consume_idx: int = 0

    def reset(self) -> None:
        self.errors.clear()
        self.errors_by_id.clear()
        self.last_error = None
        self._consume_idx = 0

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        text = str(msg or "").strip()
        if not text:
            return
        lower = text.lower()
        if _is_bot_or_403_error(lower) or "unavailable" in lower or "ffmpeg" in lower:
            clean = _clean_ytdlp_error_line(text)
            if clean:
                self.last_error = clean

    def error(self, msg: str) -> None:
        raw = _ANSI_RE.sub("", str(msg or "")).strip()
        if not raw:
            return
        match = _YTDLP_ID_ERR_RE.search(raw)
        clean = _clean_ytdlp_error_line(raw)
        if not clean:
            return
        if match:
            self.errors_by_id[match.group(1)] = clean
        self.errors.append(clean)
        self.last_error = clean

    def get_error(self, track_id: str | None) -> str | None:
        if track_id and track_id in self.errors_by_id:
            return self.errors_by_id[track_id]
        return None

    def pop_next_error(self) -> str | None:
        if self._consume_idx < len(self.errors):
            val = self.errors[self._consume_idx]
            self._consume_idx += 1
            return val
        return None

    def has_bot_or_403_error(self) -> bool:
        if self.last_error and _is_bot_or_403_error(self.last_error):
            return True
        return any(_is_bot_or_403_error(err) for err in self.errors)


def _check_ytdlp_status() -> dict[str, Any]:
    """Check whether yt-dlp is importable and in PATH, returning its version."""
    ytdlp_path = shutil.which("yt-dlp")
    try:
        import yt_dlp

        version = getattr(getattr(yt_dlp, "version", None), "__version__", None)
        return {
            "available": True,
            "version": str(version) if version else None,
            "path": ytdlp_path,
            "error": None,
        }
    except Exception as exc:
        return {
            "available": False,
            "version": None,
            "path": ytdlp_path,
            "error": str(exc),
        }


class SystemPreflightAdapter:
    """Checks availability of ffmpeg, ffprobe, and yt-dlp."""

    def check_ffmpeg(self) -> dict[str, Any]:
        ffmpeg_path = shutil.which("ffmpeg")
        ffprobe_path = shutil.which("ffprobe")
        return {
            "available": bool(ffmpeg_path and ffprobe_path),
            "ffmpeg": bool(ffmpeg_path),
            "ffprobe": bool(ffprobe_path),
            "ffmpeg_path": ffmpeg_path,
            "ffprobe_path": ffprobe_path,
        }

    def check_ytdlp(self) -> dict[str, Any]:
        """Check availability and version of yt-dlp."""
        return _check_ytdlp_status()


def _default_ydl_factory(opts: dict[str, Any]) -> Any:
    import yt_dlp

    return yt_dlp.YoutubeDL(opts)


def build_ytdlp_options(
    *,
    request: DownloadRequest,
    staging_dir: Path,
    archive_path: Path,
    progress_hook: Callable[[dict[str, Any]], None],
    logger: Any = None,
    extractor_args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Construct yt-dlp configuration for single tracks or playlists."""
    if request.audio_format not in ALLOWED_AUDIO_FORMATS:
        raise ValueError(f"Unsupported audio format: {request.audio_format!r}")

    if request.is_playlist:
        outtmpl = str(
            staging_dir
            / "%(playlist_title)s"
            / "%(playlist_index&{:02d} - |)s%(title)s.%(ext)s"
        )
    else:
        outtmpl = str(staging_dir / "%(title)s.%(ext)s")

    if request.audio_format == "m4a" and request.quality == "best":
        preferred_quality = "0"
    elif request.quality in {"320", "192", "best"}:
        preferred_quality = "0" if request.quality == "best" else request.quality
    else:
        preferred_quality = "192"

    postprocessors: list[dict[str, Any]] = []
    if request.audio_format in {"mp3", "m4a"}:
        postprocessors.append(
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": request.audio_format,
                "preferredquality": preferred_quality,
            }
        )
    postprocessors.extend(
        [
            {
                "key": "FFmpegThumbnailsConvertor",
                "format": "jpg",
            },
            {
                "key": "FFmpegMetadata",
                "add_metadata": True,
            },
            {
                "key": "EmbedThumbnail",
            },
        ]
    )

    opts: dict[str, Any] = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "download_archive": str(archive_path),
        "writethumbnail": True,
        "noplaylist": not request.is_playlist,
        "ignoreerrors": request.is_playlist,
        "quiet": True,
        "no_warnings": True,
        "sleep_interval": 1.5,
        "max_sleep_interval": 3.5,
        "retries": 3,
        "fragment_retries": 3,
        "progress_hooks": [progress_hook],
        "postprocessors": postprocessors,
    }
    if logger is not None:
        opts["logger"] = logger
    if extractor_args is not None:
        opts["extractor_args"] = extractor_args
    if request.is_playlist and request.selected_tracks:
        opts["playlist_items"] = ",".join(str(idx) for idx in request.selected_tracks)
    return opts


class YtDlpAdapter:
    """DownloaderPort implementation backed by yt_dlp.YoutubeDL."""

    def __init__(
        self,
        ydl_factory: Callable[[dict[str, Any]], Any] | None = None,
        *,
        max_retries: int = 3,
        backoff_base: float = 1.0,
    ) -> None:
        self._ydl_factory = ydl_factory or _default_ydl_factory
        self._max_retries = max(0, int(max_retries))
        self._backoff_base = max(0.01, float(backoff_base))

    def download(
        self,
        request: DownloadRequest,
        staging_dir: Path,
        archive_path: Path,
        on_progress: Callable[[ProgressSnapshot], None],
        is_cancelled: Callable[[], bool],
    ) -> DownloadManifest:
        downloaded: list[str] = []
        skipped_archive: list[str] = []
        failed: list[dict[str, str]] = []
        tracks: list[TrackResult] = []
        seen_archive_before = self._read_archive_ids(archive_path)

        def progress_hook(data: dict[str, Any]) -> None:
            if is_cancelled():
                raise CancelledDownloadError("Download cancelled by user.")

            status = data.get("status")
            info = data.get("info_dict") or {}
            title = info.get("title") or data.get("filename") or "Track"
            thumb = info.get("thumbnail")
            pl_index = info.get("playlist_index")
            pl_count = info.get("n_entries") or info.get("playlist_count")
            pl_prefix = f"Canción {pl_index} de {pl_count} • " if pl_index and pl_count else ""

            if status == "downloading":
                total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
                downloaded_bytes = data.get("downloaded_bytes") or 0
                pct = (downloaded_bytes / total * 100.0) if total > 0 else 0.0
                on_progress(
                    ProgressSnapshot(
                        job_id="",
                        status="downloading",
                        progress=round(min(max(pct, 0.0), 99.0), 1),
                        current_track=str(title),
                        thumbnail_url=thumb,
                        message=f"{pl_prefix}Descargando {title}",
                    )
                )
            elif status == "finished":
                on_progress(
                    ProgressSnapshot(
                        job_id="",
                        status="postprocessing",
                        progress=99.0,
                        current_track=str(title),
                        thumbnail_url=thumb,
                        message=f"{pl_prefix}Procesando audio: {title}",
                    )
                )
            elif status == "error":
                raise FatalDownloadError(str(data.get("error") or "Download error"))

        capture_logger = _YtdlpCaptureLogger()
        opts = build_ytdlp_options(
            request=request,
            staging_dir=staging_dir,
            archive_path=archive_path,
            progress_hook=progress_hook,
            logger=capture_logger,
        )
        target_url = normalize_youtube_url(request.url)

        metadata_by_index: dict[int, dict[str, Any]] = {}
        metadata_by_pos: dict[int, dict[str, Any]] = {}
        for pos, meta in enumerate(request.track_metadata, start=1):
            if isinstance(meta, dict):
                metadata_by_pos[pos] = meta
                raw_idx = meta.get("index")
                if isinstance(raw_idx, int):
                    metadata_by_index[raw_idx] = meta

        info: Any = None
        current_opts = dict(opts)
        used_fallback_client = False
        attempt = 0
        while True:
            if is_cancelled():
                raise CancelledDownloadError("Download cancelled by user.")
            try:
                with self._ydl_factory(current_opts) as ydl:
                    info = ydl.extract_info(target_url, download=True)

                entries_check = info.get("entries") if isinstance(info, dict) else None
                if (
                    entries_check is not None
                    and any(e is None for e in entries_check)
                    and capture_logger.has_bot_or_403_error()
                    and not used_fallback_client
                ):
                    used_fallback_client = True
                    capture_logger.reset()
                    current_opts = {
                        **opts,
                        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
                    }
                    continue
                break
            except CancelledDownloadError:
                raise
            except Exception as exc:
                msg = str(exc)
                if _is_bot_or_403_error(msg) and not used_fallback_client:
                    used_fallback_client = True
                    capture_logger.reset()
                    current_opts = {
                        **opts,
                        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
                    }
                    continue
                if _is_transient_error(msg) and attempt < self._max_retries:
                    delay = self._backoff_base * (2 ** attempt)
                    attempt += 1
                    time.sleep(delay)
                    if is_cancelled():
                        raise CancelledDownloadError("Download cancelled by user.")
                    continue
                lower = msg.lower()
                if any(marker in lower for marker in _FATAL_ERROR_MARKERS) or not request.is_playlist:
                    raise FatalDownloadError(msg) from exc
                failed.append({"id": "playlist", "reason": _clean_ytdlp_error_line(msg) or msg})
                info = None
                break

        if is_cancelled():
            raise CancelledDownloadError("Download cancelled by user.")

        if info is None and not failed:
            raise FatalDownloadError("Failed to extract media information.")

        entries = info.get("entries") if isinstance(info, dict) else None
        if entries is not None:
            for idx, entry in enumerate(entries, start=1):
                orig_index = (
                    request.selected_tracks[idx - 1]
                    if request.selected_tracks and idx - 1 < len(request.selected_tracks)
                    else idx
                )
                meta = metadata_by_index.get(orig_index) or metadata_by_pos.get(idx) or {}
                if entry is None:
                    err_id = str(meta.get("id") or f"track-{orig_index}")
                    real_title = str(meta.get("title") or f"Pista {orig_index}")
                    track_url = meta.get("url")
                    reason = (
                        capture_logger.get_error(err_id)
                        or capture_logger.pop_next_error()
                        or capture_logger.last_error
                        or "Video unavailable or private"
                    )
                    failed.append({"id": err_id, "title": real_title, "reason": reason})
                    tracks.append(
                        TrackResult(
                            track_id=err_id,
                            title=real_title,
                            status="failed",
                            reason=reason,
                            url=str(track_url) if track_url else None,
                        )
                    )
                    continue

                vid = str(entry.get("id") or meta.get("id") or f"track-{orig_index}")
                title = str(entry.get("title") or meta.get("title") or vid)
                track_url = entry.get("webpage_url") or entry.get("url") or meta.get("url")
                if vid in seen_archive_before:
                    skipped_archive.append(vid)
                    tracks.append(
                        TrackResult(
                            track_id=vid,
                            title=title,
                            status="skipped",
                            url=str(track_url) if track_url else None,
                        )
                    )
                else:
                    downloaded.append(title)
                    tracks.append(
                        TrackResult(
                            track_id=vid,
                            title=title,
                            status="downloaded",
                            url=str(track_url) if track_url else None,
                        )
                    )
        elif isinstance(info, dict):
            vid = str(info.get("id") or "track")
            title = str(info.get("title") or vid)
            if vid in seen_archive_before:
                skipped_archive.append(vid)
                tracks.append(TrackResult(track_id=vid, title=title, status="skipped"))
            else:
                downloaded.append(title)
                tracks.append(TrackResult(track_id=vid, title=title, status="downloaded"))

        return DownloadManifest(
            downloaded=tuple(downloaded),
            skipped_archive=tuple(skipped_archive),
            failed=tuple(failed),
            tracks=tuple(tracks),
        )

    def preview(self, url: str) -> dict[str, Any]:
        """Extract metadata for a YouTube video or playlist without downloading."""
        target_url = normalize_youtube_url(url)
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": "in_playlist",
            "skip_download": True,
        }
        with self._ydl_factory(opts) as ydl:
            info = ydl.extract_info(target_url, download=False)

        if not isinstance(info, dict):
            raise FatalDownloadError("No se pudieron obtener metadatos de la URL.")

        entries_raw = info.get("entries")
        is_playlist = info.get("_type") == "playlist" or entries_raw is not None
        entries = [e for e in entries_raw if e is not None] if entries_raw is not None else []

        if is_playlist:
            track_count = int(info.get("playlist_count") or len(entries))
            title = str(info.get("title") or "Playlist de YouTube")
            author = str(
                info.get("uploader")
                or info.get("channel")
                or info.get("playlist_uploader")
                or "YouTube"
            )
        else:
            track_count = 1
            title = str(info.get("title") or "Pista de YouTube")
            author = str(
                info.get("uploader")
                or info.get("channel")
                or info.get("artist")
                or "YouTube"
            )

        thumbnail = info.get("thumbnail")
        if not thumbnail and isinstance(info.get("thumbnails"), list) and info["thumbnails"]:
            thumbnail = info["thumbnails"][-1].get("url")
        if not thumbnail and entries:
            first = entries[0]
            if isinstance(first, dict):
                thumbnail = first.get("thumbnail")
                if not thumbnail and isinstance(first.get("thumbnails"), list) and first["thumbnails"]:
                    thumbnail = first["thumbnails"][-1].get("url")

        tracks: list[dict[str, Any]] = []
        if is_playlist:
            for idx, entry in enumerate(entries, start=1):
                if not isinstance(entry, dict):
                    continue
                entry_thumb = entry.get("thumbnail")
                if (
                    not entry_thumb
                    and isinstance(entry.get("thumbnails"), list)
                    and entry["thumbnails"]
                ):
                    entry_thumb = entry["thumbnails"][-1].get("url")
                entry_id = entry.get("id") or f"track-{idx}"
                entry_url = (
                    entry.get("webpage_url")
                    or entry.get("url")
                    or (f"https://www.youtube.com/watch?v={entry['id']}" if entry.get("id") else None)
                )
                tracks.append(
                    {
                        "index": idx,
                        "id": entry_id,
                        "title": str(entry.get("title") or f"Pista {idx}"),
                        "url": entry_url,
                        "duration": entry.get("duration"),
                        "thumbnail": entry_thumb,
                        "thumbnail_url": entry_thumb,
                    }
                )

        return {
            "title": title,
            "author": author,
            "channel": author,
            "uploader": author,
            "thumbnail": thumbnail,
            "thumbnail_url": thumbnail,
            "track_count": track_count,
            "is_playlist": is_playlist,
            "duration": info.get("duration"),
            "tracks": tracks,
        }

    @staticmethod
    def _read_archive_ids(archive_path: Path) -> set[str]:
        if not archive_path.exists():
            return set()
        ids: set[str] = set()
        for line in archive_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            parts = line.strip().split()
            if len(parts) >= 2:
                ids.add(parts[1])
        return ids
