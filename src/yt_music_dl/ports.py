"""Port interfaces (Protocols) isolating external dependencies from application core."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable, Protocol, runtime_checkable
import queue

from yt_music_dl.models import DownloadManifest, DownloadRequest, JobState, ProgressSnapshot


@runtime_checkable
class DownloaderPort(Protocol):
    """Port for media downloaders (e.g., yt-dlp)."""

    def download(
        self,
        request: DownloadRequest,
        staging_dir: Path,
        archive_path: Path,
        on_progress: Callable[[ProgressSnapshot], None],
        is_cancelled: Callable[[], bool],
    ) -> DownloadManifest:
        """Download audio into staging_dir using archive_path for deduplication."""
        ...


@runtime_checkable
class FileSystemPort(Protocol):
    """Port for local filesystem operations."""

    def resolve_dir(self, raw_path: str) -> Path:
        """Resolve and normalize a user-provided directory path."""
        ...

    def ensure_writable(self, path: Path) -> None:
        """Ensure directory exists and is writable; raise OSError/PermissionError otherwise."""
        ...

    def sanitize_name(self, name: str, max_len: int = 120) -> str:
        """Sanitize a file or directory name for cross-platform safety."""
        ...

    def promote_file(
        self,
        src: Path,
        dest_dir: Path,
        relative_path: Path | None = None,
    ) -> Path:
        """Atomically move a validated file from staging into dest_dir."""
        ...

    def cleanup_dir(self, path: Path) -> None:
        """Remove a temporary directory tree safely."""
        ...

    def default_music_dir(self) -> Path:
        """Return the user's default Music directory."""
        ...


@runtime_checkable
class EventBusPort(Protocol):
    """Port for thread-safe per-job event publishing and SSE subscriptions."""

    def publish(self, job_id: str, event_type: str, payload: dict[str, Any]) -> None:
        """Publish an event for a job to all active subscribers and history."""
        ...

    def subscribe_with_snapshot(
        self, job_id: str
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]], queue.Queue[dict[str, Any]]]:
        """Return (latest_snapshot, recent_events, live_queue) atomically."""
        ...


@runtime_checkable
class PreflightPort(Protocol):
    """Port for checking external runtime prerequisites (ffmpeg/ffprobe)."""

    def check_ffmpeg(self) -> dict[str, Any]:
        """Check availability of ffmpeg and ffprobe binaries."""
        ...


@runtime_checkable
class PersistencePort(Protocol):
    """Port for lightweight local persistence of jobs and downloaded video IDs."""

    def save_job(self, job: JobState) -> None:
        """Persist or update a job's state."""
        ...

    def load_jobs(self) -> list[JobState]:
        """Load all persisted jobs ordered by creation time."""
        ...

    def get_job(self, job_id: str) -> JobState | None:
        """Load a single persisted job by ID, or None if not found."""
        ...

    def record_downloaded_ids(
        self, video_ids: Iterable[str], audio_format: str | None = None
    ) -> None:
        """Record one or more video IDs as already downloaded (optionally scoped by format)."""
        ...

    def get_downloaded_ids(self, audio_format: str | None = None) -> set[str]:
        """Return the set of previously downloaded video IDs (optionally filtered by format)."""
        ...
