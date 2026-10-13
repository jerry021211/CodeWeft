"""Guarded local delivery paths and file fingerprints."""
import hashlib
import stat
from pathlib import PurePosixPath
from .errors import WorktreeError


def safe_path(root, name):
    parts = PurePosixPath(name).parts
    if not parts or PurePosixPath(name).is_absolute() or any(p in {".", ".."} or p.rstrip(" .").casefold() == ".git" or ":" in p or "\\" in p for p in parts):
        raise WorktreeError("Invalid delivery path")
    target = root.joinpath(*parts)
    cursor = root
    for part in parts:
        cursor = cursor / part
        if cursor.is_symlink() or (hasattr(cursor, "is_junction") and cursor.is_junction()):
            raise WorktreeError(f"Delivery through a symlink/junction requires resolution: {name}")
    target.resolve().relative_to(root.resolve())
    return target

def file_state(path):
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink():
        raise WorktreeError(f"Delivery needs a regular file: {path}")
    mode = stat.S_IMODE(path.stat().st_mode)
    return {"hash": hashlib.sha256(path.read_bytes()).hexdigest(), "mode": mode}
