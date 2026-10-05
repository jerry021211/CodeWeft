"""Incremental summaries and bounded request views over canonical history."""

from __future__ import annotations

import json
import hashlib
import re
import time
from dataclasses import replace
from copy import deepcopy
from threading import RLock
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4

from codeagent.context.models import ContextConfig, RuntimeState
from codeagent.context.budget import RequestBudget, RequestBudgetError, enforce_request, inspect_request, validate_budget
from codeagent.context.history import conversation_view, history_hash, is_user_turn, serializable, user_content
from codeagent.context.projection import build_tool_projection
from codeagent.context.read_references import project_read_references, record_read
from codeagent.context.observation import fingerprint
from codeagent.context.telemetry import tool_projection_metrics
from codeagent.context.summary_source import summary_source_messages
from codeagent.context.summary_chunks import chunk_requests
from codeagent.context.summary_prompt import SUMMARIZATION_SYSTEM_PROMPT, summary_handoff, summary_user_prompt
from codeagent.events import EventEmitter
from codeagent.messages import Message, ToolUse, extract_text, validate_tool_history
from codeagent.tools.todo import TodoStore
from codeagent.tools.base import ToolOutput, normalize_tool_output
from codeagent.tools.output_limits import (BATCH_CHARS, RESULT_METADATA_CHARS, DEFAULT_BODY_CHARS,
    BASH_BODY_CHARS, WEB_SEARCH_BODY_CHARS, WEB_SEARCH_RESULTS, CODE_SEARCH_BODY_CHARS, SEARCH_BODY_CHARS,
    SUMMARY_CHARS, SUMMARY_OUTPUT_TOKENS, SUMMARY_TIMEOUT_SECONDS, SUMMARY_BLOCKS, SUMMARY_BLOCK_TOKENS,
    WARNING_RATIO, COMPACT_RATIO, COMPACT_TARGET_RATIO)
from codeagent.tools.output_pages import page, facts, allocate, text_page, json_page
from codeagent.context.output_archive import archive_text, log_page

class ContextCompactionError(RuntimeError):
    """Raised when a required context checkpoint cannot be generated."""


