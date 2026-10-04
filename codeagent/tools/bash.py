"""Shell command execution tool."""

from __future__ import annotations

import hashlib
import math
import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
import codecs
from codeagent.context.output_archive import OutputArchive, log_page

from codeagent.runtime.cancellation import CancelledError
from codeagent.runtime.execution import ExecutionStopped
from codeagent.runtime_platform import RuntimePlatform, current_runtime_platform
from codeagent.tools.base import ToolDefinition, ToolOutput, parameter_error
from codeagent.tools.workspace import WorkspaceGuard, WorkspaceViolationError

_DANGEROUS_PATTERNS = [
    (r"\brm\s+(-\w*)?-r\w*\s+(/|~|\$HOME)", "recursive delete on home/root"),
    (r"\brm\s+(-\w*)?-rf\s", "force recursive delete"),
    (r"\bmkfs\b", "format filesystem"),
    (r"\bdd\s+.*of=/dev/", "raw disk write"),
    (r">\s*/dev/sd[a-z]", "overwrite block device"),
    (r"\bchmod\s+(-R\s+)?777\s+/", "chmod 777 on root"),
    (r":\(\)\s*\{.*:\|:.*\}", "fork bomb"),
    (r"\bcurl\b.*\|\s*(sudo\s+)?bash", "pipe curl to bash"),
    (r"\bwget\b.*\|\s*(sudo\s+)?bash", "pipe wget to bash"),
]

_TRACE_SUBPROCESS_ENV = "CODEAGENT_TRACE_SUBPROCESSES"
_TRACING_ENABLE_ENV_VARS = (
    "LANGSMITH_TRACING",
    "LANGCHAIN_TRACING_V2",
)


