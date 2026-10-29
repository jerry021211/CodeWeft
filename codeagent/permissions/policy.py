"""Permission checks for tool execution."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codeagent.permissions.broker import PermissionBroker
from codeagent.runtime import CancellationToken
from codeagent.permissions.read_only import is_safe_read_only_command

APPROVAL_MODES = frozenset({"ask", "auto", "full"})
RISK_RULE_VERSION = 2


def validate_approval_mode(mode: str | None) -> str | None:
    if mode is not None and mode not in APPROVAL_MODES:
        raise ValueError("Unknown approval mode")
    return mode


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    allowed: bool
    reason: str = ""


AskUser = Callable[[str, dict[str, Any], str], bool]


def ask_user(tool_name: str, tool_input: dict[str, Any], reason: str) -> bool:
    print()
    print(f"Permission required: {reason}")
    print(f"Tool: {tool_name}({tool_input})")
    choice = input("Allow? [y/N] ").strip().lower()
    return choice in {"y", "yes"}


@dataclass(slots=True)
class PermissionPolicy:
    """Three-gate permission policy inspired by the reference s03 example."""

    workspace: Path = field(default_factory=Path.cwd)
    ask: AskUser = ask_user
    broker: PermissionBroker | None = None
    cancellation: CancellationToken | None = None
    approval_timeout: float | None = 600.0
    # None preserves the legacy CLI/SDK and historical-run behavior.
    approval_mode: str | None = None
    mcp_read_only: Callable[[str, dict[str, Any]], bool] | None = None
    on_decision: Callable[[dict[str, Any]], None] | None = None

    def __post_init__(self) -> None:
        validate_approval_mode(self.approval_mode)

    hard_deny_patterns: tuple[str, ...] = (
        # POSIX shell: destructive system operations.
        "rm -rf /",
        "rm -rf ~",
        "rm -rf $home",
        "sudo",
        "shutdown",
        "reboot",
        "mkfs",
        "dd if=",
        "> /dev/sda",
        "chmod 777 /",
        "chown -r ",
        # Windows cmd / PowerShell: destructive system operations.
        "format ",
        "diskpart",
        "bcdedit",
        "reg delete",
        "del /s /q c:\\",
        "erase /s /q c:\\",
        "rmdir /s /q c:\\",
        "rd /s /q c:\\",
        "remove-item -recurse -force c:\\",
        "remove-item -r -force c:\\",
    )
    destructive_command_patterns: tuple[str, ...] = (
        # POSIX shell.
        "rm ",
        "rmdir ",
        "mv ",
        "> /etc/",
        "chmod ",
        "chown ",
        "chmod 777",
        # Windows cmd.
        "del ",
        "erase ",
        "rmdir ",
        "rd ",
        "move ",
        "ren ",
        "rename ",
        # PowerShell.
        "remove-item",
        "move-item",
        "rename-item",
        "set-acl",
        "clear-content",
        # Cross-platform data destruction utilities.
        "truncate ",
        "shred ",
        "wipefs ",
    )
    write_tools: tuple[str, ...] = ("write_file", "edit_file")

    def check(self, tool_name: str, tool_input: dict[str, Any]) -> PermissionDecision:
        validate_approval_mode(self.approval_mode)
        if self.cancellation is not None:
            self.cancellation.raise_if_cancelled()
        # Hard boundaries precede all approval modes, including full access.
        if tool_name == "bash":
            command = str(tool_input.get("command", ""))
            reason = self._hard_deny_reason(command)
            if reason:
                return self._record(tool_name, "deny", "hard_deny", PermissionDecision(False, reason))
        if tool_name in self.write_tools:
            reason = self._workspace_write_reason(tool_input)
            if reason:
                return self._record(tool_name, "deny", "workspace_boundary", PermissionDecision(False, reason))

        reason, code = "", "ordinary_operation"
        if tool_name.startswith("mcp__"):
            reason, code = "External MCP tool call", "unknown_external_operation"
            if self.approval_mode == "auto":
                reason = "无法自动确认此 MCP 操作的影响，需要批准。"
            if self.approval_mode == "auto" and self.mcp_read_only is not None:
                if self.mcp_read_only(tool_name, tool_input):
                    reason, code = "", "verified_browser_read"
        elif tool_name == "bash":
            reason = self._destructive_command_reason(command)
            code = "destructive_command" if reason else "read_command"
            if not reason and self.approval_mode in {"ask", "auto"} and not is_safe_read_only_command(command):
                reason, code = "Command effects cannot be verified automatically", "unknown_command"
        elif tool_name == "web_search" and self.approval_mode == "ask":
            reason, code = "Internet search requires approval in this mode", "network_access"

        if self.approval_mode == "full":
            return self._record(tool_name, "allow", "full_access", PermissionDecision(True))
        if reason:
            self._record(tool_name, "ask", code, PermissionDecision(False, reason))
            return self._approval_decision(tool_name, tool_input, reason)
        return self._record(tool_name, "allow", code, PermissionDecision(True))

    def _record(self, name, action, code, result):
        if self.on_decision is not None:
            # No arguments or content: credentials must not enter decision logs.
            self.on_decision({"tool_name": name, "decision": action,
                              "approval_mode": self.approval_mode, "reason_code": code,
                              "rule_version": RISK_RULE_VERSION})
        return result

    def _approval_decision(self, tool_name: str, tool_input: dict[str, Any], reason: str) -> PermissionDecision:
        detailed_request = getattr(self.broker, "request_result", None)
        if callable(detailed_request):
            result = detailed_request(tool_name, tool_input, reason,
                cancellation=self.cancellation, timeout=self.approval_timeout)
            return PermissionDecision(result.allowed, result.reason)
        if self.broker is not None:
            allowed = self.broker.request(
                tool_name,
                tool_input,
                reason,
                cancellation=self.cancellation,
                timeout=self.approval_timeout,
            )
        else:
            allowed = self.ask(tool_name, tool_input, reason)
        return PermissionDecision(allowed, "" if allowed else "Permission denied by user")

    def _hard_deny_reason(self, command: str) -> str:
        normalized = command.casefold()
        for pattern in self.hard_deny_patterns:
            if pattern.casefold() in normalized:
                return f"Blocked: '{pattern}' is on the deny list"
        return ""

    def _destructive_command_reason(self, command: str) -> str:
        normalized = command.casefold()
        for pattern in self.destructive_command_patterns:
            if pattern.casefold() in normalized:
                return "Potentially destructive command"
        return ""

    def _workspace_write_reason(self, tool_input: dict[str, Any]) -> str:
        raw_path = tool_input.get("file_path") or tool_input.get("path") or ""
        if not raw_path:
            return ""

        target = Path(os.path.expanduser(str(raw_path)))
        if not target.is_absolute():
            target = self.workspace / target
        try:
            workspace = self.workspace.resolve()
            resolved = target.resolve()
            resolved.relative_to(workspace)
            # Resolve existing ancestors as well so a not-yet-created child of
            # an escaping symlink/junction cannot bypass the workspace check.
            ancestor = resolved if resolved.exists() else resolved.parent
            while not ancestor.exists() and ancestor != ancestor.parent:
                ancestor = ancestor.parent
            ancestor.resolve().relative_to(workspace)
        except (OSError, ValueError):
            return "Writing outside workspace"
        return ""
