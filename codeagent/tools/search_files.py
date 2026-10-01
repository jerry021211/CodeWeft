"""Shared search scope and deterministic paging, without a persistent index."""

from __future__ import annotations

import fnmatch
import os
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from codeagent.tools.workspace import WorkspaceGuard

# Used for non-Git directories only. Git repositories use their own ignore rules.
_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", "dist", "build",
    ".reference", "_reference", "eval-results", "tmp", ".tmp", ".task_outputs",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".codeagent", ".transcripts",
}

PAGE_PROPERTIES = {
    "offset": {"type": "integer", "description": "跳过的匹配数，默认 0；继续时使用结果中的 next_offset，其他参数保持一致。"},
    "limit": {"type": "integer", "description": "每页结果数，1–1000。"},
    "include_ignored": {"type": "boolean", "description": "默认 false，遵守 Git 忽略规则；查旧副本、临时文件时设 true，仍不搜索 .git。"},
}


def validate_page(offset: int, limit: int) -> None:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer between 1 and 1000")


def validate_pattern(pattern: str) -> str:
    if not pattern or Path(pattern).is_absolute() or ".." in Path(pattern).parts:
        raise ValueError("Search patterns must be non-empty, relative, and contain no '..'")
    return pattern


def matches_path(relative: Path, pattern: str, *, recursive: bool = False) -> bool:
    """Component-wise glob matching: ** includes zero or more directories."""
    parts = tuple(os.path.normcase(p) for p in relative.parts)
    tokens = tuple(os.path.normcase(p) for p in Path(pattern).parts)
    if recursive:
        tokens = ("**",) + tokens

    @lru_cache(maxsize=None)
    def match(i: int, j: int) -> bool:
        if j == len(tokens):
            return i == len(parts)
        if tokens[j] == "**":
            return match(i, j + 1) or (i < len(parts) and match(i + 1, j))
        return i < len(parts) and fnmatch.fnmatchcase(parts[i], tokens[j]) and match(i + 1, j + 1)

    return match(0, 0)


@dataclass
class SearchFiles:
    paths: list[Path] = field(default_factory=list)
    directories: set[Path] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)
    incomplete: bool = False

    def warning(self, message: str) -> None:
        self.incomplete = True
        # Keep error reporting bounded even when many directories are inaccessible.
        if len(self.notes) < 5:
            self.notes.append(message)


def search_files(root: Path, guard: WorkspaceGuard | None, *, include_ignored: bool = False) -> SearchFiles:
    result = SearchFiles()
    candidates: list[Path] | None = None
    if not include_ignored:
        try:
            completed = subprocess.run(
                ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "."],
                cwd=root, capture_output=True, timeout=15, check=False,
            )
            if completed.returncode == 0:
                candidates = [root / os.fsdecode(name) for name in completed.stdout.split(b"\0") if name]
                if completed.stderr:
                    result.notes.append("Scope warning: Git reported warnings; some ignore configuration may be unavailable.")
        except (OSError, subprocess.TimeoutExpired):
            pass

    if candidates is None:
        candidates = []
        if not include_ignored:
            result.notes.append("Scope: fallback directory exclusions; Git inventory unavailable, .gitignore rules not applied.")

        def onerror(error: OSError) -> None:
            result.warning(f"Unreadable directory: {error.filename}")

        for directory, dirs, files in os.walk(root, topdown=True, followlinks=False, onerror=onerror):
            parent = Path(directory)
            kept = []
            for name in sorted(dirs):
                child = parent / name
                if name == ".git" or (not include_ignored and name in _SKIP_DIRS):
                    continue
                if guard is not None and not guard.allows(child):
                    continue
                if child.is_symlink() or getattr(child, "is_junction", lambda: False)():
                    result.warning(f"Directory link not traversed: {child}")
                    continue
                kept.append(name)
                result.directories.add(child)
            dirs[:] = kept
            candidates.extend(parent / name for name in files if name != ".git")

    for candidate in sorted(set(candidates), key=lambda p: (str(p).casefold(), str(p))):
        if ".git" in candidate.relative_to(root).parts:
            continue
        if guard is not None and not guard.allows(candidate):
            continue
        try:
            if not candidate.is_file():
                if candidate.is_dir():
                    result.warning(f"Nested repository/directory not searched: {candidate}; search its path explicitly.")
                continue  # Includes tracked files deleted from the working tree.
        except OSError:
            result.warning(f"Cannot inspect file: {candidate}")
            continue
        result.paths.append(candidate)
        for parent in candidate.parents:
            if parent == root:
                break
            result.directories.add(parent)
    return result


def page_footer(*, offset: int, count: int, has_more: bool, complete: bool) -> str:
    next_page = f", next_offset={offset + count}" if has_more else ""
    return (
        f"Search: returned={count}, has_more={str(has_more).lower()}{next_page}, "
        f"scan_complete={str(complete).lower()}."
    )
