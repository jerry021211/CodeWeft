"""Context compaction configuration and runtime state."""

from __future__ import annotations

import json
import math
import warnings
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from codeagent.messages import ToolUse
from codeagent.tools.base import normalize_tool_output
from codeagent.tools.output_limits import (READ_SOURCE_LINES, BATCH_CHARS, DEFAULT_BODY_CHARS,
    BASH_BODY_CHARS, SUMMARY_CHARS, SUMMARY_OUTPUT_TOKENS, SUMMARY_TIMEOUT_SECONDS, COMPACT_RATIO)

_LIMIT_DEPRECATION_WARNED = False


def warn_retired_limits():
    global _LIMIT_DEPRECATION_WARNED
    if not _LIMIT_DEPRECATION_WARNED:
        warnings.warn('Tool/summary output limits are fixed code constants; retired quota overrides are ignored.',
                      DeprecationWarning, stacklevel=2)
        _LIMIT_DEPRECATION_WARNED = True


@dataclass(slots=True)
class ContextConfig:
    mode: str = "model"
    summarization_model: str = ""
    summarization_api_key: str | None = None
    tool_result_budget_chars: int = BATCH_CHARS
    single_tool_output_max_chars: int = DEFAULT_BODY_CHARS
    # Legacy configuration only; automatic compaction uses the model token window.
    compact_threshold_chars: int = 300_000
    summary_max_chars: int = SUMMARY_CHARS
    # Independent from the final character limit; includes reasoning where the
    # provider counts it against completion tokens.
    summary_max_tokens: int = SUMMARY_OUTPUT_TOKENS
    # Deprecated compatibility fields; fixed code constants always take priority.
    command_output_max_chars: int = BASH_BODY_CHARS
    read_reference_enabled: bool = True
    transcript_dir: Path = Path(".transcripts")
    tool_output_dir: Path = Path(".task_outputs/tool-results")
    reactive_retries: int = 1
    persisted_preview_chars: int = 0
    recency_messages: int = 12
    recency_rounds: int = 2
    min_fold_messages: int = 4
    # Accepted for older SDK/env configurations; counts no longer trigger summaries.
    message_trigger_min_fold: int = 16
    round_trigger_min_fold: int = 8
    max_fold_messages: int = 200
    max_fold_rounds: int = 12
    summary_input_max_chars: int = 0
    # Legacy SDK field, ignored: request admission uses the model token window.
    max_request_chars: int = 0
    context_window_tokens: int = 0
    summary_context_window_tokens: int = 0
    failure_cooldown_seconds: float = 90.0
    tool_projection_enabled: bool = True
    investigation_keep_rounds: int = 2
    command_keep_rounds: int = 1
    write_keep_rounds: int = 2
    tool_clear_min_chars: int = 8_000
    write_clear_min_chars: int = 8_000
    summary_timeout_seconds: float = SUMMARY_TIMEOUT_SECONDS
    summary_text_preview_chars: int = 0
    summary_argument_preview_chars: int = 0
    model_context_windows: dict[str, int] = field(default_factory=dict)
    near_context_ratio: float = COMPACT_RATIO
    cache_policy: str = "auto"
    cache_soft_ratio: float = COMPACT_RATIO
    cache_boundary_growth_ratio: float = 0.1

    def __post_init__(self) -> None:
        # Compatibility keys are accepted but cannot override code-owned limits.
        fixed = dict(tool_result_budget_chars=BATCH_CHARS, single_tool_output_max_chars=DEFAULT_BODY_CHARS,
                     command_output_max_chars=BASH_BODY_CHARS, persisted_preview_chars=0,
                     summary_max_chars=SUMMARY_CHARS, summary_max_tokens=SUMMARY_OUTPUT_TOKENS,
                     summary_input_max_chars=0, summary_timeout_seconds=SUMMARY_TIMEOUT_SECONDS,
                     summary_text_preview_chars=0, summary_argument_preview_chars=0,
                     near_context_ratio=COMPACT_RATIO, cache_soft_ratio=COMPACT_RATIO)
        for key, value in fixed.items():
            if getattr(self, key) != value:
                warn_retired_limits()
            setattr(self, key, value)
        if type(self.read_reference_enabled) is not bool:
            raise ValueError("read_reference_enabled must be a boolean")
        if self.cache_policy not in {"auto", "legacy", "cache_friendly"}:
            raise ValueError("cache_policy must be auto, legacy, or cache_friendly")
        for name in ("cache_soft_ratio", "cache_boundary_growth_ratio"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < 1:
                raise ValueError(f"{name} must be in (0, 1)")
        if self.mode not in {"model", "off"}:
            raise ValueError("CONTEXT_COMPACT_MODE must be 'model' or 'off'")
        for name in (
            "tool_result_budget_chars", "single_tool_output_max_chars", "compact_threshold_chars",
            "summary_max_chars", "summary_max_tokens", "recency_messages", "recency_rounds", "min_fold_messages",
            "max_fold_messages", "max_fold_rounds",
            "tool_clear_min_chars", "write_clear_min_chars",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("max_request_chars", "reactive_retries", "persisted_preview_chars", "command_output_max_chars", "context_window_tokens",
                     "summary_context_window_tokens", "investigation_keep_rounds",
                     "command_keep_rounds", "write_keep_rounds"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name in ("failure_cooldown_seconds", "summary_timeout_seconds"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value)
                    or value < 0 or (name == "summary_timeout_seconds" and value == 0)):
                raise ValueError(f"{name} must be finite and {'positive' if name == 'summary_timeout_seconds' else 'non-negative'}")
        if self.max_fold_messages < self.min_fold_messages:
            raise ValueError("max_fold_messages must be >= min_fold_messages")
        if not isinstance(self.model_context_windows, dict) or any(
            not isinstance(model, str) or not model.strip() or type(window) is not int or window <= 0
            for model, window in self.model_context_windows.items()
        ):
            raise ValueError("model_context_windows must map model names to positive token windows")
        if (isinstance(self.near_context_ratio, bool) or not isinstance(self.near_context_ratio, (float, int))
                or not 0 < self.near_context_ratio <= 1):
            raise ValueError("near_context_ratio must be in (0, 1]")

    def window_for_model(self, model: str) -> int:
        return self.model_context_windows.get(model, self.context_window_tokens)


@dataclass(slots=True)
class RuntimeState:
    user_goal: str = ""
    history_generation: int = 0
    tool_schema_hash: str = ""
    last_prompt_mode: str | None = None  # Legacy checkpoints only; cleared after permission migration.
    # Client snapshots only; never imply that a provider still has a cache entry.
    prompt_snapshot: dict[str, Any] = field(default_factory=dict)
    prompt_revision: int = 0
    request_baselines: dict[str, Any] = field(default_factory=dict)
    runtime_reminders: dict[str, Any] = field(default_factory=dict)
    request_view: dict[str, Any] = field(default_factory=dict)
    # Session-local read receipts, never a promise of provider-side caching.
    read_references: dict[str, Any] = field(default_factory=dict)
    tool_output_records: dict[str, Any] = field(default_factory=dict)
    last_tool_batch: dict[str, Any] = field(default_factory=dict)
    # These fields are committed with canonical messages in the existing checkpoint.
    summary_text: str = ""
    compacted_message_count: int = 0
    compacted_prefix_hash: str = ""
    summary_revision: int = 0
    summary_transcript: str = ""
    summary_source_count: int = 0
    summary_source_hash: str = ""
    summary_retry_after_epoch: float = 0.0
    summary_failure_scope: str = ""
    summary_recovery_attempted: bool = False
    current_turn_start: int = -1
    latest_request_prompt_tokens: int = 0
    peak_request_prompt_tokens: int = 0
    accumulated_input_tokens: int = 0
    accumulated_output_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    latest_request_model: str = ""
    latest_request_estimated: bool = True
    files_read: dict[str, dict[str, Any]] = field(default_factory=dict)
    tool_call_counts: dict[str, int] = field(default_factory=dict)
    tool_artifacts: list[str] = field(default_factory=list)
    loaded_skills: list[str] = field(default_factory=list)
    subagent_results: list[str] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    commands_run: list[str] = field(default_factory=list)
    test_results: list[str] = field(default_factory=list)
    important_notes: list[str] = field(default_factory=list)
    transcripts: list[str] = field(default_factory=list)

    def set_user_goal(self, prompt: str) -> None:
        if prompt.strip():
            self.user_goal = prompt.strip()

    def record_tool_result(self, tool_use: ToolUse, output: str) -> None:
        self.tool_call_counts[tool_use.name] = (
            self.tool_call_counts.get(tool_use.name, 0) + 1
        )

        result = normalize_tool_output(output)
        if result.status != "success" and tool_use.name != "bash":
            self.important_notes.append(f"{tool_use.name} [{result.status}]: {_shorten(output, 500)}")
            self.important_notes[:] = self.important_notes[-10:]
            return

        if tool_use.name == "read_file":
            path = str(
                tool_use.input.get("file_path") or tool_use.input.get("path") or ""
            )
            offset = tool_use.input.get("offset", 1)
            limit = tool_use.input.get("limit", READ_SOURCE_LINES)
            key = f"{path}|{offset}|{limit}"
            record = self.files_read.setdefault(
                key,
                {"path": path, "offset": offset, "limit": limit, "count": 0},
            )
            record["count"] += 1

        if tool_use.name == "load_skill":
            # Delivery is committed only after complete-request admission.
            return

        if tool_use.name == "subagent":
            self.subagent_results.append(_shorten(output, 1_500))
            self.subagent_results[:] = self.subagent_results[-5:]
            return

        if tool_use.name in {"write_file", "edit_file"}:
            path = (
                tool_use.input.get("file_path")
                or tool_use.input.get("path")
                or tool_use.input.get("target")
            )
            if path:
                _append_unique(self.files_changed, str(path))
            return

        if tool_use.name == "bash":
            command = str(tool_use.input.get("command", "")).strip()
            if not command:
                return
            self.commands_run.append(command)
            self.commands_run[:] = self.commands_run[-20:]
            if _looks_like_test_command(command):
                self.test_results.append(f"{command} [status={result.status}, exit_code={result.exit_code}]: {_shorten(output, 1_000)}")
                self.test_results[:] = self.test_results[-10:]

    def record_tool_artifact(self, path: Path) -> None:
        _append_unique(self.tool_artifacts, str(path))

    def record_transcript(self, path: Path) -> None:
        self.transcripts.append(str(path))
        self.transcripts[:] = self.transcripts[-10:]

    def to_summary_source(self) -> str:
        data = asdict(self)
        # Request implementation snapshots are not task evidence or summary input.
        for key in ("prompt_snapshot", "request_baselines", "runtime_reminders", "request_view", "read_references"):
            data.pop(key, None)
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _looks_like_test_command(command: str) -> bool:
    normalized = command.casefold()
    return any(
        marker in normalized
        for marker in ("pytest", "unittest", " test", "tests", "tox", "nox")
    )


def _shorten(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}\n... ({len(value) - limit} more chars)"
