"""Automatic local integration and unstaged delivery of managed Team results."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path, PurePosixPath
from uuid import uuid4

from codeagent.teams.candidates import CandidateService
from codeagent.worktrees.manager import WorktreeError
from codeagent.worktrees.snapshots import LocalGit, exclusive_file


class IntegrationService:
    def __init__(self, repository, worktrees, *, validation_timeout=600, stop_event=None):
        self.repository = repository
        self.worktrees = worktrees
        self.validation_timeout = validation_timeout
        self.stop_event = stop_event

    def process_team(self, team_id):
        manager = self.worktrees.for_team(team_id) if hasattr(self.worktrees, "for_team") else self.worktrees
        git = LocalGit(manager.source_workspace, manager.managed_root)
        with exclusive_file(git.root / team_id / "integration.lock"):
            team = self.repository.get_team_run(team_id)
            if team.integration_mode != "managed" or team.state.value != "running":
                return None
            operations = self.repository.list_team_integrations(team_id)
            if any(r["status"] == "recovery_required" for r in operations):
                return None
            active = next((r for r in operations if r["status"] in {"preparing", "validating", "publishing", "applying"}), None)
            if active:
                if active["status"] == "publishing":
                    return self._publish(git, active)
                if active["status"] == "applying":
                    return self._deliver_files(git, active)
                # A live operation holds the OS lock. Reaching here means it exited.
                return self.repository.update_team_integration(active["id"], status="interrupted",
                    error="Integration worker stopped before publishing; inspect retained trial and retry")
            candidate = next((c for c in self.repository.list_candidates(team_id)
                if c.status.value == "committed" and c.team_integrated_revision is None and not c.superseded_at
                and not any(r["candidate_id"] == c.id and r["status"] != "superseded" for r in operations)), None)
            if candidate:
                return self._integrate(git, manager, team, candidate)
            if self.repository.managed_delivery_ready(team_id) and not any(
                    r["kind"] == "delivery" and r["status"] != "superseded" for r in operations):
                return self._delivery(git, manager, team)
            return None

    def _commands(self, team, candidate=None):
        ids = {c.task_id for c in self.repository.list_candidates(team.id)
               if c.team_integrated_revision is not None and not c.superseded_at}
        if candidate:
            ids.add(candidate.task_id)
        commands = ["git diff --check HEAD^ HEAD"]
        commands.extend(team.metadata.get("mandatory_validation_commands", []))
        plan = self.repository.get_team_plan_revision(team.id, team.active_plan_revision).plan
        commands.extend(plan.get("integration_validation_commands", []))
        for resource in self.repository.list_task_resources(team.task_list_id):
            if resource.task.id in ids:
                commands.extend(resource.task.metadata.get("validation_commands", []))
        if any(not isinstance(c, str) or not c.strip() for c in commands):
            raise WorktreeError("Team validation commands must be non-empty strings")
        return list(dict.fromkeys(commands))

    def _begin(self, git, team, *, candidate=None):
        identifier = "integration_" + uuid4().hex
        path = git.root / team.id / "integration" / identifier
        return self.repository.begin_team_integration(team.id, identifier=identifier,
            kind="candidate" if candidate else "delivery", candidate_id=candidate.id if candidate else None,
            worktree_path=str(path), commands=self._commands(team, candidate))

    def _integrate(self, git, manager, team, candidate):
        op = self._begin(git, team, candidate=candidate)
        try:
            attempt = self.repository.get_task_attempt(candidate.attempt_id)
            if candidate.base_commit != attempt.attempt_base_commit:
                raise WorktreeError("Candidate baseline differs from its Attempt")
            valid_bases = {team.base_commit} | {r["trial_commit"] for r in self.repository.list_team_integrations(team.id) if r["status"] == "published"}
            if candidate.base_commit not in valid_bases:
                raise WorktreeError("Candidate does not descend from an authorized Team snapshot")
            if git.run("merge-base", "--is-ancestor", candidate.base_commit, candidate.commit_hash, check=False).returncode:
                raise WorktreeError("Candidate ancestry does not match its approved base")
            if not self._merge(git, op, op["parent_head"], candidate.commit_hash):
                return self.repository.get_team_integration(op["id"])
            op = self.repository.get_team_integration(op["id"])
            if not self._validate(git, manager, op):
                return self.repository.get_team_integration(op["id"])
            self.repository.update_team_integration(op["id"], status="publishing")
            return self._publish(git, self.repository.get_team_integration(op["id"]))
        except Exception as exc:
            return self._fail(op, exc)

    def _merge(self, git, op, base, candidate):
        path = Path(op["worktree_path"])
        git.add_worktree(path, base)
        result = git.run("merge", "--no-ff", "--no-edit", candidate, cwd=path, check=False)
        if result.returncode:
            error = (result.stdout + result.stderr).decode("utf-8", "replace")
            self.repository.update_team_integration(op["id"], status="conflicted", error=error)
            return False
        trial = git.text("rev-parse", "HEAD", cwd=path)
        # Empty/no-op candidates still have a validation record and a durable result.
        self.repository.update_team_integration(op["id"], trial_commit=trial, status="validating")
        return True

    def _validate(self, git, manager, op):
        path = Path(op["worktree_path"])
        runner = CandidateService(self.repository, manager, validation_timeout=self.validation_timeout)
        expected = git.text("rev-parse", op["trial_commit"] + "^{tree}")
        records = []
        for command in op["commands"]:
            status, exit_code, output, duration = runner._run_validation(path, command)
            log = manager.write_artifact(op["team_run_id"], f"{op['id']}-{len(records)}.log", output)
            records.append({"command": command, "status": status, "exit_code": exit_code,
                            "output_ref": log, "duration_ms": duration, "commit": op["trial_commit"]})
            self.repository.update_team_integration(op["id"], validations=records)
            if status != "passed":
                self.repository.update_team_integration(op["id"], status="recovery_required" if status == "timed_out" else "validation_failed",
                    error=f"Combined validation {status}: {command}")
                return False
            if git.text("rev-parse", "HEAD", cwd=path) != op["trial_commit"] or git.tree(path) != expected:
                self.repository.update_team_integration(op["id"], status="validation_failed",
                    error="Validation changed the frozen integration code; result was not published")
                return False
            self._require_current(op)
        return True

    def _require_current(self, op):
        if self.stop_event is not None and self.stop_event.is_set():
            raise WorktreeError("Team runtime is stopping")
        team = self.repository.get_team_run(op["team_run_id"])
        if (team.state.value != "running" or team.integration_head != op["parent_head"]
                or team.integration_revision != op["parent_revision"] or team.active_plan_revision != op["plan_revision"]):
            raise WorktreeError("Team was cancelled or changed while integration was running")

    def _publish(self, git, op):
        try:
            self._require_current(op)
            if len(op["validations"]) != len(op["commands"]) or any(v["status"] != "passed" or v["commit"] != op["trial_commit"] for v in op["validations"]):
                raise WorktreeError("Publish intent is missing successful validation")
            if git.tree(Path(op["worktree_path"])) != git.text("rev-parse", op["trial_commit"] + "^{tree}"):
                raise WorktreeError("Validated integration tree changed before publication")
            ref = f"refs/heads/codex/{op['team_run_id']}/integration"
            current = git.run("rev-parse", "--verify", ref, check=False)
            head = current.stdout.decode().strip() if current.returncode == 0 else None
            if head == op["trial_commit"]:
                pass  # Git completed before the previous process could confirm SQLite.
            elif head == op["parent_head"] or (head is None and op["parent_revision"] == 0):
                git.run("update-ref", ref, op["trial_commit"], head or "0" * len(op["parent_head"]))
            else:
                raise WorktreeError("Team integration ref changed outside the recorded operation")
            return self.repository.publish_team_integration(op["id"])
        except Exception as exc:
            # An unexpected ref/tree is not a transient database failure. Freeze
            # it instead of continuously trying to publish unrecognized state.
            self.repository.update_team_integration(op["id"],
                status="recovery_required" if isinstance(exc, WorktreeError) else None, error=str(exc))
            return self.repository.get_team_integration(op["id"])

    def _delivery(self, git, manager, team):
        op = self._begin(git, team)
        try:
            with exclusive_file(git.root / "delivery.lock"):
                head, index = git.text("rev-parse", "HEAD"), git.index_hash()
                before = self._manifest(git)
                local_tree = git.tree()
                if before != self._manifest(git) or head != git.text("rev-parse", "HEAD") or index != git.index_hash():
                    raise WorktreeError("Local files changed during delivery capture; retry")
                # Both sides descend from S, even when the user has made new commits.
                local = git.commit(local_tree, [team.base_commit], "CodeAgent delivery local input")
                if not self._merge(git, op, local, team.integration_head):
                    return self.repository.get_team_integration(op["id"])
                op = self.repository.get_team_integration(op["id"])
                if not self._validate(git, manager, op):
                    return self.repository.get_team_integration(op["id"])
                path = Path(op["worktree_path"])
                changed = git.run("diff", "--name-only", "--no-renames", "-z", local, op["trial_commit"]).stdout
                files = []
                backup = path.parent / (op["id"] + "-delivery")
                backup.mkdir()
                for raw in changed.split(b"\0"):
                    if not raw:
                        continue
                    name = raw.decode("utf-8", "surrogateescape")
                    target, result_file = self._safe_path(git.source, name), self._safe_path(path, name)
                    old = self._file_state(target)
                    new = self._file_state(result_file)
                    if old is not None and new is not None:
                        new["mode"] = (old["mode"] & ~0o111) | (new["mode"] & 0o111)
                    if name not in before and old is not None:
                        raise WorktreeError(f"Delivery would overwrite an ignored or untracked local path: {name}")
                    if old != before.get(name):
                        raise WorktreeError(f"Local file changed before delivery: {name}")
                    record = {"path": name, "before": old, "after": new}
                    for label, file, value in (("before", target, old), ("after", result_file, new)):
                        if value is not None:
                            saved = backup / f"{len(files)}-{label}"
                            saved.write_bytes(file.read_bytes())
                            record[label + "_file"] = str(saved)
                    files.append(record)
                result = {"source_head": head, "index_hash": index, "before_manifest": before,
                          "files": files, "source_tree": local_tree}
                if before != self._manifest(git) or head != git.text("rev-parse", "HEAD") or index != git.index_hash():
                    raise WorktreeError("Local files changed during combined validation; retry delivery")
                self._require_current(op)
                self.repository.update_team_integration(op["id"], status="applying", result=result)
                return self._apply_files(git, self.repository.get_team_integration(op["id"]))
        except Exception as exc:
            return self._fail(op, exc)

    def _deliver_files(self, git, op):
        try:
            with exclusive_file(git.root / "delivery.lock"):
                return self._apply_files(git, op)
        except Exception as exc:
            return self.repository.update_team_integration(op["id"], status="recovery_required", error=str(exc))

    def resume_delivery(self, team_id, identifier, *, reason="Inspected retained operation"):
        """Explicit recovery after inspection, still requiring exact before/after files."""
        manager = self.worktrees.for_team(team_id) if hasattr(self.worktrees, "for_team") else self.worktrees
        git = LocalGit(manager.source_workspace, manager.managed_root)
        with exclusive_file(git.root / team_id / "integration.lock"):
            op = self.repository.get_team_integration(identifier)
            if op["team_run_id"] != team_id or op["status"] != "recovery_required":
                raise WorktreeError("This operation does not need explicit recovery")
            self._require_current(op)
            if op["kind"] == "delivery" and "files" in op["result"]:
                self.repository.update_team_integration(identifier, status="applying", error=reason)
                return self._deliver_files(git, self.repository.get_team_integration(identifier))
            validated = bool(op["trial_commit"]) and len(op["validations"]) == len(op["commands"]) and all(
                v["status"] == "passed" and v["commit"] == op["trial_commit"] for v in op["validations"])
            if op["kind"] == "candidate" and validated:
                self.repository.update_team_integration(identifier, status="publishing", error=reason)
                return self._publish(git, self.repository.get_team_integration(identifier))
            # No publish/writeback intent exists. Retain this trial and permit a
            # fresh retry only after the user has inspected the stopped process.
            return self.repository.update_team_integration(identifier, status="interrupted", error=reason)

    def _apply_files(self, git, op):
        self._require_current(op)
        if (op["status"] != "applying" or len(op["commands"]) != len(op["validations"])
                or any(v["status"] != "passed" or v["commit"] != op["trial_commit"] for v in op["validations"])):
            raise WorktreeError("Local delivery does not have complete validation evidence")
        result = op["result"]
        if git.text("rev-parse", "HEAD") != result["source_head"] or git.index_hash() != result["index_hash"]:
            raise WorktreeError("Local branch or staging changed; delivery requires inspection")
        current = self._manifest(git, include=set(result["before_manifest"]) | {f["path"] for f in result["files"]})
        expected = dict(result["before_manifest"])
        # Recover only files whose exact before/after contents are known. Never
        # overwrite a user's edits made after a partial delivery.
        for file in result["files"]:
            name = file["path"]
            state = self._file_state(self._safe_path(git.source, name))
            if state != file["before"] and state != file["after"]:
                raise WorktreeError(f"Local file changed during interrupted delivery: {name}")
            if state is None:
                expected.pop(name, None)
            else:
                expected[name] = state
        if current != expected:
            raise WorktreeError("Local workspace changed since delivery validation")
        for file in sorted(result["files"], key=lambda f: (f["after"] is not None, -len(f["path"]))):
            self._require_current(op)
            target = self._safe_path(git.source, file["path"])
            state = self._file_state(target)
            if state == file["after"]:
                continue
            if state != file["before"] or git.index_hash() != result["index_hash"] or git.text("rev-parse", "HEAD") != result["source_head"]:
                raise WorktreeError("Local file or Git state changed during delivery")
            if file["after"] is None:
                target.unlink()
            else:
                saved = Path(file["after_file"])
                saved.resolve().relative_to(git.root)
                data = saved.read_bytes()
                if hashlib.sha256(data).hexdigest() != file["after"]["hash"]:
                    raise WorktreeError("Delivery artifact changed")
                target.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(prefix=".codeagent-", dir=target.parent)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.chmod(temporary, file["after"]["mode"])
                    os.replace(temporary, target)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
        final = dict(result["before_manifest"])
        for file in result["files"]:
            if file["after"] is None:
                final.pop(file["path"], None)
            else:
                final[file["path"]] = file["after"]
        if self._manifest(git, include=final) != final or git.index_hash() != result["index_hash"] or git.text("rev-parse", "HEAD") != result["source_head"]:
            raise WorktreeError("Local state changed before delivery confirmation")
        return self.repository.complete_team_delivery(op["id"])

    @staticmethod
    def _safe_path(root, name):
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

    @staticmethod
    def _file_state(path):
        if not path.exists():
            return None
        if not path.is_file() or path.is_symlink():
            raise WorktreeError(f"Delivery needs a regular file: {path}")
        mode = stat.S_IMODE(path.stat().st_mode)
        return {"hash": hashlib.sha256(path.read_bytes()).hexdigest(), "mode": mode}

    def _manifest(self, git, include=()):
        names = git.run("ls-files", "-z", "--cached", "--others", "--exclude-standard").stdout.split(b"\0")
        result = {}
        for raw in set(names) | {n.encode("utf-8", "surrogateescape") for n in include}:
            if raw:
                name = raw.decode("utf-8", "surrogateescape")
                state = self._file_state(self._safe_path(git.source, name))
                if state is not None:
                    result[name] = state
        return result

    def _fail(self, op, exc):
        current = self.repository.get_team_integration(op["id"])
        if current["status"] in {"published", "delivered"}:
            return current
        status = "recovery_required" if current["status"] == "applying" else "interrupted"
        if current["status"] == "publishing":
            status = "publishing"
        return self.repository.update_team_integration(op["id"], status=status, error=str(exc))
