"""Project-neutral validation plans and execution diagnostics."""
from __future__ import annotations

import json
import locale
import os
import re
import shlex


def validate_checks(plan):
    checks = plan.get("validation_checks", [])
    if not isinstance(checks, list):
        raise ValueError("validation_checks must be an array")
    task_ids = {str(t["task_id"]) for t in plan.get("tasks", []) if isinstance(t, dict) and "task_id" in t}
    seen = set()
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get("id"), str) or not check["id"].strip() or check["id"] in seen:
            raise ValueError("Validation checks require unique non-empty ids")
        seen.add(check["id"])
        if check.get("stage") not in {"integration", "delivery"}:
            raise ValueError("Validation stage must be integration or delivery")
        requires = check.get("requires_tasks", [])
        if not isinstance(requires, list) or any(str(t) not in task_ids for t in requires):
            raise ValueError("Validation prerequisites must reference planned task ids")
        steps = check.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError("Validation checks require non-empty steps")
        for step in steps:
            validate_step(step)
    return checks


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
