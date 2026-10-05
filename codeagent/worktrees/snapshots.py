"""Local-only Git snapshots and process locks for managed Team integration.

Snapshots use a temporary index: neither the user's HEAD nor staging choices
are changed. No operation in this module contacts a remote.
Approved Team startup may initialize a missing repository with an empty commit.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from codeagent.worktrees.manager import WorktreeError


@contextmanager
def exclusive_file(path: Path):
    """An OS lock, released on process exit; never steal a live worker's lease."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise WorktreeError("Another runtime owns this workspace operation") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)


class LocalGit:
    def __init__(self, source: Path, root: Path):
        self.source = source.resolve()
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def run(self, *args: str, cwd: Path | None = None, env=None, input=None,
            check: bool = True) -> subprocess.CompletedProcess:
        environment = dict(os.environ)
        # Do not inherit a parent git command's index/repository selection.
        for name in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
            environment.pop(name, None)
        environment.update({
            "GIT_AUTHOR_NAME": "CodeAgent Runtime", "GIT_COMMITTER_NAME": "CodeAgent Runtime",
            "GIT_AUTHOR_EMAIL": "runtime@codeagent.invalid",
            "GIT_COMMITTER_EMAIL": "runtime@codeagent.invalid",
            "GIT_NO_REPLACE_OBJECTS": "1",
        })
        environment.update(env or {})
        result = subprocess.run(
            ["git", "-C", str(cwd or self.source), "-c", "core.hooksPath=" + str(self.root / "no-hooks"),
             "-c", "commit.gpgSign=false", "-c", "merge.autoStash=false", *args],
            input=input, capture_output=True, env=environment, timeout=120,
        )
        if check and result.returncode:
            raise WorktreeError(result.stderr.decode("utf-8", "replace").strip() or "Git operation failed")
        return result

    def text(self, *args: str, **kwargs) -> str:
        return self.run(*args, **kwargs).stdout.decode("utf-8", "replace").strip()

    def index_hash(self) -> str:
        path = Path(self.text("rev-parse", "--git-path", "index"))
        if not path.is_absolute():
            path = self.source / path
        return hashlib.sha256(path.read_bytes() if path.exists() else b"").hexdigest()

    def ensure_repository(self) -> str:
        """Prepare an approved Team's local baseline without staging user files."""
        with exclusive_file(self.root / "bootstrap.lock"):
            try:
                probe = self.run("rev-parse", "--show-toplevel", check=False)
            except FileNotFoundError as exc:
                raise WorktreeError("未找到 Git，请安装 Git 并重启服务后重试启动") from exc
            if probe.returncode:
                # Failed access to an existing/broken repo is not a missing repo.
                markers = [parent / ".git" for parent in (self.source, *self.source.parents)]
                if (any(path.exists() or path.is_symlink() for path in markers)
                        or self.run("rev-parse", "--git-dir", check=False).returncode == 0):
                    raise WorktreeError(probe.stderr.decode("utf-8", "replace").strip()
                                        or "现有 Git 仓库无法读取，请修复后重试")
                self.run("init", "--", str(self.source))
                probe = self.run("rev-parse", "--show-toplevel")
            if Path(os.fsdecode(probe.stdout).strip()).resolve() != self.source:
                raise WorktreeError("Agent Team source workspace must be the Git repository root")
            head = self.run("rev-parse", "--verify", "HEAD^{commit}", check=False)
            if head.returncode == 0:
                return head.stdout.decode().strip()
            branch = self.text("symbolic-ref", "-q", "HEAD")
            if not branch.startswith("refs/heads/"):
                raise WorktreeError("Git HEAD 未指向有效的本地分支")
            if self.run("show-ref", "--verify", "--quiet", branch, check=False).returncode != 1:
                raise WorktreeError("现有 Git HEAD 无法解析，不能自动替换，请修复后重试")
            # Plumbing avoids hooks, user identity setup, and the real index.
            tree = self.text("hash-object", "-t", "tree", "--stdin", "-w", input=b"")
            commit = self.commit(tree, [], "CodeAgent initial local baseline")
            self.run("update-ref", branch, commit, "0" * len(commit))
            return self.text("rev-parse", "--verify", "HEAD^{commit}")

    def tree(self, cwd: Path | None = None) -> str:
        """Capture tracked changes and non-ignored untracked files, including deletions."""
        with tempfile.TemporaryDirectory(prefix="index-", dir=self.root) as directory:
            env = {"GIT_INDEX_FILE": str(Path(directory) / "index")}
            self.run("read-tree", "HEAD", cwd=cwd, env=env)
            self.run("add", "-A", "--", ".", cwd=cwd, env=env)
            # Files explicitly staged with `git add -f` are tracked by the real
            # index even though HEAD does not yet know them and ignore rules do.
            ignored = self.run("ls-files", "--cached", "--ignored", "--exclude-standard", "-z", cwd=cwd).stdout
            paths = [name for name in ignored.split(b"\0") if name and
                     ((cwd or self.source) / os.fsdecode(name)).exists()]
            if paths:
                self.run("add", "-f", "--pathspec-from-file=-", "--pathspec-file-nul", cwd=cwd, env=env,
                         input=b"\0".join(paths) + b"\0")
            return self.text("write-tree", cwd=cwd, env=env)

    def commit(self, tree: str, parents: list[str], message: str) -> str:
        args = ["commit-tree", tree]
        for parent in parents:
            args.extend(["-p", parent])
        return self.text(*args, input=(message + "\n").encode())

    def snapshot(self, base: str = "HEAD") -> dict:
        head = self.text("rev-parse", "HEAD")
        if self.text("rev-parse", "--verify", base + "^{commit}") != head:
            raise WorktreeError("Managed Teams start from the current local HEAD and working files")
        index = self.index_hash()
        tree = self.tree()
        if self.tree() != tree or self.text("rev-parse", "HEAD") != head or self.index_hash() != index:
            raise WorktreeError("Local files or staging changed while capturing the Team snapshot; retry")
        commit = self.commit(tree, [head], "CodeAgent local workspace snapshot")
        ref = "refs/codeagent/snapshots/" + uuid4().hex
        self.run("update-ref", ref, commit)
        return {"commit": commit, "source_head": head, "snapshot_ref": ref,
                "source_dirty_at_creation": tree != self.text("rev-parse", head + "^{tree}")}

    def add_worktree(self, path: Path, commit: str, branch: str | None = None) -> None:
        path.resolve().relative_to(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        args = ["worktree", "add"]
        args.extend(["-b", branch] if branch else ["--detach"])
        self.run(*args, str(path), commit)

    def read_view(self, commit: str) -> str:
        canonical = self.text("rev-parse", "--verify", commit + "^{commit}")
        path = self.root / "views" / canonical
        with exclusive_file(self.root / "view.lock"):
            if not path.exists():
                self.add_worktree(path, canonical)
            if self.text("rev-parse", "HEAD", cwd=path) != canonical or self.tree(path) != self.text("rev-parse", canonical + "^{tree}"):
                raise WorktreeError("The pinned read-only Team snapshot was modified")
        return str(path)
