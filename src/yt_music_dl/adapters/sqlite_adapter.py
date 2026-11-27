"""SQLite persistence adapter implementing PersistencePort."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, cast
import json
import sqlite3
import threading
import time

from yt_music_dl.models import (
    AudioFormat,
    DownloadManifest,
    DownloadRequest,
    JobState,
    JobStatus,
    ProgressSnapshot,
    TrackResult,
)


class SqlitePersistenceAdapter:
    """Thread-safe SQLite store for job history and downloaded video IDs."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    request_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress_json TEXT,
                    manifest_json TEXT,
                    promoted_files_json TEXT NOT NULL,
                    error TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS downloaded_videos (
                    video_id TEXT PRIMARY KEY,
                    downloaded_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS downloaded_tracks (
                    video_id TEXT NOT NULL,
                    audio_format TEXT NOT NULL DEFAULT '',
                    downloaded_at REAL NOT NULL,
                    PRIMARY KEY (video_id, audio_format)
                )
                """
            )
            conn.commit()

    def save_job(self, job: JobState) -> None:
        """Insert or update a JobState record atomically."""
        request_payload = {
            "url": job.request.url,
            "dest_dir": str(job.request.dest_dir),
            "audio_format": job.request.audio_format,
            "is_playlist": job.request.is_playlist,
            "quality": job.request.quality,
            "selected_tracks": (
                list(job.request.selected_tracks)
                if job.request.selected_tracks is not None
                else None
            ),
            "skip_duplicates": job.request.skip_duplicates,
        }
        progress_json = (
            json.dumps(job.progress.to_dict()) if job.progress is not None else None
        )
        manifest_json = (
            json.dumps(job.manifest.to_dict()) if job.manifest is not None else None
        )
        promoted_json = json.dumps([str(p) for p in job.promoted_files])

        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, request_json, status, progress_json,
                    manifest_json, promoted_files_json, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    request_json = excluded.request_json,
                    status = excluded.status,
                    progress_json = excluded.progress_json,
                    manifest_json = excluded.manifest_json,
                    promoted_files_json = excluded.promoted_files_json,
                    error = excluded.error,
                    created_at = excluded.created_at
                """,
                (
                    job.job_id,
                    json.dumps(request_payload),
                    job.status,
                    progress_json,
                    manifest_json,
                    promoted_json,
                    job.error,
                    job.created_at,
                ),
            )
            conn.commit()

    def load_jobs(self) -> list[JobState]:
        """Load all persisted jobs ordered from oldest to newest."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at ASC"
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def get_job(self, job_id: str) -> JobState | None:
        """Load a single persisted job by ID."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return self._row_to_job(row) if row is not None else None

    def record_downloaded_ids(
        self, video_ids: Iterable[str], audio_format: str | None = None
    ) -> None:
        """Record downloaded video IDs (scoped by audio_format) for duplicate skipping."""
        now = time.time()
        fmt = (audio_format or "").strip().lower()
        clean_ids = [vid.strip() for vid in video_ids if vid and vid.strip()]
        if not clean_ids:
            return
        with self._lock, self._connect() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO downloaded_videos (video_id, downloaded_at)
                VALUES (?, ?)
                """,
                [(vid, now) for vid in clean_ids],
            )
            conn.executemany(
                """
                INSERT OR REPLACE INTO downloaded_tracks (video_id, audio_format, downloaded_at)
                VALUES (?, ?, ?)
                """,
                [(vid, fmt, now) for vid in clean_ids],
            )
            conn.commit()

    def get_downloaded_ids(self, audio_format: str | None = None) -> set[str]:
        """Return downloaded video IDs, optionally filtered by audio_format."""
        with self._lock, self._connect() as conn:
            if audio_format is None:
                rows = conn.execute(
                    """
                    SELECT video_id FROM downloaded_tracks
                    UNION
                    SELECT video_id FROM downloaded_videos
                    """
                ).fetchall()
            else:
                fmt = audio_format.strip().lower()
                rows = conn.execute(
                    "SELECT video_id FROM downloaded_tracks WHERE audio_format = ?",
                    (fmt,),
                ).fetchall()
        return {str(row["video_id"]) for row in rows}

    def _row_to_job(self, row: sqlite3.Row) -> JobState:
        req_data: dict[str, Any] = json.loads(row["request_json"])
        selected_raw = req_data.get("selected_tracks")
        selected_tracks = (
            tuple(int(i) for i in selected_raw)
            if isinstance(selected_raw, list)
            else None
        )
        request = DownloadRequest(
            url=str(req_data["url"]),
            dest_dir=Path(str(req_data["dest_dir"])),
            audio_format=cast(AudioFormat, req_data.get("audio_format", "mp3")),
            is_playlist=bool(req_data.get("is_playlist", False)),
            quality=str(req_data.get("quality", "192")),
            selected_tracks=selected_tracks,
            skip_duplicates=bool(req_data.get("skip_duplicates", False)),
        )

        progress: ProgressSnapshot | None = None
        if row["progress_json"]:
            p_data = json.loads(row["progress_json"])
            progress = ProgressSnapshot(
                job_id=str(p_data.get("job_id", row["job_id"])),
                status=cast(JobStatus, p_data.get("status", row["status"])),
                progress=float(p_data.get("progress", 0.0)),
                current_track=p_data.get("current_track"),
                thumbnail_url=p_data.get("thumbnail_url"),
                message=str(p_data.get("message", "")),
                downloaded_count=int(p_data.get("downloaded_count", 0)),
                skipped_count=int(p_data.get("skipped_count", 0)),
                failed_count=int(p_data.get("failed_count", 0)),
                error=p_data.get("error"),
            )

        manifest: DownloadManifest | None = None
        if row["manifest_json"]:
            m_data = json.loads(row["manifest_json"])
            tracks = tuple(
                TrackResult(
                    track_id=str(t.get("track_id", "")),
                    title=str(t.get("title", "")),
                    status=t.get("status", "downloaded"),
                    file_path=t.get("file_path"),
                    reason=t.get("reason"),
                )
                for t in m_data.get("tracks", [])
            )
            manifest = DownloadManifest(
                downloaded=tuple(str(x) for x in m_data.get("downloaded", [])),
                skipped_archive=tuple(str(x) for x in m_data.get("skipped_archive", [])),
                failed=tuple(dict(item) for item in m_data.get("failed", [])),
                tracks=tracks,
            )

        promoted_raw = json.loads(row["promoted_files_json"] or "[]")
        promoted_files = tuple(Path(str(p)) for p in promoted_raw)

        return JobState(
            job_id=str(row["job_id"]),
            request=request,
            status=cast(JobStatus, row["status"]),
            progress=progress,
            manifest=manifest,
            promoted_files=promoted_files,
            error=row["error"],
            created_at=float(row["created_at"]),
        )
