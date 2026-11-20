"""Local filesystem adapter implementing FileSystemPort."""

from __future__ import annotations

from pathlib import Path
import shutil
import uuid

from yt_music_dl.validation import sanitize_filename


class LocalFileSystemAdapter:
    """Filesystem adapter backed by pathlib and shutil."""

    def default_music_dir(self) -> Path:
        """Return ~/Music or ~/Música if it exists, defaulting to ~/Music."""
        home = Path.home()
        for candidate_name in ("Music", "Música"):
            candidate = home / candidate_name
            if candidate.exists() and candidate.is_dir():
                return candidate
        return home / "Music"

    def resolve_dir(self, raw_path: str) -> Path:
        """Expand user home (~) and resolve the directory path."""
        expanded = Path(raw_path.strip()).expanduser()
        return expanded.resolve()

    def ensure_writable(self, path: Path) -> None:
        """Create path if needed and verify real write access with a probe file."""
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            raise NotADirectoryError(f"Path is not a directory: {path}")

        probe = path / f".yt_music_dl_probe_{uuid.uuid4().hex}"
        try:
            probe.write_bytes(b"ok")
        finally:
            if probe.exists():
                probe.unlink(missing_ok=True)

    def sanitize_name(self, name: str, max_len: int = 120) -> str:
        """Sanitize a file or folder name."""
        return sanitize_filename(name, max_len=max_len)

    def promote_file(
        self,
        src: Path,
        dest_dir: Path,
        relative_path: Path | None = None,
    ) -> Path:
        """Sanitize target path components and move src into dest_dir."""
        rel = relative_path if relative_path is not None else Path(src.name)
        sanitized_parts: list[str] = []
        for idx, part in enumerate(rel.parts):
            if idx == len(rel.parts) - 1:
                stem = self.sanitize_name(Path(part).stem)
                suffix = Path(part).suffix.lower()
                sanitized_parts.append(f"{stem}{suffix}")
            else:
                sanitized_parts.append(self.sanitize_name(part))

        target = dest_dir.joinpath(*sanitized_parts)
        resolved_dest = dest_dir.resolve()
        resolved_target = target.resolve()
        if resolved_dest not in resolved_target.parents and resolved_target != resolved_dest:
            raise ValueError(f"Refusing to promote file outside destination: {target}")

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(target))
        return target

    def cleanup_dir(self, path: Path) -> None:
        """Remove a staging directory tree safely."""
        shutil.rmtree(path, ignore_errors=True)
