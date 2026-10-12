"""Bounded, workspace-confined content snapshots of explicitly declared inputs."""
from __future__ import annotations

import hashlib
import json
import os
from itertools import islice
from pathlib import Path
import time

from codeagent.tools.workspace import WorkspaceGuard

# These generated directories are explicitly outside the snapshot policy.
EXCLUDED = {'.git', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache'}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def capture(workspace, inputs, *, cwd=None, max_files=5000, max_bytes=32 * 1024 * 1024):
    started = time.perf_counter()
    files, errors, directories = {}, [], {}
    scanned = 0
    try:
        guard = WorkspaceGuard(Path(workspace))
        base = guard.resolve(cwd or '.')
        seen = set()

        def visit(raw):
            nonlocal scanned
            # Reject links, including in-workspace links: do not claim complete traversal.
            if raw.is_symlink() or getattr(raw, 'is_junction', lambda: False)():
                errors.append('linked_input')
                return
            path = guard.ensure_within(raw)
            relative = path.relative_to(guard.root).as_posix()
            if path in seen:
                return
            seen.add(path)
            if len(seen) > max_files or scanned >= max_bytes:
                errors.append('snapshot_limit')
                return
            if path.name in EXCLUDED:
                return
            if path.name == '.env' or path.name.startswith('.env.') or path.suffix.lower() in {'.pem', '.key', '.pfx'}:
                errors.append('sensitive_input_excluded')
                return
            if not path.exists():
                files[relative] = {'missing': True}
                return
            if path.is_dir():
                children = sorted(islice(path.iterdir(), max_files + 1), key=lambda p: p.name)
                if len(children) > max_files:
                    errors.append('snapshot_limit')
                directories[relative] = [p.name for p in children if p.name not in EXCLUDED]
                for child in children:
                    if len(seen) > max_files or scanned >= max_bytes:
                        errors.append('snapshot_limit')
                        break
                    visit(child)
            elif path.is_file():
                before = path.stat()
                if before.st_size > max_bytes - scanned:
                    errors.append('snapshot_limit')
                    return
                h = hashlib.sha256()
                with path.open('rb') as stream:
                    while chunk := stream.read(min(65536, max_bytes - scanned + 1)):
                        scanned += len(chunk)
                        if scanned > max_bytes:
                            errors.append('snapshot_limit')
                            return
                        h.update(chunk)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    errors.append('input_changed_during_read')
                files[relative] = {'sha256': h.hexdigest(), 'bytes': after.st_size}
            else:
                errors.append('unsupported_input')

        if not inputs:
            errors.append('no_declared_inputs')
        for item in inputs:
            try:
                # Check every component before resolve follows any links.
                raw = Path(item)
                raw = raw if raw.is_absolute() else base / raw
                guard.ensure_within(raw)
                if any(p.is_symlink() or getattr(p, 'is_junction', lambda: False)()
                       for p in (raw, *raw.parents) if p != guard.root):
                    errors.append('linked_input')
                    continue
                visit(raw)
            except (OSError, ValueError):
                errors.append('input_unreadable_or_outside_workspace')
        # Detect membership changes during this scan, including adds/deletes.
        for relative, names in directories.items():
            if sorted(p.name for p in islice((guard.root / relative).iterdir(), max_files + 1) if p.name not in EXCLUDED) != names:
                errors.append('membership_changed_during_scan')
    except (OSError, ValueError):
        errors.append('snapshot_unavailable')
        base = Path(cwd or workspace)
    quality = 'partial' if errors else 'complete'
    body = {'cwd': os.path.normcase(str(base)), 'inputs': list(inputs), 'files': files,
            'directories': directories, 'excluded_directories': sorted(EXCLUDED)}
    return {**body, 'fingerprint': digest(body), 'quality': quality,
            'errors': sorted(set(errors)), 'scanned_bytes': scanned,
            'duration_ms': round((time.perf_counter() - started) * 1000, 3)}


def same_version(left, right):
    return (left.get('quality') == right.get('quality') == 'complete'
            and left.get('fingerprint') == right.get('fingerprint'))
