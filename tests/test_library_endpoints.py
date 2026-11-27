"""Tests for read-only library listing, HTTP Range audio streaming, and path traversal protection."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import sys

import pytest

_SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from yt_music_dl.adapters.fs_adapter import LocalFileSystemAdapter
from yt_music_dl.adapters.ytdlp_adapter import YtDlpAdapter
from yt_music_dl.app import create_app
from yt_music_dl.event_bus import InMemoryEventBus
from yt_music_dl.job_manager import JobManager


class _FakePreflight:
    def check_ffmpeg(self) -> dict[str, Any]:
        return {"available": True, "ffmpeg": True, "ffprobe": True}

    def check_ytdlp(self) -> dict[str, Any]:
        return {"available": True, "version": "2026.01.01", "path": "yt-dlp"}


class _ScopedFSAdapter(LocalFileSystemAdapter):
    """LocalFileSystemAdapter scoped to a temporary default music directory."""

    def __init__(self, music_dir: Path) -> None:
        self._music_dir = music_dir

    def default_music_dir(self) -> Path:
        return self._music_dir


def _build_library_app(tmp_path: Path) -> tuple[Any, JobManager, Path]:
    music_dir = tmp_path / "downloads"
    music_dir.mkdir(parents=True, exist_ok=True)
    fs = _ScopedFSAdapter(music_dir)
    event_bus = InMemoryEventBus()
    manager = JobManager(
        downloader=YtDlpAdapter(),
        fs=fs,
        event_bus=event_bus,
        staging_root=tmp_path / "staging",
    )
    app = create_app(
        job_manager=manager,
        event_bus=event_bus,
        fs=fs,
        preflight=_FakePreflight(),
        csrf_token="library-test-csrf",
    )
    app.config["TESTING"] = True
    return app, manager, music_dir


def _extract_library_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("tracks", "files", "items", "library"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    raise AssertionError(f"Unexpected library response payload: {payload!r}")


def test_library_lists_audio_files_and_ignores_non_audio(tmp_path: Path) -> None:
    app, manager, music_dir = _build_library_app(tmp_path)
    try:
        (music_dir / "01 - Opening.mp3").write_bytes(b"ID3" + b"a" * 64)
        playlist_dir = music_dir / "Chill Mix"
        playlist_dir.mkdir()
        (playlist_dir / "02 - Sunset.m4a").write_bytes(b"ftypM4A" + b"b" * 64)
        (music_dir / "archive.txt").write_text("youtube abc123\n", encoding="utf-8")
        (music_dir / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0")
        (playlist_dir / "notes.json").write_text("{}", encoding="utf-8")

        client = app.test_client()
        resp = client.get("/api/library")
        assert resp.status_code == 200

        items = _extract_library_items(resp.get_json())
        names = {
            item.get("name") or Path(item.get("path", item.get("relative_path", ""))).name
            for item in items
        }
        assert "01 - Opening.mp3" in names
        assert "02 - Sunset.m4a" in names
        assert "archive.txt" not in names
        assert "cover.jpg" not in names
        assert "notes.json" not in names
    finally:
        manager.shutdown()


def test_library_empty_directory_returns_empty_list(tmp_path: Path) -> None:
    app, manager, _ = _build_library_app(tmp_path)
    try:
        client = app.test_client()
        resp = client.get("/api/library")
        assert resp.status_code == 200
        items = _extract_library_items(resp.get_json())
        assert items == []
    finally:
        manager.shutdown()


def test_library_audio_supports_http_range_partial_streaming(tmp_path: Path) -> None:
    app, manager, music_dir = _build_library_app(tmp_path)
    try:
        payload_bytes = bytes(range(100))
        track_dir = music_dir / "Album"
        track_dir.mkdir()
        track_file = track_dir / "01 - Range Test.mp3"
        track_file.write_bytes(payload_bytes)

        client = app.test_client()
        rel_path = "Album/01 - Range Test.mp3"

        full_resp = client.get("/api/library/audio", query_string={"path": rel_path})
        assert full_resp.status_code == 200
        assert full_resp.data == payload_bytes

        partial_resp = client.get(
            "/api/library/audio",
            query_string={"path": rel_path},
            headers={"Range": "bytes=10-24"},
        )
        assert partial_resp.status_code == 206
        assert partial_resp.data == payload_bytes[10:25]
        content_range = partial_resp.headers.get("Content-Range", "")
        assert content_range.startswith("bytes 10-24/100")
    finally:
        manager.shutdown()


@pytest.mark.parametrize(
    "malicious_path",
    [
        "../outside/secret.mp3",
        "..\\outside\\secret.mp3",
        "Album/../../outside/secret.mp3",
        "%2e%2e/outside/secret.mp3",
        "/etc/passwd",
        "C:\\Windows\\System32\\drivers\\etc\\hosts",
        "",
        "   ",
    ],
)
def test_library_audio_rejects_path_traversal_outside_allowed_dir(
    tmp_path: Path, malicious_path: str
) -> None:
    app, manager, _ = _build_library_app(tmp_path)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir(exist_ok=True)
    outside_file = outside_dir / "secret.mp3"
    outside_file.write_bytes(b"SECRET_AUDIO_DATA")

    try:
        client = app.test_client()
        for candidate in (malicious_path, str(outside_file)):
            resp = client.get("/api/library/audio", query_string={"path": candidate})
            assert resp.status_code in {400, 403, 404}
            assert b"SECRET_AUDIO_DATA" not in resp.data
    finally:
        manager.shutdown()


def test_library_audio_missing_file_returns_404(tmp_path: Path) -> None:
    app, manager, _ = _build_library_app(tmp_path)
    try:
        client = app.test_client()
        resp = client.get("/api/library/audio", query_string={"path": "nonexistent.mp3"})
        assert resp.status_code == 404
    finally:
        manager.shutdown()


def test_library_ui_includes_library_panel_and_html5_audio_player(tmp_path: Path) -> None:
    app, manager, _ = _build_library_app(tmp_path)
    try:
        client = app.test_client()
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "<audio" in html.lower()
        assert "biblioteca" in html.lower() or "library" in html.lower()
    finally:
        manager.shutdown()
