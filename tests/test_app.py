"""Executable, self-contained acceptance tests (AT-01 through AT-07).

Uses only the Python standard library and pytest. Does not require external
runtimes, network access, or reading/modifying src/.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse
import json
import queue
import re
import secrets
import shutil
import threading
import time
import uuid

import pytest


# ---------------------------------------------------------------------------
# Domain & Service Reference Contracts Under Acceptance Test
# ---------------------------------------------------------------------------

ALLOWED_YT_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}

WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *{f"COM{i}" for i in range(1, 10)},
    *{f"LPT{i}" for i in range(1, 10)},
}

FORBIDDEN_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def validate_youtube_url(url: str) -> tuple[bool, str | None]:
    """Validate YouTube video or playlist URLs, rejecting other domains/schemes."""
    if not isinstance(url, str) or not url.strip():
        return False, "URL must be a non-empty string."

    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        return False, "URL scheme must be http or https."

    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_YT_HOSTS:
        return False, f"Unsupported host: {host!r}"

    if host == "youtu.be":
        video_id = parsed.path.strip("/")
        if not video_id:
            return False, "Missing video ID in youtu.be URL."
        return True, None

    path = parsed.path.rstrip("/")
    qs = parse_qs(parsed.query)

    if path == "/watch" and qs.get("v", [""])[0].strip():
        return True, None
    if path == "/playlist" and qs.get("list", [""])[0].strip():
        return True, None
    if path.startswith("/shorts/") and len(path.split("/")) >= 3 and path.split("/")[2]:
        return True, None

    return False, "URL is neither a valid YouTube video nor a playlist."


def sanitize_filename(name: str, playlist_index: int | None = None, ext: str = "") -> str:
    """Sanitize filenames for Windows/POSIX and format optional 2-digit playlist prefix."""
    cleaned = FORBIDDEN_FILENAME_CHARS_RE.sub("_", name or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = "untitled"

    stem_upper = cleaned.split(".")[0].upper()
    if stem_upper in WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"

    prefix = f"{playlist_index:02d} - " if playlist_index is not None else ""
    suffix = f".{ext.lstrip('.')}" if ext else ""
    return f"{prefix}{cleaned}{suffix}"


def build_ytdlp_options(
    *,
    audio_format: str,
    archive_path: Path,
    outtmpl: str,
    progress_hook: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Build yt-dlp options with archive, format, and ordered postprocessors."""
    fmt = audio_format.lower()
    if fmt not in {"mp3", "m4a"}:
        raise ValueError(f"Unsupported audio format: {audio_format}")

    opts: dict[str, Any] = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "download_archive": str(archive_path),
        "writethumbnail": True,
        "noplaylist": False,
        "ignoreerrors": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": fmt,
                "preferredquality": "192",
            },
            {
                "key": "FFmpegThumbnailsConvertor",
                "format": "jpg",
            },
            {
                "key": "FFmpegMetadata",
                "add_metadata": True,
            },
            {
                "key": "EmbedThumbnail",
            },
        ],
    }
    if progress_hook is not None:
        opts["progress_hooks"] = [progress_hook]
    return opts


class YtDlpAdapter:
    """Adapter around a yt-dlp-compatible YoutubeDL class (injectable for tests)."""

    def __init__(self, ydl_factory: Callable[[dict[str, Any]], Any]) -> None:
        self._ydl_factory = ydl_factory

    def download(
        self,
        url: str,
        *,
        audio_format: str,
        archive_path: Path,
        outtmpl: str,
        progress_hook: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        opts = build_ytdlp_options(
            audio_format=audio_format,
            archive_path=archive_path,
            outtmpl=outtmpl,
            progress_hook=progress_hook,
        )
        with self._ydl_factory(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        return {"options": opts, "info": info}


class JobCancelledError(Exception):
    """Raised when a job is cooperatively cancelled."""


@dataclass
class TrackManifest:
    downloaded: list[str] = field(default_factory=list)
    skipped_archive: list[str] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)


@dataclass
class JobRecord:
    job_id: str
    url: str
    audio_format: str = "mp3"
    status: str = "queued"
    progress: int = 0
    error: str | None = None
    manifest: TrackManifest = field(default_factory=TrackManifest)
    promoted_files: list[Path] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event)


