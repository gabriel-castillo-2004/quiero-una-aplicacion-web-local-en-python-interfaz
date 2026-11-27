"""Flask application factory, local security middleware, REST/SSE endpoints, and Waitress entrypoint."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse
import io
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import zipfile

from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context

from yt_music_dl.adapters.fs_adapter import LocalFileSystemAdapter
from yt_music_dl.adapters.sqlite_adapter import SqlitePersistenceAdapter
from yt_music_dl.adapters.ytdlp_adapter import (
    SystemPreflightAdapter,
    YtDlpAdapter,
    _check_ytdlp_status,
)
from yt_music_dl.event_bus import InMemoryEventBus
from yt_music_dl.job_manager import JobManager
from yt_music_dl.models import ALLOWED_AUDIO_FORMATS, AudioFormat, DownloadRequest
from yt_music_dl.ports import EventBusPort, FileSystemPort, PreflightPort
from yt_music_dl.validation import (
    ValidationError,
    is_playlist_url,
    resolve_safe_library_path,
    validate_destination_dir,
    validate_youtube_url,
)

ALLOWED_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})
_RANGE_HEADER_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
_AUDIO_MIME_OVERRIDES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".opus": "audio/opus",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".aac": "audio/aac",
}


def _guess_audio_mimetype(file_path: Path) -> str:
    ext = file_path.suffix.lower()
    if ext in _AUDIO_MIME_OVERRIDES:
        return _AUDIO_MIME_OVERRIDES[ext]
    guessed, _ = mimetypes.guess_type(str(file_path))
    return guessed or "application/octet-stream"


def _serve_audio_with_range(file_path: Path) -> Response:
    """Serve an audio file with HTTP Range (206 Partial Content) support."""
    mime = _guess_audio_mimetype(file_path)
    file_size = file_path.stat().st_size
    range_header = request.headers.get("Range")

    if not range_header:
        resp = send_file(file_path, mimetype=mime, conditional=True)
        resp.headers.setdefault("Accept-Ranges", "bytes")
        return resp

    match = _RANGE_HEADER_RE.match(range_header.strip())
    if not match or file_size == 0:
        return Response(
            status=416,
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    start_str, end_str = match.groups()
    if not start_str and not end_str:
        return Response(
            status=416,
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    if start_str:
        start = int(start_str)
        end = int(end_str) if end_str else file_size - 1
    else:
        suffix_len = int(end_str)
        if suffix_len <= 0:
            return Response(
                status=416,
                headers={"Content-Range": f"bytes */{file_size}"},
            )
        start = max(file_size - suffix_len, 0)
        end = file_size - 1

    if start >= file_size or end < start:
        return Response(
            status=416,
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    end = min(end, file_size - 1)
    chunk_len = end - start + 1
    with file_path.open("rb") as fh:
        fh.seek(start)
        data = fh.read(chunk_len)

    return Response(
        data,
        status=206,
        mimetype=mime,
        headers={
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(len(data)),
        },
    )


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
    preview_adapter = YtDlpAdapter()

    def _run_startup_preflight() -> dict[str, Any]:
        ffmpeg_info = preflight.check_ffmpeg()
        ytdlp_checker = getattr(preflight, "check_ytdlp", None)
        ytdlp_info = ytdlp_checker() if callable(ytdlp_checker) else _check_ytdlp_status()
        summary = {
            "ffmpeg": bool(ffmpeg_info.get("ffmpeg", ffmpeg_info.get("available"))),
            "ffprobe": bool(ffmpeg_info.get("ffprobe", ffmpeg_info.get("available"))),
            "yt_dlp": bool(ytdlp_info.get("available")),
            "yt_dlp_in_path": bool(ytdlp_info.get("path") or shutil.which("yt-dlp")),
        }
        missing = [name for name in ("ffmpeg", "ffprobe") if not summary[name]]
        if not summary["yt_dlp"]:
            missing.append("yt-dlp")
        if missing:
            app.logger.warning(
                "Dependencias faltantes al iniciar yt-music-dl: %s",
                ", ".join(missing),
            )
        return summary

    app.config["STARTUP_PREFLIGHT"] = _run_startup_preflight()

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
        ffmpeg_available = bool(check.get("available"))

        ytdlp_checker = getattr(preflight, "check_ytdlp", None)
        ytdlp_info = ytdlp_checker() if callable(ytdlp_checker) else _check_ytdlp_status()
        ytdlp_available = bool(ytdlp_info.get("available"))
        ytdlp_version = ytdlp_info.get("version")

        raw_dest = request.args.get("dest_dir") or str(fs.default_music_dir())
        dest_writable = False
        dest_error: str | None = None
        resolved_dest_str = str(raw_dest)
        try:
            resolved_dest = fs.resolve_dir(str(raw_dest))
            resolved_dest_str = str(resolved_dest)
            fs.ensure_writable(resolved_dest)
            dest_writable = True
        except Exception as exc:
            dest_error = str(exc)

        all_ok = ffmpeg_available and ytdlp_available and dest_writable
        details = {
            **check,
            "yt_dlp": ytdlp_info,
            "download_dir": {
                "path": resolved_dest_str,
                "writable": dest_writable,
                "error": dest_error,
            },
        }
        return (
            jsonify(
                {
                    "status": "ok" if all_ok else "degraded",
                    "ffmpeg_available": ffmpeg_available,
                    "yt_dlp_available": ytdlp_available,
                    "yt_dlp_version": ytdlp_version,
                    "download_dir": resolved_dest_str,
                    "download_dir_writable": dest_writable,
                    "details": details,
                }
            ),
            200 if all_ok else 503,
        )

    @app.get("/api/default-dir")
    def default_dir() -> tuple[Response, int]:
        return jsonify({"default_dir": str(fs.default_music_dir())}), 200

    def _resolve_library_dir() -> Path:
        raw_dest = request.args.get("dest_dir")
        if raw_dest and raw_dest.strip():
            return fs.resolve_dir(raw_dest)
        return fs.default_music_dir().resolve()

    @app.get("/api/library")
    def list_library() -> tuple[Response, int]:
        try:
            library_dir = _resolve_library_dir()
        except (OSError, ValueError) as exc:
            return jsonify({"error": f"Invalid library directory: {exc}"}), 400

        list_fn = getattr(fs, "list_audio_files", None)
        if callable(list_fn):
            items = list_fn(library_dir)
        else:
            items = LocalFileSystemAdapter().list_audio_files(library_dir)

        query = (request.args.get("q") or "").strip().lower()
        fmt = (request.args.get("format") or "").strip().lower()
        if query:
            items = [
                it
                for it in items
                if query in str(it.get("name", "")).lower()
                or query in str(it.get("relative_path", "")).lower()
                or query in str(it.get("folder", "")).lower()
            ]
        if fmt:
            items = [it for it in items if str(it.get("format", "")).lower() == fmt]

        return jsonify({"directory": str(library_dir), "tracks": items}), 200

    @app.get("/api/library/audio")
    def stream_library_audio() -> Response | tuple[Response, int]:
        raw_path = request.args.get("path", "")
        try:
            library_dir = _resolve_library_dir()
        except (OSError, ValueError) as exc:
            return jsonify({"error": f"Invalid library directory: {exc}"}), 400

        try:
            target = resolve_safe_library_path(library_dir, raw_path)
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 403

        if not target.is_file():
            return jsonify({"error": "Audio file not found"}), 404

        return _serve_audio_with_range(target)

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

    @app.post("/api/inspect")
    @app.post("/api/preview")
    def preview_url() -> tuple[Response, int]:
        payload: dict[str, Any] = request.get_json(silent=True) or {}
        raw_url = payload.get("url", "")
        raw_format = str(payload.get("format", "mp3")).strip().lower()
        refresh = bool(payload.get("refresh", False))
        try:
            clean_url = validate_youtube_url(raw_url)
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400

        try:
            metadata = preview_adapter.preview(clean_url, refresh=refresh)
        except Exception as exc:
            return jsonify({"error": f"No se pudo obtener la vista previa: {exc}"}), 422

        get_ids_fn = getattr(job_manager, "get_downloaded_ids", None)
        known_ids: set[str] = (
            get_ids_fn(audio_format=raw_format) if callable(get_ids_fn) else set()
        )
        tracks = metadata.get("tracks")
        if isinstance(tracks, list):
            for track in tracks:
                if isinstance(track, dict):
                    tid = track.get("id")
                    track["already_downloaded"] = bool(tid and str(tid) in known_ids)
        if metadata.get("id"):
            metadata["already_downloaded"] = str(metadata["id"]) in known_ids

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

        if raw_format not in ALLOWED_AUDIO_FORMATS:
            return jsonify({"error": "Unsupported audio format; use 'mp3', 'm4a', or 'original'."}), 400
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

        skip_duplicates = bool(payload.get("skip_duplicates", False))
        raw_concurrency = payload.get("concurrency", 1)
        if (
            isinstance(raw_concurrency, bool)
            or not isinstance(raw_concurrency, int)
            or not (1 <= raw_concurrency <= 3)
        ):
            return jsonify({"error": "concurrency must be an integer between 1 and 3."}), 400

        raw_tracks = payload.get("tracks")
        track_metadata: list[dict[str, Any]] = []
        if isinstance(raw_tracks, list):
            for entry in raw_tracks:
                if isinstance(entry, dict):
                    track_metadata.append(
                        {
                            "index": entry.get("index"),
                            "id": entry.get("id"),
                            "title": entry.get("title"),
                            "url": entry.get("url"),
                        }
                    )

        audio_format: AudioFormat = cast(AudioFormat, raw_format)
        req = DownloadRequest(
            url=clean_url,
            dest_dir=dest_path,
            audio_format=audio_format,
            is_playlist=is_playlist_url(clean_url),
            quality=raw_quality,
            selected_tracks=selected_tracks,
            skip_duplicates=skip_duplicates,
            concurrency=raw_concurrency,
            track_metadata=tuple(track_metadata),
        )
        job = job_manager.submit_job(req)
        return jsonify({"job_id": job.job_id, "status": job.status}), 201

    @app.get("/api/jobs")
    def list_jobs() -> tuple[Response, int]:
        list_fn = getattr(job_manager, "list_jobs", None)
        jobs = list_fn() if callable(list_fn) else []
        return jsonify({"jobs": [j.to_dict() for j in jobs]}), 200

    @app.get("/api/jobs/<job_id>")
    def get_job(job_id: str) -> tuple[Response, int]:
        job = job_manager.get_job(job_id)
        if job is None:
            return jsonify({"error": "Job not found"}), 404
        return jsonify(job.to_dict()), 200

    @app.post("/api/jobs/<job_id>/retry")
    def retry_job(job_id: str) -> tuple[Response, int]:
        retry_fn = getattr(job_manager, "retry_job", None)
        if callable(retry_fn):
            try:
                retried = retry_fn(job_id)
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 409
            if retried is None:
                return jsonify({"error": "Job not found"}), 404
            return jsonify({"job_id": retried.job_id, "status": retried.status, "retried_from": job_id}), 201

        existing = job_manager.get_job(job_id)
        if existing is None:
            return jsonify({"error": "Job not found"}), 404
        retried = job_manager.submit_job(existing.request)
        return jsonify({"job_id": retried.job_id, "status": retried.status, "retried_from": job_id}), 201

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
        resolved_dest = job.request.dest_dir.resolve()
        resolved_audio = audio_path.resolve()
        if resolved_dest not in resolved_audio.parents and resolved_audio != resolved_dest:
            return jsonify({"error": "Audio file path escapes destination directory"}), 403
        if not resolved_audio.is_file():
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
    store_path = Path(
        os.environ.get("YT_MUSIC_DL_STATE_DB", str(Path.home() / ".yt_music_dl" / "state.db"))
    )
    store = SqlitePersistenceAdapter(store_path)
    max_workers = max(1, min(int(os.environ.get("YT_MUSIC_DL_MAX_WORKERS", "3")), 3))
    job_manager = JobManager(
        downloader=downloader,
        fs=fs,
        event_bus=event_bus,
        staging_root=staging_root,
        store=store,
        max_workers=max_workers,
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
