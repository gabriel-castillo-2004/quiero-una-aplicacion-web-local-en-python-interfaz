"""Acceptance tests for MOD-16: POST /api/inspect, partial track selection,
real-time per-track progress (SSE & polling), and safe job cancellation.

All tests run completely offline using Flask's test_client(), tmp_path, and
mocked yt-dlp.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
import json
import sys
import threading
import time

import pytest

_SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from yt_music_dl.adapters.fs_adapter import LocalFileSystemAdapter
from yt_music_dl.adapters.sqlite_adapter import SqlitePersistenceAdapter
from yt_music_dl.adapters.ytdlp_adapter import SystemPreflightAdapter, YtDlpAdapter
from yt_music_dl.app import create_app
from yt_music_dl.event_bus import InMemoryEventBus
from yt_music_dl.job_manager import JobManager


def _build_test_app(
    tmp_path: Path,
    *,
    downloader: Any = None,
    store: Any = None,
) -> tuple[Any, JobManager]:
    fs = LocalFileSystemAdapter()
    event_bus = InMemoryEventBus()
    staging_root = tmp_path / "staging"
    manager = JobManager(
        downloader=downloader or YtDlpAdapter(),
        fs=fs,
        event_bus=event_bus,
        staging_root=staging_root,
        store=store,
    )
    app = create_app(
        job_manager=manager,
        event_bus=event_bus,
        fs=fs,
        preflight=SystemPreflightAdapter(),
        csrf_token="test-csrf-token",
    )
    app.config["TESTING"] = True
    return app, manager


# ---------------------------------------------------------------------------
# 1. POST /api/inspect (extract_flat, single track & playlist, validation)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://music.youtube.com/watch?v=abc123XYZ",
    ],
)
def test_mod16_inspect_single_track_uses_extract_flat(tmp_path: Path, url: str) -> None:
    app, manager = _build_test_app(tmp_path)
    try:
        client = app.test_client()
        captured_opts: list[dict[str, Any]] = []

        mock_ydl = MagicMock()
        mock_ydl.__enter__.return_value = mock_ydl
        mock_ydl.__exit__.return_value = False
        mock_ydl.extract_info.return_value = {
            "id": "dQw4w9WgXcQ",
            "title": "Single Inspected Track",
            "uploader": "Artist Channel",
            "duration": 213,
            "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
        }

        def fake_ydl_factory(opts: dict[str, Any]) -> Any:
            captured_opts.append(opts)
            return mock_ydl

        with patch("yt_dlp.YoutubeDL", side_effect=fake_ydl_factory):
            resp = client.post(
                "/api/inspect",
                json={"url": url},
                headers={"X-CSRF-Token": "test-csrf-token"},
            )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["title"] == "Single Inspected Track"
        assert (data.get("thumbnail") or data.get("thumbnail_url")) == "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"
        assert data.get("duration") == 213
        assert isinstance(data.get("tracks"), list)
        assert len(captured_opts) == 1
        assert captured_opts[0].get("extract_flat") in {True, "in_playlist"}
        _, kwargs = mock_ydl.extract_info.call_args
        assert kwargs.get("download") is False
    finally:
        manager.shutdown()


def test_mod16_inspect_playlist_returns_tracks_and_metadata(tmp_path: Path) -> None:
    app, manager = _build_test_app(tmp_path)
    try:
        client = app.test_client()
        captured_opts: list[dict[str, Any]] = []

        mock_ydl = MagicMock()
        mock_ydl.__enter__.return_value = mock_ydl
        mock_ydl.__exit__.return_value = False
        mock_ydl.extract_info.return_value = {
            "_type": "playlist",
            "id": "PL_MOD16",
            "title": "Inspectable Playlist",
            "uploader": "Playlist Owner",
            "duration": 425,
            "thumbnail": "https://i.ytimg.com/vi/v1/hqdefault.jpg",
            "entries": [
                {
                    "id": "v1",
                    "title": "First Song",
                    "duration": 185,
                    "thumbnail": "https://i.ytimg.com/vi/v1/hqdefault.jpg",
                },
                {
                    "id": "v2",
                    "title": "Second Song",
                    "duration": 240,
                    "thumbnails": [{"url": "https://i.ytimg.com/vi/v2/hqdefault.jpg"}],
                },
            ],
        }

        def fake_ydl_factory(opts: dict[str, Any]) -> Any:
            captured_opts.append(opts)
            return mock_ydl

        with patch("yt_dlp.YoutubeDL", side_effect=fake_ydl_factory):
            resp = client.post(
                "/api/inspect",
                json={"url": "https://www.youtube.com/playlist?list=PL_MOD16"},
                headers={"X-CSRF-Token": "test-csrf-token"},
            )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["title"] == "Inspectable Playlist"
        assert (data.get("thumbnail") or data.get("thumbnail_url")) == "https://i.ytimg.com/vi/v1/hqdefault.jpg"
        assert len(captured_opts) == 1
        assert captured_opts[0].get("extract_flat") in {True, "in_playlist"}

        tracks = data.get("tracks")
        assert isinstance(tracks, list)
        assert len(tracks) == 2
        assert tracks[0]["index"] == 1
        assert tracks[0]["title"] == "First Song"
        assert tracks[0]["duration"] == 185
        assert tracks[1]["index"] == 2
        assert tracks[1]["title"] == "Second Song"
        assert tracks[1]["duration"] == 240
    finally:
        manager.shutdown()


def test_mod16_inspect_rejects_invalid_url_and_missing_csrf(tmp_path: Path) -> None:
    app, manager = _build_test_app(tmp_path)
    try:
        client = app.test_client()

        no_csrf = client.post(
            "/api/inspect",
            json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
        )
        assert no_csrf.status_code == 403

        bad_url = client.post(
            "/api/inspect",
            json={"url": "https://example.com/not-youtube"},
            headers={"X-CSRF-Token": "test-csrf-token"},
        )
        assert bad_url.status_code == 400
    finally:
        manager.shutdown()


# ---------------------------------------------------------------------------
# 2. Partial selection + real-time per-track progress (SSE & polling)
# ---------------------------------------------------------------------------


def test_mod16_partial_selection_reports_per_track_progress_via_sse_and_polling(
    tmp_path: Path,
) -> None:
    dest_dir = tmp_path / "music"
    dest_dir.mkdir()
    captured_opts: list[dict[str, Any]] = []

    def fake_ydl_factory(opts: dict[str, Any]) -> Any:
        captured_opts.append(opts)
        mock_ydl = MagicMock()
        mock_ydl.__enter__.return_value = mock_ydl
        mock_ydl.__exit__.return_value = False

        def fake_extract_info(url: str, download: bool = False) -> dict[str, Any]:
            hook = opts["progress_hooks"][0]
            hook(
                {
                    "status": "downloading",
                    "downloaded_bytes": 50,
                    "total_bytes": 100,
                    "info_dict": {
                        "id": "v1",
                        "title": "First Song",
                        "playlist_index": 1,
                        "playlist_count": 2,
                    },
                }
            )
            hook(
                {
                    "status": "finished",
                    "info_dict": {
                        "id": "v1",
                        "title": "First Song",
                        "playlist_index": 1,
                        "playlist_count": 2,
                    },
                }
            )
            if download:
                out_parent = Path(opts["outtmpl"]).parent
                out_parent.mkdir(parents=True, exist_ok=True)
                (out_parent / "01 - First Song.mp3").write_bytes(b"mp3-1")
                (out_parent / "03 - Third Song.mp3").write_bytes(b"mp3-3")
            return {
                "_type": "playlist",
                "id": "PL_MOD16",
                "title": "Inspectable Playlist",
                "entries": [
                    {"id": "v1", "title": "First Song"},
                    {"id": "v3", "title": "Third Song"},
                ],
            }

        mock_ydl.extract_info.side_effect = fake_extract_info
        return mock_ydl

    downloader = YtDlpAdapter(ydl_factory=fake_ydl_factory)
    app, manager = _build_test_app(tmp_path, downloader=downloader)
    try:
        client = app.test_client()
        resp = client.post(
            "/api/jobs",
            json={
                "url": "https://www.youtube.com/playlist?list=PL_MOD16",
                "format": "mp3",
                "quality": "192",
                "dest_dir": str(dest_dir),
                "selected_tracks": [1, 3],
            },
            headers={"X-CSRF-Token": "test-csrf-token"},
        )
        assert resp.status_code == 201
        job_id = resp.get_json()["job_id"]

        # Poll until terminal state
        job_data: dict[str, Any] = {}
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            poll_resp = client.get(f"/api/jobs/{job_id}")
            assert poll_resp.status_code == 200
            job_data = poll_resp.get_json()
            if job_data["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.02)

        assert job_data["status"] == "completed"
        assert len(captured_opts) == 1
        assert captured_opts[0].get("playlist_items") == "1,3"
        assert len(job_data.get("promoted_files", [])) == 2

        # SSE stream replays initial/terminal snapshot cleanly
        sse_resp = client.get(f"/api/jobs/{job_id}/events")
        assert sse_resp.status_code == 200
        assert "text/event-stream" in sse_resp.mimetype
        sse_body = sse_resp.get_data(as_text=True)
        assert "data:" in sse_body
        assert job_id in sse_body
    finally:
        manager.shutdown()


# ---------------------------------------------------------------------------
# 3. Safe cancellation of an active download job
# ---------------------------------------------------------------------------


def test_mod16_cancel_active_job_transitions_to_cancelled_and_cleans_staging(
    tmp_path: Path,
) -> None:
    dest_dir = tmp_path / "music"
    dest_dir.mkdir()
    started = threading.Event()
    allow_continue = threading.Event()

    def fake_ydl_factory(opts: dict[str, Any]) -> Any:
        mock_ydl = MagicMock()
        mock_ydl.__enter__.return_value = mock_ydl
        mock_ydl.__exit__.return_value = False

        def fake_extract_info(url: str, download: bool = False) -> dict[str, Any]:
            hook = opts["progress_hooks"][0]
            out_parent = Path(opts["outtmpl"]).parent
            out_parent.mkdir(parents=True, exist_ok=True)
            (out_parent / "partial.part").write_bytes(b"partial")

            started.set()
            allow_continue.wait(timeout=3.0)
            hook(
                {
                    "status": "downloading",
                    "downloaded_bytes": 10,
                    "total_bytes": 100,
                    "info_dict": {"id": "v1", "title": "Long Track"},
                }
            )
            return {"id": "v1", "title": "Long Track"}

        mock_ydl.extract_info.side_effect = fake_extract_info
        return mock_ydl

    downloader = YtDlpAdapter(ydl_factory=fake_ydl_factory)
    app, manager = _build_test_app(tmp_path, downloader=downloader)
    try:
        client = app.test_client()
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

        assert started.wait(timeout=3.0), "Download did not start in time"

        cancel_resp = client.post(
            f"/api/jobs/{job_id}/cancel",
            headers={"X-CSRF-Token": "test-csrf-token"},
        )
        assert cancel_resp.status_code == 200
        allow_continue.set()

        job_data: dict[str, Any] = {}
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            poll_resp = client.get(f"/api/jobs/{job_id}")
            assert poll_resp.status_code == 200
            job_data = poll_resp.get_json()
            if job_data["status"] == "cancelled":
                break
            time.sleep(0.02)

        assert job_data["status"] == "cancelled"
        staging_job_dir = tmp_path / "staging" / job_id
        assert not staging_job_dir.exists(), "Staging directory must be cleaned up on cancellation"
        assert list(dest_dir.glob("*.mp3")) == []
    finally:
        allow_continue.set()
        manager.shutdown()


# ---------------------------------------------------------------------------
# 4. Metadata inspection cache (TTL, max entries, explicit refresh) & library flags
# ---------------------------------------------------------------------------


def _build_ytdlp_adapter_with_cache(
    ydl_factory: Any,
    *,
    ttl_seconds: float | None = None,
    max_entries: int | None = None,
) -> YtDlpAdapter:
    import inspect

    sig = inspect.signature(YtDlpAdapter.__init__)
    kwargs: dict[str, Any] = {"ydl_factory": ydl_factory}
    if ttl_seconds is not None:
        for name in ("preview_cache_ttl", "cache_ttl", "inspect_cache_ttl", "ttl"):
            if name in sig.parameters:
                kwargs[name] = ttl_seconds
                break
        else:
            pytest.fail("YtDlpAdapter.__init__ must accept a configurable cache TTL parameter")
    if max_entries is not None:
        for name in (
            "preview_cache_max_entries",
            "cache_max_entries",
            "inspect_cache_max_entries",
            "max_cache_entries",
            "max_entries",
        ):
            if name in sig.parameters:
                kwargs[name] = max_entries
                break
        else:
            pytest.fail("YtDlpAdapter.__init__ must accept a configurable cache max_entries parameter")
    return YtDlpAdapter(**kwargs)


def _preview_with_refresh(adapter: YtDlpAdapter, url: str, *, refresh: bool) -> dict[str, Any]:
    import inspect

    sig = inspect.signature(adapter.preview)
    for name in ("refresh", "force_refresh", "bypass_cache"):
        if name in sig.parameters:
            return adapter.preview(url, **{name: refresh})  # type: ignore[call-arg]
    pytest.fail("YtDlpAdapter.preview must accept an explicit refresh parameter")


def test_mod16_inspect_preview_uses_in_memory_cache_and_supports_refresh() -> None:
    call_count = 0

    def fake_ydl_factory(opts: dict[str, Any]) -> Any:
        mock_ydl = MagicMock()
        mock_ydl.__enter__.return_value = mock_ydl
        mock_ydl.__exit__.return_value = False

        def fake_extract_info(url: str, download: bool = False) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            return {
                "id": "cacheVid1",
                "title": f"Cached Track v{call_count}",
                "uploader": "Artist",
                "duration": 180,
                "thumbnail": "https://i.ytimg.com/vi/cacheVid1/hqdefault.jpg",
            }

        mock_ydl.extract_info.side_effect = fake_extract_info
        return mock_ydl

    adapter = _build_ytdlp_adapter_with_cache(fake_ydl_factory, ttl_seconds=300.0, max_entries=32)
    url = "https://www.youtube.com/watch?v=cacheVid1"

    first = adapter.preview(url)
    second = adapter.preview(url)
    assert call_count == 1
    assert first["title"] == "Cached Track v1"
    assert second["title"] == "Cached Track v1"

    refreshed = _preview_with_refresh(adapter, url, refresh=True)
    assert call_count == 2
    assert refreshed["title"] == "Cached Track v2"


def test_mod16_inspect_preview_cache_expires_after_ttl() -> None:
    call_count = 0

    def fake_ydl_factory(opts: dict[str, Any]) -> Any:
        mock_ydl = MagicMock()
        mock_ydl.__enter__.return_value = mock_ydl
        mock_ydl.__exit__.return_value = False

        def fake_extract_info(url: str, download: bool = False) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            return {
                "id": "ttlVid1",
                "title": f"TTL Track v{call_count}",
                "uploader": "Artist",
                "duration": 120,
            }

        mock_ydl.extract_info.side_effect = fake_extract_info
        return mock_ydl

    adapter = _build_ytdlp_adapter_with_cache(fake_ydl_factory, ttl_seconds=0.05, max_entries=16)
    url = "https://www.youtube.com/watch?v=ttlVid1"

    first = adapter.preview(url)
    assert first["title"] == "TTL Track v1"
    assert call_count == 1

    time.sleep(0.08)
    after_ttl = adapter.preview(url)
    assert call_count == 2
    assert after_ttl["title"] == "TTL Track v2"


def test_mod16_inspect_preview_cache_evicts_when_max_entries_exceeded() -> None:
    calls: list[str] = []

    def fake_ydl_factory(opts: dict[str, Any]) -> Any:
        mock_ydl = MagicMock()
        mock_ydl.__enter__.return_value = mock_ydl
        mock_ydl.__exit__.return_value = False

        def fake_extract_info(url: str, download: bool = False) -> dict[str, Any]:
            calls.append(url)
            vid = url.split("v=")[-1]
            return {"id": vid, "title": f"Track {vid}", "duration": 100}

        mock_ydl.extract_info.side_effect = fake_extract_info
        return mock_ydl

    adapter = _build_ytdlp_adapter_with_cache(fake_ydl_factory, ttl_seconds=300.0, max_entries=2)
    u1 = "https://www.youtube.com/watch?v=vidA1"
    u2 = "https://www.youtube.com/watch?v=vidB2"
    u3 = "https://www.youtube.com/watch?v=vidC3"

    adapter.preview(u1)
    adapter.preview(u2)
    adapter.preview(u3)
    assert len(calls) == 3

    # u1 must have been evicted when u3 was inserted into a 2-entry cache
    adapter.preview(u1)
    assert len(calls) == 4


def test_mod16_inspect_marks_already_downloaded_tracks_by_id_and_format(tmp_path: Path) -> None:
    store = SqlitePersistenceAdapter(tmp_path / "library.db")
    import inspect

    rec_sig = inspect.signature(store.record_downloaded_ids)
    if "audio_format" in rec_sig.parameters:
        store.record_downloaded_ids(["v1"], audio_format="mp3")  # type: ignore[call-arg]
    elif "format" in rec_sig.parameters:
        store.record_downloaded_ids(["v1"], format="mp3")  # type: ignore[call-arg]
    else:
        store.record_downloaded_ids(["v1"])

    app, manager = _build_test_app(tmp_path, store=store)
    try:
        client = app.test_client()
        mock_ydl = MagicMock()
        mock_ydl.__enter__.return_value = mock_ydl
        mock_ydl.__exit__.return_value = False
        mock_ydl.extract_info.return_value = {
            "_type": "playlist",
            "id": "PL_LIBRARY",
            "title": "Library Check Playlist",
            "entries": [
                {"id": "v1", "title": "Already Downloaded Song", "duration": 180},
                {"id": "v2", "title": "New Song", "duration": 200},
            ],
        }

        with patch("yt_dlp.YoutubeDL", return_value=mock_ydl):
            mp3_resp = client.post(
                "/api/inspect",
                json={
                    "url": "https://www.youtube.com/playlist?list=PL_LIBRARY",
                    "format": "mp3",
                    "refresh": True,
                },
                headers={"X-CSRF-Token": "test-csrf-token"},
            )
            assert mp3_resp.status_code == 200
            mp3_tracks = mp3_resp.get_json()["tracks"]
            assert mp3_tracks[0]["already_downloaded"] is True
            assert mp3_tracks[1]["already_downloaded"] is False

            m4a_resp = client.post(
                "/api/inspect",
                json={
                    "url": "https://www.youtube.com/playlist?list=PL_LIBRARY",
                    "format": "m4a",
                    "refresh": True,
                },
                headers={"X-CSRF-Token": "test-csrf-token"},
            )
            assert m4a_resp.status_code == 200
            m4a_tracks = m4a_resp.get_json()["tracks"]
            assert m4a_tracks[0]["already_downloaded"] is False
            assert m4a_tracks[1]["already_downloaded"] is False
    finally:
        manager.shutdown()
