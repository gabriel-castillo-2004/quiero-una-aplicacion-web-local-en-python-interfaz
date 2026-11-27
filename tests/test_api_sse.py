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


def test_event_bus_thread_safe_concurrent_track_progress_and_sse_isolation(
    tmp_path: Path,
) -> None:
    """Concurrent progress emissions across multiple jobs remain thread-safe and isolated per SSE stream."""
    import json
    import threading
    import time
    from unittest.mock import MagicMock

    dest_dir = tmp_path / "music_concurrent"
    dest_dir.mkdir()

    def fake_ydl_factory(opts: dict[str, Any]) -> Any:
        mock_ydl = MagicMock()
        mock_ydl.__enter__.return_value = mock_ydl
        mock_ydl.__exit__.return_value = False

        def fake_extract_info(url: str, download: bool = False) -> dict[str, Any]:
            vid = url.split("=")[-1]
            hook = opts["progress_hooks"][0]
            for pct in (250, 750):
                hook(
                    {
                        "status": "downloading",
                        "downloaded_bytes": pct,
                        "total_bytes": 1000,
                        "info_dict": {"id": vid, "title": f"Track {vid}"},
                    }
                )
                time.sleep(0.01)
            hook(
                {
                    "status": "finished",
                    "info_dict": {"id": vid, "title": f"Track {vid}"},
                }
            )
            if download:
                out_parent = Path(opts["outtmpl"]).parent
                out_parent.mkdir(parents=True, exist_ok=True)
                (out_parent / f"Track {vid}.mp3").write_bytes(b"mp3-bytes")
            return {"id": vid, "title": f"Track {vid}"}

        mock_ydl.extract_info.side_effect = fake_extract_info
        return mock_ydl

    fs = LocalFileSystemAdapter()
    event_bus = InMemoryEventBus()
    manager = JobManager(
        downloader=YtDlpAdapter(ydl_factory=fake_ydl_factory),
        fs=fs,
        event_bus=event_bus,
        staging_root=tmp_path / "staging",
        max_workers=3,
    )
    app = create_app(
        job_manager=manager,
        event_bus=event_bus,
        fs=fs,
        preflight=_FakePreflight(True),
        csrf_token="test-csrf-token",
    )
    app.config["TESTING"] = True

    try:
        client = app.test_client()
        job_ids: list[str] = []
        for idx in range(3):
            resp = client.post(
                "/api/jobs",
                json={
                    "url": f"https://www.youtube.com/watch?v=concurrent{idx:02d}",
                    "format": "mp3",
                    "quality": "192",
                    "dest_dir": str(dest_dir),
                    "concurrency": 3,
                },
                headers={"X-CSRF-Token": "test-csrf-token"},
            )
            assert resp.status_code == 201
            job_ids.append(resp.get_json()["job_id"])

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            statuses = [
                client.get(f"/api/jobs/{jid}").get_json()["status"] for jid in job_ids
            ]
            if all(s == "completed" for s in statuses):
                break
            time.sleep(0.02)

        for jid in job_ids:
            job_payload = client.get(f"/api/jobs/{jid}").get_json()
            assert job_payload["status"] == "completed"

            sse_resp = client.get(f"/api/jobs/{jid}/events")
            assert sse_resp.status_code == 200
            body = sse_resp.get_data(as_text=True)
            assert f'"job_id": "{jid}"' in body
            assert "completed" in body

        # Also verify direct thread-safe publishing on InMemoryEventBus under contention
        errors: list[Exception] = []

        def _spam_publish(worker_idx: int) -> None:
            try:
                for step in range(30):
                    event_bus.publish(
                        f"stress-{worker_idx}",
                        "progress",
                        {"job_id": f"stress-{worker_idx}", "progress": float(step)},
                    )
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=_spam_publish, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=3.0)

        assert errors == []
        for i in range(4):
            snap, history, q = event_bus.subscribe_with_snapshot(f"stress-{i}")
            event_bus.unsubscribe(f"stress-{i}", q)
            assert snap is not None
            assert snap["job_id"] == f"stress-{i}"
            assert len(history) == 30
            assert json.loads(json.dumps(snap))["progress"] == 29.0
    finally:
        manager.shutdown()


def test_api_recovers_from_transient_rate_limit_and_reports_completion_via_sse(
    tmp_path: Path,
) -> None:
    """A job experiencing a transient 429 error recovers via retry and streams completion via SSE."""
    import time
    from unittest.mock import MagicMock

    dest_dir = tmp_path / "music_retry"
    dest_dir.mkdir()
    attempts = 0

    def fake_ydl_factory(opts: dict[str, Any]) -> Any:
        mock_ydl = MagicMock()
        mock_ydl.__enter__.return_value = mock_ydl
        mock_ydl.__exit__.return_value = False

        def fake_extract_info(url: str, download: bool = False) -> dict[str, Any]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("HTTP Error 429: Too Many Requests")
            if download:
                out_parent = Path(opts["outtmpl"]).parent
                out_parent.mkdir(parents=True, exist_ok=True)
                (out_parent / "Recovered.mp3").write_bytes(b"recovered-audio")
            return {"id": "rec1", "title": "Recovered"}

        mock_ydl.extract_info.side_effect = fake_extract_info
        return mock_ydl

    fs = LocalFileSystemAdapter()
    event_bus = InMemoryEventBus()
    manager = JobManager(
        downloader=YtDlpAdapter(ydl_factory=fake_ydl_factory),
        fs=fs,
        event_bus=event_bus,
        staging_root=tmp_path / "staging",
        max_workers=2,
    )
    app = create_app(
        job_manager=manager,
        event_bus=event_bus,
        fs=fs,
        preflight=_FakePreflight(True),
        csrf_token="test-csrf-token",
    )
    app.config["TESTING"] = True

    try:
        client = app.test_client()
        with patch("time.sleep"):
            resp = client.post(
                "/api/jobs",
                json={
                    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    "format": "mp3",
                    "quality": "192",
                    "dest_dir": str(dest_dir),
                    "concurrency": 2,
                },
                headers={"X-CSRF-Token": "test-csrf-token"},
            )
            assert resp.status_code == 201
            job_id = resp.get_json()["job_id"]

            deadline = time.monotonic() + 5.0
            status = "pending"
            while time.monotonic() < deadline:
                status = client.get(f"/api/jobs/{job_id}").get_json()["status"]
                if status in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.02)

        assert status == "completed"
        assert attempts == 2
        sse_resp = client.get(f"/api/jobs/{job_id}/events")
        assert sse_resp.status_code == 200
        assert "completed" in sse_resp.get_data(as_text=True)
    finally:
        manager.shutdown()

