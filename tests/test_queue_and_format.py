"""Queue progress, format selection (MP3/M4A/original), and direct download tests.

All tests run offline using Flask's test_client(), tmp_path, and mocked yt-dlp.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
import sys
import time

_SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from yt_music_dl.adapters.fs_adapter import LocalFileSystemAdapter
from yt_music_dl.adapters.ytdlp_adapter import YtDlpAdapter, build_ytdlp_options
from yt_music_dl.app import create_app
from yt_music_dl.event_bus import InMemoryEventBus
from yt_music_dl.job_manager import JobManager
from yt_music_dl.models import DownloadRequest


class _FakePreflight:
    def check_ffmpeg(self) -> dict[str, Any]:
        return {"available": True, "ffmpeg": True, "ffprobe": True}


def _build_test_app(tmp_path: Path, downloader: Any) -> tuple[Any, JobManager]:
    fs = LocalFileSystemAdapter()
    event_bus = InMemoryEventBus()
    manager = JobManager(
        downloader=downloader,
        fs=fs,
        event_bus=event_bus,
        staging_root=tmp_path / "staging",
    )
    app = create_app(
        job_manager=manager,
        event_bus=event_bus,
        fs=fs,
        preflight=_FakePreflight(),
        csrf_token="test-csrf-token",
    )
    app.config["TESTING"] = True
    return app, manager


def test_build_ytdlp_options_original_skips_audio_reencode(tmp_path: Path) -> None:
    req = DownloadRequest(
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        dest_dir=tmp_path / "music",
        audio_format="original",
        is_playlist=False,
        quality="best",
    )
    opts = build_ytdlp_options(
        request=req,
        staging_dir=tmp_path / "staging",
        archive_path=tmp_path / "archive.txt",
        progress_hook=lambda _d: None,
    )
    pp_keys = [pp["key"] for pp in opts["postprocessors"]]
    assert "FFmpegExtractAudio" not in pp_keys
    assert pp_keys == ["FFmpegThumbnailsConvertor", "FFmpegMetadata", "EmbedThumbnail"]


def test_playlist_progress_hooks_report_percentage_status_and_position_via_sse_and_polling(
    tmp_path: Path,
) -> None:
    dest_dir = tmp_path / "music"
    dest_dir.mkdir()
    observed_snapshots: list[dict[str, Any]] = []

    def fake_ydl_factory(opts: dict[str, Any]) -> Any:
        mock_ydl = MagicMock()
        mock_ydl.__enter__.return_value = mock_ydl
        mock_ydl.__exit__.return_value = False

        def fake_extract_info(url: str, download: bool = False) -> dict[str, Any]:
            hook = opts["progress_hooks"][0]
            hook(
                {
                    "status": "downloading",
                    "downloaded_bytes": 500,
                    "total_bytes": 1000,
                    "info_dict": {
                        "id": "trk2",
                        "title": "Second Track",
                        "playlist_index": 2,
                        "playlist_count": 5,
                    },
                }
            )
            hook(
                {
                    "status": "finished",
                    "info_dict": {
                        "id": "trk2",
                        "title": "Second Track",
                        "playlist_index": 2,
                        "playlist_count": 5,
                    },
                }
            )
            if download:
                out_parent = Path(opts["outtmpl"]).parent
                out_parent.mkdir(parents=True, exist_ok=True)
                (out_parent / "02 - Second Track.opus").write_bytes(b"original-opus-bytes")
            return {
                "_type": "playlist",
                "id": "PL_QUEUE",
                "title": "Queue Playlist",
                "entries": [{"id": "trk2", "title": "Second Track"}],
            }

        mock_ydl.extract_info.side_effect = fake_extract_info
        return mock_ydl

    class _CapturingAdapter(YtDlpAdapter):
        def download(self, request, staging_dir, archive_path, on_progress, is_cancelled):
            def _capture(snap):
                observed_snapshots.append(snap.to_dict())
                on_progress(snap)

            return super().download(request, staging_dir, archive_path, _capture, is_cancelled)

    app, manager = _build_test_app(
        tmp_path,
        downloader=_CapturingAdapter(ydl_factory=fake_ydl_factory),
    )
    try:
        client = app.test_client()
        resp = client.post(
            "/api/jobs",
            json={
                "url": "https://www.youtube.com/playlist?list=PL_QUEUE",
                "format": "original",
                "quality": "best",
                "dest_dir": str(dest_dir),
            },
            headers={"X-CSRF-Token": "test-csrf-token"},
        )
        assert resp.status_code == 201
        job_id = resp.get_json()["job_id"]

        job_data: dict[str, Any] = {}
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            poll = client.get(f"/api/jobs/{job_id}")
            assert poll.status_code == 200
            job_data = poll.get_json()
            if job_data["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.02)

        assert job_data["status"] == "completed"
        assert any(
            s["status"] == "downloading"
            and s["progress"] == 50.0
            and "Canción 2 de 5" in s["message"]
            for s in observed_snapshots
        )
        assert any(s["status"] == "postprocessing" and s["progress"] == 99.0 for s in observed_snapshots)

        sse_resp = client.get(f"/api/jobs/{job_id}/events")
        assert sse_resp.status_code == 200
        assert "text/event-stream" in sse_resp.mimetype
        assert job_id in sse_resp.get_data(as_text=True)

        dl_resp = client.get(f"/api/jobs/{job_id}/audio/0?download=1")
        assert dl_resp.status_code == 200
        assert "attachment" in dl_resp.headers.get("Content-Disposition", "")
        assert dl_resp.data == b"original-opus-bytes"
        dl_resp.close()
    finally:
        manager.shutdown()


def test_ui_exposes_queue_panel_cancel_retry_format_selector_and_download_links(
    tmp_path: Path,
) -> None:
    app, manager = _build_test_app(tmp_path, downloader=YtDlpAdapter())
    try:
        client = app.test_client()
        html = client.get("/").get_data(as_text=True)
        app_js = client.get("/static/app.js").get_data(as_text=True)

        assert 'id="live-track-queue"' in html
        assert 'id="progress-bar"' in html
        assert 'id="cancel-btn"' in html
        assert 'id="format-quality-select"' in html
        assert 'value="mp3:320"' in html
        assert 'value="mp3:192"' in html
        assert 'value="m4a:best"' in html
        assert "Reintentar" in app_js
        assert "?download=1" in app_js
    finally:
        manager.shutdown()


def test_job_manager_runs_concurrent_pool_bounded_by_max_workers(tmp_path: Path) -> None:
    """JobManager executes up to max_workers (1..3) jobs in parallel without exceeding the limit."""
    import threading

    active_lock = threading.Lock()
    active_count = 0
    max_observed_active = 0
    two_running = threading.Event()

    class _BarrierDownloader:
        def download(
            self,
            request: DownloadRequest,
            staging_dir: Path,
            archive_path: Path,
            on_progress: Any,
            is_cancelled: Any,
        ) -> Any:
            nonlocal active_count, max_observed_active
            from yt_music_dl.models import DownloadManifest, ProgressSnapshot, TrackResult

            with active_lock:
                active_count += 1
                if active_count > max_observed_active:
                    max_observed_active = active_count
                if active_count >= 2:
                    two_running.set()

            # Wait briefly so concurrent workers overlap deterministically
            two_running.wait(timeout=2.0)
            time.sleep(0.05)

            try:
                on_progress(
                    ProgressSnapshot(
                        job_id="",
                        status="downloading",
                        progress=50.0,
                        current_track="Track",
                        message="Downloading",
                    )
                )
                out_file = staging_dir / f"{request.url.split('=')[-1]}.mp3"
                out_file.write_bytes(b"audio")
                return DownloadManifest(
                    downloaded=("Track",),
                    tracks=(TrackResult(track_id="1", title="Track", status="downloaded"),),
                )
            finally:
                with active_lock:
                    active_count -= 1

        def preview(self, url: str) -> dict[str, Any]:
            return {}

    fs = LocalFileSystemAdapter()
    event_bus = InMemoryEventBus()
    manager = JobManager(
        downloader=_BarrierDownloader(),
        fs=fs,
        event_bus=event_bus,
        staging_root=tmp_path / "staging",
        max_workers=2,
    )
    try:
        dest_dir = tmp_path / "music"
        dest_dir.mkdir()
        jobs = [
            manager.submit_job(
                DownloadRequest(
                    url=f"https://www.youtube.com/watch?v=vid{i:02d}aaaaaaa",
                    dest_dir=dest_dir,
                    audio_format="mp3",
                    quality="192",
                )
            )
            for i in range(3)
        ]

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            states = [manager.get_job(j.job_id) for j in jobs]
            if all(s is not None and s.status == "completed" for s in states):
                break
            time.sleep(0.02)

        states = [manager.get_job(j.job_id) for j in jobs]
        assert all(s is not None and s.status == "completed" for s in states)
        assert max_observed_active == 2
    finally:
        manager.shutdown()


def test_ytdlp_adapter_retries_transient_errors_with_exponential_backoff(
    tmp_path: Path,
) -> None:
    """YtDlpAdapter retries transient network/rate-limit errors with exponential backoff."""
    from unittest.mock import patch

    attempts = 0
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    archive_path = tmp_path / "archive.txt"

    def fake_ydl_factory(opts: dict[str, Any]) -> Any:
        mock_ydl = MagicMock()
        mock_ydl.__enter__.return_value = mock_ydl
        mock_ydl.__exit__.return_value = False

        def fake_extract_info(url: str, download: bool = False) -> dict[str, Any]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("HTTP Error 429: Too Many Requests")
            if attempts == 2:
                raise RuntimeError("Connection reset by peer")
            return {
                "id": "vid123",
                "title": "Recovered Track",
            }

        mock_ydl.extract_info.side_effect = fake_extract_info
        return mock_ydl

    adapter = YtDlpAdapter(ydl_factory=fake_ydl_factory)
    req = DownloadRequest(
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        dest_dir=tmp_path / "music",
        audio_format="mp3",
        is_playlist=False,
        quality="192",
    )

    sleep_delays: list[float] = []
    with patch("time.sleep", side_effect=lambda d: sleep_delays.append(float(d))):
        manifest = adapter.download(
            request=req,
            staging_dir=staging_dir,
            archive_path=archive_path,
            on_progress=lambda _s: None,
            is_cancelled=lambda: False,
        )

    assert attempts == 3
    assert len(sleep_delays) >= 2
    assert sleep_delays[0] > 0
    assert sleep_delays[1] > sleep_delays[0]
    assert manifest.downloaded == ("Recovered Track",)
    assert manifest.failed == ()


def test_ui_exposes_concurrency_selector_for_1_to_3_workers(tmp_path: Path) -> None:
    """Frontend HTML and JS expose a concurrency selector (1 to 3 workers)."""
    app, manager = _build_test_app(tmp_path, downloader=YtDlpAdapter())
    try:
        client = app.test_client()
        html = client.get("/").get_data(as_text=True)
        app_js = client.get("/static/app.js").get_data(as_text=True)

        assert "concurrency" in html.lower()
        assert 'value="1"' in html
        assert 'value="2"' in html
        assert 'value="3"' in html
        assert "concurrency" in app_js
    finally:
        manager.shutdown()


def test_concurrent_jobs_publish_isolated_events_to_event_bus(tmp_path: Path) -> None:
    """Concurrent workers safely publish isolated progress and completion events."""
    import threading

    from yt_music_dl.models import DownloadManifest, ProgressSnapshot, TrackResult

    barrier = threading.Barrier(3, timeout=3.0)

    class _ConcurrentEventDownloader:
        def download(
            self,
            request: DownloadRequest,
            staging_dir: Path,
            archive_path: Path,
            on_progress: Any,
            is_cancelled: Any,
        ) -> DownloadManifest:
            vid = request.url.split("=")[-1]
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            on_progress(
                ProgressSnapshot(
                    job_id="",
                    status="downloading",
                    progress=45.0,
                    current_track=f"Track-{vid}",
                    message=f"Downloading {vid}",
                )
            )
            out_file = staging_dir / f"{vid}.mp3"
            out_file.write_bytes(b"audio-bytes")
            return DownloadManifest(
                downloaded=(vid,),
                tracks=(TrackResult(track_id=vid, title=f"Track-{vid}", status="downloaded"),),
            )

    fs = LocalFileSystemAdapter()
    event_bus = InMemoryEventBus()
    manager = JobManager(
        downloader=_ConcurrentEventDownloader(),
        fs=fs,
        event_bus=event_bus,
        staging_root=tmp_path / "staging",
        max_workers=3,
    )
    try:
        dest_dir = tmp_path / "music"
        dest_dir.mkdir()
        jobs = [
            manager.submit_job(
                DownloadRequest(
                    url=f"https://www.youtube.com/watch?v=conc{i:02d}aaaaa",
                    dest_dir=dest_dir,
                    audio_format="mp3",
                    quality="192",
                    concurrency=3,
                )
            )
            for i in range(3)
        ]

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if all(
                (s := manager.get_job(j.job_id)) is not None and s.status == "completed"
                for j in jobs
            ):
                break
            time.sleep(0.02)

        for job in jobs:
            state = manager.get_job(job.job_id)
            assert state is not None and state.status == "completed"
            snapshot, history, q = event_bus.subscribe_with_snapshot(job.job_id)
            event_bus.unsubscribe(job.job_id, q)
            assert snapshot is not None
            assert snapshot["job_id"] == job.job_id
            assert snapshot["status"] == "completed"
            assert all(ev["data"]["job_id"] == job.job_id for ev in history)
            vid = job.request.url.split("=")[-1]
            assert any(
                ev["event"] == "progress" and ev["data"].get("current_track") == f"Track-{vid}"
                for ev in history
            )
    finally:
        manager.shutdown()


def test_api_jobs_validates_concurrency_and_skip_duplicates(tmp_path: Path) -> None:
    """POST /api/jobs validates concurrency (1..3) and forwards skip_duplicates."""
    from yt_music_dl.models import DownloadManifest

    recorded_requests: list[DownloadRequest] = []

    class _CaptureDownloader:
        def download(
            self,
            request: DownloadRequest,
            staging_dir: Path,
            archive_path: Path,
            on_progress: Any,
            is_cancelled: Any,
        ) -> DownloadManifest:
            recorded_requests.append(request)
            (staging_dir / "track.mp3").write_bytes(b"audio")
            return DownloadManifest(downloaded=("dQw4w9WgXcQ",))

    app, manager = _build_test_app(tmp_path, downloader=_CaptureDownloader())
    dest_dir = tmp_path / "music"
    dest_dir.mkdir()
    try:
        client = app.test_client()
        headers = {"X-CSRF-Token": "test-csrf-token"}

        for invalid in (0, 4, True, "2"):
            bad_resp = client.post(
                "/api/jobs",
                json={
                    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    "format": "mp3",
                    "quality": "192",
                    "dest_dir": str(dest_dir),
                    "concurrency": invalid,
                },
                headers=headers,
            )
            assert bad_resp.status_code == 400

        ok_skip = client.post(
            "/api/jobs",
            json={
                "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "format": "mp3",
                "quality": "192",
                "dest_dir": str(dest_dir),
                "concurrency": 3,
                "skip_duplicates": True,
            },
            headers=headers,
        )
        assert ok_skip.status_code == 201

        ok_force = client.post(
            "/api/jobs",
            json={
                "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "format": "mp3",
                "quality": "192",
                "dest_dir": str(dest_dir),
                "concurrency": 1,
                "skip_duplicates": False,
            },
            headers=headers,
        )
        assert ok_force.status_code == 201

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and len(recorded_requests) < 2:
            time.sleep(0.02)

        assert len(recorded_requests) == 2
        by_conc = {r.concurrency: r for r in recorded_requests}
        assert by_conc[3].skip_duplicates is True
        assert by_conc[1].skip_duplicates is False
    finally:
        manager.shutdown()


def test_duplicate_skipping_and_force_redownload_via_sqlite_store(tmp_path: Path) -> None:
    """SqlitePersistenceAdapter + JobManager skip duplicates when enabled and redownload when disabled."""
    from yt_music_dl.adapters.sqlite_adapter import SqlitePersistenceAdapter
    from yt_music_dl.models import DownloadManifest, TrackResult

    class _ArchiveAwareDownloader:
        def __init__(self, tracks: list[tuple[str, str]]) -> None:
            self.tracks = tracks

        def download(
            self,
            request: DownloadRequest,
            staging_dir: Path,
            archive_path: Path,
            on_progress: Any,
            is_cancelled: Any,
        ) -> DownloadManifest:
            archived: set[str] = set()
            if archive_path.is_file():
                for line in archive_path.read_text(encoding="utf-8").splitlines():
                    parts = line.strip().split()
                    if parts:
                        archived.add(parts[-1])

            downloaded: list[str] = []
            skipped: list[str] = []
            results: list[TrackResult] = []
            for idx, (vid, title) in enumerate(self.tracks, start=1):
                if vid in archived:
                    skipped.append(vid)
                    results.append(
                        TrackResult(track_id=vid, title=title, status="skipped", reason="Already in archive")
                    )
                    continue
                out_file = staging_dir / f"{idx:02d} - {title}.mp3"
                out_file.write_bytes(b"audio-data")
                downloaded.append(vid)
                results.append(
                    TrackResult(track_id=vid, title=title, status="downloaded", file_path=str(out_file))
                )
            return DownloadManifest(
                downloaded=tuple(downloaded),
                skipped_archive=tuple(skipped),
                tracks=tuple(results),
            )

    store = SqlitePersistenceAdapter(tmp_path / "state.db")
    downloader = _ArchiveAwareDownloader([("vidDup1", "First Song"), ("vidNew2", "Second Song")])
    manager = JobManager(
        downloader=downloader,
        fs=LocalFileSystemAdapter(),
        event_bus=InMemoryEventBus(),
        staging_root=tmp_path / "staging",
        store=store,
        max_workers=2,
    )
    try:
        dest1 = tmp_path / "music1"
        dest1.mkdir()
        job1 = manager.submit_job(
            DownloadRequest(
                url="https://www.youtube.com/playlist?list=PLdupA",
                dest_dir=dest1,
                audio_format="mp3",
                is_playlist=True,
                quality="192",
                skip_duplicates=False,
            )
        )
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            s1 = manager.get_job(job1.job_id)
            if s1 is not None and s1.status == "completed":
                break
            time.sleep(0.02)
        assert {"vidDup1", "vidNew2"}.issubset(store.get_downloaded_ids())

        # New destination with skip_duplicates=True should seed archive.txt from store and skip both
        dest2 = tmp_path / "music2"
        dest2.mkdir()
        job2 = manager.submit_job(
            DownloadRequest(
                url="https://www.youtube.com/playlist?list=PLdupB",
                dest_dir=dest2,
                audio_format="mp3",
                is_playlist=True,
                quality="192",
                skip_duplicates=True,
            )
        )
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            s2 = manager.get_job(job2.job_id)
            if s2 is not None and s2.status == "completed":
                break
            time.sleep(0.02)
        s2 = manager.get_job(job2.job_id)
        assert s2 is not None and s2.manifest is not None
        assert s2.manifest.downloaded == ()
        assert set(s2.manifest.skipped_archive) == {"vidDup1", "vidNew2"}

        # Force redownload (skip_duplicates=False) in a clean destination must NOT seed archive.txt
        dest3 = tmp_path / "music3"
        dest3.mkdir()
        job3 = manager.submit_job(
            DownloadRequest(
                url="https://www.youtube.com/playlist?list=PLdupC",
                dest_dir=dest3,
                audio_format="mp3",
                is_playlist=True,
                quality="192",
                skip_duplicates=False,
            )
        )
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            s3 = manager.get_job(job3.job_id)
            if s3 is not None and s3.status == "completed":
                break
            time.sleep(0.02)
        s3 = manager.get_job(job3.job_id)
        assert s3 is not None and s3.manifest is not None
        assert set(s3.manifest.downloaded) == {"vidDup1", "vidNew2"}
        assert s3.manifest.skipped_archive == ()
    finally:
        manager.shutdown()


def test_ui_exposes_skip_duplicates_checkbox_and_wiring(tmp_path: Path) -> None:
    """Frontend HTML and JS expose the duplicate-skipping toggle."""
    app, manager = _build_test_app(tmp_path, downloader=YtDlpAdapter())
    try:
        client = app.test_client()
        html = client.get("/").get_data(as_text=True)
        app_js = client.get("/static/app.js").get_data(as_text=True)

        assert 'id="skip-duplicates-checkbox"' in html
        assert 'name="skip_duplicates"' in html
        assert "skip-duplicates-checkbox" in app_js
        assert "skip_duplicates" in app_js
    finally:
        manager.shutdown()

