"""Versioned Team boundaries. Task responsibilities are not host permissions.

Legacy runs retain their original contract. V2 is selected by the application,
never by a tool argument supplied by a worker.
"""
from __future__ import annotations

from codeagent.worktrees.scopes import normalize_scopes, path_is_allowed


def collaborative(metadata) -> bool:
    return metadata.get("kernel_version") == 2


def validate_contract(metadata):
    for key in ("artifact_paths", "strict_write_scopes"):
        values = metadata.get(key, [])
        if not isinstance(values, list):
            raise ValueError(f"{key} must be a list of repository-relative paths")
        normalize_scopes(values)


def authorized_path(path, team_metadata, contract, planned_scopes):
    if not collaborative(team_metadata):
        return path_is_allowed(path, planned_scopes)
    # Never turn Git internals into an editable responsibility or an artifact.
    parts = path.replace("\\", "/").split("/")
    if any(p in {"", ".", ".."} or p.casefold() == ".git" for p in parts) or ":" in path:
        return False
    if path.startswith(("/", "\\")):
        return False
    if path_is_allowed(path, team_metadata.get("denied_paths", [])):
        return False
    for scopes in (team_metadata.get("authorized_write_scopes", []), contract.get("strict_write_scopes", [])):
        if scopes and not path_is_allowed(path, scopes):
            return False
    return True


def source_entries(entries, artifact_paths):
    """Exclude only declared, untracked output. Tracked source always participates."""
    return tuple((status, path) for status, path in entries
                 if status != "??" or not path_is_allowed(path, artifact_paths))
