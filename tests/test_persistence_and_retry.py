"""Unit and integration tests for local persistence, restart restoration,
optional duplicate skipping in playlists, and the job retry endpoint.

All tests run offline using tmp_path, mocked downloaders, and Flask test_client().
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
import sys
import threading
import time
from typing import Any

import pytest

_SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from yt_music_dl import ports
from yt_music_dl.adapters.fs_adapter import LocalFileSystemAdapter
from yt_music_dl.adapters.ytdlp_adapter import SystemPreflightAdapter
from yt_music_dl.app import create_app
from yt_music_dl.event_bus import InMemoryEventBus
from yt_music_dl.job_manager import JobManager
from yt_music_dl.models import (
    DownloadManifest,
    DownloadRequest,
    ProgressSnapshot,
    TrackResult,
)


_ORIGINAL_PORTS = {
    "DownloaderPort",
    "FileSystemPort",
    "EventBusPort",
    "PreflightPort",
}

_ORIGINAL_ADAPTER_FILES = {"__init__.py", "fs_adapter.py", "ytdlp_adapter.py"}


def _wait_for_terminal(manager: JobManager, job_id: str, timeout: float = 5.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = manager.get_job(job_id)
        if job is not None and job.status in {"completed", "failed", "cancelled"}:
            return job
        time.sleep(0.02)
    raise TimeoutError(f"Job {job_id} did not reach terminal state within {timeout}s")


class _RecordingDownloader:
    """Fake downloader that records calls and produces deterministic tracks."""

    def __init__(self, tracks: list[tuple[str, str]] | None = None, *, fail: bool = False) -> None:
        self.tracks = tracks or [("vid001", "Track One"), ("vid002", "Track Two")]
        self.fail = fail
        self.calls: list[DownloadRequest] = []
        self._lock = threading.Lock()

    def download(
        self,
        request: DownloadRequest,
        staging_dir: Path,
        archive_path: Path,
        on_progress: Any,
        is_cancelled: Any,
    ) -> DownloadManifest:
        with self._lock:
            self.calls.append(request)

        if self.fail:
            raise RuntimeError("Simulated download failure")

        downloaded_ids: list[str] = []
        skipped_ids: list[str] = []
        track_results: list[TrackResult] = []

        existing_archive: set[str] = set()
        if archive_path.is_file():
            for line in archive_path.read_text(encoding="utf-8").splitlines():
                parts = line.strip().split()
                if parts:
                    existing_archive.add(parts[-1])

        for idx, (vid, title) in enumerate(self.tracks, start=1):
            if request.selected_tracks is not None and idx not in request.selected_tracks:
                continue
            if vid in existing_archive:
                skipped_ids.append(vid)
                track_results.append(
                    TrackResult(
                        track_id=vid,
                        title=title,
                        status="skipped",
                        reason="Already in archive",
                    )
                )
                continue

            out_file = staging_dir / f"{idx:02d} - {title}.{request.audio_format}"
            out_file.write_bytes(b"FAKEAUDIO" * 16)
            downloaded_ids.append(vid)
            track_results.append(
                TrackResult(
                    track_id=vid,
                    title=title,
                    status="downloaded",
                    file_path=str(out_file),
                )
            )
            on_progress(
                ProgressSnapshot(
                    job_id="",
                    status="downloading",
                    progress=50.0,
                    current_track=title,
                    downloaded_count=len(downloaded_ids),
                )
            )

        return DownloadManifest(
            downloaded=tuple(downloaded_ids),
            skipped_archive=tuple(skipped_ids),
            failed=(),
            tracks=tuple(track_results),
        )


def _discover_persistence_adapter_class() -> type[Any] | None:
    """Locate the persistence adapter class added under yt_music_dl.adapters."""
    adapters_dir = Path(_SRC_DIR) / "yt_music_dl" / "adapters"
    for py_file in sorted(adapters_dir.glob("*.py")):
        if py_file.name in _ORIGINAL_ADAPTER_FILES:
            continue
        mod_name = f"yt_music_dl.adapters.{py_file.stem}"
        mod = importlib.import_module(mod_name)
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            if obj.__module__ == mod_name and not obj.__name__.endswith("Error"):
                return obj
    return None


def _build_persistent_manager(
    tmp_path: Path,
    downloader: Any,
    store_path: Path,
) -> tuple[JobManager, Any]:
    """Instantiate JobManager wired to a persistence store at store_path."""
    adapter_cls = _discover_persistence_adapter_class()
    assert adapter_cls is not None, (
        "Expected a new persistence adapter module in src/yt_music_dl/adapters/"
    )
    store = adapter_cls(store_path)

    sig = inspect.signature(JobManager.__init__)
    kwargs: dict[str, Any] = {
        "downloader": downloader,
        "fs": LocalFileSystemAdapter(),
        "event_bus": InMemoryEventBus(),
        "staging_root": tmp_path / "staging",
    }
    for param_name in ("store", "persistence", "job_store", "state_store"):
        if param_name in sig.parameters:
            kwargs[param_name] = store
            break
    else:
        for param_name in ("db_path", "state_path", "persistence_path", "store_path"):
            if param_name in sig.parameters:
                kwargs[param_name] = store_path
                break
        else:
            pytest.fail(
                "JobManager.__init__ must accept a persistence store or path parameter"
            )

    manager = JobManager(**kwargs)
    return manager, store


# ---------------------------------------------------------------------------
# 1. Ports & Adapters Architecture Verification
# ---------------------------------------------------------------------------


def test_persistence_port_and_adapter_exist() -> None:
    new_ports = [
        name
        for name, obj in inspect.getmembers(ports)
        if name.endswith("Port") and name not in _ORIGINAL_PORTS
    ]
    assert new_ports, "Expected a new persistence Protocol (*Port) in src/yt_music_dl/ports.py"

    adapter_cls = _discover_persistence_adapter_class()
    assert adapter_cls is not None, (
        "Expected a new persistence adapter class in src/yt_music_dl/adapters/"
    )


# ---------------------------------------------------------------------------
# 2. Persistence & History Restoration Across Restarts
# ---------------------------------------------------------------------------


def test_job_history_and_downloaded_ids_restored_after_restart(tmp_path: Path) -> None:
    store_path = tmp_path / "state.db"
    dest_dir = tmp_path / "music"
    dest_dir.mkdir()

    downloader1 = _RecordingDownloader(tracks=[("vidA", "Alpha"), ("vidB", "Beta")])
    manager1, _ = _build_persistent_manager(tmp_path, downloader1, store_path)
    try:
        req = DownloadRequest(
            url="https://www.youtube.com/playlist?list=PLrestoration123",
            dest_dir=dest_dir,
            audio_format="mp3",
            is_playlist=True,
            quality="192",
        )
        submitted = manager1.submit_job(req)
        done = _wait_for_terminal(manager1, submitted.job_id)
        assert done.status == "completed"
        assert set(done.manifest.downloaded) == {"vidA", "vidB"}
    finally:
        manager1.shutdown()

    # Simulate server restart: new JobManager instance pointing to the same store_path
    downloader2 = _RecordingDownloader()
    manager2, _ = _build_persistent_manager(tmp_path, downloader2, store_path)
    try:
        restored = manager2.get_job(submitted.job_id)
        assert restored is not None, "Completed job must be restored after restart"
        assert restored.status == "completed"
        assert restored.request.url == "https://www.youtube.com/playlist?list=PLrestoration123"
        assert restored.request.dest_dir == dest_dir
        assert restored.manifest is not None
        assert set(restored.manifest.downloaded) == {"vidA", "vidB"}
        assert len(restored.promoted_files) == 2

        # Verify restored job is also accessible via HTTP API
        app = create_app(
            job_manager=manager2,
            event_bus=InMemoryEventBus(),
            fs=LocalFileSystemAdapter(),
            preflight=SystemPreflightAdapter(),
            csrf_token="test-csrf-token",
        )
        client = app.test_client()
        resp = client.get(f"/api/jobs/{submitted.job_id}")
        assert resp.status_code == 200
        payload = resp.get_json()
        assert payload["job_id"] == submitted.job_id
        assert payload["status"] == "completed"
    finally:
        manager2.shutdown()


# ---------------------------------------------------------------------------
# 3. Optional Duplicate Skipping in Playlists
# ---------------------------------------------------------------------------


def test_playlist_skips_already_downloaded_video_ids_when_enabled(tmp_path: Path) -> None:
    store_path = tmp_path / "state.db"
    dest_dir = tmp_path / "music"
    dest_dir.mkdir()

    # First job downloads vid1 and vid2
    downloader1 = _RecordingDownloader(tracks=[("vid1", "Song 1"), ("vid2", "Song 2")])
    manager1, _ = _build_persistent_manager(tmp_path, downloader1, store_path)
    try:
        req1 = DownloadRequest(
            url="https://www.youtube.com/playlist?list=PLdup1",
            dest_dir=dest_dir,
            audio_format="mp3",
            is_playlist=True,
            quality="192",
        )
        job1 = manager1.submit_job(req1)
        done1 = _wait_for_terminal(manager1, job1.job_id)
        assert done1.status == "completed"
    finally:
        manager1.shutdown()

    # Second manager after restart receives a playlist containing vid1 (already downloaded) and vid3 (new)
    downloader2 = _RecordingDownloader(tracks=[("vid1", "Song 1"), ("vid3", "Song 3")])
    manager2, _ = _build_persistent_manager(tmp_path, downloader2, store_path)
    try:
        req_fields = {f.name for f in DownloadRequest.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        req_kwargs: dict[str, Any] = {
            "url": "https://www.youtube.com/playlist?list=PLdup2",
            "dest_dir": dest_dir,
            "audio_format": "mp3",
            "is_playlist": True,
            "quality": "192",
        }
        if "skip_duplicates" in req_fields:
            req_kwargs["skip_duplicates"] = True

        job2 = manager2.submit_job(DownloadRequest(**req_kwargs))
        done2 = _wait_for_terminal(manager2, job2.job_id)
        assert done2.status == "completed"
        assert done2.manifest is not None
        assert "vid1" not in done2.manifest.downloaded
        assert "vid3" in done2.manifest.downloaded
    finally:
        manager2.shutdown()


# ---------------------------------------------------------------------------
# 4. Retry Endpoint & Frontend Wiring
# ---------------------------------------------------------------------------


def test_retry_endpoint_requeues_failed_job_and_enforces_csrf(tmp_path: Path) -> None:
    store_path = tmp_path / "state.db"
    dest_dir = tmp_path / "music"
    dest_dir.mkdir()

    failing_dl = _RecordingDownloader(fail=True)
    manager, _ = _build_persistent_manager(tmp_path, failing_dl, store_path)
    event_bus = InMemoryEventBus()
    app = create_app(
        job_manager=manager,
        event_bus=event_bus,
        fs=LocalFileSystemAdapter(),
        preflight=SystemPreflightAdapter(),
        csrf_token="test-csrf-token",
    )
    app.config["TESTING"] = True
    client = app.test_client()

    try:
        create_resp = client.post(
            "/api/jobs",
            json={
                "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "format": "mp3",
                "quality": "192",
                "dest_dir": str(dest_dir),
            },
            headers={"X-CSRF-Token": "test-csrf-token"},
        )
        assert create_resp.status_code == 201
        job_id = create_resp.get_json()["job_id"]
        failed_job = _wait_for_terminal(manager, job_id)
        assert failed_job.status == "failed"

        # Missing CSRF must be rejected
        no_csrf = client.post(f"/api/jobs/{job_id}/retry")
        assert no_csrf.status_code == 403

        # Unknown job must return 404
        not_found = client.post(
            "/api/jobs/nonexistent99/retry",
            headers={"X-CSRF-Token": "test-csrf-token"},
        )
        assert not_found.status_code == 404

        # Allow next download to succeed and retry
        failing_dl.fail = False
        retry_resp = client.post(
            f"/api/jobs/{job_id}/retry",
            headers={"X-CSRF-Token": "test-csrf-token"},
        )
        assert retry_resp.status_code in {200, 201}
        retry_data = retry_resp.get_json()
        retry_job_id = retry_data["job_id"]
        retried = _wait_for_terminal(manager, retry_job_id)
        assert retried.status == "completed"
    finally:
        manager.shutdown()


def test_frontend_includes_retry_and_duplicate_skip_wiring(tmp_path: Path) -> None:
    store_path = tmp_path / "state.db"
    manager, _ = _build_persistent_manager(tmp_path, _RecordingDownloader(), store_path)
    app = create_app(
        job_manager=manager,
        event_bus=InMemoryEventBus(),
        fs=LocalFileSystemAdapter(),
        preflight=SystemPreflightAdapter(),
        csrf_token="test-csrf-token",
    )
    client = app.test_client()
    try:
        html = client.get("/").get_data(as_text=True).lower()
        js = client.get("/static/app.js").get_data(as_text=True)

        assert "/retry" in js
        assert "retry" in html or "reintentar" in html
        assert "skip_duplicates" in html or "skip_duplicates" in js
        assert "already_downloaded" in js
        assert "Ya descargada" in js
    finally:
        manager.shutdown()


def test_job_creation_marks_already_downloaded_by_video_id_and_format(tmp_path: Path) -> None:
    store_path = tmp_path / "state.db"
    dest_dir = tmp_path / "music"
    dest_dir.mkdir()

    downloader1 = _RecordingDownloader(tracks=[("vid1", "Song 1")])
    manager1, _ = _build_persistent_manager(tmp_path, downloader1, store_path)
    try:
        job1 = manager1.submit_job(
            DownloadRequest(
                url="https://www.youtube.com/watch?v=vid1",
                dest_dir=dest_dir,
                audio_format="mp3",
                is_playlist=False,
                quality="192",
            )
        )
        done1 = _wait_for_terminal(manager1, job1.job_id)
        assert done1.status == "completed"
    finally:
        manager1.shutdown()

    downloader2 = _RecordingDownloader(tracks=[("vid1", "Song 1"), ("vid2", "Song 2")])
    manager2, _ = _build_persistent_manager(tmp_path, downloader2, store_path)
    try:
        mp3_job = manager2.submit_job(
            DownloadRequest(
                url="https://www.youtube.com/playlist?list=PL_MARK",
                dest_dir=dest_dir,
                audio_format="mp3",
                is_playlist=True,
                quality="192",
                track_metadata=(
                    {"index": 1, "id": "vid1", "title": "Song 1"},
                    {"index": 2, "id": "vid2", "title": "Song 2"},
                ),
            )
        )
        assert mp3_job.request.track_metadata[0].get("already_downloaded") is True
        assert mp3_job.request.track_metadata[1].get("already_downloaded") is False

        m4a_job = manager2.submit_job(
            DownloadRequest(
                url="https://www.youtube.com/playlist?list=PL_MARK",
                dest_dir=dest_dir,
                audio_format="m4a",
                is_playlist=True,
                quality="best",
                track_metadata=(
                    {"index": 1, "id": "vid1", "title": "Song 1"},
                    {"index": 2, "id": "vid2", "title": "Song 2"},
                ),
            )
        )
        assert m4a_job.request.track_metadata[0].get("already_downloaded") is False
        assert m4a_job.request.track_metadata[1].get("already_downloaded") is False
    finally:
        manager2.shutdown()


def test_playlist_skip_duplicates_respects_format_in_sqlite_library(tmp_path: Path) -> None:
    store_path = tmp_path / "state.db"
    dest_dir = tmp_path / "music"
    dest_dir.mkdir()

    # Download vid1 as mp3 first
    downloader1 = _RecordingDownloader(tracks=[("vid1", "Song 1")])
    manager1, _ = _build_persistent_manager(tmp_path, downloader1, store_path)
    try:
        job1 = manager1.submit_job(
            DownloadRequest(
                url="https://www.youtube.com/watch?v=vid1",
                dest_dir=dest_dir,
                audio_format="mp3",
                is_playlist=False,
                quality="192",
            )
        )
        done1 = _wait_for_terminal(manager1, job1.job_id)
        assert done1.status == "completed"
    finally:
        manager1.shutdown()

    # Requesting the same video in m4a with skip_duplicates=True should still download it
    downloader2 = _RecordingDownloader(tracks=[("vid1", "Song 1")])
    manager2, _ = _build_persistent_manager(tmp_path, downloader2, store_path)
    try:
        job2 = manager2.submit_job(
            DownloadRequest(
                url="https://www.youtube.com/playlist?list=PL_FMT",
                dest_dir=dest_dir,
                audio_format="m4a",
                is_playlist=True,
                quality="best",
                skip_duplicates=True,
            )
        )
        done2 = _wait_for_terminal(manager2, job2.job_id)
        assert done2.status == "completed"
        assert done2.manifest is not None
        assert "vid1" in done2.manifest.downloaded
    finally:
        manager2.shutdown()
