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
