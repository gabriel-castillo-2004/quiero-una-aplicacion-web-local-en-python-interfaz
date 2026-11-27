"""Serialized FIFO job queue manager with isolated staging and atomic promotion."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import queue
import threading
import uuid

from yt_music_dl.adapters.ytdlp_adapter import CancelledDownloadError
from yt_music_dl.models import (
    TERMINAL_STATUSES,
    DownloadManifest,
    DownloadRequest,
    JobState,
    ProgressSnapshot,
)
from yt_music_dl.ports import DownloaderPort, EventBusPort, FileSystemPort

AUDIO_EXTENSIONS = frozenset(
    {".mp3", ".m4a", ".webm", ".opus", ".ogg", ".aac", ".flac", ".wav", ".mp4"}
)


class JobManager:
    """Orchestrates serialized FIFO downloads, cooperative cancellation, and atomic promotion."""

    def __init__(
        self,
        *,
        downloader: DownloaderPort,
        fs: FileSystemPort,
        event_bus: EventBusPort,
        staging_root: Path,
    ) -> None:
        self._downloader = downloader
        self._fs = fs
        self._event_bus = event_bus
        self._staging_root = staging_root
        self._staging_root.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._jobs: dict[str, JobState] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._shutdown_called = False

        self._worker = threading.Thread(
            target=self._worker_loop,
            name="yt-music-dl-worker",
            daemon=True,
        )
        self._worker.start()

    def submit_job(self, request: DownloadRequest) -> JobState:
        """Enqueue a validated download request and return its initial pending state."""
        job_id = uuid.uuid4().hex[:12]
        snapshot = ProgressSnapshot(
            job_id=job_id,
            status="pending",
            progress=0.0,
            message="Queued for download",
        )
        job = JobState(job_id=job_id, request=request, status="pending", progress=snapshot)

        with self._lock:
            if self._shutdown_called:
                raise RuntimeError("JobManager has been shut down.")
            self._jobs[job_id] = job
            self._cancel_events[job_id] = threading.Event()

        self._event_bus.publish(job_id, "status", snapshot.to_dict())
        self._queue.put(job_id)
        return job

    def get_job(self, job_id: str) -> JobState | None:
        """Return the current immutable JobState for job_id, or None if unknown."""
        with self._lock:
            return self._jobs.get(job_id)

    def cancel_job(self, job_id: str) -> bool:
        """Request cooperative cancellation of a job. Returns False if not found."""
        with self._lock:
            job = self._jobs.get(job_id)
            cancel_ev = self._cancel_events.get(job_id)
            if job is None or cancel_ev is None:
                return False

            cancel_ev.set()
            if job.status == "pending":
                snap = ProgressSnapshot(
                    job_id=job_id,
                    status="cancelled",
                    progress=job.progress.progress if job.progress else 0.0,
                    message="Cancelled while queued",
                )
                updated = job.transition("cancelled", progress=snap)
                self._jobs[job_id] = updated
                self._event_bus.publish(job_id, "cancelled", snap.to_dict())
            return True

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop accepting new jobs and wait for the worker thread to exit."""
        with self._lock:
            if self._shutdown_called:
                return
            self._shutdown_called = True
        self._queue.put(None)
        self._worker.join(timeout=timeout)

    def _worker_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            if job_id is None:
                self._queue.task_done()
                break
            try:
                self._execute_job(job_id)
            finally:
                self._queue.task_done()

    def _execute_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            cancel_ev = self._cancel_events.get(job_id)

        if job is None or cancel_ev is None:
            return

        if job.status in TERMINAL_STATUSES:
            return

        if cancel_ev.is_set():
            self._mark_cancelled(job_id)
            return

        staging_dir = self._staging_root / job_id
        staging_dir.mkdir(parents=True, exist_ok=True)
        archive_path = job.request.dest_dir / "archive.txt"

        self._transition_and_publish(
            job_id,
            "downloading",
            ProgressSnapshot(
                job_id=job_id,
                status="downloading",
                progress=0.0,
                message="Starting download",
            ),
            event_type="status",
        )

        def on_progress(snap: ProgressSnapshot) -> None:
            normalized = replace(snap, job_id=job_id)
            with self._lock:
                current = self._jobs.get(job_id)
                if current is None or current.status in TERMINAL_STATUSES:
                    return
                if normalized.status != current.status:
                    updated = current.transition(normalized.status, progress=normalized)
                else:
                    updated = replace(current, progress=normalized)
                self._jobs[job_id] = updated
            self._event_bus.publish(job_id, "progress", normalized.to_dict())

        try:
            manifest = self._downloader.download(
                job.request,
                staging_dir,
                archive_path,
                on_progress,
                cancel_ev.is_set,
            )

            if cancel_ev.is_set():
                raise CancelledDownloadError("Cancelled after download")

            promoted = self._promote_staged_audio(staging_dir, job.request.dest_dir)

            final_snap = ProgressSnapshot(
                job_id=job_id,
                status="completed",
                progress=100.0,
                message="Download completed",
                downloaded_count=len(manifest.downloaded),
                skipped_count=len(manifest.skipped_archive),
                failed_count=len(manifest.failed),
            )
            with self._lock:
                current = self._jobs[job_id]
                completed = current.transition(
                    "completed",
                    progress=final_snap,
                    manifest=manifest,
                    promoted_files=tuple(promoted),
                )
                self._jobs[job_id] = completed

            payload = final_snap.to_dict()
            payload["manifest"] = manifest.to_dict()
            payload["promoted_files"] = [str(p) for p in promoted]
            self._event_bus.publish(job_id, "completed", payload)

        except CancelledDownloadError:
            self._mark_cancelled(job_id)
        except Exception as exc:
            err_msg = str(exc) or exc.__class__.__name__
            fail_snap = ProgressSnapshot(
                job_id=job_id,
                status="failed",
                progress=0.0,
                message=err_msg,
                error=err_msg,
            )
            with self._lock:
                current = self._jobs[job_id]
                if current.status not in TERMINAL_STATUSES:
                    self._jobs[job_id] = current.transition(
                        "failed", progress=fail_snap, error=err_msg
                    )
            self._event_bus.publish(job_id, "failed", fail_snap.to_dict())
        finally:
            self._fs.cleanup_dir(staging_dir)

    def _promote_staged_audio(self, staging_dir: Path, dest_dir: Path) -> list[Path]:
        promoted: list[Path] = []
        for candidate in sorted(staging_dir.rglob("*")):
            if not candidate.is_file():
                continue
            if candidate.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            if candidate.stat().st_size <= 0:
                raise ValueError(f"Refusing to promote empty audio file: {candidate.name}")
            rel = candidate.relative_to(staging_dir)
            target = self._fs.promote_file(candidate, dest_dir, relative_path=rel)
            promoted.append(target)
        return promoted

    def _transition_and_publish(
        self,
        job_id: str,
        target_status: str,
        snapshot: ProgressSnapshot,
        *,
        event_type: str,
    ) -> None:
        with self._lock:
            current = self._jobs[job_id]
            updated = current.transition(target_status, progress=snapshot)  # type: ignore[arg-type]
            self._jobs[job_id] = updated
        self._event_bus.publish(job_id, event_type, snapshot.to_dict())

    def _mark_cancelled(self, job_id: str) -> None:
        with self._lock:
            current = self._jobs.get(job_id)
            if current is None or current.status in TERMINAL_STATUSES:
                return
            snap = ProgressSnapshot(
                job_id=job_id,
                status="cancelled",
                progress=current.progress.progress if current.progress else 0.0,
                message="Download cancelled",
            )
            self._jobs[job_id] = current.transition("cancelled", progress=snap)
        self._event_bus.publish(job_id, "cancelled", snap.to_dict())