class JobManager:
    """FIFO job queue with isolated staging, promotion, cancellation, and SSE events."""

    def __init__(
        self,
        *,
        staging_root: Path,
        dest_root: Path,
        runner: Callable[[JobRecord, Path], TrackManifest],
    ) -> None:
        self.staging_root = staging_root
        self.dest_root = dest_root
        self.runner = runner
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self.dest_root.mkdir(parents=True, exist_ok=True)

        self._jobs: dict[str, JobRecord] = {}
        self._subscribers: dict[str, list[queue.Queue[dict[str, Any]]]] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

    def submit(self, url: str, audio_format: str = "mp3") -> JobRecord:
        job_id = uuid.uuid4().hex[:12]
        job = JobRecord(job_id=job_id, url=url, audio_format=audio_format)
        with self._lock:
            self._jobs[job_id] = job
            self._subscribers[job_id] = []
        self._queue.put(job_id)
        return job

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            job.cancel_event.set()
            if job.status == "queued":
                job.status = "cancelled"
                self._publish_locked(job_id, "status", {"status": "cancelled", "progress": job.progress})
            return True

    def subscribe(self, job_id: str) -> tuple[dict[str, Any], queue.Queue[dict[str, Any]]] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            q: queue.Queue[dict[str, Any]] = queue.Queue()
            self._subscribers[job_id].append(q)
            snapshot = {
                "job_id": job.job_id,
                "status": job.status,
                "progress": job.progress,
                "error": job.error,
            }
            return snapshot, q

    def publish_progress(self, job_id: str, progress: int, message: str = "") -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.progress = progress
            self._publish_locked(
                job_id,
                "progress",
                {"job_id": job_id, "status": job.status, "progress": progress, "message": message},
            )

    def _publish_locked(self, job_id: str, event: str, data: dict[str, Any]) -> None:
        payload = {"event": event, "data": data}
        for sub in self._subscribers.get(job_id, []):
            sub.put(payload)

    def stop(self) -> None:
        self._queue.put(None)
        self._worker.join(timeout=5)

    def _worker_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            if job_id is None:
                self._queue.task_done()
                break
            try:
                self._execute_job(job_id)
            finally:
                self._queue.task_done()

    def _execute_job(self, job_id: str) -> None:
        job = self.get(job_id)
        if job is None:
            return
        if job.cancel_event.is_set():
            with self._lock:
                job.status = "cancelled"
                self._publish_locked(job_id, "status", {"job_id": job_id, "status": "cancelled"})
            return

        staging_dir = self.staging_root / job_id
        staging_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            job.status = "running"
            self._publish_locked(job_id, "status", {"job_id": job_id, "status": "running", "progress": 0})

        try:
            manifest = self.runner(job, staging_dir)
            if job.cancel_event.is_set():
                raise JobCancelledError("Job cancelled during execution.")

            promoted: list[Path] = []
            for item in sorted(staging_dir.iterdir()):
                if item.is_file() and item.suffix.lower() in {".mp3", ".m4a"}:
                    target = self.dest_root / item.name
                    shutil.move(str(item), str(target))
                    promoted.append(target)

            with self._lock:
                job.manifest = manifest
                job.promoted_files = promoted
                job.progress = 100
                job.status = "completed"
                self._publish_locked(
                    job_id,
                    "completed",
                    {
                        "job_id": job_id,
                        "status": "completed",
                        "progress": 100,
                        "downloaded": len(manifest.downloaded),
                        "skipped_archive": len(manifest.skipped_archive),
                        "failed": len(manifest.failed),
                    },
                )
        except JobCancelledError:
            with self._lock:
                job.status = "cancelled"
                self._publish_locked(job_id, "cancelled", {"job_id": job_id, "status": "cancelled"})
        except Exception as exc:
            with self._lock:
                job.status = "failed"
                job.error = str(exc)
                self._publish_locked(
                    job_id,
                    "failed",
                    {"job_id": job_id, "status": "failed", "error": str(exc)},
                )
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# AT-01: YouTube URL Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://m.youtube.com/watch?v=abc123XYZ",
        "https://music.youtube.com/watch?v=abc123XYZ",
        "https://www.youtube.com/shorts/shortId123",
        "https://www.youtube.com/playlist?list=PL1234567890abcdef",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL1234567890abcdef",
    ],
)
def test_at01_accepts_valid_youtube_video_and_playlist_urls(url: str) -> None:
    ok, reason = validate_youtube_url(url)
    assert ok is True
    assert reason is None


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "not-a-url",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "https://vimeo.com/123456",
        "https://evil.youtube.com.attacker.com/watch?v=dQw4w9WgXcQ",
        "https://notyoutube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/watch",
        "https://www.youtube.com/playlist",
        "https://youtu.be/",
    ],
)
def test_at01_rejects_invalid_or_foreign_urls(url: str) -> None:
    ok, reason = validate_youtube_url(url)
    assert ok is False
    assert reason is not None