@dataclass(slots=True)
class BashTool:
    """Execute commands in the detected native shell with basic safety checks."""

    runtime_platform: RuntimePlatform = field(default_factory=current_runtime_platform)
    workspace_guard: WorkspaceGuard | None = None
    cancellation_check: Callable[[], None] | None = None
    max_timeout_seconds: float = 600
    output_root: Path = Path(".task_outputs/tool-results")
    definition: ToolDefinition = field(init=False)
    _cwd: Path | None = field(default=None, init=False, repr=False)
    _processes: dict[str, subprocess.Popen] = field(default_factory=dict, init=False, repr=False)
    _process_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _remaining_seconds: Callable[[], float] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not math.isfinite(self.max_timeout_seconds) or self.max_timeout_seconds <= 0:
            raise ValueError("max_timeout_seconds must be positive and finite")
        self.definition = ToolDefinition(
            name="bash",
            description=(
                f"在 {self.runtime_platform.operating_system} 上使用 {self.runtime_platform.shell_name} 执行命令。"
                "使用该 Shell 的语法。返回输出，非零退出码会明确标记；超时可能已有副作用，重试前核实。"
                "命令退出成功不代表功能验证完成。"
                "验证预期报错时，用测试框架或包装脚本断言具体退出码和错误信息；"
                "匹配才以 0 退出并输出测试通过，不匹配保留失败。不要无条件忽略错误。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": (
                            f"待执行的 {self.runtime_platform.shell_name} 命令"
                        ),
                    },
                    "timeout": {
                        "type": "integer",
                        "description": f"超时秒数，默认 120，上限 {self.max_timeout_seconds:g}。",
                    },
                },
                "required": ["command"],
            },
        )
        self._cwd = (
            self.workspace_guard.root
            if self.workspace_guard is not None
            else Path.cwd().resolve()
        )

    @property
    def cwd(self) -> Path:
        """Return this tool instance's current working directory."""

        return self._cwd or Path.cwd().resolve()

    def bind_runtime(
        self, *, cancellation_check: Callable[[], None], remaining_seconds: Callable[[], float],
    ) -> None:
        """Bind the current task budget without resetting outstanding processes."""
        self.cancellation_check = cancellation_check
        self._remaining_seconds = remaining_seconds

    def _check_runtime(self) -> float:
        if self.cancellation_check is not None:
            self.cancellation_check()
        remaining = self._remaining_seconds() if self._remaining_seconds is not None else math.inf
        if remaining <= 0:
            raise ExecutionStopped("budget_exceeded:active_time")
        return remaining

    def run(self, command: str, timeout: int = 120) -> str:
        self._check_runtime()
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            return parameter_error("timeout 必须是有限的正数秒数。", "bash:timeout:positive_finite")
        timeout = min(timeout, self.max_timeout_seconds)
        warning = _check_dangerous(command)
        if warning:
            return ToolOutput(
                f"Blocked: {warning}\n命令：{command}\n"
                "该操作被阻止；核对允许范围，不要改写命令绕过限制。",
                status="blocked",
            )

        cwd = self.cwd

        if self.workspace_guard is not None:
            try:
                self._validate_directory_changes(command, cwd)
            except WorkspaceViolationError as exc:
                return ToolOutput(f"Blocked: {exc}", status="blocked")

        # Exact command text matters: never normalize shell quoting, case or flags.
        key = hashlib.sha256((str(cwd) + "\0" + command).encode("utf-8")).hexdigest()
        started = time.monotonic()
        proc = None
        archive = OutputArchive(self.output_root, origin={'cwd': str(cwd)})
        readers = []
        collector_errors = []
        def drain(pipe, channel):
            decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
            try:
                while chunk := pipe.read(8192):
                    archive.append(decoder.decode(chunk), channel)
                archive.append(decoder.decode(b'', final=True), channel)
            except (OSError, ValueError) as exc:
                collector_errors.append(type(exc).__name__)
            finally:
                pipe.close()
        def finish(complete=True):
            for thread in readers:
                thread.join(timeout=1)
            archive.finish(complete=complete and not collector_errors and not any(t.is_alive() for t in readers))
        try:
            with self._process_lock:
                previous = self._processes.get(key)
                if previous is not None or len(self._processes) >= 16:
                    finish()
                    return ToolOutput('Blocked: previous process cleanup is unconfirmed; inspect existing execution before retrying.',
                                      status='blocked', outcome='process_pending',
                                      process_id=previous.pid if previous else None, process_running=True)
                self._check_runtime()
                proc = subprocess.Popen(
                    self.runtime_platform.command_argv(command), shell=False,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=str(cwd), env=_subprocess_environment(),
                    **({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}),
                )
                self._processes[key] = proc
            for pipe, channel in ((proc.stdout, 'stdout'), (proc.stderr, 'stderr')):
                thread = threading.Thread(target=drain, args=(pipe, channel), daemon=True)
                readers.append(thread)
                thread.start()
            deadline = started + timeout
            timed_out = False
            cleanup_unknown = False
            try:
                while True:
                    task_remaining = self._check_runtime()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(proc.args, timeout)
                    try:
                        proc.wait(timeout=min(.1, remaining, task_remaining))
                        break
                    except subprocess.TimeoutExpired:
                        pass
            except (subprocess.TimeoutExpired, CancelledError, ExecutionStopped, KeyboardInterrupt) as exc:
                stopped = self._stop_process(proc)
                cleanup_unknown = not stopped
                if stopped:
                    self._forget_process(key)
                finish(complete=stopped)
                if not isinstance(exc, subprocess.TimeoutExpired):
                    exc.process_id, exc.process_running = proc.pid, not stopped
                    exc.tool_output = log_page(archive.attach(ToolOutput('Cancelled after execution started.',
                        status='error', exit_code=proc.poll(), outcome='cancelled', process_id=proc.pid,
                        process_running=not stopped)))
                    raise
                timed_out = True
            else:
                self._forget_process(key)
                finish()
            if proc.returncode == 0 and not timed_out:
                self._update_cwd(command, cwd)
            result = ToolOutput('命令超时，可能已有副作用；核实已有执行和归档，不要自动重跑。' if timed_out else '', status='success' if proc.returncode == 0 and not timed_out else 'error',
                                exit_code=proc.returncode, outcome='timeout' if timed_out else 'success' if proc.returncode == 0 else 'diagnostic',
                                result_signature=_result_signature(archive.prefix if archive.saved_chars <= len(archive.prefix)
                                                                   else archive.prefix + '\n' + archive.tail),
                                process_id=proc.pid, process_running=cleanup_unknown or proc.poll() is None,
                                duration_seconds=time.monotonic() - started)
            return log_page(archive.attach(result))
        except (CancelledError, ExecutionStopped, KeyboardInterrupt):
            if not archive.finished:
                finish(False)
            raise
        except OSError as exc:
            stopped = proc is None or self._stop_process(proc)
            if stopped and proc is not None:
                self._forget_process(key)
            finish(False)
            result = ToolOutput(f'Error running command: {exc}', status='error',
                outcome='permission_denied' if isinstance(exc, PermissionError) else 'infrastructure_error',
                process_id=proc.pid if proc else None, process_running=not stopped,
                exit_code=proc.poll() if proc else None, duration_seconds=time.monotonic() - started)
            return log_page(archive.attach(result))
        finally:
            if not archive.finished:
                finish(False)

    def _forget_process(self, key: str) -> None:
        with self._process_lock:
            self._processes.pop(key, None)

    @staticmethod
    def _stop_process(proc: subprocess.Popen) -> bool:
        """Bounded cleanup of our process tree; False means unconfirmed cleanup.

        Windows taskkill cannot find children after their parent has exited.
        POSIX descendants that deliberately detach can also escape the group.
        Never fall back to killing processes by command name.
        """
        try:
            if os.name == "nt":
                if proc.poll() is not None:
                    return False
                cleanup = subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=2,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                if cleanup.returncode != 0:
                    # Still reap the owned parent if tree cleanup was refused.
                    proc.kill()
                    proc.wait(timeout=1)
                    return False
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            proc.wait(timeout=1)
            if os.name != "nt":
                try:
                    os.killpg(proc.pid, 0)
                except ProcessLookupError:
                    return True
                return False
            return True
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _validate_directory_changes(self, command: str, current_cwd: Path) -> None:
        """Reject explicit ``cd`` targets outside a guarded workspace pre-run."""

        assert self.workspace_guard is not None
        for target in _cd_targets(command):
            current_cwd = self.workspace_guard.resolve(target, base=current_cwd)

    def _update_cwd(self, command: str, current_cwd: Path) -> None:
        """Apply successful shell ``cd`` operations to this instance only."""

        for target in _cd_targets(command):
            if self.workspace_guard is not None:
                new_dir = self.workspace_guard.resolve(target, base=current_cwd)
            else:
                expanded = Path(os.path.expanduser(target))
                new_dir = (
                    expanded.resolve()
                    if expanded.is_absolute()
                    else (current_cwd / expanded).resolve()
                )
            if new_dir.is_dir():
                self._cwd = new_dir
                current_cwd = new_dir


