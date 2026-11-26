"""yt_music_dl: Local web application for downloading YouTube audio tracks and playlists."""

from __future__ import annotations

from pathlib import Path
import sys


def _ensure_local_venv_site_packages() -> None:
    """Expose workspace-local .venv site-packages when invoked from an external Python."""
    repo_root = Path(__file__).resolve().parents[2]
    venv_dir = repo_root / ".venv"
    if not venv_dir.is_dir():
        return

    ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
    candidates = [
        venv_dir / "Lib" / "site-packages",
        venv_dir / "lib" / ver / "site-packages",
        venv_dir / "lib64" / ver / "site-packages",
    ]
    for site_packages in candidates:
        if site_packages.is_dir():
            site_str = str(site_packages)
            if site_str not in sys.path:
                sys.path.append(site_str)


_ensure_local_venv_site_packages()

__version__ = "0.1.0"

__all__ = ["__version__"]
