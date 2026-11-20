"""Acceptance tests for MOD-09 / MOD-1: UI redesign, /api/preview, quality selection, cancellation, and history.

All tests run completely offline using Flask's test_client(), tmp_path, and mocked yt-dlp.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
import sys
import threading
import time

import pytest

_SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from yt_music_dl.adapters.fs_adapter import LocalFileSystemAdapter
from yt_music_dl.adapters.ytdlp_adapter import YtDlpAdapter
from yt_music_dl.app import create_app
from yt_music_dl.event_bus import InMemoryEventBus
from yt_music_dl.job_manager import JobManager
from yt_music_dl.models import DownloadManifest, DownloadRequest, ProgressSnapshot, TrackResult


class _FakePreflight:
    def __init__(self, available: bool = True) -> None:
        self._available = available

    def check_ffmpeg(self) -> dict[str, Any]:
        return {
            "available": self._available,
            "ffmpeg": self._available,
            "ffprobe": self._available,
        }


def _build_test_app(tmp_path: Path, downloader: Any = None) -> tuple[Any, JobManager]:
    fs = LocalFileSystemAdapter()
    event_bus = InMemoryEventBus()
    staging_root = tmp_path / "staging"
    if downloader is None:
        downloader = YtDlpAdapter()
    manager = JobManager(
        downloader=downloader,
        fs=fs,
        event_bus=event_bus,
        staging_root=staging_root,
    )
    app = create_app(
        job_manager=manager,
        event_bus=event_bus,
        fs=fs,
        preflight=_FakePreflight(True),
        csrf_token="test-csrf-token",
    )
    app.config["TESTING"] = True
    return app, manager


# ---------------------------------------------------------------------------
# 1. Offline UI & Static Assets (HTTP 200) + Modern UI Elements
# ---------------------------------------------------------------------------


def test_mod_ui_and_static_assets_return_200_offline(tmp_path: Path) -> None:
    app, manager = _build_test_app(tmp_path)
    try:
        client = app.test_client()

        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        lower_html = html.lower()

        assert "yt-music-dl" in lower_html
        assert "abrir carpeta" in lower_html
        assert "cancelar" in lower_html
        assert "historial" in lower_html
        assert "320" in html
        assert "192" in html
        assert "m4a" in lower_html

        for asset_path in ("/static/styles.css", "/static/app.js", "/static/favicon.svg", "/favicon.ico"):
            asset_resp = client.get(asset_path)
            assert asset_resp.status_code == 200, f"{asset_path} returned {asset_resp.status_code}"
            assert len(asset_resp.data) > 0

        js_text = client.get("/static/app.js").get_data(as_text=True)
        assert "/api/preview" in js_text
        assert "<audio" in js_text.lower() or "createelement('audio')" in js_text.lower() or 'createelement("audio")' in js_text.lower()
    finally:
        manager.shutdown()


# ---------------------------------------------------------------------------
# 2. POST /api/preview (Mocked yt-dlp, no actual download)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://music.youtube.com/watch?v=abc123XYZ",
    ],
)
def test_mod_preview_single_track_returns_metadata_without_downloading(
    tmp_path: Path, url: str
) -> None:
    app, manager = _build_test_app(tmp_path)
    try:
        client = app.test_client()
        mock_ydl = MagicMock()
        mock_ydl.__enter__.return_value = mock_ydl
        mock_ydl.__exit__.return_value = False
        mock_ydl.extract_info.return_value = {
            "id": "dQw4w9WgXcQ",
            "title": "Sample Track",
            "uploader": "Sample Artist",
            "channel": "Sample Artist",
            "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
        }

        with patch("yt_dlp.YoutubeDL", return_value=mock_ydl):
            resp = client.post(
                "/api/preview",
                json={"url": url},
                headers={"X-CSRF-Token": "test-csrf-token"},
            )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["title"] == "Sample Track"
        assert (data.get("author") or data.get("channel") or data.get("uploader")) == "Sample Artist"
        assert (data.get("thumbnail") or data.get("thumbnail_url")) == "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"
        assert data.get("track_count") == 1
        assert mock_ydl.extract_info.call_count == 1
        _, kwargs = mock_ydl.extract_info.call_args
        assert kwargs.get("download") is False
    finally:
        manager.shutdown()


def test_mod_preview_playlist_returns_track_count_and_metadata(tmp_path: Path) -> None:
    app, manager = _build_test_app(tmp_path)
    try:
        client = app.test_client()
        mock_ydl = MagicMock()
        mock_ydl.__enter__.return_value = mock_ydl
        mock_ydl.__exit__.return_value = False
        mock_ydl.extract_info.return_value = {
            "_type": "playlist",
            "id": "PL1234567890",
            "title": "Chill Vibes Mix",
            "uploader": "Curator Channel",
            "thumbnail": "https://i.ytimg.com/vi/t1/hqdefault.jpg",
            "entries": [
                {"id": "t1", "title": "Track 1"},
                {"id": "t2", "title": "Track 2"},
                {"id": "t3", "title": "Track 3"},
            ],
        }

        with patch("yt_dlp.YoutubeDL", return_value=mock_ydl):
            resp = client.post(
                "/api/preview",
                json={"url": "https://www.youtube.com/playlist?list=PL1234567890"},
                headers={"X-CSRF-Token": "test-csrf-token"},
            )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["title"] == "Chill Vibes Mix"
        assert (data.get("author") or data.get("channel") or data.get("uploader")) == "Curator Channel"
        assert data.get("track_count") == 3
        assert data.get("is_playlist") is True
        _, kwargs = mock_ydl.extract_info.call_args
        assert kwargs.get("download") is False
    finally:
        manager.shutdown()


def test_mod_preview_rejects_invalid_url_and_missing_csrf(tmp_path: Path) -> None:
    app, manager = _build_test_app(tmp_path)
    try:
        client = app.test_client()

        no_csrf = client.post("/api/preview", json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"})
        assert no_csrf.status_code == 403

        bad_url = client.post(
            "/api/preview",
            json={"url": "https://vimeo.com/123456"},
            headers={"X-CSRF-Token": "test-csrf-token"},
        )
        assert bad_url.status_code == 400
    finally:
        manager.shutdown()


# ---------------------------------------------------------------------------
# 3. Quality Selection & History / Promoted Audio Files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("audio_format", "quality"),
    [
        ("mp3", "320"),
        ("mp3", "192"),
        ("m4a", "best"),
    ],
)
def test_mod_create_job_with_selected_quality_and_history_tracking(
    tmp_path: Path, audio_format: str, quality: str
) -> None:
    dest_dir = tmp_path / "music"
    dest_dir.mkdir()

    captured_requests: list[DownloadRequest] = []

    class _MockDownloader:
        def download(
            self,
            request: DownloadRequest,
            staging_dir: Path,
            archive_path: Path,
            on_progress: Any,
            is_cancelled: Any,
        ) -> DownloadManifest:
            captured_requests.append(request)
            ext = request.audio_format
            out_file = staging_dir / f"01 - Test Song.{ext}"
            out_file.write_bytes(b"ID3-audio-payload")
            on_progress(
                ProgressSnapshot(
                    job_id="",
                    status="downloading",
                    progress=50.0,
                    current_track="Test Song",
                    thumbnail_url="https://i.ytimg.com/vi/abc/hqdefault.jpg",
                    message="Canción 1 de 1",
                )
            )
            return DownloadManifest(
                downloaded=(out_file.name,),
                tracks=(TrackResult(track_id="abc", title="Test Song", status="downloaded"),),
            )

    app, manager = _build_test_app(tmp_path, downloader=_MockDownloader())
    try:
        client = app.test_client()
        resp = client.post(
            "/api/jobs",
            json={
                "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "format": audio_format,
                "quality": quality,
                "dest_dir": str(dest_dir),
            },
            headers={"X-CSRF-Token": "test-csrf-token"},
        )
        assert resp.status_code == 201
        job_id = resp.get_json()["job_id"]

        deadline = time.monotonic() + 5.0
        job_data: dict[str, Any] = {}
        while time.monotonic() < deadline:
            job_resp = client.get(f"/api/jobs/{job_id}")
            assert job_resp.status_code == 200
            job_data = job_resp.get_json()
            if job_data["status"] == "completed":
                break
            time.sleep(0.02)

        assert job_data["status"] == "completed"
        assert len(captured_requests) == 1
        assert captured_requests[0].audio_format == audio_format
        expected_file = dest_dir / f"01 - Test Song.{audio_format}"
        assert expected_file.exists()
        assert str(expected_file) in job_data["promoted_files"]
    finally:
        manager.shutdown()


# ---------------------------------------------------------------------------
# 4. Real Cancellation via HTTP Endpoint
# ---------------------------------------------------------------------------


def test_mod_cancel_active_job_via_http_endpoint(tmp_path: Path) -> None:
    dest_dir = tmp_path / "music"
    dest_dir.mkdir()
    started = threading.Event()

    class _BlockingDownloader:
        def download(
            self,
            request: DownloadRequest,
            staging_dir: Path,
            archive_path: Path,
            on_progress: Any,
            is_cancelled: Any,
        ) -> DownloadManifest:
            from yt_music_dl.adapters.ytdlp_adapter import CancelledDownloadError

            started.set()
            for _ in range(200):
                if is_cancelled():
                    raise CancelledDownloadError("Cancelled by HTTP request")
                time.sleep(0.01)
            return DownloadManifest()

    app, manager = _build_test_app(tmp_path, downloader=_BlockingDownloader())
    try:
        client = app.test_client()
        resp = client.post(
            "/api/jobs",
            json={
                "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "format": "mp3",
                "dest_dir": str(dest_dir),
            },
            headers={"X-CSRF-Token": "test-csrf-token"},
        )
        assert resp.status_code == 201
        job_id = resp.get_json()["job_id"]
        assert started.wait(timeout=3.0)

        cancel_resp = client.post(
            f"/api/jobs/{job_id}/cancel",
            headers={"X-CSRF-Token": "test-csrf-token"},
        )
        assert cancel_resp.status_code == 200

        deadline = time.monotonic() + 5.0
        status = ""
        while time.monotonic() < deadline:
            status = client.get(f"/api/jobs/{job_id}").get_json()["status"]
            if status == "cancelled":
                break
            time.sleep(0.02)

        assert status == "cancelled"
    finally:
        manager.shutdown()
