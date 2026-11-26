"""Tests for MOD-12: playlist MP3 options, live 'Canción X de Y' progress, track download, and responsive UI.

All tests run completely offline using Flask's test_client(), tmp_path, and mocks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
import io
import sys
import time
import zipfile

_SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from yt_music_dl.adapters.fs_adapter import LocalFileSystemAdapter
from yt_music_dl.adapters.ytdlp_adapter import SystemPreflightAdapter, YtDlpAdapter
from yt_music_dl.app import create_app
from yt_music_dl.event_bus import InMemoryEventBus
from yt_music_dl.job_manager import JobManager
from yt_music_dl.models import (
    DownloadManifest,
    DownloadRequest,
    ProgressSnapshot,
    TrackResult,
)


def _build_test_app(
    tmp_path: Path,
    *,
    downloader: Any = None,
) -> tuple[Any, JobManager]:
    fs = LocalFileSystemAdapter()
    event_bus = InMemoryEventBus()
    manager = JobManager(
        downloader=downloader or YtDlpAdapter(),
        fs=fs,
        event_bus=event_bus,
        staging_root=tmp_path / "staging",
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


def test_mod12_ytdlp_progress_reports_track_index_and_title(tmp_path: Path) -> None:
    captured_opts: dict[str, Any] = {}
    snapshots: list[ProgressSnapshot] = []

    def fake_extract_info(url: str, download: bool = True) -> dict[str, Any]:
        hook = captured_opts["progress_hooks"][0]
        hook(
            {
                "status": "downloading",
                "downloaded_bytes": 50,
                "total_bytes": 100,
                "info_dict": {
                    "title": "Track One",
                    "playlist_index": 1,
                    "playlist_count": 3,
                    "thumbnail": "https://i.ytimg.com/vi/abc/hqdefault.jpg",
                },
            }
        )
        hook(
            {
                "status": "finished",
                "info_dict": {
                    "title": "Track One",
                    "playlist_index": 1,
                    "playlist_count": 3,
                },
            }
        )
        return {
            "_type": "playlist",
            "id": "PL123",
            "title": "My Playlist",
            "entries": [{"id": "abc", "title": "Track One"}],
        }

    mock_ydl = MagicMock()
    mock_ydl.__enter__.return_value = mock_ydl
    mock_ydl.__exit__.return_value = False
    mock_ydl.extract_info.side_effect = fake_extract_info

    def fake_factory(opts: dict[str, Any]) -> Any:
        captured_opts.update(opts)
        return mock_ydl

    adapter = YtDlpAdapter(ydl_factory=fake_factory)
    archive_file = tmp_path / "archive.txt"
    req = DownloadRequest(
        url="https://www.youtube.com/playlist?list=PL123",
        dest_dir=tmp_path / "downloads",
        audio_format="mp3",
        is_playlist=True,
        quality="320",
    )

    manifest = adapter.download(
        request=req,
        staging_dir=tmp_path / "staging",
        archive_path=archive_file,
        on_progress=snapshots.append,
        is_cancelled=lambda: False,
    )

    assert manifest.downloaded == ("Track One",)
    assert captured_opts["download_archive"] == str(archive_file)
    assert "%(playlist_title)s" in captured_opts["outtmpl"]
    assert captured_opts["postprocessors"][0]["preferredcodec"] == "mp3"
    assert captured_opts["postprocessors"][0]["preferredquality"] == "320"
    assert captured_opts["postprocessors"][2] == {"key": "FFmpegMetadata", "add_metadata": True}
    assert captured_opts["postprocessors"][3] == {"key": "EmbedThumbnail"}

    assert len(snapshots) == 2
    assert "Canción 1 de 3" in snapshots[0].message
    assert "Track One" in snapshots[0].message
    assert snapshots[0].current_track == "Track One"
    assert "Canción 1 de 3" in snapshots[1].message


def test_mod12_individual_track_audio_download_endpoint(tmp_path: Path) -> None:
    dest_dir = tmp_path / "music"
    dest_dir.mkdir()

    class _MockDownloader:
        def download(
            self,
            request: DownloadRequest,
            staging_dir: Path,
            archive_path: Path,
            on_progress: Any,
            is_cancelled: Any,
        ) -> DownloadManifest:
            out_file = staging_dir / "01 - Track One.mp3"
            out_file.write_bytes(b"ID3-audio-payload")
            return DownloadManifest(
                downloaded=(out_file.name,),
                tracks=(TrackResult(track_id="abc", title="Track One", status="downloaded"),),
            )

    app, manager = _build_test_app(tmp_path, downloader=_MockDownloader())
    try:
        client = app.test_client()
        resp = client.post(
            "/api/jobs",
            json={
                "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "format": "mp3",
                "quality": "192",
                "dest_dir": str(dest_dir),
            },
            headers={"X-CSRF-Token": "test-csrf-token"},
        )
        assert resp.status_code == 201
        job_id = resp.get_json()["job_id"]

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            job_resp = client.get(f"/api/jobs/{job_id}")
            assert job_resp.status_code == 200
            if job_resp.get_json()["status"] == "completed":
                break
            time.sleep(0.02)

        audio_resp = client.get(f"/api/jobs/{job_id}/audio/0")
        assert audio_resp.status_code == 200
        assert audio_resp.data == b"ID3-audio-payload"
        audio_resp.close()

        missing_resp = client.get(f"/api/jobs/{job_id}/audio/99")
        assert missing_resp.status_code == 404
    finally:
        manager.shutdown()


def test_mod12_responsive_mobile_css_and_viewport(tmp_path: Path) -> None:
    app, manager = _build_test_app(tmp_path)
    try:
        client = app.test_client()
        index_resp = client.get("/")
        assert index_resp.status_code == 200
        html = index_resp.get_data(as_text=True)
        assert 'name="viewport"' in html

        css_resp = client.get("/static/styles.css")
        assert css_resp.status_code == 200
        css = css_resp.get_data(as_text=True)
        assert "@media (max-width: 640px)" in css or "@media (max-width: 680px)" in css
    finally:
        manager.shutdown()


def test_mod12_archive_skips_existing_tracks_and_uses_192k_mp3(tmp_path: Path) -> None:
    captured_opts: dict[str, Any] = {}
    archive_file = tmp_path / "archive.txt"
    archive_file.write_text("youtube old123\n", encoding="utf-8")

    mock_ydl = MagicMock()
    mock_ydl.__enter__.return_value = mock_ydl
    mock_ydl.__exit__.return_value = False
    mock_ydl.extract_info.return_value = {
        "_type": "playlist",
        "id": "PL999",
        "title": "Workout Mix",
        "entries": [
            {"id": "old123", "title": "Already Downloaded"},
            {"id": "new456", "title": "Brand New Track"},
        ],
    }

    def fake_factory(opts: dict[str, Any]) -> Any:
        captured_opts.update(opts)
        return mock_ydl

    adapter = YtDlpAdapter(ydl_factory=fake_factory)
    req = DownloadRequest(
        url="https://music.youtube.com/playlist?list=PL999",
        dest_dir=tmp_path / "downloads",
        audio_format="mp3",
        is_playlist=True,
        quality="192",
    )

    manifest = adapter.download(
        request=req,
        staging_dir=tmp_path / "staging",
        archive_path=archive_file,
        on_progress=lambda _snap: None,
        is_cancelled=lambda: False,
    )

    assert captured_opts["download_archive"] == str(archive_file)
    assert captured_opts["postprocessors"][0]["preferredcodec"] == "mp3"
    assert captured_opts["postprocessors"][0]["preferredquality"] == "192"
    assert manifest.skipped_archive == ("old123",)
    assert manifest.downloaded == ("Brand New Track",)
    assert [t.status for t in manifest.tracks] == ["skipped", "downloaded"]


def test_mod12_playlist_zip_download_and_empty_or_missing_404(tmp_path: Path) -> None:
    dest_dir = tmp_path / "downloads"
    dest_dir.mkdir()

    class _PlaylistDownloader:
        def download(
            self,
            request: DownloadRequest,
            staging_dir: Path,
            archive_path: Path,
            on_progress: Any,
            is_cancelled: Any,
        ) -> DownloadManifest:
            if "empty" in request.url:
                return DownloadManifest(skipped_archive=("old1",))
            playlist_dir = staging_dir / "Mi Playlist"
            playlist_dir.mkdir(parents=True, exist_ok=True)
            (playlist_dir / "01 - Track One.mp3").write_bytes(b"mp3-one")
            (playlist_dir / "02 - Track Two.mp3").write_bytes(b"mp3-two")
            return DownloadManifest(
                downloaded=("Track One", "Track Two"),
                tracks=(
                    TrackResult(track_id="t1", title="Track One", status="downloaded"),
                    TrackResult(track_id="t2", title="Track Two", status="downloaded"),
                ),
            )

    app, manager = _build_test_app(tmp_path, downloader=_PlaylistDownloader())
    try:
        client = app.test_client()
        resp = client.post(
            "/api/jobs",
            json={
                "url": "https://www.youtube.com/playlist?list=PL_ZIP_1",
                "format": "mp3",
                "quality": "320",
                "dest_dir": str(dest_dir),
            },
            headers={"X-CSRF-Token": "test-csrf-token"},
        )
        assert resp.status_code == 201
        job_id = resp.get_json()["job_id"]

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            job_resp = client.get(f"/api/jobs/{job_id}")
            assert job_resp.status_code == 200
            if job_resp.get_json()["status"] == "completed":
                break
            time.sleep(0.02)

        zip_resp = client.get(f"/api/jobs/{job_id}/zip")
        assert zip_resp.status_code == 200
        assert zip_resp.mimetype == "application/zip"
        disposition = zip_resp.headers.get("Content-Disposition", "")
        assert "attachment" in disposition
        assert "Mi Playlist.zip" in disposition

        with zipfile.ZipFile(io.BytesIO(zip_resp.data), mode="r") as zf:
            names = {name.replace("\\", "/") for name in zf.namelist()}
            assert names == {
                "Mi Playlist/01 - Track One.mp3",
                "Mi Playlist/02 - Track Two.mp3",
            }
            assert zf.read(next(n for n in zf.namelist() if "01 - Track One.mp3" in n)) == b"mp3-one"
            assert zf.read(next(n for n in zf.namelist() if "02 - Track Two.mp3" in n)) == b"mp3-two"
        zip_resp.close()

        assert client.get("/api/jobs/nonexistent/zip").status_code == 404

        empty_resp = client.post(
            "/api/jobs",
            json={
                "url": "https://www.youtube.com/playlist?list=PL_empty",
                "format": "mp3",
                "quality": "192",
                "dest_dir": str(dest_dir),
            },
            headers={"X-CSRF-Token": "test-csrf-token"},
        )
        assert empty_resp.status_code == 201
        empty_job_id = empty_resp.get_json()["job_id"]
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if client.get(f"/api/jobs/{empty_job_id}").get_json()["status"] == "completed":
                break
            time.sleep(0.02)
        assert client.get(f"/api/jobs/{empty_job_id}/zip").status_code == 404
    finally:
        manager.shutdown()


def test_mod12_zip_button_in_ui_and_track_attachment_download(tmp_path: Path) -> None:
    dest_dir = tmp_path / "music"
    dest_dir.mkdir()

    class _SingleDownloader:
        def download(
            self,
            request: DownloadRequest,
            staging_dir: Path,
            archive_path: Path,
            on_progress: Any,
            is_cancelled: Any,
        ) -> DownloadManifest:
            out_file = staging_dir / "01 - Mobile Track.mp3"
            out_file.write_bytes(b"mobile-mp3")
            return DownloadManifest(
                downloaded=(out_file.name,),
                tracks=(TrackResult(track_id="m1", title="Mobile Track", status="downloaded"),),
            )

    app, manager = _build_test_app(tmp_path, downloader=_SingleDownloader())
    try:
        client = app.test_client()
        html = client.get("/").get_data(as_text=True)
        assert 'id="download-zip-btn"' in html
        assert "Descargar ZIP para celular" in html

        resp = client.post(
            "/api/jobs",
            json={
                "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "format": "mp3",
                "quality": "192",
                "dest_dir": str(dest_dir),
            },
            headers={"X-CSRF-Token": "test-csrf-token"},
        )
        assert resp.status_code == 201
        job_id = resp.get_json()["job_id"]

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if client.get(f"/api/jobs/{job_id}").get_json()["status"] == "completed":
                break
            time.sleep(0.02)

        dl_resp = client.get(f"/api/jobs/{job_id}/audio/0?download=1")
        assert dl_resp.status_code == 200
        assert "attachment" in dl_resp.headers.get("Content-Disposition", "")
        assert "01 - Mobile Track.mp3" in dl_resp.headers.get("Content-Disposition", "")
        assert dl_resp.data == b"mobile-mp3"
        dl_resp.close()
    finally:
        manager.shutdown()
