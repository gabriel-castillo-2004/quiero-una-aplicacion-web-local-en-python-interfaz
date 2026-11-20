"""Acceptance tests for local web UI serving and static assets (MOD-08 / MOD-1).

Verifies that:
- The empty "Static Web App" template is removed from the project root.
- The Python web server reads HOST (default 127.0.0.1) and PORT (default 8081).
- GET "/" returns HTTP 200 with the real music downloader UI (form, destination
  folder, download progress, and history).
- All referenced static assets (including styles.css, app.js, and favicon.svg)
  exist and return HTTP 200 with no 404 errors.
"""

from __future__ import annotations

from html.parser import HTMLParser
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urljoin, urlparse
import json
import os
import re
import socket
import subprocess
import sys
import time


REPO_ROOT = Path(__file__).resolve().parent.parent


class _AssetParser(HTMLParser):
    """Collect local static asset URLs referenced by HTML link/script/img tags."""

    def __init__(self) -> None:
        super().__init__()
        self.assets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        candidate: str | None = None
        if tag == "link" and attr_map.get("href"):
            candidate = attr_map["href"]
        elif tag in {"script", "img"} and attr_map.get("src"):
            candidate = attr_map["src"]

        if candidate and not candidate.startswith(("http://", "https://", "data:", "//", "#")):
            self.assets.append(candidate)


def _find_server_entrypoint() -> tuple[list[str], Path]:
    """Locate the Python web server entrypoint and return (cmd, source_path)."""
    candidates = [
        REPO_ROOT / "app.py",
        REPO_ROOT / "main.py",
        REPO_ROOT / "src" / "yt_music_dl" / "app.py",
    ]
    for path in candidates:
        if path.is_file():
            if path.parent.name == "yt_music_dl":
                return [sys.executable, "-m", "yt_music_dl.app"], path
            return [sys.executable, str(path)], path
    raise AssertionError(
        "No Python web server entrypoint found (expected app.py, main.py, or src/yt_music_dl/app.py)."
    )


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_mod_no_empty_static_web_app_template_in_root() -> None:
    root_index = REPO_ROOT / "index.html"
    if root_index.exists():
        content = root_index.read_text(encoding="utf-8", errors="replace")
        assert "Static Web App" not in content, (
            "Root index.html still contains the empty 'Static Web App' template."
        )


def test_mod_server_reads_host_and_port_env_defaults() -> None:
    _, entrypoint_path = _find_server_entrypoint()
    source = entrypoint_path.read_text(encoding="utf-8")

    assert re.search(r"""os\.(?:environ\.get|getenv)\(\s*["']HOST["']\s*,\s*["']127\.0\.0\.1["']\s*\)""", source), (
        f"{entrypoint_path} must read HOST from environment with default '127.0.0.1'."
    )
    assert re.search(r"""os\.(?:environ\.get|getenv)\(\s*["']PORT["']\s*,\s*["']?8081["']?\s*\)""", source), (
        f"{entrypoint_path} must read PORT from environment with default 8081."
    )


def test_mod_live_server_serves_real_ui_and_static_assets_without_404() -> None:
    cmd, _ = _find_server_entrypoint()
    port = _get_free_port()

    env = os.environ.copy()
    env["HOST"] = "127.0.0.1"
    env["PORT"] = str(port)
    src_dir = REPO_ROOT / "src"
    if src_dir.is_dir():
        existing_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{src_dir}{os.pathsep}{existing_pp}" if existing_pp else str(src_dir)

    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        html_body = ""
        content_type = ""
        deadline = time.monotonic() + 8.0
        last_err: Exception | None = None

        while time.monotonic() < deadline:
            if proc.poll() is not None:
                stdout, stderr = proc.communicate(timeout=2)
                raise AssertionError(
                    f"Server exited prematurely with code {proc.returncode}.\n"
                    f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
                )
            try:
                conn = HTTPConnection("127.0.0.1", port, timeout=2)
                conn.request("GET", "/")
                resp = conn.getresponse()
                html_body = resp.read().decode("utf-8", errors="replace")
                content_type = resp.getheader("Content-Type") or ""
                conn.close()
                assert resp.status == 200, f"GET / returned HTTP {resp.status}"
                break
            except (OSError, ConnectionError) as exc:
                last_err = exc
                time.sleep(0.15)
        else:
            raise AssertionError(f"Server did not become ready on 127.0.0.1:{port}: {last_err}")

        assert "text/html" in content_type.lower(), f"Unexpected Content-Type for '/': {content_type}"
        assert "Static Web App" not in html_body, "GET / still serves the empty 'Static Web App' template."

        lower_html = html_body.lower()
        assert "<form" in lower_html, "Real UI must include a download form."
        assert "youtube" in lower_html, "Real UI must reference YouTube URL/playlist input."
        assert "dest" in lower_html or "carpeta" in lower_html, (
            "Real UI must include destination folder input."
        )
        assert "progress" in lower_html or "progreso" in lower_html, (
            "Real UI must include download progress section."
        )
        assert "historial" in lower_html or "history" in lower_html, (
            "Real UI must include download history section."
        )

        parser = _AssetParser()
        parser.feed(html_body)
        referenced = parser.assets

        assert any("styles.css" in asset for asset in referenced), (
            f"HTML must reference styles.css; found assets: {referenced}"
        )
        assert any("app.js" in asset for asset in referenced), (
            f"HTML must reference app.js; found assets: {referenced}"
        )
        assert any("favicon.svg" in asset for asset in referenced), (
            f"HTML must reference favicon.svg; found assets: {referenced}"
        )

        for asset in referenced:
            asset_path = urlparse(urljoin("/", asset)).path
            conn = HTTPConnection("127.0.0.1", port, timeout=3)
            conn.request("GET", asset_path)
            resp = conn.getresponse()
            data = resp.read()
            conn.close()
            assert resp.status == 200, f"Static asset {asset_path} returned HTTP {resp.status} (expected 200)"
            assert len(data) > 0, f"Static asset {asset_path} returned empty body"

        conn = HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("GET", "/api/health")
        health_resp = conn.getresponse()
        health_body = health_resp.read().decode("utf-8", errors="replace")
        conn.close()
        assert health_resp.status in {200, 503}, f"/api/health returned HTTP {health_resp.status}"
        parsed_health = json.loads(health_body)
        assert "status" in parsed_health
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