def _result_signature(output: str) -> str:
    # Only known runner summaries: numbers inside diagnostics remain significant.
    output = re.sub(
        r"(?m)^(Ran \d+ tests? in )\d+(?:\.\d+)?s$",
        r"\1<duration>s", output,
    )
    output = re.sub(
        r"(?m)^(=+ .* in )\d+(?:\.\d+)?s( =+)$",
        r"\1<duration>s\2", output,
    )
    return hashlib.sha256(output.encode("utf-8")).hexdigest()


def _check_dangerous(command: str) -> str | None:
    for pattern, reason in _DANGEROUS_PATTERNS:
        if re.search(pattern, command):
            return reason
    return None


def _subprocess_environment() -> dict[str, str]:
    """Build a child environment without accidental orphan LangSmith traces.

    LangSmith's active parent run is process-local, while environment variables
    are inherited by shell grandchildren.  Disable tracing flags by default so
    Python commands launched through this tool do not create unrelated root
    traces. Child-process tracing remains an explicit opt-in; callers that need
    a nested hierarchy must also propagate LangSmith parent headers.
    """

    environment = os.environ.copy()
    if _env_enabled(environment.get(_TRACE_SUBPROCESS_ENV)):
        return environment
    for name in _TRACING_ENABLE_ENV_VARS:
        environment[name] = "false"
    return environment


def _env_enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _cd_targets(command: str) -> list[str]:
    """Extract simple persistent ``cd`` operations from a compound command."""

    targets: list[str] = []
    parts = re.split(r"(?:&&|;)", command)
    for part in parts:
        part = part.strip()
        match = re.match(r"^(?:cd|chdir)\s+(?:/d\s+)?(.+?)\s*$", part, re.IGNORECASE)
        if match is None:
            continue
        target = match.group(1).strip().strip("'\"")
        if not target:
            continue
        targets.append(target)
    return targets