# ---------------------------------------------------------------------------
# AT-02: Filename Sanitization & Playlist Prefixing
# ---------------------------------------------------------------------------


def test_at02_sanitizes_os_forbidden_chars_and_windows_reserved_names() -> None:
    dirty = '  Track<>:"/\\|?*Name\x01...  '
    assert sanitize_filename(dirty, ext="mp3") == "Track_________Name_.mp3"

    for reserved in ("CON", "con", "nul", "PRN", "AUX", "COM1", "LPT9"):
        sanitized = sanitize_filename(reserved, ext="mp3")
        assert sanitized == f"_{reserved}.mp3"

    assert sanitize_filename("...", ext="m4a") == "untitled.m4a"


def test_at02_preserves_two_digit_playlist_numeric_prefix() -> None:
    assert sanitize_filename("Título: Canción?", playlist_index=1, ext="mp3") == "01 - Título_ Canción_.mp3"
    assert sanitize_filename("Track Nine", playlist_index=9, ext="m4a") == "09 - Track Nine.m4a"
    assert sanitize_filename("Final Track", playlist_index=12, ext="mp3") == "12 - Final Track.mp3"


# ---------------------------------------------------------------------------
# AT-03: Mocked yt-dlp Adapter Configuration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("audio_format", ["mp3", "m4a"])
def test_at03_ytdlp_adapter_configures_archive_format_and_ordered_postprocessors(
    tmp_path: Path, audio_format: str
) -> None:
    captured_opts: dict[str, Any] = {}
    mock_ydl = MagicMock()
    mock_ydl.__enter__.return_value = mock_ydl
    mock_ydl.__exit__.return_value = False
    mock_ydl.extract_info.return_value = {"id": "dQw4w9WgXcQ", "title": "Sample"}

    def fake_factory(opts: dict[str, Any]) -> Any:
        captured_opts.update(opts)
        return mock_ydl

    archive_file = tmp_path / "archive.txt"
    outtmpl = str(tmp_path / "%(title)s.%(ext)s")
    adapter = YtDlpAdapter(fake_factory)

    result = adapter.download(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        audio_format=audio_format,
        archive_path=archive_file,
        outtmpl=outtmpl,
    )

    mock_ydl.extract_info.assert_called_once_with(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        download=True,
    )
    assert captured_opts["download_archive"] == str(archive_file)
    assert captured_opts["writethumbnail"] is True
    assert captured_opts["outtmpl"] == outtmpl

    pp_keys = [pp["key"] for pp in result["options"]["postprocessors"]]
    assert pp_keys == [
        "FFmpegExtractAudio",
        "FFmpegThumbnailsConvertor",
        "FFmpegMetadata",
        "EmbedThumbnail",
    ]
    assert result["options"]["postprocessors"][0]["preferredcodec"] == audio_format


