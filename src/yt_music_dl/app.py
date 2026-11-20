"""Flask application factory, local security middleware, REST/SSE endpoints, and Waitress entrypoint."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import os
import secrets
import tempfile

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

from yt_music_dl.adapters.fs_adapter import LocalFileSystemAdapter
from yt_music_dl.adapters.ytdlp_adapter import SystemPreflightAdapter, YtDlpAdapter
from yt_music_dl.event_bus import InMemoryEventBus
from yt_music_dl.job_manager import JobManager
from yt_music_dl.models import AudioFormat, DownloadRequest
from yt_music_dl.ports import EventBusPort, FileSystemPort, PreflightPort
from yt_music_dl.validation import (
    ValidationError,
    is_playlist_url,
    validate_destination_dir,
    validate_youtube_url,
)

ALLOWED_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})


def _is_valid_local_host(host_header: str | None) -> bool:
    if not host_header:
        return False
    hostname = host_header.split(":", 1)[0].strip().lower()
    return hostname in ALLOWED_LOCAL_HOSTS


def _is_same_origin(origin_header: str | None, host_header: str | None) -> bool:
    if not origin_header:
        return True
    if not host_header:
        return False
    parsed = urlparse(origin_header)
    return parsed.netloc.lower() == host_header.lower()


def create_app(
    *,
    job_manager: JobManager,
    event_bus: EventBusPort,
    fs: FileSystemPort,
    preflight: PreflightPort,
    csrf_token: str | None = None,
) -> Flask:
    """Construct and configure the Flask application without global side effects."""
    app = Flask(__name__, template_folder="templates")
    token = csrf_token or secrets.token_hex(24)
    app.config["CSRF_TOKEN"] = token

    @app.before_request
    def _enforce_local_security() -> Response | tuple[Response, int] | None:
        host_header = request.headers.get("Host")
        if host_header and not _is_valid_local_host(host_header):
            return jsonify({"error": "Invalid Host header"}), 403

        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("Origin")
            if not _is_same_origin(origin, host_header):
                return jsonify({"error": "Cross-origin request rejected"}), 403
            if not secrets.compare_digest(
                request.headers.get("X-CSRF-Token", ""), token
            ):
                return jsonify({"error": "Invalid CSRF token"}), 403
        return None

    @app.get("/")
    def index() -> str:
        return render_template("index.html", csrf_token=token)

    @app.get("/api/health")
    def health() -> tuple[Response, int]:
        check = preflight.check_ffmpeg()
        available = bool(check.get("available"))
        return (
            jsonify(
                {
                    "status": "ok" if available else "degraded",
                    "ffmpeg_available": available,
                    "details": check,
                }
            ),
            200 if available else 503,
        )

    @app.get("/api/default-dir")
    def default_dir() -> tuple[Response, int]:
        return jsonify({"default_dir": str(fs.default_music_dir())}), 200

    @app.post("/api/jobs")
    def create_job() -> tuple[Response, int]:
        payload: dict[str, Any] = request.get_json(silent=True) or {}
        raw_url = payload.get("url", "")
        raw_format = str(payload.get("format", "mp3")).lower()
        raw_dest = payload.get("dest_dir") or str(fs.default_music_dir())

        if raw_format not in {"mp3", "m4a"}:
            return jsonify({"error": "Unsupported audio format; use 'mp3' or 'm4a'."}), 400

        try:
            clean_url = validate_youtube_url(raw_url)
            dest_path = validate_destination_dir(str(raw_dest), fs)
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400

        audio_format: AudioFormat = "mp3" if raw_format == "mp3" else "m4a"
        req = DownloadRequest(
            url=clean_url,
            dest_dir=dest_path,
            audio_format=audio_format,
            is_playlist=is_playlist_url(clean_url),
        )
        job = job_manager.submit_job(req)
        return jsonify({"job_id": job.job_id, "status": job.status}), 201

    @app.get("/api/jobs/<job_id>")
    def get_job(job_id: str) -> tuple[Response, int]:
        job = job_manager.get_job(job_id)
        if job is None:
            return jsonify({"error": "Job not found"}), 404
        return jsonify(job.to_dict()), 200

    @app.post("/api/jobs/<job_id>/cancel")
    def cancel_job(job_id: str) -> tuple[Response, int]:
        cancelled = job_manager.cancel_job(job_id)
        if not cancelled:
            return jsonify({"error": "Job not found"}), 404
        job = job_manager.get_job(job_id)
        return jsonify({"job_id": job_id, "status": job.status if job else "cancelled"}), 200

    @app.get("/api/jobs/<job_id>/events")
    def job_events(job_id: str) -> Response | tuple[Response, int]:
        job = job_manager.get_job(job_id)
        if job is None:
            return jsonify({"error": "Job not found"}), 404

        initial_snapshot = (
            job.progress.to_dict()
            if job.progress is not None
            else {"job_id": job.job_id, "status": job.status, "progress": 0.0}
        )

        stream = event_bus.stream_sse(job_id, initial_snapshot=initial_snapshot)
        return Response(
            stream_with_context(stream),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return app


def main() -> None:
    """Start the local Waitress server strictly bound to 127.0.0.1."""
    from waitress import serve

    fs = LocalFileSystemAdapter()
    preflight = SystemPreflightAdapter()
    downloader = YtDlpAdapter()
    event_bus = InMemoryEventBus()
    staging_root = Path(tempfile.gettempdir()) / "yt_music_dl_staging"
    job_manager = JobManager(
        downloader=downloader,
        fs=fs,
        event_bus=event_bus,
        staging_root=staging_root,
    )
    app = create_app(
        job_manager=job_manager,
        event_bus=event_bus,
        fs=fs,
        preflight=preflight,
    )
    port = int(os.environ.get("YT_MUSIC_DL_PORT", "8765"))
    try:
        serve(app, host="127.0.0.1", port=port)
    finally:
        job_manager.shutdown()


if __name__ == "__main__":
    main()
