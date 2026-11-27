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
from yt_music_dl.ports import DownloaderPort, EventBusPort, FileSystemPort, PersistencePort

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
        store: PersistencePort | None = None,
        max_workers: int = 1,
    ) -> None:
        self._downloader = downloader
        self._fs = fs
        self._event_bus = event_bus
        self._staging_root = staging_root
        self._staging_root.mkdir(parents=True, exist_ok=True)
        self._store = store
        self._max_workers = max(1, min(int(max_workers), 3))

        self._lock = threading.Lock()
        self._archive_lock = threading.Lock()
        self._store_lock = threading.Lock()
        self._jobs: dict[str, JobState] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._shutdown_called = False
        self._shutdown_event = threading.Event()

        if self._store is not None:
            for persisted in self._store.load_jobs():
                restored = persisted
                if persisted.status not in TERMINAL_STATUSES:
                    err_msg = "Interrupted by server restart"
                    snap = ProgressSnapshot(
                        job_id=persisted.job_id,
                        status="failed",
                        progress=persisted.progress.progress if persisted.progress else 0.0,
                        message=err_msg,
                        error=err_msg,
                    )
                    restored = persisted.transition("failed", progress=snap, error=err_msg)
                    self._store.save_job(restored)
                self._jobs[restored.job_id] = restored
                self._cancel_events[restored.job_id] = threading.Event()

        self._workers: list[threading.Thread] = []
        for idx in range(self._max_workers):
            worker = threading.Thread(
                target=self._worker_loop,
                args=(idx,),
                name=f"yt-music-dl-worker-{idx + 1}",
                daemon=True,
            )
            worker.start()
            self._workers.append(worker)

    def _persist_job(self, job: JobState) -> None:
        if self._store is not None:
            with self._store_lock:
                self._store.save_job(job)

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

        self._persist_job(job)
        self._event_bus.publish(job_id, "status", snapshot.to_dict())
        self._queue.put(job_id)
        return job

    def get_job(self, job_id: str) -> JobState | None:
        """Return the current immutable JobState for job_id, or None if unknown."""
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self) -> list[JobState]:
        """Return all known jobs ordered from newest to oldest."""
        with self._lock:
            jobs = list(self._jobs.values())
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs

    def retry_job(self, job_id: str) -> JobState | None:
        """Re-enqueue a finished job using its original request. Returns None if unknown."""
        with self._lock:
            existing = self._jobs.get(job_id)
        if existing is None:
            return None
        if existing.status not in TERMINAL_STATUSES:
            raise ValueError(f"Job {job_id} is still active ({existing.status}).")
        return self.submit_job(existing.request)

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
                self._persist_job(updated)
                self._event_bus.publish(job_id, "cancelled", snap.to_dict())
            return True

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop accepting new jobs and wait for all worker threads to exit."""
        with self._lock:
            if self._shutdown_called:
                return
            self._shutdown_called = True
        self._shutdown_event.set()
        for _ in self._workers:
            self._queue.put(None)
        for worker in self._workers:
            worker.join(timeout=timeout)

    def _worker_loop(self, worker_idx: int = 0) -> None:
        if worker_idx > 0 and self._shutdown_event.wait(timeout=worker_idx * 1.5):
            return
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
        if job.request.skip_duplicates and self._store is not None:
            with self._archive_lock:
                self._seed_archive_from_store(archive_path)

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

            with self._archive_lock:
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

            if self._store is not None and manifest.downloaded:
                with self._store_lock:
                    self._store.record_downloaded_ids(manifest.downloaded)
            self._persist_job(completed)

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
            failed_job: JobState | None = None
            with self._lock:
                current = self._jobs[job_id]
                if current.status not in TERMINAL_STATUSES:
                    failed_job = current.transition(
                        "failed", progress=fail_snap, error=err_msg
                    )
                    self._jobs[job_id] = failed_job
            if failed_job is not None:
                self._persist_job(failed_job)
            self._event_bus.publish(job_id, "failed", fail_snap.to_dict())
        finally:
            self._fs.cleanup_dir(staging_dir)

    def _seed_archive_from_store(self, archive_path: Path) -> None:
        if self._store is None:
            return
        known_ids = self._store.get_downloaded_ids()
        if not known_ids:
            return
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        existing_ids: set[str] = set()
        if archive_path.is_file():
            for line in archive_path.read_text(encoding="utf-8").splitlines():
                parts = line.strip().split()
                if parts:
                    existing_ids.add(parts[-1])
        missing_ids = sorted(vid for vid in known_ids if vid not in existing_ids)
        if not missing_ids:
            return
        with archive_path.open("a", encoding="utf-8") as fh:
            for vid in missing_ids:
                fh.write(f"youtube {vid}\n")

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
        self._persist_job(updated)
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
            updated = current.transition("cancelled", progress=snap)
            self._jobs[job_id] = updated
        self._persist_job(updated)
        self._event_bus.publish(job_id, "cancelled", snap.to_dict())
