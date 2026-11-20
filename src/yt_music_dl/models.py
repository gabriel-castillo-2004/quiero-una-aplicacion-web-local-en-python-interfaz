"""Immutable domain models and job state machine for yt_music_dl."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal
import time

AudioFormat = Literal["mp3", "m4a"]

JobStatus = Literal[
    "pending",
    "downloading",
    "postprocessing",
    "completed",
    "failed",
    "cancelled",
]

TERMINAL_STATUSES: frozenset[JobStatus] = frozenset({"completed", "failed", "cancelled"})

VALID_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    "pending": frozenset({"downloading", "cancelled", "failed"}),
    "downloading": frozenset({"postprocessing", "completed", "cancelled", "failed"}),
    "postprocessing": frozenset({"completed", "cancelled", "failed"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


class InvalidTransitionError(ValueError):
    """Raised when an invalid job status transition is attempted."""


def validate_transition(current: JobStatus, target: JobStatus) -> JobStatus:
    """Validate and return the target status if transitioning from current is allowed."""
    allowed = VALID_TRANSITIONS.get(current)
    if allowed is None:
        raise InvalidTransitionError(f"Unknown current status: {current!r}")
    if target not in allowed:
        raise InvalidTransitionError(
            f"Invalid job state transition: {current!r} -> {target!r}"
        )
    return target


@dataclass(frozen=True, slots=True)
class TrackResult:
    """Outcome of a single track within a download job."""

    track_id: str
    title: str
    status: Literal["downloaded", "skipped", "failed"]
    file_path: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "title": self.title,
            "status": self.status,
            "file_path": self.file_path,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class DownloadManifest:
    """Summary of all tracks processed during a download job."""

    downloaded: tuple[str, ...] = ()
    skipped_archive: tuple[str, ...] = ()
    failed: tuple[dict[str, str], ...] = ()
    tracks: tuple[TrackResult, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "downloaded": list(self.downloaded),
            "skipped_archive": list(self.skipped_archive),
            "failed": [dict(item) for item in self.failed],
            "tracks": [t.to_dict() for t in self.tracks],
        }


@dataclass(frozen=True, slots=True)
class ProgressSnapshot:
    """Point-in-time progress snapshot for a download job."""

    job_id: str
    status: JobStatus
    progress: float = 0.0
    current_track: str | None = None
    thumbnail_url: str | None = None
    message: str = ""
    downloaded_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "progress": self.progress,
            "current_track": self.current_track,
            "thumbnail_url": self.thumbnail_url,
            "message": self.message,
            "downloaded_count": self.downloaded_count,
            "skipped_count": self.skipped_count,
            "failed_count": self.failed_count,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class DownloadRequest:
    """Validated request parameters for a download job."""

    url: str
    dest_dir: Path
    audio_format: AudioFormat = "mp3"
    is_playlist: bool = False
    quality: str = "192"
    selected_tracks: tuple[int, ...] | None = None


@dataclass(frozen=True, slots=True)
class JobState:
    """Immutable snapshot of a job's lifecycle state."""

    job_id: str
    request: DownloadRequest
    status: JobStatus = "pending"
    progress: ProgressSnapshot | None = None
    manifest: DownloadManifest | None = None
    promoted_files: tuple[Path, ...] = ()
    error: str | None = None
    created_at: float = field(default_factory=time.time)

    def transition(
        self,
        target: JobStatus,
        *,
        progress: ProgressSnapshot | None = None,
        manifest: DownloadManifest | None = None,
        promoted_files: tuple[Path, ...] | None = None,
        error: str | None = None,
    ) -> JobState:
        """Return a new JobState after validating the status transition."""
        next_status = validate_transition(self.status, target)
        return replace(
            self,
            status=next_status,
            progress=progress if progress is not None else self.progress,
            manifest=manifest if manifest is not None else self.manifest,
            promoted_files=(
                promoted_files if promoted_files is not None else self.promoted_files
            ),
            error=error if error is not None else self.error,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "url": self.request.url,
            "dest_dir": str(self.request.dest_dir),
            "audio_format": self.request.audio_format,
            "quality": self.request.quality,
            "is_playlist": self.request.is_playlist,
            "status": self.status,
            "progress": self.progress.to_dict() if self.progress else None,
            "manifest": self.manifest.to_dict() if self.manifest else None,
            "promoted_files": [str(p) for p in self.promoted_files],
            "error": self.error,
            "created_at": self.created_at,
        }
