"""Flask application factory, local security middleware, REST/SSE endpoints, and Waitress entrypoint."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import io
import os
import secrets
import subprocess
import sys
import tempfile
import zipfile

from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context

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

    @app.get("/favicon.ico")
    def favicon_ico() -> Response:
        return app.send_static_file("favicon.svg")

    @app.get("/health")
    def liveness_health() -> tuple[Response, int]:
        check = preflight.check_ffmpeg()
        available = bool(check.get("available"))
        return (
            jsonify(
                {
                    "status": "ok",
                    "ffmpeg_available": available,
                    "details": check,
                }
            ),
            200,
        )

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

    @app.post("/api/open-folder")
    def open_folder() -> tuple[Response, int]:
        payload: dict[str, Any] = request.get_json(silent=True) or {}
        raw_dest = payload.get("dest_dir") or str(fs.default_music_dir())
        try:
            dest_path = validate_destination_dir(str(raw_dest), fs)
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400

        try:
            if sys.platform == "win32":
                os.startfile(str(dest_path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(dest_path)])
            else:
                subprocess.Popen(["xdg-open", str(dest_path)])
        except Exception as exc:
            return jsonify({"error": f"No se pudo abrir la carpeta: {exc}"}), 500
        return jsonify({"opened": str(dest_path)}), 200

    @app.post("/api/pick-folder")
    def pick_folder() -> tuple[Response, int]:
        payload: dict[str, Any] = request.get_json(silent=True) or {}
        initial_dir = str(payload.get("initial_dir") or fs.default_music_dir())
        try:
            import _tkinter
            tcl_error_cls: type[BaseException] = _tkinter.TclError
        except Exception:
            tcl_error_cls = Exception

        root = None
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            try:
                root.attributes("-topmost", True)
            except Exception:
                pass
            chosen = filedialog.askdirectory(
                initialdir=initial_dir if Path(initial_dir).is_dir() else str(fs.default_music_dir()),
                title="Seleccionar carpeta de destino",
            )
        except (ImportError, tcl_error_cls, Exception):
            return (
                jsonify(
                    {
                        "error": (
                            "El selector nativo de carpetas no está disponible en este entorno. "
                            "Escribe la ruta de destino manualmente."
                        )
                    }
                ),
                501,
            )
        finally:
            if root is not None:
                try:
                    root.destroy()
                except Exception:
                    pass

        if not chosen:
            return jsonify({"cancelled": True}), 200
        return jsonify({"selected_dir": str(Path(chosen))}), 200

    @app.post("/api/preview")
    def preview_url() -> tuple[Response, int]:
        payload: dict[str, Any] = request.get_json(silent=True) or {}
        raw_url = payload.get("url", "")
        try:
            clean_url = validate_youtube_url(raw_url)
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400

        adapter = YtDlpAdapter()
        try:
            metadata = adapter.preview(clean_url)
        except Exception as exc:
            return jsonify({"error": f"No se pudo obtener la vista previa: {exc}"}), 422
        resp = jsonify(metadata)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return resp, 200

    @app.post("/api/jobs")
    def create_job() -> tuple[Response, int]:
        payload: dict[str, Any] = request.get_json(silent=True) or {}
        raw_url = payload.get("url", "")
        raw_format = str(payload.get("format", "mp3")).lower()
        raw_quality = str(
            payload.get("quality", "192" if raw_format == "mp3" else "best")
        ).lower()
        raw_dest = payload.get("dest_dir") or str(fs.default_music_dir())

        if raw_format not in {"mp3", "m4a"}:
            return jsonify({"error": "Unsupported audio format; use 'mp3' or 'm4a'."}), 400
        if raw_quality not in {"320", "192", "best"}:
            return jsonify({"error": "Unsupported quality; use '320', '192', or 'best'."}), 400

        try:
            clean_url = validate_youtube_url(raw_url)
            dest_path = validate_destination_dir(str(raw_dest), fs)
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400

        raw_selected = payload.get("selected_tracks")
        selected_tracks: tuple[int, ...] | None = None
        if raw_selected is not None:
            if not isinstance(raw_selected, list):
                return jsonify({"error": "selected_tracks must be a list of track indices."}), 400
            parsed_indices: list[int] = []
            for item in raw_selected:
                if isinstance(item, bool) or not isinstance(item, int) or item < 1:
                    return jsonify({"error": "selected_tracks must contain positive integers."}), 400
                if item not in parsed_indices:
                    parsed_indices.append(item)
            if parsed_indices:
                selected_tracks = tuple(parsed_indices)

        audio_format: AudioFormat = "mp3" if raw_format == "mp3" else "m4a"
        req = DownloadRequest(
            url=clean_url,
            dest_dir=dest_path,
            audio_format=audio_format,
            is_playlist=is_playlist_url(clean_url),
            quality=raw_quality,
            selected_tracks=selected_tracks,
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

    @app.get("/api/jobs/<job_id>/audio/<int:index>")
    def stream_job_audio(job_id: str, index: int) -> Response | tuple[Response, int]:
        job = job_manager.get_job(job_id)
        if job is None:
            return jsonify({"error": "Job not found"}), 404
        if index < 0 or index >= len(job.promoted_files):
            return jsonify({"error": "Audio file not found"}), 404
        audio_path = job.promoted_files[index]
        if not audio_path.is_file():
            return jsonify({"error": "Audio file missing on disk"}), 404
        as_attachment = request.args.get("download") == "1"
        if as_attachment:
            return send_file(
                audio_path,
                as_attachment=True,
                download_name=audio_path.name,
            )
        return send_file(audio_path, conditional=True)

    @app.get("/api/jobs/<job_id>/zip")
    def download_job_zip(job_id: str) -> Response | tuple[Response, int]:
        job = job_manager.get_job(job_id)
        if job is None:
            return jsonify({"error": "Job not found"}), 404

        files = [p for p in job.promoted_files if p.is_file()]
        if not files:
            return jsonify({"error": "No audio files available for ZIP"}), 404

        buffer = io.BytesIO()
        dest_dir = job.request.dest_dir.resolve()
        with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_STORED) as zf:
            for file_path in files:
                resolved = file_path.resolve()
                try:
                    arcname = str(resolved.relative_to(dest_dir))
                except ValueError:
                    arcname = file_path.name
                zf.write(resolved, arcname=arcname)
        buffer.seek(0)

        first_parent = files[0].parent.name
        zip_stem = (
            first_parent
            if files[0].parent.resolve() != dest_dir and first_parent
            else f"playlist-{job_id}"
        )
        return send_file(
            buffer,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"{zip_stem}.zip",
        )

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
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8081"))
    try:
        serve(app, host=host, port=port)
    finally:
        job_manager.shutdown()


if __name__ == "__main__":
    main()
