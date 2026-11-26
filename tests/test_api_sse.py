"""API and cross-platform endpoint tests for yt_music_dl.

All tests run completely offline using Flask's test_client(), tmp_path, and mocks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch
import sys

_SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from yt_music_dl.adapters.fs_adapter import LocalFileSystemAdapter
from yt_music_dl.adapters.ytdlp_adapter import YtDlpAdapter
from yt_music_dl.app import create_app
from yt_music_dl.event_bus import InMemoryEventBus
from yt_music_dl.job_manager import JobManager


class _FakePreflight:
    def __init__(self, available: bool = True) -> None:
        self._available = available

    def check_ffmpeg(self) -> dict[str, Any]:
        return {
            "available": self._available,
            "ffmpeg": self._available,
            "ffprobe": self._available,
        }


def _build_test_app(tmp_path: Path) -> tuple[Any, JobManager]:
    fs = LocalFileSystemAdapter()
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
        preflight=_FakePreflight(True),
        csrf_token="test-csrf-token",
    )
    app.config["TESTING"] = True
    return app, manager


def test_open_folder_windows_uses_os_startfile(tmp_path: Path) -> None:
    app, manager = _build_test_app(tmp_path)
    dest_dir = tmp_path / "music_win"
    dest_dir.mkdir()
    try:
        client = app.test_client()
        with (
            patch("yt_music_dl.app.sys.platform", "win32"),
            patch("yt_music_dl.app.os.startfile", create=True) as mock_startfile,
            patch("yt_music_dl.app.subprocess.Popen") as mock_popen,
        ):
            resp = client.post(
                "/api/open-folder",
                json={"dest_dir": str(dest_dir)},
                headers={"X-CSRF-Token": "test-csrf-token"},
            )

        assert resp.status_code == 200
        assert resp.get_json() == {"opened": str(dest_dir)}
        mock_startfile.assert_called_once_with(str(dest_dir))
        mock_popen.assert_not_called()
    finally:
        manager.shutdown()


def test_open_folder_linux_uses_xdg_open(tmp_path: Path) -> None:
    app, manager = _build_test_app(tmp_path)
    dest_dir = tmp_path / "music_linux"
    dest_dir.mkdir()
    try:
        client = app.test_client()
        with (
            patch("yt_music_dl.app.sys.platform", "linux"),
            patch("yt_music_dl.app.os.startfile", create=True) as mock_startfile,
            patch("yt_music_dl.app.subprocess.Popen") as mock_popen,
        ):
            resp = client.post(
                "/api/open-folder",
                json={"dest_dir": str(dest_dir)},
                headers={"X-CSRF-Token": "test-csrf-token"},
            )

        assert resp.status_code == 200
        assert resp.get_json() == {"opened": str(dest_dir)}
        mock_popen.assert_called_once_with(["xdg-open", str(dest_dir)])
        mock_startfile.assert_not_called()
    finally:
        manager.shutdown()


def test_open_folder_macos_uses_open(tmp_path: Path) -> None:
    app, manager = _build_test_app(tmp_path)
    dest_dir = tmp_path / "music_macos"
    dest_dir.mkdir()
    try:
        client = app.test_client()
        with (
            patch("yt_music_dl.app.sys.platform", "darwin"),
            patch("yt_music_dl.app.os.startfile", create=True) as mock_startfile,
            patch("yt_music_dl.app.subprocess.Popen") as mock_popen,
        ):
            resp = client.post(
                "/api/open-folder",
                json={"dest_dir": str(dest_dir)},
                headers={"X-CSRF-Token": "test-csrf-token"},
            )

        assert resp.status_code == 200
        assert resp.get_json() == {"opened": str(dest_dir)}
        mock_popen.assert_called_once_with(["open", str(dest_dir)])
        mock_startfile.assert_not_called()
    finally:
        manager.shutdown()


def test_open_folder_headless_returns_clear_json_error(tmp_path: Path) -> None:
    app, manager = _build_test_app(tmp_path)
    dest_dir = tmp_path / "music_headless"
    dest_dir.mkdir()
    try:
        client = app.test_client()
        with (
            patch("yt_music_dl.app.sys.platform", "linux"),
            patch(
                "yt_music_dl.app.subprocess.Popen",
                side_effect=FileNotFoundError("xdg-open not found"),
            ),
        ):
            resp = client.post(
                "/api/open-folder",
                json={"dest_dir": str(dest_dir)},
                headers={"X-CSRF-Token": "test-csrf-token"},
            )

        assert resp.status_code == 500
        data = resp.get_json()
        assert isinstance(data, dict)
        assert "error" in data
        assert "No se pudo abrir la carpeta" in data["error"]
    finally:
        manager.shutdown()