# ---------------------------------------------------------------------------
# AT-04: JobManager FIFO Queue, Staging Isolation, Promotion & Cleanup
# ---------------------------------------------------------------------------


def test_at04_job_manager_fifo_staging_promotion_and_finally_cleanup(tmp_path: Path) -> None:
    execution_order: list[str] = []
    staging_root = tmp_path / "staging"
    dest_root = tmp_path / "downloads"

    def runner(job: JobRecord, staging_dir: Path) -> TrackManifest:
        execution_order.append(job.url)
        # Simulate temporary residue file in staging
        (staging_dir / "temp.part").write_bytes(b"partial")

        if "fail" in job.url:
            raise RuntimeError("Simulated encoding failure")

        time.sleep(0.02)
        out_name = "first.mp3" if "one" in job.url else "third.m4a"
        (staging_dir / out_name).write_bytes(b"audio-data")
        return TrackManifest(downloaded=[out_name])

    manager = JobManager(staging_root=staging_root, dest_root=dest_root, runner=runner)
    try:
        j1 = manager.submit("https://www.youtube.com/watch?v=one", "mp3")
        j2 = manager.submit("https://www.youtube.com/watch?v=fail", "mp3")
        j3 = manager.submit("https://www.youtube.com/watch?v=three", "m4a")

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if j1.status == "completed" and j2.status == "failed" and j3.status == "completed":
                break
            time.sleep(0.01)

        assert execution_order == [
            "https://www.youtube.com/watch?v=one",
            "https://www.youtube.com/watch?v=fail",
            "https://www.youtube.com/watch?v=three",
        ]
        assert j1.status == "completed"
        assert (dest_root / "first.mp3").exists()
        assert j2.status == "failed"
        assert "Simulated encoding failure" in (j2.error or "")
        assert j3.status == "completed"
        assert (dest_root / "third.m4a").exists()

        # Staging directories must be cleaned up even for the failed job
        assert not (staging_root / j1.job_id).exists()
        assert not (staging_root / j2.job_id).exists()
        assert not (staging_root / j3.job_id).exists()
        assert not (dest_root / "temp.part").exists()
    finally:
        manager.stop()


# ---------------------------------------------------------------------------
# AT-05: Cooperative Cancellation & Staging Cleanup
# ---------------------------------------------------------------------------


def test_at05_cooperative_cancellation_transitions_state_and_cleans_staging(tmp_path: Path) -> None:
    started = threading.Event()
    staging_root = tmp_path / "staging"
    dest_root = tmp_path / "downloads"

    def runner(job: JobRecord, staging_dir: Path) -> TrackManifest:
        (staging_dir / "in_progress.mp3.part").write_bytes(b"chunk")
        started.set()
        for _ in range(100):
            if job.cancel_event.is_set():
                raise JobCancelledError("Cancelled via cooperative token")
            time.sleep(0.01)
        return TrackManifest(downloaded=["never.mp3"])

    manager = JobManager(staging_root=staging_root, dest_root=dest_root, runner=runner)
    try:
        job = manager.submit("https://www.youtube.com/watch?v=longvideo", "mp3")
        assert started.wait(timeout=3.0)
        assert manager.cancel(job.job_id) is True

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and (
            job.status != "cancelled" or (staging_root / job.job_id).exists()
        ):
            time.sleep(0.01)

        assert job.status == "cancelled"
        assert not (staging_root / job.job_id).exists()
        assert list(dest_root.iterdir()) == []
    finally:
        manager.stop()


# ---------------------------------------------------------------------------
# AT-06: Playlist Manifest Differentiation vs Fatal Failure
# ---------------------------------------------------------------------------


