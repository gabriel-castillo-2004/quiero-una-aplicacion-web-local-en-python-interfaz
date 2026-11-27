"""Security tests for yt_music_dl input validation, path containment, and local binding."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch
import os
import sys

import pytest

_SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from yt_music_dl.adapters.fs_adapter import LocalFileSystemAdapter
from yt_music_dl.adapters.ytdlp_adapter import YtDlpAdapter
from yt_music_dl.app import create_app, main
from yt_music_dl.event_bus import InMemoryEventBus
from yt_music_dl.job_manager import JobManager
from yt_music_dl.validation import ValidationError, sanitize_filename, validate_youtube_url


class _FakePreflight:
    def check_ffmpeg(self) -> dict[str, Any]:
        return {"available": True, "ffmpeg": True, "ffprobe": True}


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
        preflight=_FakePreflight(),
        csrf_token="security-test-csrf",
    )
    app.config["TESTING"] = True
    return app, manager


@pytest.mark.parametrize(
    "valid_url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "http://youtube.com/watch?v=dQw4w9WgXcQ",
        "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://music.youtube.com/playlist?list=PL1234567890",
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/abc123XYZ",
    ],
)
def test_validate_youtube_url_accepts_valid_youtube_and_ytmusic_urls(valid_url: str) -> None:
    assert validate_youtube_url(valid_url) == valid_url


@pytest.mark.parametrize(
    "malicious_url",
    [
        "-o /etc/passwd",
        "--exec=id",
        "--postprocessor-args=-f",
        "-x https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "-https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "ftp://youtube.com/watch?v=dQw4w9WgXcQ",
        "data:text/html,<script>alert(1)</script>",
        "https://evil.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com.evil.com/watch?v=dQw4w9WgXcQ",
        "https://notyoutube.com/watch?v=dQw4w9WgXcQ",
        "https://vimeo.com/123456",
        "http://127.0.0.1:8081/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/",
        "https://youtu.be/",
        "",
        "   ",
    ],
)
def test_validate_youtube_url_rejects_malicious_and_disallowed_urls(malicious_url: str) -> None:
    with pytest.raises(ValidationError):
        validate_youtube_url(malicious_url)


@pytest.mark.parametrize(
    "malicious_url",
    [
        "--exec=whoami",
        "-o /tmp/pwned",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://evil.com/watch?v=123",
        "https://youtube.com.attacker.tld/watch?v=123",
    ],
)
def test_api_endpoints_reject_malicious_urls_with_400(
    tmp_path: Path, malicious_url: str
) -> None:
    app, manager = _build_test_app(tmp_path)
    dest_dir = tmp_path / "downloads"
    dest_dir.mkdir()
    try:
        client = app.test_client()
        headers = {"X-CSRF-Token": "security-test-csrf"}

        job_resp = client.post(
            "/api/jobs",
            json={"url": malicious_url, "format": "mp3", "dest_dir": str(dest_dir)},
            headers=headers,
        )
        assert job_resp.status_code == 400
        assert "error" in job_resp.get_json()

        preview_resp = client.post(
            "/api/preview",
            json={"url": malicious_url},
            headers=headers,
        )
        assert preview_resp.status_code == 400
        assert "error" in preview_resp.get_json()
    finally:
        manager.shutdown()


@pytest.mark.parametrize("allowed_format", ["mp3", "m4a"])
def test_create_job_accepts_allowed_formats(tmp_path: Path, allowed_format: str) -> None:
    app, manager = _build_test_app(tmp_path)
    dest_dir = tmp_path / "downloads"
    dest_dir.mkdir()
    try:
        client = app.test_client()
        with patch.object(manager, "submit_job") as mock_submit:
            mock_submit.return_value.job_id = "job123"
            mock_submit.return_value.status = "pending"
            resp = client.post(
                "/api/jobs",
                json={
                    "url": "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
                    "format": allowed_format,
                    "dest_dir": str(dest_dir),
                },
                headers={"X-CSRF-Token": "security-test-csrf"},
            )
        assert resp.status_code == 201
        assert resp.get_json()["job_id"] == "job123"
    finally:
        manager.shutdown()


@pytest.mark.parametrize(
    "bad_format",
    ["flac", "wav", "ogg", "exe", "--help", "mp3;rm -rf /", ""],
)
def test_create_job_rejects_disallowed_formats(tmp_path: Path, bad_format: str) -> None:
    app, manager = _build_test_app(tmp_path)
    dest_dir = tmp_path / "downloads"
    dest_dir.mkdir()
    try:
        client = app.test_client()
        resp = client.post(
            "/api/jobs",
            json={
                "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "format": bad_format,
                "dest_dir": str(dest_dir),
            },
            headers={"X-CSRF-Token": "security-test-csrf"},
        )
        assert resp.status_code == 400
        assert "error" in resp.get_json()
    finally:
        manager.shutdown()


def test_sanitize_filename_blocks_traversal_and_reserved_names() -> None:
    assert sanitize_filename("..") == "untitled"
    assert "/" not in sanitize_filename("../../etc/passwd")
    assert "\\" not in sanitize_filename("..\\..\\etc\\passwd")
    assert sanitize_filename("CON") == "_CON"
    assert sanitize_filename("nul.mp3").startswith("_nul")
    assert sanitize_filename("...") == "untitled"


def test_promote_file_keeps_traversal_paths_inside_dest_dir(tmp_path: Path) -> None:
    fs = LocalFileSystemAdapter()
    staging = tmp_path / "staging"
    dest = tmp_path / "downloads"
    outside = tmp_path / "outside"
    staging.mkdir()
    dest.mkdir()
    outside.mkdir()

    src_file = staging / "track.mp3"
    src_file.write_bytes(b"ID3-audio-data")

    promoted = fs.promote_file(
        src_file,
        dest,
        relative_path=Path("../../outside/evil.mp3"),
    )

    resolved_dest = dest.resolve()
    resolved_promoted = promoted.resolve()
    assert resolved_dest in resolved_promoted.parents
    assert not (outside / "evil.mp3").exists()
    assert promoted.is_file()


def test_promote_file_raises_when_resolved_target_escapes_dest_dir(tmp_path: Path) -> None:
    fs = LocalFileSystemAdapter()
    staging = tmp_path / "staging"
    dest = tmp_path / "downloads"
    outside = tmp_path / "outside"
    staging.mkdir()
    dest.mkdir()
    outside.mkdir()

    src_file = staging / "track.mp3"
    src_file.write_bytes(b"ID3-audio-data")

    original_resolve = Path.resolve

    def fake_resolve(self: Path, strict: bool = False) -> Path:
        if self.name == "track.mp3" and dest in self.parents:
            return outside / "escaped.mp3"
        return original_resolve(self, strict=strict)

    with patch.object(Path, "resolve", fake_resolve):
        with pytest.raises(ValueError, match="outside destination"):
            fs.promote_file(src_file, dest)

    assert src_file.exists()
    assert not (outside / "escaped.mp3").exists()


def test_waitress_binds_to_127_0_0_1_by_default() -> None:
    env_without_host_port = {
        k: v for k, v in os.environ.items() if k not in {"HOST", "PORT"}
    }
    with (
        patch.dict(os.environ, env_without_host_port, clear=True),
        patch("waitress.serve") as mock_serve,
    ):
        main()

    mock_serve.assert_called_once()
    _, kwargs = mock_serve.call_args
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 8081


@pytest.mark.parametrize(
    "bad_host",
    ["evil.com:8081", "0.0.0.0:8081", "192.168.1.10:8081"],
)
def test_local_host_header_security_rejects_non_local_hosts(
    tmp_path: Path, bad_host: str
) -> None:
    app, manager = _build_test_app(tmp_path)
    try:
        client = app.test_client()
        resp = client.get("/health", headers={"Host": bad_host})
        assert resp.status_code == 403
    finally:
        manager.shutdown()