class ContextManager:
    """Keep canonical history intact and checkpoint incremental request summaries."""

    def __init__(
        self,
        *,
        config: ContextConfig | None = None,
        state: RuntimeState | None = None,
        todo_store: TodoStore | None = None,
        task_state_provider: Callable[[], str] | None = None,
    ) -> None:
        self.config = config or ContextConfig()
        self.state = state or RuntimeState()
        self.todo_store = todo_store
        self.task_state_provider = task_state_provider
        self._reactive_retries = 0
        self._generation_reason: str | None = None
        self._lock = RLock()
        self._cooldown_until = 0.0
        self._cooldown_scope = ""
        self.summary_credentials_scope = ""
        self.cancellation_check: Callable[[], None] | None = None
        self.last_compaction: dict[str, Any] = {"status": "no_work", "reason": "not_started"}
        self.last_read_projection: dict[str, int] = {}

    def begin_turn(self, message_count: int) -> None:
        self.state.current_turn_start = message_count
        self.state.peak_request_prompt_tokens = 0
        self.reset_reactive_retries()

    def _turn_start(self, messages: list[Message]) -> int:
        index = self.state.current_turn_start
        if 0 <= index < len(messages):
            return index
        return next((i for i in range(len(messages) - 1, -1, -1) if is_user_turn(messages[i])), 0)

    def _validate_summary(self, messages: list[Message]) -> None:
        count = self.state.compacted_message_count
        if self.state.summary_text and (
            count <= 0 or count > len(messages)
            or history_hash(messages[:count]) != self.state.compacted_prefix_hash
            or (self.state.summary_source_count and (
                self.state.summary_source_count > len(messages)
                or history_hash(messages[:self.state.summary_source_count]) != self.state.summary_source_hash
            ))
        ):
            self.state.summary_text = ""
            self.state.compacted_message_count = 0
            self.state.compacted_prefix_hash = ""
            self.state.summary_transcript = ""
            self.state.summary_source_count = 0
            self.state.summary_source_hash = ""
            self.state.current_turn_start = -1
            self.last_compaction = {"status": "skipped", "reason": "history_changed"}

    def project_messages(self, messages: list[Message], *, clean_tools: bool = False) -> list[Message]:
        """Build a canonical view, then replay checkpointed loss-aware edits."""
        validate_tool_history(messages)
        self._validate_summary(messages)
        start = self.state.compacted_message_count if self.state.summary_text else 0
        projected = self._retained_messages(messages, start)
        if self.state.summary_text:
            # Anthropic history starts with user. Summary remains explicitly untrusted data.
            projected = [{"role": "user", "content": summary_handoff(
                summary=self.state.summary_text, revision=self.state.summary_revision,
                transcript=self.state.summary_transcript,
            )}] + projected
        base = projected
        projected = self._stable_tool_view(base)
        if clean_tools:
            projected = self._project_tools(projected)
            self._remember_tool_view(base, projected)
        # Run after cleanup and summary projection. The full canonical results
        # remain available for rehydration when an earlier anchor is removed.
        snapshot = self.state.read_references
        if snapshot and (not isinstance(snapshot, dict) or snapshot.get("version") != 1
                         or not isinstance(snapshot.get("receipts"), dict)):
            self.state.read_references = snapshot = {}
            self._generation_reason = "read_reference_snapshot_rebuilt"
        if snapshot:
            previous_policy = snapshot.get("enabled")
            if type(previous_policy) is bool and previous_policy != self.config.read_reference_enabled:
                self._generation_reason = "read_reference_policy_changed"
            snapshot["enabled"] = self.config.read_reference_enabled
        self.last_read_projection = {"read_reference_count": 0, "read_reference_chars_saved": 0}
        if self.config.read_reference_enabled:
            projected, self.last_read_projection = project_read_references(projected, self.state.read_references)
        validate_tool_history(projected)
        return projected

    def _view_key(self) -> str:
        return fingerprint({"summary_revision": self.state.summary_revision,
                            "prefix": self.state.compacted_prefix_hash,
                            "summary": self.state.summary_text,
                            "enabled": self.config.tool_projection_enabled,
                            "investigation": self.config.investigation_keep_rounds,
                            "command": self.config.command_keep_rounds,
                            "write": self.config.write_keep_rounds,
                            "min_chars": self.config.tool_clear_min_chars,
                            "write_min": self.config.write_clear_min_chars,
                            "policy": self.config.cache_policy,
                            "read_reference_enabled": self.config.read_reference_enabled})["hash"]

    def _stable_tool_view(self, base: list[Message]) -> list[Message]:
        view = self.state.request_view
        if view and (view.get("version") != 1 or view.get("key") != self._view_key()):
            self.state.request_view = {}
            self._generation_reason = "request_view_configuration_or_summary_changed"
            return base
        if not view.get("patches"):
            return base
        result = list(base)
        for patch in view.get("patches", []):
            index = patch["index"]
            if index >= len(base) or fingerprint(base[index])["hash"] != patch["source_hash"]:
                self.state.request_view = {}
                self._generation_reason = "request_view_history_changed"
                return base
            result[index] = deepcopy(patch["message"])
        return result

    def _remember_tool_view(self, base: list[Message], projected: list[Message]) -> None:
        previous = self.state.request_view
        if base == projected and not previous:
            return
        self.state.request_view = {
            **previous, "version": 1, "key": self._view_key(),
            "patches": [{"index": i, "source_hash": fingerprint(old)["hash"],
                         "message": serializable(deepcopy(new))}
                        for i, (old, new) in enumerate(zip(base, projected)) if old != new],
        }

    def _retained_messages(self, messages: list[Message], start: int) -> list[Message]:
        """Shared retained view for actual requests and the optimistic savings bound."""
        turn_start = self._turn_start(messages)
        tail = messages[start:] if start else messages
        projected = conversation_view(tail, max(0, turn_start - start))
        # The active task and any steering within folded rounds remain verbatim.
        protected = [user for message in messages[turn_start:start]
                     if message.get("_context_source") != "runtime"
                     and (user := user_content(message)) is not None]
        if protected:
            projected = protected + projected
        if any("_context_source" in message for message in projected):
            projected = [{key: value for key, value in message.items() if key != "_context_source"}
                         for message in projected]
        return projected

    def _project_tools(self, messages: list[Message]) -> list[Message]:
        # Do not turn uncaptured evidence into lossy previews. Repeated read
        # anchors are handled separately; older evidence goes through the
        # transactional summary/archive path instead of generic string cleanup.
        return messages

    def _under_pressure(self, budget: RequestBudget, window: int, *, cache_friendly: bool = False) -> bool:
        # Counts and a previous request's usage cannot establish current pressure.
        if window <= 0:
            return False
        return budget.estimated_prompt_tokens >= (window - budget.output_reserve_tokens) * COMPACT_RATIO

    def prepare_before_model_call(
        self,
        messages: list[Message],
        *,
        client: Any | None = None,
        event_emitter: EventEmitter | None = None,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        model: str = "",
        max_tokens: int = 0,
        model_window: dict[str, Any] | None = None,
        cache_capabilities: dict[str, Any] | None = None,
    ) -> list[Message]:
        projected = self.project_messages(messages)
        params = dict(model=model, system=system, messages=projected, tools=tools or [], max_tokens=max_tokens)
        budget = inspect_request(**params)
        # Production clients use discovery exclusively, including when unknown.
        # Keep explicit limits for SDK clients that do not implement discovery.
        window = model_window["context_window_tokens"] if model_window is not None else self.config.window_for_model(model)
        cache_friendly = window > 0 and (self.config.cache_policy == "cache_friendly" or (
            self.config.cache_policy == "auto" and bool((cache_capabilities or {}).get("cheap_prefix_reads"))))
        hard_pressure = window > 0 and budget.estimated_total_tokens > window
        view = self.state.request_view
        boundary_config = fingerprint({"model": model, "window": window, "max_tokens": max_tokens,
                                       "system": system, "tools": tools, "friendly": cache_friendly,
                                       "soft": self.config.cache_soft_ratio,
                                       "growth": self.config.cache_boundary_growth_ratio,
                                       "trigger": "model_window"})["hash"]
        usable_input = window - max_tokens
        if window > 0 and usable_input <= 0:
            raise RequestBudgetError(budget, reason='context_window_tokens', limit=window)
        boundary_due = True
        pressure = lambda value: self._under_pressure(value, window, cache_friendly=cache_friendly)
        target_tokens = max(0, int(usable_input * COMPACT_TARGET_RATIO))
        needs_headroom = lambda value: value.estimated_prompt_tokens > target_tokens
        self._summary_deadline = time.monotonic() + SUMMARY_TIMEOUT_SECONDS
        cleanup_boundary = boundary_due and (hard_pressure or pressure(budget))
        if cleanup_boundary:
            # Only inspect the irreducible portion when it could block compaction.
            enforce_request(**{**params, "messages": []}, context_window_tokens=window)
            cleaned = self.project_messages(messages, clean_tools=True)
            if cleaned is not projected:
                if cleaned != projected:
                    self._generation_reason = "tool_cleanup_boundary"
                projected = cleaned
                params["messages"] = projected
                budget = inspect_request(**params)
        summary_started = False
        summaries_written = 0
        for _ in range(3):
            needs_summary = (needs_headroom(budget) if summary_started else pressure(budget))
            if not cleanup_boundary or not needs_summary or self.config.mode == "off":
                break
            revision = self.state.summary_revision
            summary_started = True
            required = window > 0 and budget.estimated_total_tokens > window
            try:
                self.compact_history(messages, reason="hard_limit" if required else "auto_compact",
                                     client=client, event_emitter=event_emitter,
                                     request=params, request_budget=budget)
            except ContextCompactionError:
                # Failure is observable; only continue when the whole request still fits.
                break
            if revision == self.state.summary_revision:
                break
            summaries_written += 1
            projected = self.project_messages(messages, clean_tools=True)
            params["messages"] = projected
            budget = inspect_request(**params)
            if not self._eligible_cuts(messages):
                break
        if cleanup_boundary and cache_friendly:
            self.state.request_view.update(boundary_config=boundary_config,
                                           version=1, key=self._view_key(),
                                           boundary_chars=budget.text_request_chars,
                                           boundary_tokens=budget.estimated_prompt_tokens)
        telemetry = {
            "model": model,
            "cache_policy": "cache_friendly" if cache_friendly else "legacy",
            "cleanup_boundary": cleanup_boundary,
            "cache_soft_ratio": COMPACT_RATIO,
            "cache_boundary_growth_ratio": self.config.cache_boundary_growth_ratio,
            "compaction_trigger": "model_window",
            "input_budget_tokens": usable_input if window else None,
            "input_warning": window > 0 and budget.estimated_prompt_tokens >= usable_input * WARNING_RATIO,
            "compact_target_chars": None,
            "compact_target_prompt_tokens": target_tokens if window else None,
            "summaries_written": summaries_written,
            "compact_target_reached": not needs_headroom(budget) if window else None,
            "effective_soft_request_chars": None,
            "effective_soft_prompt_tokens": usable_input * COMPACT_RATIO if window else None,
            "context_window_tokens": window,
            "context_window_source": model_window["context_window_source"] if model_window is not None else "configuration",
            "context_window_reason": model_window.get("context_window_reason") if model_window is not None else None,
            "context_window_model": model_window.get("context_window_model") if model_window is not None else model,
            "near_context_ratio": COMPACT_RATIO,
            "max_request_chars": None,
            "compact_threshold_chars": None,
            "canonical_messages": len(messages),
            "projected_messages": len(projected),
            "summary_revision": self.state.summary_revision,
            "compacted_message_count": self.state.compacted_message_count,
            **self.last_read_projection,
            **(tool_projection_metrics(messages, projected) if event_emitter is not None else {}),
        }
        try:
            validate_budget(budget, context_window_tokens=window)
        except RequestBudgetError as exc:
            if event_emitter is not None:
                event_emitter.emit("context.request_blocked", {
                    **telemetry, **exc.budget.to_dict(), "reason": exc.reason,
                    "last_compaction": self.last_compaction,
                })
            raise
        if event_emitter is not None:
            event_emitter.emit("context.request_projected", {
                **telemetry, **budget.to_dict(),
                "last_compaction_status": self.last_compaction["status"],
            })
        for message in projected:
            for block in message.get('content', []) if isinstance(message.get('content'), list) else []:
                if isinstance(block, dict) and block.get('type') == 'tool_result':
                    record = self.state.tool_output_records.get(block.get('tool_use_id'), {})
                    name = record.get('skill_name')
                    if name and name not in self.state.loaded_skills:
                        self.state.loaded_skills.append(name)
        self._summary_deadline = None
        return projected

    def finalize_tool_results(
        self,
        tool_uses: list[ToolUse],
        outputs: list[str],
    ) -> list[str]:
        if len(tool_uses) != len(outputs):
            raise ValueError("工具调用和结果数量不一致")
        prepared = [self._prepare_result(call, value) for call, value in zip(tool_uses, outputs)]
        # One invocation corresponds to one assistant response, in tool_use order.
        # Reserve necessary envelopes and indivisible skill instructions first.
        indivisible = [call.name == 'load_skill' and value.status == 'success'
                       for call, value in zip(tool_uses, prepared)]
        priorities = [0 if value.status in {'error', 'blocked'} else
                      1 if call.name in {'read_file', 'load_tool_output', 'load_context_history'} else 2
                      for call, value in zip(tool_uses, prepared)]
        empty = [value if indivisible[i] else self._render_result(value, 0)
                 for i, value in enumerate(prepared)]
        metadata = [len(value) - (len(value.body) if indivisible[i] else 0) for i, value in enumerate(empty)]
        skills_chars = sum(len(value.body) for i, value in enumerate(prepared) if indivisible[i])
        # Cursor/header lengths can change when a page shrinks. Recompute the
        # same priority and fair shares with the measured metadata reservation.
        for attempt in range(10):
            if attempt == 9:
                metadata = [RESULT_METADATA_CHARS] * len(prepared)
            allocations = [len(value.body) if indivisible[i] else 0 for i, value in enumerate(prepared)]
            remaining = max(0, BATCH_CHARS - sum(metadata) - skills_chars)
            for category in (0, 1, 2):
                indices = [i for i in range(len(prepared)) if not indivisible[i] and priorities[i] == category]
                shares = allocate([len(prepared[i].body) for i in indices], remaining)
                for index, share in zip(indices, shares):
                    allocations[index] = share
                remaining -= sum(shares)
            finalized = [value if indivisible[i] else self._render_result(value, allocations[i])
                         for i, value in enumerate(prepared)]
            if sum(map(len, finalized)) <= BATCH_CHARS or sum(map(len, empty)) > BATCH_CHARS:
                break
            metadata = [max(old, len(value) - len(value.body)) for old, value in zip(metadata, finalized)]
        self.state.last_tool_batch = {'assistant_batch_id': uuid4().hex, 'tool_use_ids': [call.id for call in tool_uses],
                                     'chars': sum(map(len, finalized)),
                                     'batch_soft_limit_exceeded': sum(map(len, finalized)) > BATCH_CHARS}
        for index, (call, value) in enumerate(zip(tool_uses, finalized)):
            self.state.tool_output_records[call.id] = {**facts(value),
                'body_chars': len(value.body), 'file_read_snapshot': value.file_read_snapshot,
                'media_references': getattr(value, 'media_references', []),
                'execution_id': getattr(value, 'execution_id', None),
                'source_hash': getattr(prepared[index], 'source_hash', None),
                'skill_name': call.input.get('name') if call.name == 'load_skill' and value.status == 'success' else None}
            if value.file_read_snapshot:
                self.state.read_references = record_read(self.state.read_references, call, value)
        return finalized

    def _prepare_result(self, call, value):
        output = normalize_tool_output(value)
        digest = hashlib.sha256()
        for offset in range(0, len(value), 65536):
            digest.update(value[offset:offset + 65536].encode('utf-8'))
        output.source_hash = digest.hexdigest()
        previous = self.state.tool_output_records.get(call.id, {})
        same_execution = (previous.get('execution_id') == output.execution_id or
                          (not isinstance(value, ToolOutput) and previous.get('source_hash') == output.source_hash))
        if same_execution and previous.get('output_id') and not getattr(output, 'archive', None):
            from codeagent.context.output_archive import OutputArchive
            try:
                OutputArchive.restore(self.config.tool_output_dir, previous['output_id']).attach(output)
            except (OSError, ValueError):
                output.source_complete, output.storage_error = False, 'archive_missing'
        receipt = self.state.read_references.get('receipts', {}).get(call.id, {})
        if (call.name == 'read_file' and not getattr(output, 'read_page', None)
                and receipt.get('actual_range') and receipt.get('output_hash') == hashlib.sha256(str(value).encode()).hexdigest()):
            from codeagent.tools.read import _output
            meta = receipt['actual_range']
            body = str(value).split('\n', 1)[1]
            raw = re.sub(r'(^|(?<=[\r\n]))\d+\t', '', body)
            start = (meta['start_line'], meta['start_char_offset']) if meta['start_line'] else None
            end = (meta['end_line'], meta['end_char_offset']) if meta['end_line'] else None
            output = _output((body, raw, start, end, meta['has_more'], meta['next_offset'], meta['next_char_offset']),
                             version=meta['source_version'], snapshot=receipt, requested_offset=receipt['offset'],
                             requested_char_offset=receipt.get('char_offset', 0), requested_limit=receipt['limit'])
        # Source-file and archive readers already have a stable recovery source.
        if getattr(output, 'page_ready', False):
            if call.name == 'load_skill' and getattr(output, 'feedback', None):
                output = page(output, output.body, feedback=''.join(output.feedback)[:512])
            if call.name not in {'read_file', 'load_tool_output', 'load_context_history', 'load_skill'} and not output.output_id and not getattr(output, 'archive', None):
                original = getattr(output, 'source_text', None)
                if original is None and hasattr(output, 'original_payload'):
                    original = json.dumps(output.original_payload, ensure_ascii=False)
                archive_text(self.config.tool_output_dir, output, original if original is not None else output.body,
                             content_type='json' if hasattr(output, 'original_payload') else 'text')
                if output.output_id:
                    self.state.record_tool_artifact(output.archive.path)
            return output
        if call.name == 'load_skill' and output.status == 'success':
            output = page(output, str(output))
            output.page_renderer = lambda size: output
            return output
        text = str(value)
        structured_ok = True
        try:
            structured = json.loads(text)
            structured_ok = isinstance(structured, (dict, list)) or getattr(output, 'structured_json', False)
        except (ValueError, TypeError):
            structured, structured_ok = None, False
        if not getattr(output, 'archive', None):
            archive_text(self.config.tool_output_dir, output, origin=getattr(output, "source_url", None),
                         content_type='json' if structured_ok else 'text')
        if output.output_id:
            self.state.record_tool_artifact(output.archive.path)
        if call.name == 'bash':
            return log_page(output)
        limit = {'web_search': WEB_SEARCH_BODY_CHARS, 'search_code': CODE_SEARCH_BODY_CHARS,
                 'grep': SEARCH_BODY_CHARS, 'glob': SEARCH_BODY_CHARS}.get(call.name, DEFAULT_BODY_CHARS)
        if getattr(output, 'output_policy', None) == 'web_search':
            limit = WEB_SEARCH_BODY_CHARS
        if getattr(output, 'source_url', None):
            url = output.source_url
            output.result_metadata = {'source_url': url if len(url) <= 1000 else {'archive_manifest': output.output_id}}
        if structured_ok:
            if not isinstance(structured, (dict, list)):
                structured = {'result': structured}
            if getattr(output, 'output_policy', None) == 'web_search':
                if isinstance(structured, dict) and 'structured_content' in structured:
                    structured = structured['structured_content']
                if isinstance(structured, dict) and isinstance(structured.get('web'), dict):
                    structured = {'results': structured['web'].get('results', [])}
                return json_page(output, structured, limit, max_records=WEB_SEARCH_RESULTS)
            return json_page(output, structured, limit)
        return text_page(output, output.archive.prefix if output.storage_error else text[:output.archive.saved_chars], limit)

    def _render_result(self, output, allowance):
        renderer = getattr(output, 'page_renderer', None)
        rendered = renderer(allowance) if renderer else text_page(output, output.body, allowance)
        if getattr(rendered, 'read_page', None) or getattr(rendered, 'read_batch', None):
            return rendered
        rendered.output_id = output.output_id
        rendered.source_complete = output.source_complete
        rendered.storage_error = output.storage_error
        if output.storage_error and not output.output_id:
            rendered.has_more = False
            rendered.next_cursor = None
        if output.truncated_reason and output.truncated_reason != 'page_limit':
            rendered.truncated_reason = output.truncated_reason
        if getattr(output, 'feedback', None):
            feedback = ''.join(output.feedback)[:512]
        else:
            feedback = ''
        # Re-render the legal envelope after attaching the stable source ID.
        if hasattr(rendered, 'original_payload'):
            value = json.loads(str(rendered))
            value['_output'] = facts(rendered)
            if feedback:
                value['_output']['feedback'] = feedback
            result = ToolOutput(json.dumps(value, ensure_ascii=False, separators=(',', ':')))
            result.__dict__.update(rendered.__dict__)
            result.metadata_chars = len(result) - len(result.body)
            return result
        return page(rendered, rendered.body, feedback=feedback) if feedback else page(rendered, rendered.body)

    def record_user_prompt(self, prompt: str) -> None:
        self.state.set_user_goal(prompt)

    def record_tool_result(self, tool_use: ToolUse, output: str) -> None:
        self.state.record_tool_result(tool_use, output)
        self.state.read_references = record_read(self.state.read_references, tool_use, output)

    def force_compact(
        self,
        messages: list[Message],
        *,
        client: Any | None = None,
        reason: str = "manual_compact",
        event_emitter: EventEmitter | None = None,
    ) -> list[Message]:
        if self.config.mode == "off":
            return messages
        return self.compact_history(
            messages,
            reason=reason,
            client=client,
            event_emitter=event_emitter,
        )

    def reactive_compact(
        self,
        messages: list[Message],
        *,
        client: Any | None = None,
        event_emitter: EventEmitter | None = None,
    ) -> list[Message] | None:
        if self.config.mode == "off" or self._reactive_retries >= self.config.reactive_retries:
            return None
        self._reactive_retries += 1
        revision = self.state.summary_revision
        self.compact_history(
            messages,
            reason="reactive_compact",
            client=client,
            event_emitter=event_emitter,
        )
        # Recovery keeps operating on canonical history; the next send reprojects.
        return messages if self.state.summary_revision > revision else None

    def reset_reactive_retries(self) -> None:
        self._reactive_retries = 0

    def consume_generation_reason(self) -> str | None:
        reason = self._generation_reason
        self._generation_reason = None
        return reason

    def compact_history(
        self,
        messages: list[Message],
        *,
        reason: str,
        client: Any | None,
        event_emitter: EventEmitter | None = None,
        request: dict[str, Any] | None = None,
        request_budget: RequestBudget | None = None,
    ) -> list[Message]:
        with self._lock:
            if reason in {'manual_compact', 'reactive_compact'}:
                self._summary_deadline = time.monotonic() + SUMMARY_TIMEOUT_SECONDS
            self._check_cancelled()
            self._validate_summary(messages)
            if self.config.mode == "off":
                self.last_compaction = {"status": "skipped", "reason": "disabled"}
                return self.project_messages(messages)
            if self._in_failure_cooldown() and reason != "manual_compact":
                # A single persisted emergency retry can escape a soft failure.
                # Repeated sends/restores cannot turn a failing summary into an
                # unbounded paid retry loop; the final hard check still blocks.
                if reason in {"hard_limit", "reactive_compact"} and not self.state.summary_recovery_attempted:
                    self.state.summary_recovery_attempted = True
                else:
                    self.last_compaction = {"status": "skipped", "reason": "failure_cooldown"}
                    return self.project_messages(messages)
            validate_tool_history(messages)
            start = self.state.compacted_message_count
            cuts = self._eligible_cuts(messages)
            if not cuts:
                self.last_compaction = {"status": "no_work", "reason": "insufficient_complete_history"}
                return self.project_messages(messages)
            before = request or dict(model="", system="", messages=self.project_messages(messages), tools=[], max_tokens=0)
            before_budget = request_budget or inspect_request(**before)
            required_savings = max(256, int(before_budget.text_request_chars * 0.05))
            # Omit the entire future summary envelope. Even this optimistic bound
            # must save enough; never charge the model for a provably useless cut.
            preflights = {}
            for cut in cuts:
                retained = self._retained_messages(messages, cut)
                if request is not None:
                    retained = self._project_tools(retained)
                if self.config.read_reference_enabled:
                    retained, _ = project_read_references(retained, self.state.read_references)
                floor = inspect_request(**{**before, "messages": retained})
                protected = [user for message in messages[self._turn_start(messages):cut]
                             if message.get("_context_source") != "runtime"
                             and (user := user_content(message)) is not None]
                preflights[cut] = {
                    "candidate_cut": cut,
                    "before_request_chars": before_budget.text_request_chars,
                    "retained_request_chars": floor.text_request_chars,
                    "protected_user_chars": sum(len(json.dumps(serializable(m), ensure_ascii=False,
                                                               separators=(",", ":"))) for m in protected),
                    "max_possible_saved_chars": before_budget.text_request_chars - floor.text_request_chars,
                    "required_saved_chars": required_savings,
                }
            viable = [cut for cut in cuts if preflights[cut]["max_possible_saved_chars"] >= required_savings]
            if not viable:
                best = max(preflights.values(), key=lambda entry: entry["max_possible_saved_chars"])
                self.last_compaction = {"status": "no_work", "reason": "insufficient_compressible_history",
                                        "summary_called": False, "candidate_count": len(cuts), **best}
                if event_emitter is not None:
                    event_emitter.emit("context.compaction_skipped", dict(self.last_compaction))
                return before["messages"]
            params = None
            bounded_source = False
            if not getattr(self, '_summary_deadline', None):
                self._summary_deadline = time.monotonic() + SUMMARY_TIMEOUT_SECONDS
            if callable(client) and not hasattr(client, 'create_message'):
                client = client()
            resolver = getattr(client, 'get_model_window', None)
            self._summary_active_window = self.config.summary_context_window_tokens
            if callable(resolver):
                self._summary_active_window = ((resolver(self.config.summarization_model) or {}).get('context_window_tokens')
                                               or self._summary_active_window)
            try:
                enforce_request(**self._summary_request(''), context_window_tokens=self._summary_active_window)
            except RequestBudgetError as exc:
                self._compaction_failed(exc, reason=reason, event_emitter=event_emitter)
            for end in reversed(viable):
                try:
                    params = chunk_requests(messages[start:end], self._summary_request,
                                            self._summary_active_window)
                    break
                except (ValueError, RequestBudgetError):
                    continue
            if not params:
                self.last_compaction = {'status': 'no_work', 'reason': 'summary_input_budget', 'coverage_complete': False}
                return self.project_messages(messages)
            prefix_hash = history_hash(messages[:end])
            source_count = len(messages)
            source_hash = history_hash(messages)
            revision = self.state.summary_revision
            started = time.monotonic()
            try:
                self._check_cancelled()
                from codeagent.tools.runtime_data import _archive_path
                for message in messages[start:end]:
                    for block in message.get('content', []) if isinstance(message.get('content'), list) else []:
                        if isinstance(block, dict) and block.get('type') == 'tool_result':
                            saved = self.state.tool_output_records.get(block.get('tool_use_id'), {})
                            if saved.get('output_id'):
                                _archive_path(self.config.tool_output_dir, saved['output_id'] + '.txt')
                summary = self._model_summary(messages[start:end], client=client, params=params)
                self._check_cancelled()
                if (source_hash != history_hash(messages) or revision != self.state.summary_revision):
                    self.last_compaction = {"status": "skipped", "reason": "history_changed_during_summary"}
                    return self.project_messages(messages)
                transcript = self._transcript_path(reason)
                candidate_state = replace(
                    self.state, summary_text=summary, compacted_message_count=end,
                    compacted_prefix_hash=prefix_hash, summary_revision=revision + 1,
                    summary_transcript=str(transcript), summary_source_count=source_count,
                    summary_source_hash=source_hash,
                )
                candidate_view = ContextManager(config=self.config, state=candidate_state).project_messages(
                    messages, clean_tools=request is not None,
                )
                after_budget = inspect_request(**{**before, "messages": candidate_view})
                saved = before_budget.text_request_chars - after_budget.text_request_chars
                if (saved < required_savings
                        or after_budget.estimated_prompt_tokens >= before_budget.estimated_prompt_tokens):
                    self._start_cooldown()
                    self.last_compaction = {"status": "skipped", "reason": "insufficient_savings",
                                            "saved_chars": saved, "summary_called": True,
                                            "after_request_chars": after_budget.text_request_chars,
                                            "preflight": preflights[end]}
                    return before["messages"]
                # Immutable linked segments keep old checkpoint references stable,
                # without rewriting every previously archived message each time.
                previous = self.state.summary_transcript
                if previous:
                    from codeagent.tools.runtime_data import _history_path
                    try:
                        old_path = _history_path(self.config.transcript_dir, previous)
                        if old_path.parent != transcript.parent:
                            previous = ""  # Bootstrap old nested layouts once.
                    except (OSError, RuntimeError, ValueError):
                        previous = ""
                self.write_transcript(messages[start:end] if previous else messages[:end], reason=reason,
                                      record_state=False, path=transcript, previous=previous,
                                      start=start if previous else 0)
                self._check_cancelled()
                if source_hash != history_hash(messages) or revision != self.state.summary_revision:
                    self.last_compaction = {'status': 'skipped', 'reason': 'history_changed_before_commit'}
                    return self.project_messages(messages)
            except Exception as exc:
                self._compaction_failed(exc, reason=reason, event_emitter=event_emitter)
            self.state.summary_text = summary
            self.state.compacted_message_count = end
            self.state.compacted_prefix_hash = prefix_hash
            self.state.summary_transcript = str(transcript)
            self.state.summary_source_count = source_count
            self.state.summary_source_hash = source_hash
            self.state.summary_revision += 1
            self.state.request_view = deepcopy(candidate_state.request_view)
            self.state.history_generation += 1
            self.state.record_transcript(transcript)
            self.state.summary_retry_after_epoch = 0.0
            self.state.summary_failure_scope = ""
            self.state.summary_recovery_attempted = False
            self._cooldown_until = 0.0
            self._generation_reason = reason
            self.last_compaction = {
                "status": "written", "reason": reason, "previous_cursor": start,
                "covered_cursor": end, "folded_messages": end - start,
                "retained_messages": len(messages) - end, "summary_chars": len(summary),
                "summary_revision": self.state.summary_revision,
                "summary_called": True, "preflight": preflights[end],
                "source_previews_used": False,
                "source_blocks": len(params), "coverage_complete": True,
                "duration_ms": round((time.monotonic() - started) * 1000),
            }
            if event_emitter is not None:
                event_emitter.emit("context.compacted", {
                    **self.last_compaction, "generation_reason": reason,
                    "history_generation": self.state.history_generation,
                    "message_count_before": len(messages), "transcript": str(transcript),
                })
            return self.project_messages(messages)

    def _compaction_failed(self, exc: Exception, *, reason: str,
                           event_emitter: EventEmitter | None) -> NoReturn:
        from codeagent.runtime.cancellation import CancelledError
        if isinstance(exc, CancelledError):
            raise exc
        self._start_cooldown()
        self.last_compaction = {"status": "failed", "reason": type(exc).__name__}
        if event_emitter is not None:
            event_emitter.emit("context.compaction_failed", {
                "generation_reason": reason, "error_type": type(exc).__name__,
                "compacted_message_count": self.state.compacted_message_count,
            })
        raise ContextCompactionError(f"Context summary failed: {exc}") from exc

    def _start_cooldown(self) -> None:
        if not self._in_failure_cooldown():
            self.state.summary_recovery_attempted = False
        self._cooldown_until = time.monotonic() + self.config.failure_cooldown_seconds
        self._cooldown_scope = self._failure_scope()
        self.state.summary_failure_scope = self._cooldown_scope
        self.state.summary_retry_after_epoch = time.time() + self.config.failure_cooldown_seconds

    def _check_cancelled(self) -> None:
        if self.cancellation_check is not None:
            self.cancellation_check()

    def _failure_scope(self) -> str:
        return hashlib.sha256(json.dumps([
            self.config.summarization_model, self.config.summarization_api_key,
            self.summary_credentials_scope,
            SUMMARY_OUTPUT_TOKENS, SUMMARY_CHARS,
            self.config.summary_context_window_tokens,
        ]).encode("utf-8")).hexdigest()

    def _in_failure_cooldown(self) -> bool:
        scope = self._failure_scope()
        if self._cooldown_scope != scope:
            self._cooldown_scope = scope
            self._cooldown_until = 0.0
            if self.state.summary_failure_scope == scope:
                # Convert the persisted wall-clock deadline once on restore. Active
                # waiting uses monotonic time and is capped if the wall clock moved.
                remaining = min(self.config.failure_cooldown_seconds,
                                max(0.0, self.state.summary_retry_after_epoch - time.time()))
                self._cooldown_until = time.monotonic() + remaining
        return time.monotonic() < self._cooldown_until

    def _eligible_cuts(self, messages: list[Message]) -> list[int]:
        start = self.state.compacted_message_count
        turn = self._turn_start(messages)
        conversation_limit = min(turn, len(messages) - self.config.recency_messages)
        rounds = [i for i in range(turn, len(messages)) if messages[i].get("role") == "assistant"]
        worker_limit = rounds[-self.config.recency_rounds] if len(rounds) > self.config.recency_rounds else -1
        new_rounds = [index for index in rounds if index >= start]
        if len(new_rounds) > self.config.max_fold_rounds:
            worker_limit = min(worker_limit, new_rounds[self.config.max_fold_rounds])
        limit = min(len(messages) - 1, start + self.config.max_fold_messages)
        return [i for i in range(start + self.config.min_fold_messages, limit + 1)
                if (i <= conversation_limit and is_user_turn(messages[i]))
                or (turn <= i <= worker_limit and messages[i].get("role") == "assistant")]

    def _transcript_path(self, reason: str) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        safe_reason = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in reason)[:80]
        return (self.config.transcript_dir / f"{stamp}-{safe_reason}-{uuid4().hex}.jsonl").resolve()

    def write_transcript(self, messages: list[Message], *, reason: str, record_state: bool = True,
                         path: Path | None = None, previous: str = "", start: int = 0) -> Path:
        self.config.transcript_dir.mkdir(parents=True, exist_ok=True)
        path = path or self._transcript_path(reason)
        with path.open("x", encoding="utf-8") as handle:
            if previous:
                handle.write(json.dumps({"_context_archive": 1, "previous": Path(previous).name,
                                         "start": start, "end": start + len(messages)}) + "\n")
            for message in messages:
                handle.write(json.dumps(serializable(message), ensure_ascii=False, default=str))
                handle.write("\n")
        if record_state:
            self.state.record_transcript(path)
        return path

    def _finalize_single_tool_result(self, tool_use, output):
        return self._prepare_result(tool_use, output)

    def _write_tool_output(self, tool_use_id, output):
        result = archive_text(self.config.tool_output_dir, normalize_tool_output(output))
        if result.storage_error:
            raise OSError(result.storage_error)
        self.state.record_tool_artifact(result.archive.path)
        return result.archive.path

    def _summary_request(self, conversation, *, previous=None):
        return dict(model=self.config.summarization_model, system=SUMMARIZATION_SYSTEM_PROMPT,
                    messages=[{'role': 'user', 'content': summary_user_prompt(
                        previous_summary=self.state.summary_text if previous is None else previous,
                        conversation=conversation, summary_char_budget=SUMMARY_CHARS)}],
                    tools=[], max_tokens=SUMMARY_OUTPUT_TOKENS)

    def _summary_params(self, messages, *, bounded=False):
        return self._summary_request(json.dumps(serializable(summary_source_messages(messages)), ensure_ascii=False))

    def _model_summary(self, messages, *, client, params=None):
        if client is None or not self.config.summarization_model:
            raise RuntimeError('summary client and model must be configured')
        if callable(client) and not hasattr(client, 'create_message'):
            client = client()
        deadline = getattr(self, '_summary_deadline', None) or time.monotonic() + SUMMARY_TIMEOUT_SECONDS
        window = getattr(self, '_summary_active_window', self.config.summary_context_window_tokens)
        requests = params if isinstance(params, list) else [params or self._summary_params(messages)]
        last_request = None
        def call(request):
            nonlocal last_request
            last_request = request
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Shared 180-second summary deadline expired')
            enforce_request(**request, context_window_tokens=window)
            self._check_cancelled()
            underlying = getattr(client, 'client', client)
            if hasattr(underlying, 'request_timeout'):
                underlying.request_timeout = remaining
            response = client.create_message(**request)
            self._check_cancelled()
            if time.monotonic() > deadline:
                raise TimeoutError('Shared summary deadline expired')
            if getattr(response, 'stop_reason', None) not in {None, 'end_turn', 'stop_sequence'}:
                raise RuntimeError('summary model returned unfinished output')
            text = extract_text(response.content).strip()
            if not text:
                raise RuntimeError('summary model returned no text')
            return text
        summaries = [call(request) for request in requests]
        while len(summaries) > 1:
            groups, current = [], []
            for summary in summaries:
                candidate = self._summary_request(json.dumps([*current, summary], ensure_ascii=False))
                try:
                    enforce_request(**candidate, context_window_tokens=window)
                except RequestBudgetError:
                    if not current:
                        raise RuntimeError('An intermediate summary cannot fit the merge window')
                    groups.append(current)
                    current = [summary]
                else:
                    current.append(summary)
            if current:
                groups.append(current)
            if len(groups) >= len(summaries):
                raise RuntimeError('Summary window cannot merge two intermediate summaries')
            summaries = [call(self._summary_request(json.dumps(group, ensure_ascii=False))) for group in groups]
        summary = summaries[0]
        if len(summary) > SUMMARY_CHARS:
            # Exactly one final rewrite. No hard string truncation on failure.
            # Keep the source of the final draft available during the repair.
            # The combined request must still fit the summary model's window.
            repair = deepcopy(last_request)
            repair['messages'].extend([
                {'role': 'assistant', 'content': summary},
                {'role': 'user', 'content': f'当前草稿 {len(summary)} 字符。请根据原始材料重新压缩，最多 {SUMMARY_CHARS} 字符，保留任务事实和恢复引用。'},
            ])
            summary = call(repair)
        if len(summary) > SUMMARY_CHARS:
            raise RuntimeError('Final summary still exceeds 16000 characters; original history retained')
        return summary

    def _task_state(self) -> str:
        if self.task_state_provider is not None:
            return self.task_state_provider()
        if self.todo_store is not None:
            return self.todo_store.format()
        return "(none)"
