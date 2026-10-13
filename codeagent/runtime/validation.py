"""Project-neutral validation plans and execution diagnostics."""
from __future__ import annotations

import json
import locale
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from codeagent.runtime_platform import current_runtime_platform
from codeagent.runtime.cancellation import CancellationToken


class ValidationCancellation(CancellationToken):
    """Permission waits observe the same durable stop boundary as subprocesses."""
    def __init__(self, stopped):
        super().__init__()
        self.stopped = stopped

    def raise_if_cancelled(self):
        if self.stopped():
            self.cancel("Team validation stopped")
        super().raise_if_cancelled()


def permission_input(cwd, command):
    return {"command": command if isinstance(command, str) else shlex.join(command["argv"]),
            "cwd": str(cwd if isinstance(command, str) else cwd / command.get("cwd", ".")),
            "validation_step": command}


def empty_test_run(command, output):
    """Recognize explicit zero-test summaries; do not invent counts for custom tools."""
    if not re.search(r"\b(?:test|tests|pytest|unittest|jest|vitest|playwright)\b", command, re.I):
        return False
    return bool(re.search(r"(?im)^(?:#|ℹ)\s*tests\s+0\s*$|^Ran 0 tests\b|^\s*collected 0 items\b|^\s*Test Suites:\s*0\b", output))


def validate_step(step):
    if isinstance(step, str) and step.strip():
        return
    if not isinstance(step, dict):
        raise ValueError("Validation step must be a command string or an argv object")
    argv = step.get("argv")
    if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or not a or "\x00" in a for a in argv):
        raise ValueError("Validation argv must contain non-empty strings")
    cwd = step.get("cwd", ".")
    if not isinstance(cwd, str) or not cwd or cwd.startswith(("/", "\\")) or ":" in cwd or ".." in cwd.replace("\\", "/").split("/"):
        raise ValueError("Validation cwd must stay within the worktree")
    if set(step) - {"argv", "cwd"}:
        raise ValueError("Unsupported validation step fields; use argv and cwd")


def command_label(step):
    return step if isinstance(step, str) else json.dumps(step, ensure_ascii=False)


def portable_chain(command):
    """Recognize only simple executable && executable chains, never rewrite scripts.

    Complex shell syntax stays with its declared host shell. No substitution,
    redirects or pipelines are translated. Quoted arguments keep their grouping.
    """
    if "&&" not in command or any(c in command for c in "$`|;<>\n\r()%!"):
        return None
    lex = shlex.shlex(command, posix=False, punctuation_chars="&")
    lex.whitespace_split = True
    lex.commenters = ""
    lex.escape = ""  # retain Windows path separators
    try:
        tokens = list(lex)
    except ValueError:
        return None
    groups = [[]]
    for token in tokens:
        quoted = len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'"
        if quoted:
            token = token[1:-1]
        if token == "&&" and not quoted:
            if not groups[-1]:
                return None
            groups.append([])
        elif any(c in token for c in "&*?"):
            return None
        else:
            groups[-1].append(token)
    if len(groups) < 2 or not groups[-1]:
        return None
    # Shell builtins and assignments depend on interpreter/session semantics.
    if any("=" in g[0] or g[0].lower() in {"cd", "set", "export", "call", "exit", "if", "echo"} for g in groups):
        return None
    return groups


