"""Persistent, bounded task orchestration over the existing Agent execution loop."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import threading

from codeagent.runtime.cancellation import CancelledError, ModelCallTimeout
from codeagent.worktrees.snapshots import exclusive_file
from codeagent.tasks.execution import ready_wave, validate_plan
from .executions import fingerprint, code_fingerprint
from .workspaces import Workspaces


class Coordinator:
    def __init__(self, repository, root, conversation, workspace, token, emit, worker, verify,
                 *, read_only=False, max_workers=3):
        self.repo, self.root, self.conversation = repository, Path(root), conversation
        self.workspace, self.token = Path(workspace).resolve(), token
        self.emit, self.worker, self.verify = emit, worker, verify
        self.read_only, self.max_workers = read_only, max(1, min(4, max_workers))
        self.write_lock = threading.RLock()

    def publish(self, identifier):
        value = self.repo.get(identifier)
        self.emit("collaboration.snapshot", self.repo.public(value))
        return self.repo.public(value)

    def start(self, objective, task_ids, *, mode="plan"):
        if not isinstance(objective, str) or not objective.strip():
            raise ValueError("Provide the overall objective")
        record = self.repo.create(self.conversation, self.workspace, objective, task_ids, read_only=self.read_only, mode=mode)
        self.publish(record["id"])
        return self.run(record["id"])

    def get(self, identifier):
        return self.repo.get(identifier, conversation=self.conversation, workspace=self.workspace)

    def close(self, identifier):
        record = self.get(identifier)
        if record.get("delivery") or record.get("runtime_unknown") or any(v.get("inflight") for v in record["records"].values()):
            raise ValueError("Resolve interrupted operations or local delivery before closing")
        if record["status"] in {"running", "verifying", "delivering"}:
            raise ValueError("Stop the active execution before closing")
        self.repo.change(identifier, lambda r: r.update(status="closed", issue=None))
        return self.publish(identifier)

    def run(self, identifier):
        self.get(identifier)
        root = self.root / identifier
        root.mkdir(parents=True, exist_ok=True)
        with exclusive_file(root / "execution.lock"):
            record = self.get(identifier)
            if record["status"] in {"completed", "closed"}:
                return self.publish(identifier)
            if self.read_only and any(t["profile"] in {"implement", "verify"} for t in record["tasks"]):
                raise ValueError("A read-only conversation cannot resume code execution")
            if record.get("runtime_unknown") or any(r.get("inflight", {}).get("effect") == "write" for r in record["records"].values()):
                raise ValueError("An interrupted write has unknown results; inspect its workspace before authorizing recovery")
            validate_plan(record["tasks"], read_only=self.read_only or record["read_only"])
            def begin(r):
                for task in r['tasks']:
                    own = r['records'][task['id']]
                    current_hash = fingerprint(task)
                    if own.get('input_hash') != current_hash:
                        if own['status'] == 'accepted':
                            if own.get('code_hash') != code_fingerprint(task):
                                raise ValueError('Verified task input changed; create a follow-up Task')
                            own.update(status='pending', verify_only=True, input_hash=current_hash)
                            continue
                        r['checkpoints'].pop(task['id'], None)
                        own.update(previous_attempts=own.get('previous_attempts', 0) + own.get('attempts', 0),
                                   attempts=0, input_hash=current_hash, code_hash=code_fingerprint(task), summary='')
                r["epoch"] += 1
                r["status"], r["issue"] = "running", None
                for value in r["records"].values():
                    if value["status"] != "accepted":
                        value["status"] = "pending"
            record = self.repo.change(identifier, begin)
            epoch = record["epoch"]
            self.publish(identifier)
            has_writes = any(t["profile"] in {"implement", "verify"} for t in record["tasks"])
            spaces = Workspaces(self.workspace, root, self.repo, identifier, self.token.raise_if_cancelled) if has_writes and record.get("mode", "plan") == "plan" else None
            try:
                if spaces:
                    spaces.prepare()
                while True:
                    self.token.raise_if_cancelled()
                    record = self.get(identifier)
                    if all(record["records"][t["id"]]["status"] == "accepted" for t in record["tasks"]):
                        break
                    wave = ready_wave(record["tasks"], record["records"], self.max_workers)
                    if not wave:
                        raise RuntimeError("No ready tasks; failed prerequisites require main Agent coordination")
                    # Worktree operations are serial; only Agent executions run concurrently.
                    locations = {t["id"]: spaces.worker_path(t) if spaces else self.workspace for t in wave}
                    with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="native-worker") as pool:
                        jobs = {pool.submit(self._execute, identifier, epoch, t, locations[t["id"]]): t for t in wave}
                        outcomes = {jobs[f]["id"]: f.result() for f in as_completed(jobs)}
                    self.token.raise_if_cancelled()
                    if any(not result for result in outcomes.values()):
                        raise RuntimeError("Member task needs coordination; inspect task evidence and revise or resume")
                    if spaces:
                        for task in wave:
                            spaces.merge_worker(task, locations[task["id"]])
                        self.repo.change(identifier, lambda r: r.update(status="verifying"), epoch=epoch)
                        self.publish(identifier)
                        before = spaces.freeze(record["workspaces"].get("shared") or spaces.prepare())
                        for task in wave:
                            if task["checks"]:
                                self.verify(Path(self.get(identifier)["workspaces"]["shared"]), task["checks"], identifier, task["id"])
                        after = spaces.freeze(self.get(identifier)["workspaces"]["shared"])
                        if spaces.git.text("rev-parse", before + "^{tree}") != spaces.git.text("rev-parse", after + "^{tree}"):
                            raise RuntimeError("Checks changed source files; review the actual diff before accepting this wave")
                        spaces.accept()
                    else:
                        for task in wave:
                            if task['checks']:
                                self.verify(self.workspace, task['checks'], identifier, task['id'])
                    def accept(r):
                        r["status"] = "running"
                        for task in wave:
                            r["records"][task["id"]]["status"] = "accepted"
                            r["records"][task["id"]]["reason"] = (
                                f"已通过 {len(task['checks'])} 项阶段检查" if task["checks"]
                                else "阶段成果已返回；本任务未声明独立执行检查")
                    self.repo.change(identifier, accept, epoch=epoch)
                    self.publish(identifier)
                if spaces:
                    self.repo.change(identifier, lambda r: r.update(status="verifying"), epoch=epoch)
                    self.publish(identifier)
                    checks = [c for t in record["tasks"] for c in t["checks"]]
                    if not checks:
                        raise RuntimeError("Code delivery needs executable acceptance checks; main Agent must use TaskUpdate to add checks to an unfinished verification task")
                    spaces.deliver(lambda path: self.verify(path, checks, identifier, "delivery"))
                self.repo.change(identifier, lambda r: r.update(status="completed", issue=None), epoch=epoch)
            except CancelledError:
                self.repo.change(identifier, lambda r: r.update(status="paused", issue=dict(owner="user", reason="执行已停止，工作现场已保留；可继续协作")), epoch=epoch)
                self.publish(identifier)
                raise
            except Exception as exc:
                self.repo.change(identifier, lambda r: r.update(status="blocked", issue=dict(owner="main", reason=str(exc))), epoch=epoch)
            return self.publish(identifier)

    def _execute(self, identifier, epoch, task, path):
        key = task["id"]
        def update(**fields):
            self.repo.change(identifier, lambda r: r["records"][key].update(fields), epoch=epoch)
            self.publish(identifier)
        record = self.get(identifier)
        if record["records"][key].get("verify_only"):
            update(status="reported", reason="保留已完成成果，重新执行修订后的检查")
            return True
        def claim(r):
            own = r["records"][key]
            total = sum(v.get("attempts", 0) + v.get("previous_attempts", 0) for v in r["records"].values())
            if own["attempts"] >= 3 or total >= 96:
                own.update(status="blocked", reason="已达到执行次数上限，请主 Agent 调整任务")
            else:
                own.update(status="running", attempts=own["attempts"] + 1, workspace=str(path), reason="")
        record = self.repo.change(identifier, claim, epoch=epoch)
        self.publish(identifier)
        if record["records"][key]["status"] == "blocked":
            return False
        def save(checkpoint):
            self.repo.change(identifier, lambda r: r["checkpoints"].update({key: checkpoint}), epoch=epoch)
        def operation(value):
            self.repo.change(identifier, lambda r: r["records"][key].update(inflight=value), epoch=epoch)
        try:
            result = self.worker(task, path, record["checkpoints"].get(key), save, operation,
                                 identifier, self.write_lock)
            self.token.raise_if_cancelled()
            if not result.get("ok"):
                update(status="failed", summary=result.get("summary", ""), reason=result.get("reason", "成员执行未完成"))
                return False
            update(status="reported", summary=result["summary"], reason="等待本批验证", inflight={})
            return True
        except ModelCallTimeout as exc:
            update(status="failed", reason="成员模型响应超时：" + str(exc))
            return False
        except CancelledError:
            update(status="paused", reason="工作现场已保留")
            if not self.token.is_cancelled:
                return False
            raise
        except Exception as exc:
            update(status="failed", reason=str(exc))
            return False