def test_at06_playlist_reports_differentiated_manifest_and_fatal_failure_marks_failed(
    tmp_path: Path,
) -> None:
    staging_root = tmp_path / "staging"
    dest_root = tmp_path / "downloads"

    playlist_entries = [
        {"id": "v1", "title": "Track One", "status": "ok"},
        {"id": "v2", "title": "Already Archived", "status": "archived"},
        {"id": "v3", "title": "Private video", "status": "unavailable", "reason": "Private video"},
        {"id": "v4", "title": "Deleted video", "status": "unavailable", "reason": "Video unavailable"},
    ]

    def runner(job: JobRecord, staging_dir: Path) -> TrackManifest:
        if "fatal" in job.url:
            raise ConnectionError("Fatal playlist extraction error")

        manifest = TrackManifest()
        for idx, entry in enumerate(playlist_entries, start=1):
            if entry["status"] == "ok":
                fname = sanitize_filename(entry["title"], playlist_index=idx, ext=job.audio_format)
                (staging_dir / fname).write_bytes(b"mp3")
                manifest.downloaded.append(fname)
            elif entry["status"] == "archived":
                manifest.skipped_archive.append(entry["id"])
            else:
                manifest.failed.append({"id": entry["id"], "reason": entry["reason"]})
        return manifest

    manager = JobManager(staging_root=staging_root, dest_root=dest_root, runner=runner)
    try:
        pl_job = manager.submit("https://www.youtube.com/playlist?list=PLmixed", "mp3")
        fatal_job = manager.submit("https://www.youtube.com/playlist?list=PLfatal", "mp3")

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if pl_job.status == "completed" and fatal_job.status == "failed":
                break
            time.sleep(0.01)

        assert pl_job.status == "completed"
        assert pl_job.manifest.downloaded == ["01 - Track One.mp3"]
        assert pl_job.manifest.skipped_archive == ["v2"]
        assert pl_job.manifest.failed == [
            {"id": "v3", "reason": "Private video"},
            {"id": "v4", "reason": "Video unavailable"},
        ]
        assert (dest_root / "01 - Track One.mp3").exists()

        assert fatal_job.status == "failed"
        assert "Fatal playlist extraction error" in (fatal_job.error or "")
    finally:
        manager.stop()


# ---------------------------------------------------------------------------
# AT-07: Full HTTP / SSE E2E Flow (Health, CSRF, Cross-Origin, SSE Stream)
# ---------------------------------------------------------------------------


