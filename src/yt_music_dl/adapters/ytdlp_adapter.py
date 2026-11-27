"""yt-dlp downloader adapter and ffmpeg preflight checker."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
import shutil

from yt_music_dl.models import (
    ALLOWED_AUDIO_FORMATS,
    DownloadManifest,
    DownloadRequest,
    ProgressSnapshot,
    TrackResult,
)


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


class SystemPreflightAdapter:
    """Checks availability of ffmpeg and ffprobe on PATH."""

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


def _default_ydl_factory(opts: dict[str, Any]) -> Any:
    import yt_dlp

    return yt_dlp.YoutubeDL(opts)


def build_ytdlp_options(
    *,
    request: DownloadRequest,
    staging_dir: Path,
    archive_path: Path,
    progress_hook: Callable[[dict[str, Any]], None],
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
        "progress_hooks": [progress_hook],
        "postprocessors": postprocessors,
    }
    if request.is_playlist and request.selected_tracks:
        opts["playlist_items"] = ",".join(str(idx) for idx in request.selected_tracks)
    return opts


class YtDlpAdapter:
    """DownloaderPort implementation backed by yt_dlp.YoutubeDL."""

    def __init__(
        self,
        ydl_factory: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self._ydl_factory = ydl_factory or _default_ydl_factory

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

        opts = build_ytdlp_options(
            request=request,
            staging_dir=staging_dir,
            archive_path=archive_path,
            progress_hook=progress_hook,
        )

        try:
            with self._ydl_factory(opts) as ydl:
                info = ydl.extract_info(request.url, download=True)
        except CancelledDownloadError:
            raise
        except Exception as exc:
            msg = str(exc)
            lower = msg.lower()
            if any(marker in lower for marker in _FATAL_ERROR_MARKERS) or not request.is_playlist:
                raise FatalDownloadError(msg) from exc
            failed.append({"id": "playlist", "reason": msg})
            info = None

        if is_cancelled():
            raise CancelledDownloadError("Download cancelled by user.")

        if info is None and not failed:
            raise FatalDownloadError("Failed to extract media information.")

        entries = info.get("entries") if isinstance(info, dict) else None
        if entries is not None:
            for idx, entry in enumerate(entries, start=1):
                if entry is None:
                    err_id = f"entry-{idx}"
                    reason = "Video unavailable or private"
                    failed.append({"id": err_id, "reason": reason})
                    tracks.append(
                        TrackResult(
                            track_id=err_id,
                            title=err_id,
                            status="failed",
                            reason=reason,
                        )
                    )
                    continue

                vid = str(entry.get("id") or f"entry-{idx}")
                title = str(entry.get("title") or vid)
                if vid in seen_archive_before:
                    skipped_archive.append(vid)
                    tracks.append(
                        TrackResult(track_id=vid, title=title, status="skipped")
                    )
                else:
                    downloaded.append(title)
                    tracks.append(
                        TrackResult(track_id=vid, title=title, status="downloaded")
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
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": "in_playlist",
            "skip_download": True,
        }
        with self._ydl_factory(opts) as ydl:
            info = ydl.extract_info(url, download=False)

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
                tracks.append(
                    {
                        "index": idx,
                        "id": entry.get("id") or f"track-{idx}",
                        "title": str(entry.get("title") or f"Pista {idx}"),
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
