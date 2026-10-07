"""Shared Team scope semantics for admission, execution, submission and recovery.

Scopes are repository-relative paths, optionally ending in /**. A bare path
covers itself and descendants for compatibility with directory scopes. Other
glob syntax is rejected; it must never accidentally broaden an approval.
"""
from __future__ import annotations

import os
from collections.abc import Sequence


def normalize_scope(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Write scopes must be strings")
    scope = value.replace("\\", "/").strip().rstrip("/")
    root = scope[:-3] if scope.endswith("/**") else scope
    _validate_relative_path(root)
    if any(c in root for c in "*?[]"):
        raise ValueError(f"Invalid write scope: {value}; use a relative path or directory/**")
    return scope


def _validate_relative_path(value: str) -> None:
    if (not value or value.startswith("/") or any(c in value for c in ":\x00")
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise ValueError(f"Expected a repository-relative path: {value}")


def _file_key(value: str) -> str:
    path = value.replace("\\", "/")
    _validate_relative_path(path)
    if any(c in path for c in "*?"):
        raise ValueError("Expected a concrete path, not a pattern")
    return path.casefold() if os.name == "nt" else path


def normalize_scopes(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(normalize_scope(value) for value in values))


def scope_root(value: str) -> str:
    scope = normalize_scope(value)
    root = scope[:-3] if scope.endswith("/**") else scope
    return root.casefold() if os.name == "nt" else root


def scope_is_within(scope: str, approved_scopes: Sequence[str]) -> bool:
    """Whether every path allowed by a child scope is covered by an approval."""
    try:
        child = scope_root(scope)
        return any(child == scope_root(parent) or child.startswith(scope_root(parent) + "/")
                   for parent in approved_scopes)
    except ValueError:
        return False


def path_is_allowed(path: str, scopes: Sequence[str]) -> bool:
    """Check a concrete relative file path; patterns are not file paths."""
    try:
        key = _file_key(path)
        return any(key == scope_root(scope) or key.startswith(scope_root(scope) + "/")
                   for scope in scopes)
    except ValueError:
        return False


def path_is_scope_parent(path: str, scopes: Sequence[str]) -> bool:
    """Permit creating an ancestor directory without granting writes outside scope."""
    try:
        parent = _file_key(path)
        return any(scope_root(scope).startswith(parent + "/") for scope in scopes)
    except ValueError:
        return False
