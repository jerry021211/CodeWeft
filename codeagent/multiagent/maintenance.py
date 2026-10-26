"""Explicit archive/cleanup of terminal native execution resources.

No scheduled deletion, no project files removed, no guessed ownership of old refs.
"""
from datetime import datetime, UTC, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import zipfile

from codeagent.worktrees.snapshots import LocalGit, exclusive_file
from .leases import project_access


class ExecutionResources:
    def __init__(self, repository, root, identifier):
        if not re.fullmatch(r'collab_[0-9a-f]{32}', identifier):
            raise ValueError('Unsupported execution identifier for resource maintenance')
        self.repo, self.id = repository, identifier
        self.parent = Path(root).resolve()
        self.root = self.parent / identifier
        self._contained(self.root)

    def _contained(self, path):
        path = Path(path)
        path.resolve().relative_to(self.parent)
        cursor = path
        while cursor != self.parent:
            if cursor.is_symlink() or (hasattr(cursor, 'is_junction') and cursor.is_junction()):
                raise ValueError('Resource path contains a symlink/junction; preserve it for inspection')
            cursor = cursor.parent
        return path

    def _files(self):
        result = {}
        if not self.root.exists():
            return result
        self._contained(self.root)
        for directory, dirs, files in os.walk(self.root, followlinks=False):
            for name in dirs:
                self._contained(Path(directory) / name)
                if name == '.git':
                    raise ValueError('Embedded Git repository found; automatic cleanup is disabled')
            for name in files:
                path = self._contained(Path(directory) / name)
                if path == self.root / 'execution.lock':
                    continue
                if not path.is_file():
                    raise ValueError('Resource is not a regular file')
                result[path.relative_to(self.root).as_posix()] = path.stat().st_size
        return result

    def inspect(self):
        value = self.repo.get(self.id)
        files = self._files()
        terminal = value['status'] in {'completed', 'closed'}
        unknown = bool(value.get('runtime_unknown')) or any(r.get('inflight') for r in value['records'].values())
        archive = value.get('resource_archive')
        return dict(path=str(self.root), bytes=sum(files.values()), files=len(files),
            status=value['status'], can_archive=terminal and not unknown and not archive,
            archive=archive, retention_days=30,
            reason=('已归档，工作目录已释放' if archive else '执行已结束，可以归档后清理' if terminal and not unknown
                    else '未完成执行或未知操作仍需保留现场'))

    def archive(self, *, retention_days=0):
        if not isinstance(retention_days, int) or not 0 <= retention_days <= 36500:
            raise ValueError('Invalid retention days')
        with exclusive_file(self._contained(self.root / 'execution.lock')):
            state = self.inspect()
            if state['archive']:
                return state
            if not state['can_archive']:
                raise ValueError(state['reason'])
            value = self.repo.get(self.id)
            if retention_days and datetime.fromisoformat(value['updated_at']) > datetime.now(UTC) - timedelta(days=retention_days):
                raise ValueError('Execution is inside its retention period')
            source = Path(value['workspace']).resolve()
            if source == self.root or self.root in source.parents:
                raise ValueError('Cannot clean the original project workspace')
            def busy():
                raise ValueError('项目正被另一协作使用，请稍后再清理')
            with project_access(source, lambda: None, on_wait=busy):
                return self._archive(value)

    def _archive(self, value):
        files = self._files()  # Validate every path before Git or filesystem mutations.
        has_git = bool(value.get('workspaces', {}).get('base'))
        git = LocalGit(Path(value['workspace']), self.root) if has_git else None
        worktrees = []
        refs = value.get('owned_refs', {})
        if git:
            for part in git.run('worktree', 'list', '--porcelain', '-z').stdout.split(b'\0'):
                if part.startswith(b'worktree '):
                    path = Path(os.fsdecode(part[9:])).resolve()
                    if self.root in path.parents:
                        worktrees.append(self._contained(path))
            for ref, oid in refs.items():
                if not (ref.startswith('refs/codeagent/native/' + self.id + '/') or re.fullmatch(r'refs/codeagent/snapshots/[0-9a-f]{32}', ref)):
                    raise ValueError('Ref is outside the recorded execution namespace')
                actual = git.run('rev-parse', '--verify', ref, check=False)
                if actual.returncode == 0 and actual.stdout.decode().strip() != oid:
                    raise ValueError('Snapshot ref changed; preserve resources for inspection')
        # Archive outside the removed subtree. Keep user changes, ignored files
        # and snapshot history; exclude only the live execution lock.
        archive_dir = self._contained(self.parent / 'archives')
        archive_dir.mkdir(exist_ok=True)
        destination = archive_dir / (self.id + '.zip')
        fd, temp = tempfile.mkstemp(suffix='.zip', dir=archive_dir)
        os.close(fd)
        hashes = {}
        try:
            with zipfile.ZipFile(temp, 'w', compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                for name in files:
                    path = self._contained(self.root / name)
                    digest = hashlib.sha256()
                    with path.open('rb') as source, archive.open('files/' + name, 'w', force_zip64=True) as target:
                        while chunk := source.read(1024 * 1024):
                            digest.update(chunk)
                            target.write(chunk)
                    hashes[name] = digest.hexdigest()
                if git and refs:
                    present = [ref for ref in refs if git.run('show-ref', '--verify', '--quiet', ref, check=False).returncode == 0]
                    if present:
                        with tempfile.TemporaryDirectory(dir=archive_dir) as directory:
                            bundle = Path(directory) / 'snapshots.bundle'
                            git.run('bundle', 'create', str(bundle), *present)
                            archive.write(bundle, 'snapshots.bundle')
                archive.writestr('manifest.json', json.dumps(dict(execution_id=self.id, workspace=value['workspace'],
                    hashes=hashes, refs=refs, created_at=datetime.now(UTC).isoformat()), ensure_ascii=False))
            with zipfile.ZipFile(temp) as archive:
                if archive.testzip() is not None:
                    raise ValueError('Archive verification failed; original resources retained')
            def unchanged(name, expected):
                with self._contained(self.root / name).open('rb') as source:
                    return hashlib.file_digest(source, 'sha256').hexdigest() == expected
            if self._files() != files or not all(unchanged(name, digest) for name, digest in hashes.items()):
                raise ValueError('Files changed during archive; original resources retained')
            if destination.exists():
                # A prior interrupted cleanup archive may contain files already
                # removed from disk. Never overwrite its only remaining copy.
                destination = archive_dir / (self.id + '-' + Path(temp).stem + '.zip')
            os.replace(temp, destination)
            for path in sorted(worktrees, key=lambda p: len(p.parts), reverse=True):
                self._contained(path)
                git.run('worktree', 'remove', '--force', str(path))
            # All targets are resolved, validated descendants of this execution.
            for path in self.root.iterdir():
                if path.name == 'execution.lock':
                    continue
                self._contained(path)
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            if git:
                for ref, oid in refs.items():
                    if git.run('show-ref', '--verify', '--quiet', ref, check=False).returncode == 0:
                        git.run('update-ref', '-d', ref, oid)
            self.repo.change(self.id, lambda r: r.update(resource_archive=dict(path=str(destination),
                bytes=destination.stat().st_size, created_at=datetime.now(UTC).isoformat())))
            return self.inspect()
        finally:
            Path(temp).unlink(missing_ok=True)
