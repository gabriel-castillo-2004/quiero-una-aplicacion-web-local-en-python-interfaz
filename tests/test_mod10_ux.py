"""Acceptance tests for MOD-10: UX improvements, track selection, ffmpeg detection, and HTTP 200.

All tests run completely offline using Flask's test_client(), tmp_path, and mocked yt-dlp / shutil.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
import shutil
import sys
import time
import types

_SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from yt_music_dl.adapters.fs_adapter import LocalFileSystemAdapter
from yt_music_dl.adapters.ytdlp_adapter import SystemPreflightAdapter, YtDlpAdapter
from yt_music_dl.app import create_app
from yt_music_dl.event_bus import InMemoryEventBus
from yt_music_dl.job_manager import JobManager
from yt_music_dl.ports import PreflightPort


def _build_app(
    tmp_path: Path,
    *,
    preflight: PreflightPort | None = None,
    downloader: Any = None,
) -> tuple[Any, JobManager]:
    fs = LocalFileSystemAdapter()
    event_bus = InMemoryEventBus()
    staging_root = tmp_path / "staging"
    manager = JobManager(
        downloader=downloader or YtDlpAdapter(),
        fs=fs,
        event_bus=event_bus,
        staging_root=staging_root,
    )
    app = create_app(
        job_manager=manager,
        event_bus=event_bus,
        fs=fs,
        preflight=preflight or SystemPreflightAdapter(),
        csrf_token="test-csrf-token",
    )
    app.config["TESTING"] = True
    return app, manager


# ---------------------------------------------------------------------------
# 1. HTTP 200 on "/" and UX improvements in UI & static assets
# ---------------------------------------------------------------------------


def test_mod10_index_returns_200_with_unified_selector_and_ux_controls(tmp_path: Path) -> None:
    app, manager = _build_app(tmp_path)
    try:
        client = app.test_client()

        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        lower_html = html.lower()

        # Unified format/quality selector options
        assert "320 kbps" in lower_html
        assert "192 kbps" in lower_html
        assert "audio original" in lower_html or "sin recodificar" in lower_html

        # Frictionless form controls: Paste button & destination chips/picker
        assert "pegar" in lower_html

        # Granular playlist track selection controls
        assert "elegir canciones" in lower_html
        assert "todas" in lower_html
        assert "ninguna" in lower_html

        # History search/filter & fixed bottom continuous player bar
        assert "historial" in lower_html
        assert "anterior" in lower_html
        assert "siguiente" in lower_html

        for asset in ("/static/styles.css", "/static/app.js", "/static/favicon.svg"):
            asset_resp = client.get(asset)
            assert asset_resp.status_code == 200, f"{asset} returned {asset_resp.status_code}"
            assert len(asset_resp.data) > 0
    finally:
        manager.shutdown()


# ---------------------------------------------------------------------------
# 2. POST /api/preview returns playlist track list with flat extraction
# ---------------------------------------------------------------------------


def test_mod10_preview_playlist_returns_track_list_with_flat_extraction(tmp_path: Path) -> None:
    app, manager = _build_app(tmp_path)
    try:
        client = app.test_client()
        captured_opts: list[dict[str, Any]] = []

        mock_ydl = MagicMock()
        mock_ydl.__enter__.return_value = mock_ydl
        mock_ydl.__exit__.return_value = False
        mock_ydl.extract_info.return_value = {
            "_type": "playlist",
            "id": "PL999",
            "title": "Focus Beats",
            "uploader": "LoFi Radio",
            "entries": [
                {
                    "id": "vid1",
                    "title": "Morning Coffee",
                    "duration": 185,
                    "thumbnail": "https://i.ytimg.com/vi/vid1/hqdefault.jpg",
                },
                {
                    "id": "vid2",
                    "title": "Rainy Window",
                    "duration": 240,
                    "thumbnails": [{"url": "https://i.ytimg.com/vi/vid2/hqdefault.jpg"}],
                },
            ],
        }

        def fake_ydl_factory(opts: dict[str, Any]) -> Any:
            captured_opts.append(opts)
            return mock_ydl

        with patch("yt_dlp.YoutubeDL", side_effect=fake_ydl_factory):
            resp = client.post(
                "/api/preview",
                json={"url": "https://www.youtube.com/playlist?list=PL999"},
                headers={"X-CSRF-Token": "test-csrf-token"},
            )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["is_playlist"] is True
        assert data["track_count"] == 2
        assert len(captured_opts) == 1
        assert captured_opts[0].get("extract_flat") in {True, "in_playlist"}
        _, kwargs = mock_ydl.extract_info.call_args
        assert kwargs.get("download") is False

        tracks = data.get("tracks")
        assert isinstance(tracks, list)
        assert len(tracks) == 2

        assert tracks[0]["index"] == 1
        assert tracks[0]["title"] == "Morning Coffee"
        assert tracks[0]["duration"] == 185
        assert (tracks[0].get("thumbnail") or tracks[0].get("thumbnail_url")) == "https://i.ytimg.com/vi/vid1/hqdefault.jpg"

        assert tracks[1]["index"] == 2
        assert tracks[1]["title"] == "Rainy Window"
        assert tracks[1]["duration"] == 240
        assert (tracks[1].get("thumbnail") or tracks[1].get("thumbnail_url")) == "https://i.ytimg.com/vi/vid2/hqdefault.jpg"
    finally:
        manager.shutdown()


# ---------------------------------------------------------------------------
# 3. Granular download of selected playlist tracks (mocked yt-dlp)
# ---------------------------------------------------------------------------


def test_mod10_download_selected_playlist_tracks_configures_playlist_items(tmp_path: Path) -> None:
    dest_dir = tmp_path / "music"
    dest_dir.mkdir()
    captured_opts: list[dict[str, Any]] = []

    def fake_ydl_factory(opts: dict[str, Any]) -> Any:
        captured_opts.append(opts)
        mock_ydl = MagicMock()
        mock_ydl.__enter__.return_value = mock_ydl
        mock_ydl.__exit__.return_value = False

        def fake_extract_info(url: str, download: bool = False) -> dict[str, Any]:
            if download:
                # Create only the selected tracks in staging_dir
                outtmpl_parent = Path(opts["outtmpl"]).parent
                outtmpl_parent.mkdir(parents=True, exist_ok=True)
                (outtmpl_parent / "01 - Track One.mp3").write_bytes(b"mp3-1")
                (outtmpl_parent / "03 - Track Three.mp3").write_bytes(b"mp3-3")
            return {
                "_type": "playlist",
                "id": "PL999",
                "title": "Focus Beats",
                "entries": [
                    {"id": "vid1", "title": "Track One"},
                    {"id": "vid3", "title": "Track Three"},
                ],
            }

        mock_ydl.extract_info.side_effect = fake_extract_info
        return mock_ydl

    app, manager = _build_app(tmp_path)
    try:
        client = app.test_client()
        with patch("yt_dlp.YoutubeDL", side_effect=fake_ydl_factory):
            resp = client.post(
                "/api/jobs",
                json={
                    "url": "https://www.youtube.com/playlist?list=PL999",
                    "format": "mp3",
                    "quality": "320",
                    "dest_dir": str(dest_dir),
                    "selected_tracks": [1, 3],
                },
                headers={"X-CSRF-Token": "test-csrf-token"},
            )
            assert resp.status_code == 201
            job_id = resp.get_json()["job_id"]

            deadline = time.monotonic() + 5.0
            job_data: dict[str, Any] = {}
            while time.monotonic() < deadline:
                job_data = client.get(f"/api/jobs/{job_id}").get_json()
                if job_data["status"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.02)

        assert job_data["status"] == "completed"
        assert len(captured_opts) == 1
        assert captured_opts[0].get("playlist_items") == "1,3"
        assert len(job_data["promoted_files"]) == 2
        assert any("01 - Track One.mp3" in p for p in job_data["promoted_files"])
        assert any("03 - Track Three.mp3" in p for p in job_data["promoted_files"])
    finally:
        manager.shutdown()


# ---------------------------------------------------------------------------
# 4. ffmpeg detection via shutil.which("ffmpeg") & /api/health
# ---------------------------------------------------------------------------


def test_mod10_ffmpeg_detection_reflects_shutil_which_in_preflight_and_health(tmp_path: Path) -> None:
    app, manager = _build_app(tmp_path, preflight=SystemPreflightAdapter())
    try:
        client = app.test_client()

        with patch.object(shutil, "which", side_effect=lambda name: None):
            check = SystemPreflightAdapter().check_ffmpeg()
            assert check["available"] is False
            assert check["ffmpeg"] is False

            health_missing = client.get("/api/health")
            assert health_missing.status_code == 503
            missing_json = health_missing.get_json()
            assert missing_json["ffmpeg_available"] is False

        with patch.object(shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"):
            check_ok = SystemPreflightAdapter().check_ffmpeg()
            assert check_ok["available"] is True
            assert check_ok["ffmpeg"] is True

            health_ok = client.get("/api/health")
            assert health_ok.status_code == 200
            ok_json = health_ok.get_json()
            assert ok_json["ffmpeg_available"] is True
    finally:
        manager.shutdown()


def test_health_endpoint_reports_partial_and_full_ffmpeg_ffprobe_availability(tmp_path: Path) -> None:
    """Verify /api/health reports detailed status for present, partial, and missing ffmpeg/ffprobe."""
    app, manager = _build_app(tmp_path, preflight=SystemPreflightAdapter())
    try:
        client = app.test_client()

        scenarios = [
            (
                {"ffmpeg": "/usr/bin/ffmpeg", "ffprobe": "/usr/bin/ffprobe"},
                200,
                "ok",
                True,
                True,
                True,
            ),
            (
                {"ffmpeg": "/usr/bin/ffmpeg", "ffprobe": None},
                503,
                "degraded",
                False,
                True,
                False,
            ),
            (
                {"ffmpeg": None, "ffprobe": "/usr/bin/ffprobe"},
                503,
                "degraded",
                False,
                False,
                True,
            ),
            (
                {"ffmpeg": None, "ffprobe": None},
                503,
                "degraded",
                False,
                False,
                False,
            ),
        ]

        for mapping, expected_status, expected_health, expected_avail, exp_ffmpeg, exp_ffprobe in scenarios:
            with patch.object(shutil, "which", side_effect=lambda name, m=mapping: m.get(name)):
                resp = client.get("/api/health")
                assert resp.status_code == expected_status
                payload = resp.get_json()
                assert payload["status"] == expected_health
                assert payload["ffmpeg_available"] is expected_avail
                details = payload["details"]
                assert details["ffmpeg"] is exp_ffmpeg
                assert details["ffprobe"] is exp_ffprobe
                assert details["ffmpeg_path"] == mapping["ffmpeg"]
                assert details["ffprobe_path"] == mapping["ffprobe"]
    finally:
        manager.shutdown()


def test_frontend_checks_api_health_and_renders_ffmpeg_warning(tmp_path: Path) -> None:
    """Verify the web UI includes /api/health startup check and ffmpeg warning banner."""
    app, manager = _build_app(tmp_path)
    try:
        client = app.test_client()
        index_resp = client.get("/")
        assert index_resp.status_code == 200
        html = index_resp.get_data(as_text=True)
        assert 'id="ffmpeg-warning"' in html
        assert 'id="ffmpeg-warning-text"' in html

        js_resp = client.get("/static/app.js")
        assert js_resp.status_code == 200
        js = js_resp.get_data(as_text=True)
        assert "fetch('/api/health')" in js
        assert "checkBackendHealth" in js
        assert "updateFfmpegWarning" in js
    finally:
        manager.shutdown()


def test_health_endpoint_reports_ytdlp_present_and_absent(tmp_path: Path) -> None:
    """Verify /api/health reports yt-dlp availability and version when present or missing."""
    app, manager = _build_app(tmp_path, preflight=SystemPreflightAdapter())
    try:
        client = app.test_client()
        dest_dir = tmp_path / "downloads"
        dest_dir.mkdir()

        fake_yt_dlp = types.SimpleNamespace(
            version=types.SimpleNamespace(__version__="2025.10.22")
        )
        with (
            patch.object(shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch.dict(sys.modules, {"yt_dlp": fake_yt_dlp}),
        ):
            resp_ok = client.get(f"/api/health?dest_dir={dest_dir}")
            assert resp_ok.status_code == 200
            ok_json = resp_ok.get_json()
            assert ok_json["status"] == "ok"
            assert ok_json["yt_dlp_available"] is True
            assert ok_json["yt_dlp_version"] == "2025.10.22"
            assert ok_json["details"]["yt_dlp"]["available"] is True
            assert ok_json["details"]["yt_dlp"]["version"] == "2025.10.22"
            assert ok_json["details"]["yt_dlp"]["error"] is None

        with (
            patch.object(shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch.dict(sys.modules, {"yt_dlp": None}),
        ):
            resp_missing = client.get(f"/api/health?dest_dir={dest_dir}")
            assert resp_missing.status_code == 503
            missing_json = resp_missing.get_json()
            assert missing_json["status"] == "degraded"
            assert missing_json["yt_dlp_available"] is False
            assert missing_json["yt_dlp_version"] is None
            assert missing_json["details"]["yt_dlp"]["available"] is False
            assert missing_json["details"]["yt_dlp"]["error"]
    finally:
        manager.shutdown()


def test_health_endpoint_checks_download_dir_write_permissions(tmp_path: Path) -> None:
    """Verify /api/health checks write permissions on the download directory."""
    app, manager = _build_app(tmp_path, preflight=SystemPreflightAdapter())
    try:
        client = app.test_client()
        dest_dir = tmp_path / "music"
        dest_dir.mkdir()
        fake_yt_dlp = types.SimpleNamespace(
            version=types.SimpleNamespace(__version__="2025.10.22")
        )

        with (
            patch.object(shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch.dict(sys.modules, {"yt_dlp": fake_yt_dlp}),
        ):
            resp_ok = client.get(f"/api/health?dest_dir={dest_dir}")
            assert resp_ok.status_code == 200
            ok_json = resp_ok.get_json()
            assert ok_json["status"] == "ok"
            assert ok_json["download_dir_writable"] is True
            assert ok_json["download_dir"] == str(dest_dir.resolve())
            assert ok_json["details"]["download_dir"]["writable"] is True
            assert ok_json["details"]["download_dir"]["error"] is None

        with (
            patch.object(shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch.dict(sys.modules, {"yt_dlp": fake_yt_dlp}),
            patch.object(
                LocalFileSystemAdapter,
                "ensure_writable",
                side_effect=PermissionError("Permission denied"),
            ),
        ):
            resp_denied = client.get(f"/api/health?dest_dir={dest_dir}")
            assert resp_denied.status_code == 503
            denied_json = resp_denied.get_json()
            assert denied_json["status"] == "degraded"
            assert denied_json["download_dir_writable"] is False
            assert denied_json["details"]["download_dir"]["writable"] is False
            assert "Permission denied" in denied_json["details"]["download_dir"]["error"]
    finally:
        manager.shutdown()


def test_frontend_health_remediation_and_blocks_ffmpeg_options(tmp_path: Path) -> None:
    """Verify the web UI renders remediation steps and disables MP3 options when ffmpeg is missing."""
    app, manager = _build_app(tmp_path)
    try:
        client = app.test_client()
        html = client.get("/").get_data(as_text=True)
        assert 'id="health-remediation-steps"' in html
        assert 'role="alert"' in html

        js = client.get("/static/app.js").get_data(as_text=True)
        assert "syncFfmpegFormatOptions" in js
        assert "opt.disabled = !ffmpegAvailable" in js
        assert "yt_dlp_available" in js
        assert "download_dir_writable" in js
        assert "healthRemediationSteps" in js
    finally:
        manager.shutdown()