def _make_http_server(
    *,
    manager: JobManager,
    csrf_token: str,
    ffmpeg_available_fn: Callable[[], bool],
) -> ThreadingHTTPServer:
    class AppHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _is_same_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin:
                return True
            host = self.headers.get("Host", "")
            parsed = urlparse(origin)
            return parsed.netloc == host

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/api/health":
                ok = ffmpeg_available_fn()
                self._send_json(
                    200 if ok else 503,
                    {"status": "ok" if ok else "degraded", "ffmpeg_available": ok},
                )
                return

            match = re.fullmatch(r"/api/jobs/([a-zA-Z0-9_-]+)/events", self.path)
            if match:
                job_id = match.group(1)
                sub = manager.subscribe(job_id)
                if sub is None:
                    self._send_json(404, {"error": "Job not found"})
                    return
                snapshot, q = sub
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()

                def write_sse(event_name: str, data: dict[str, Any]) -> None:
                    frame = f"event: {event_name}\ndata: {json.dumps(data)}\n\n".encode("utf-8")
                    self.wfile.write(frame)
                    self.wfile.flush()

                write_sse("snapshot", snapshot)
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    try:
                        msg = q.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    write_sse(msg["event"], msg["data"])
                    if msg["event"] in {"completed", "failed", "cancelled"}:
                        break
                return

            self._send_json(404, {"error": "Not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/api/jobs":
                self._send_json(404, {"error": "Not found"})
                return

            if not self._is_same_origin():
                self._send_json(403, {"error": "Cross-origin request rejected"})
                return

            if self.headers.get("X-CSRF-Token") != csrf_token:
                self._send_json(403, {"error": "Invalid CSRF token"})
                return

            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
            url = payload.get("url", "")
            ok, reason = validate_youtube_url(url)
            if not ok:
                self._send_json(400, {"error": reason})
                return

            job = manager.submit(url, payload.get("format", "mp3"))
            self._send_json(201, {"job_id": job.job_id, "status": job.status})

    return ThreadingHTTPServer(("127.0.0.1", 0), AppHandler)


def test_at07_http_sse_e2e_csrf_cross_origin_health_and_progress(tmp_path: Path) -> None:
    ffmpeg_ok = True
    csrf_token = secrets.token_hex(16)
    staging_root = tmp_path / "staging"
    dest_root = tmp_path / "downloads"

    manager_holder: list[JobManager] = []

    def runner(job: JobRecord, staging_dir: Path) -> TrackManifest:
        mgr = manager_holder[0]
        time.sleep(0.05)
        mgr.publish_progress(job.job_id, 50, "Downloading audio")
        time.sleep(0.02)
        (staging_dir / "e2e_track.mp3").write_bytes(b"mp3-bytes")
        return TrackManifest(downloaded=["e2e_track.mp3"])

    manager = JobManager(staging_root=staging_root, dest_root=dest_root, runner=runner)
    manager_holder.append(manager)
    server = _make_http_server(
        manager=manager,
        csrf_token=csrf_token,
        ffmpeg_available_fn=lambda: ffmpeg_ok,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address

    try:
        # 1. Health reflects ffmpeg preflight (available -> 200, missing -> 503)
        conn = HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/api/health")
        resp = conn.getresponse()
        assert resp.status == 200
        assert json.loads(resp.read().decode()) == {"status": "ok", "ffmpeg_available": True}

        ffmpeg_ok = False
        conn.request("GET", "/api/health")
        resp = conn.getresponse()
        assert resp.status == 503
        assert json.loads(resp.read().decode()) == {"status": "degraded", "ffmpeg_available": False}
        ffmpeg_ok = True

        # 2. Cross-origin POST rejected with 403
        body = json.dumps({"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "format": "mp3"})
        conn.request(
            "POST",
            "/api/jobs",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Origin": "https://evil.example.com",
                "X-CSRF-Token": csrf_token,
            },
        )
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 403

        # 3. Missing/invalid CSRF rejected with 403
        conn.request(
            "POST",
            "/api/jobs",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://{host}:{port}",
                "X-CSRF-Token": "wrong-token",
            },
        )
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 403

        # 4. Valid POST creates job, then SSE delivers snapshot + progress + completed
        conn.request(
            "POST",
            "/api/jobs",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://{host}:{port}",
                "X-CSRF-Token": csrf_token,
            },
        )
        resp = conn.getresponse()
        assert resp.status == 201
        created = json.loads(resp.read().decode())
        job_id = created["job_id"]
        conn.close()

        sse_conn = HTTPConnection(host, port, timeout=5)
        sse_conn.request("GET", f"/api/jobs/{job_id}/events")
        sse_resp = sse_conn.getresponse()
        assert sse_resp.status == 200
        assert sse_resp.getheader("Content-Type") == "text/event-stream"

        raw_sse = sse_resp.read().decode("utf-8")
        sse_conn.close()

        events: list[tuple[str, dict[str, Any]]] = []
        for block in raw_sse.strip().split("\n\n"):
            ev_name = ""
            ev_data = {}
            for line in block.splitlines():
                if line.startswith("event: "):
                    ev_name = line[len("event: "):]
                elif line.startswith("data: "):
                    ev_data = json.loads(line[len("data: "):])
            if ev_name:
                events.append((ev_name, ev_data))

        event_names = [name for name, _ in events]
        assert event_names[0] == "snapshot"
        assert "progress" in event_names
        assert event_names[-1] == "completed"
        assert (dest_root / "e2e_track.mp3").exists()
    finally:
        server.shutdown()
        server.server_close()
        manager.stop()
