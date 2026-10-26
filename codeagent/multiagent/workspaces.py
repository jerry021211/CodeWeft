"""One shared team checkout, optional isolated workers, journaled local delivery."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
from uuid import uuid4

from codeagent.worktrees.files import safe_path, file_state
from codeagent.worktrees.snapshots import LocalGit, exclusive_file
from codeagent.worktrees.errors import WorktreeError
from .leases import project_access
from .artifacts import cache_paths


class Workspaces:
    safe_path = staticmethod(safe_path)
    file_state = staticmethod(file_state)

    def __init__(self, source, root, repository, identifier, check):
        self.root = Path(root).resolve()
        self.git = LocalGit(Path(source), self.root)
        self.repo, self.id, self.check = repository, identifier, check

    def prepare(self):
        if self.repo.get(self.id)['workspaces'].get('shared'):
            return self._prepare()  # Existing isolated work needs no source-project lease.
        with project_access(self.git.source, self.check):
            return self._prepare()

    def _prepare(self):
        record = self.repo.get(self.id)
        if record["workspaces"].get("shared"):
            path = Path(record["workspaces"]["shared"])
            if not path.is_dir():
                raise WorktreeError("Team workspace is missing; retained results require recovery")
            return path
        self.check()
        self.git.ensure_repository()
        base = record["workspaces"].get("base")
        if not base:
            snapshot = self.git.snapshot()
            base = snapshot['commit']
            self.repo.change(self.id, lambda r: r.setdefault('owned_refs', {}).update({snapshot['snapshot_ref']: base}))
        path = self.root / "shared"
        # Persist intent before creating Git's checkout. A restart after creation
        # can adopt this exact clean checkout instead of colliding with its path.
        self.repo.change(self.id, lambda r: r["workspaces"].update(base=base, accepted=base))
        if path.exists():
            if self.git.text("rev-parse", "HEAD", cwd=path) != base or self.git.tree(path) != self.git.text("rev-parse", base + "^{tree}"):
                raise WorktreeError("Interrupted workspace creation has unexpected changes; preserve it for inspection")
        else:
            self.git.add_worktree(path, base)
        self.repo.change(self.id, lambda r: r["workspaces"].update(shared=str(path), base=base, accepted=base))
        return path

    def freeze(self, path, *, parent=None):
        record = self.repo.get(self.id)
        tree = self.source_tree(path)
        commit = self.git.commit(tree, [parent or record["workspaces"]["accepted"]], "CodeAgent collaboration checkpoint")
        # Keep unreferenced snapshots reachable across Git maintenance/restarts.
        ref = 'refs/codeagent/native/' + self.id + '/' + uuid4().hex
        self.git.run("update-ref", ref, commit)
        self.repo.change(self.id, lambda r: r.setdefault('owned_refs', {}).update({ref: commit}))
        return commit

    def source_tree(self, path):
        base = self.repo.get(self.id)['workspaces']['base']
        artifacts = cache_paths(self.git, Path(path), base)
        if artifacts:
            self.repo.change(self.id, lambda r: r.setdefault('artifacts', {}).update({str(path): artifacts}))
        return self.git.tree(Path(path), artifact_paths=artifacts)

    def worker_path(self, task):
        shared = self.prepare()
        if task["profile"] in {"research", "review"}:
            return Path(self.git.read_view(self.repo.get(self.id)["workspaces"]["accepted"]))
        if task["isolation"] != "isolated" and task["files"]:
            return shared
        key = "task:" + task["id"]
        record = self.repo.get(self.id)
        if key in record["workspaces"]:
            return Path(record["workspaces"][key])
        path = self.root / "isolated" / (task["id"] + "-" + uuid4().hex[:8])
        base = record["workspaces"]["accepted"]
        self.git.add_worktree(path, base)
        self.repo.change(self.id, lambda r: r["workspaces"].update({key: str(path), key + ":base": base}))
        return path

    def merge_worker(self, task, path):
        record = self.repo.get(self.id)
        shared = Path(record["workspaces"]["shared"])
        if Path(path) == shared or task["profile"] in {"research", "review"}:
            return
        current = self.freeze(shared)
        result = self.freeze(path, parent=record["workspaces"]["task:" + task["id"] + ":base"])
        merge = self.git.run("merge-tree", "--write-tree", current, result, check=False)
        if merge.returncode:
            raise WorktreeError("Isolated result conflicts with shared work; retained both workspaces: " + merge.stdout.decode("utf-8", "replace")[-4000:])
        tree = merge.stdout.decode().splitlines()[0]
        patch = self.git.run("diff", "--binary", current, tree).stdout
        if patch:
            self.check()
            self.git.run("apply", "--check", "-", cwd=shared, input=patch)
            self.git.run("apply", "-", cwd=shared, input=patch)

    def accept(self):
        record = self.repo.get(self.id)
        commit = self.freeze(record["workspaces"]["shared"])
        self.repo.change(self.id, lambda r: r["workspaces"].update(accepted=commit))
        return commit

    def changed(self, path):
        record = self.repo.get(self.id)
        commit = self.freeze(path)
        return [os.fsdecode(p) for p in self.git.run("diff", "--name-only", "-z", record["workspaces"]["base"], commit).stdout.split(b"\0") if p]

    def manifest(self, include=()):
        names = self.git.run("ls-files", "--cached", "--others", "--exclude-standard", "-z").stdout.split(b"\0")
        result = {}
        for name in {os.fsdecode(n) for n in names if n} | set(include):
            state = self.file_state(self.safe_path(self.git.source, name))
            if state is not None:
                result[name] = state
        return result

    def deliver(self, verify):
        """Validation and publication are separate; Git HEAD/index never move."""
        with project_access(self.git.source, self.check), exclusive_file(self.root.parent / "delivery.lock"):
            record = self.repo.get(self.id)
            # On resume, merge the actual local tree again. It can contain a
            # subset of the previous delivery plus new user edits. Reusing the
            # old manifest would permanently reject even unrelated new files.
            # Keep the old intent until its replacement has passed validation.
            self.check()
            head, index = self.git.text("rev-parse", "HEAD"), self.git.index_hash()
            before = self.manifest()
            local = self.git.commit(self.git.tree(), [record["workspaces"]["base"]], "CodeAgent local delivery input")
            merged = self.git.run("merge-tree", "--write-tree", local, record["workspaces"]["accepted"], check=False)
            if merged.returncode:
                raise WorktreeError("Local edits conflict with team result; both versions retained")
            commit = self.git.commit(merged.stdout.decode().splitlines()[0], [local, record["workspaces"]["accepted"]], "CodeAgent delivery preview")
            view = self.root / ("delivery-" + uuid4().hex[:8])
            self.git.add_worktree(view, commit)
            verify(view)
            if self.source_tree(view) != self.git.text("rev-parse", commit + "^{tree}"):
                raise WorktreeError("Verification changed deliverable source; inspect the retained preview")
            if before != self.manifest() or head != self.git.text("rev-parse", "HEAD") or index != self.git.index_hash():
                raise WorktreeError("Local files changed during verification; retry delivery")
            files = []
            backup = self.root / ("delivery-files-" + uuid4().hex[:8])
            backup.mkdir()
            for raw in self.git.run("diff", "--name-only", "--no-renames", "-z", local, commit).stdout.split(b"\0"):
                if not raw:
                    continue
                name = os.fsdecode(raw)
                target, source = self.safe_path(self.git.source, name), self.safe_path(view, name)
                old, new = self.file_state(target), self.file_state(source)
                if old is not None and new is not None:
                    # Git preserves executable intent, not all local permission bits.
                    new["mode"] = (old["mode"] & ~0o111) | (new["mode"] & 0o111)
                if old != before.get(name):
                    raise WorktreeError("Local file changed or untracked path would be overwritten: " + name)
                item = dict(path=name, before=old, after=new)
                for label, file, state in (("before", target, old), ("after", source, new)):
                    if state is not None:
                        saved = backup / (str(len(files)) + "-" + label)
                        saved.write_bytes(file.read_bytes())
                        item[label + "_file"] = str(saved)
                files.append(item)
            intent = dict(head=head, index=index, before=before, files=files)
            def publish_intent(r):
                if r.get('delivery'):
                    r.setdefault('delivery_history', []).append(dict(
                        disposition='superseded_after_revalidation', intent=r['delivery']))
                r.update(delivery=intent, status='delivering')
            self.repo.change(self.id, publish_intent)
            self.apply_delivery(intent)

    def apply_delivery(self, intent):
        expected = dict(intent["before"])
        for item in intent["files"]:
            state = self.file_state(self.safe_path(self.git.source, item["path"]))
            if state not in (item["before"], item["after"]):
                raise WorktreeError("Local file changed during delivery: " + item["path"])
            if state is None:
                expected.pop(item["path"], None)
            else:
                expected[item["path"]] = state
        if self.manifest(expected) != expected:
            raise WorktreeError("Local workspace changed since delivery validation")
        for item in sorted(intent["files"], key=lambda f: (f["after"] is not None, -len(f["path"]))):
            self.check()
            if self.git.text("rev-parse", "HEAD") != intent["head"] or self.git.index_hash() != intent["index"]:
                raise WorktreeError("Git branch or staging changed during delivery")
            target = self.safe_path(self.git.source, item["path"])
            state = self.file_state(target)
            if state == item["after"]:
                continue
            if state != item["before"]:
                raise WorktreeError("Local file changed during delivery")
            if item["after"] is None:
                target.unlink()
                continue
            saved = Path(item["after_file"])
            saved.resolve().relative_to(self.root)
            data = saved.read_bytes()
            if hashlib.sha256(data).hexdigest() != item["after"]["hash"]:
                raise WorktreeError("Delivery evidence changed")
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".codeagent-", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary, item["after"]["mode"])
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        final = dict(intent["before"])
        for item in intent["files"]:
            if item["after"] is None:
                final.pop(item["path"], None)
            else:
                final[item["path"]] = item["after"]
        if self.manifest(final) != final or self.git.text("rev-parse", "HEAD") != intent["head"] or self.git.index_hash() != intent["index"]:
            raise WorktreeError("Local state changed before delivery confirmation")