def decode_output(data):
    for encoding in ("utf-8-sig", locale.getpreferredencoding(False), "gb18030" if os.name == "nt" else "utf-8"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", "replace")


def diagnose(status, output):
    lower = output.lower()
    if status == "timed_out":
        return {"category": "timeout", "owner": "runtime", "summary": "验证超时，需确认进程已停止后继续", "retryable": False}
    if any(x in lower for x in ("parsererror", "invalidendofline", "commandnotfound", "not recognized", "filenotfounderror: validation")):
        return {"category": "environment", "owner": "lead", "summary": "验证环境或命令无法启动；不能据此判定业务代码失败", "retryable": False}
    if re.search(r"\b(?:econnreset|etimedout|eai_again|enotfound)\b", lower):
        return {"category": "dependency_install", "owner": "lead", "summary": "依赖下载或网络失败；检查环境后再重试", "retryable": False}
    return {"category": "check_failed", "owner": "lead", "summary": "检查未通过，Lead 需根据日志定位并安排修复", "retryable": False}


class ValidationRunner:
    """Cancellable checks shared by native execution and legacy integrations."""

    def __init__(self, *, timeout=600, stopped=lambda: False):
        self.validation_timeout = max(1, int(timeout))
        self.stopped = stopped
        self.validation_interrupted = False
        self.validation_cleanup_unknown = False

    def run(
        self, cwd: Path, command: str | dict[str, Any]
    ) -> tuple[str, int, str, int]:
        started = time.monotonic()
        outputs = []
        self.validation_interrupted = False
        self.validation_cleanup_unknown = False
        try:
            validate_step(command)
            chain = portable_chain(command) if isinstance(command, str) else None
            steps = [{"argv": argv} for argv in chain] if chain else [command]
            for step in steps:
                step_cwd = cwd
                if isinstance(step, dict):
                    step_cwd = (cwd / step.get("cwd", ".")).resolve()
                    step_cwd.relative_to(cwd.resolve())
                    if not step_cwd.is_dir():
                        raise FileNotFoundError(f"Validation working directory does not exist: {step_cwd}")
                    argv = list(step["argv"])
                    executable = shutil.which(argv[0])
                    if executable is None:
                        raise FileNotFoundError(f"Validation executable not found: {argv[0]}")
                    argv[0] = executable
                    if os.name == "nt" and executable.lower().endswith((".cmd", ".bat")):
                        if any(any(c in arg for c in '&|<>^%!\r\n') for arg in argv):
                            raise ValueError("Batch validation arguments contain shell metacharacters; use an explicit script")
                        argv = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", subprocess.list2cmdline(argv)]
                else:
                    argv = (["git", "-C", str(cwd), "diff", "--check", "HEAD"]
                            if step == "git diff --check HEAD" else current_runtime_platform().command_argv(step))
                remaining = self.validation_timeout - (time.monotonic() - started)
                if remaining <= 0:
                    return "timed_out", 124, "\n".join(outputs) + "\nValidation time budget exhausted before next step", round((time.monotonic() - started) * 1000)
                status, code, output, _ = self._run_validation_process(step_cwd, argv, timeout=remaining)
                if status == "passed" and empty_test_run(command_label(step), output):
                    status, code = "failed", 1
                    output += "\nNo tests executed; this is not successful test evidence."
                outputs.append(f"$ {command_label(step)}\n{output}")
                if status != "passed":
                    return status, code, "\n".join(outputs), round((time.monotonic() - started) * 1000)
            return "passed", 0, "\n".join(outputs), round((time.monotonic() - started) * 1000)
        except (OSError, ValueError) as exc:
            return "failed", 1, "\n".join([*outputs, f"{type(exc).__name__}: {exc}"]), round((time.monotonic() - started) * 1000)

    def _validation_stopped(self):
        return bool(self.stopped())

    def _run_validation_process(self, cwd, argv, *, timeout=None):
        started = time.monotonic()
        if self._validation_stopped():
            self.validation_interrupted = True
            return "failed", 130, "Runtime validation interrupted: execution_stopped; process not started", 0
        from codeagent.tools.bash import BashTool
        options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        # Files avoid waiting forever for inherited stdout pipes when a timed
        # out shell leaves children behind. Cleanup is bounded and PID-scoped.
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd),
                stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                **options,
            )
            timed_out = False
            interrupted = False
            deadline = started + (self.validation_timeout if timeout is None else timeout)
            try:
                while proc.poll() is None:
                    if self._validation_stopped():
                        interrupted = True
                        cleaned = BashTool._stop_process(proc)
                        if not cleaned:
                            timed_out = True
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        cleaned = BashTool._stop_process(proc)
                        break
                    try:
                        proc.wait(timeout=min(0.2, remaining))
                    except subprocess.TimeoutExpired:
                        continue
            except BaseException:
                BashTool._stop_process(proc)
                raise
            stdout.seek(0)
            stderr.seek(0)
            output = decode_output(stdout.read())
            errors = decode_output(stderr.read())
            if errors:
                output += f"\n[stderr]\n{errors}"
            if interrupted and not timed_out:
                self.validation_interrupted = True
                output += "\nRuntime validation interrupted: execution_stopped; process cleanup confirmed=True"
            if timed_out:
                self.validation_cleanup_unknown = not cleaned
                output += f"\nTimed out after {self.validation_timeout}s; process cleanup confirmed={cleaned}"
        return (
            "timed_out" if timed_out else "failed" if interrupted else "passed" if proc.returncode == 0 else "failed",
            124 if timed_out else proc.returncode,
            output or "(no output)",
            round((time.monotonic() - started) * 1000),
        )
