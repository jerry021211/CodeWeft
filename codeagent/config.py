"""Environment-backed runtime configuration."""

from __future__ import annotations

import os
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from codeagent.agent import AgentConfig
from codeagent.anthropic_client import AnthropicModelClient
from codeagent.providers import OpenAIModelClient
from codeagent.models import ModelClient
from codeagent.speech import SpeechConfig
from codeagent.context import ContextConfig
from codeagent.hooks.loop_guard import LoopGuardConfig
from codeagent.memory import MemoryConfig
from codeagent.planning import PlanningBackend
from codeagent.prompts import PromptConfig
from codeagent.recovery import RecoveryConfig
from codeagent.runtime.data_paths import default_runtime_data_dir
from codeagent.runtime.parallel import ParallelConfig
from codeagent.tools.web_search import WebSearchConfig
from codeagent.code_search.embedding import EmbeddingConfig
from codeagent.lsp.registry import LspConfig, configured_servers


def _load_dotenv() -> None:
    """Load .env from cwd or parent directories without overriding env vars."""

    env_path = _find_dotenv()
    if env_path is None:
        return

    try:
        from dotenv import load_dotenv
    except ImportError:
        _load_dotenv_fallback(env_path)
        return

    load_dotenv(env_path, override=False)


def _find_dotenv() -> Path | None:
    current = Path.cwd().resolve()
    while True:
        candidate = current / ".env"
        if candidate.exists():
            return candidate
        if current == current.parent:
            return None
        current = current.parent


def _load_dotenv_fallback(env_path: Path) -> None:
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip("'\"")
        if name and name not in os.environ:
            os.environ[name] = value


@dataclass(frozen=True, slots=True)
class EnvironmentConfig:
    model_id: str
    api_key: str | None = None
    base_url: str | None = None
    max_tokens: int = 32_000
    reasoning_effort: str = "default"
    max_iterations: int = 50
    stream: bool = False
    enable_skills: bool = True
    skill_roots: tuple[Path, ...] = (Path("skills"),)
    context_config: ContextConfig = field(default_factory=ContextConfig)
    memory_config: MemoryConfig = field(default_factory=MemoryConfig)
    prompt_config: PromptConfig = field(default_factory=PromptConfig)
    recovery_config: RecoveryConfig = field(default_factory=RecoveryConfig)
    loop_guard_config: LoopGuardConfig = field(default_factory=LoopGuardConfig)
    data_dir: Path = field(default_factory=default_runtime_data_dir)
    planning_mode: PlanningBackend = PlanningBackend.AUTO
    mcp_config_path: Path = Path("mcp.json")
    team_runtime_enabled: bool = False
    team_write_enabled: bool = False
    team_worktree_root: Path = Path(".codeagent-worktrees")
    team_model_response_timeout: float = 300.0
    team_model_call_timeout: float = 600.0
    web_max_concurrent_runs: int = 4
    parallel_config: ParallelConfig = field(default_factory=ParallelConfig)
    web_search_config: WebSearchConfig = field(default_factory=WebSearchConfig)
    embedding_config: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    lsp_config: LspConfig = field(default_factory=LspConfig)
    model_protocol: str = "anthropic"
    input_modalities: tuple[str, ...] | None = None
    reasoning_levels: tuple[str, ...] = ()
    chat_token_parameter: str = "max_completion_tokens"
    speech_config: SpeechConfig = field(default_factory=SpeechConfig)

    def __post_init__(self) -> None:
        if self.model_protocol not in {"anthropic", "openai_chat", "openai_responses"}:
            raise ValueError("MODEL_PROTOCOL must be anthropic, openai_chat or openai_responses")
        if self.chat_token_parameter not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("OPENAI_CHAT_TOKEN_PARAMETER must be max_tokens or max_completion_tokens")
        if self.input_modalities is not None and (not self.input_modalities or set(self.input_modalities) - {"text", "image", "document", "audio"}):
            raise ValueError("MODEL_INPUT_MODALITIES contains an unsupported modality")
        if type(self.web_max_concurrent_runs) is not int or self.web_max_concurrent_runs < 1:
            raise ValueError("CODEAGENT_WEB_MAX_CONCURRENT_RUNS must be a positive integer")

    @classmethod
    def from_env(cls, *, allow_unconfigured: bool = False) -> "EnvironmentConfig":
        _load_dotenv()
        from codeagent.model_settings import read_settings, apply_settings
        saved = read_settings(default_runtime_data_dir())
        chat = saved["services"]["chat"] if saved else None
        model_id = chat["model"] if chat else os.getenv("MODEL_ID", "")
        if not model_id and not allow_unconfigured:
            raise RuntimeError("Missing required environment variable: MODEL_ID")
        context_mode = os.getenv("CONTEXT_COMPACT_MODE", "model")
        summarization_model = (
            (_optional_env("SUMMARIZATION_MODEL_ID") or model_id)
            if context_mode == "model"
            else ""
        )
        config = cls(
            model_id=model_id,
            model_protocol=chat["protocol"] if chat else os.getenv("MODEL_PROTOCOL", "anthropic"),
            input_modalities=None if chat else tuple(part.strip() for part in os.environ["MODEL_INPUT_MODALITIES"].split(",")) if os.getenv("MODEL_INPUT_MODALITIES") else None,
            reasoning_levels=tuple(part.strip() for part in os.getenv("MODEL_REASONING_LEVELS", "").split(",") if part.strip()),
            chat_token_parameter=chat["token_parameter"] if chat else os.getenv("OPENAI_CHAT_TOKEN_PARAMETER", "max_completion_tokens"),
            speech_config=SpeechConfig() if saved else SpeechConfig(
                enabled=_bool_env("CODEAGENT_SPEECH_ENABLED", False),
                model=_optional_env("CODEAGENT_SPEECH_MODEL") or "",
                base_url=_optional_env("CODEAGENT_SPEECH_BASE_URL") or "",
                api_key=_optional_env("CODEAGENT_SPEECH_API_KEY") or "",
                language=_optional_env("CODEAGENT_SPEECH_LANGUAGE"),
                timeout_seconds=_float_env("CODEAGENT_SPEECH_TIMEOUT", 120.0),
            ),
            embedding_config=embedding_config_from_env(),
            lsp_config=LspConfig(
                enabled=_bool_env("CODEAGENT_LSP_ENABLED", True),
                servers=configured_servers(),
                timeout_seconds=_float_env("CODEAGENT_LSP_TIMEOUT", 5.),
                feedback_seconds=_float_env("CODEAGENT_LSP_FEEDBACK_TIMEOUT", 1.5),
            ),
            web_search_config=WebSearchConfig(
                enabled=_bool_env("CODEAGENT_WEB_SEARCH_ENABLED", False),
                api_key=_optional_env("TAVILY_API_KEY"),
                timeout_seconds=_float_env("CODEAGENT_WEB_SEARCH_TIMEOUT", 20.0),
            ),
            api_key=_first_optional_env("API_KEY", "ANTHROPIC_API_KEY" if os.getenv("MODEL_PROTOCOL", "anthropic") == "anthropic" else "OPENAI_API_KEY"),
            base_url=_first_optional_env("BASE_URL", "ANTHROPIC_BASE_URL" if os.getenv("MODEL_PROTOCOL", "anthropic") == "anthropic" else "OPENAI_BASE_URL"),
            max_tokens=chat["max_tokens"] if chat else _int_env("MAX_TOKENS", 32_000),
            reasoning_effort=_optional_env("REASONING_EFFORT") or "default",
            max_iterations=_int_env("MAX_ITERATIONS", 50),
            stream=chat["stream"] if chat else _bool_env("STREAMING", False),
            enable_skills=_bool_env("ENABLE_SKILLS", True),
            skill_roots=_path_list_env("SKILLS_DIR", (Path("skills"),)),
            context_config=ContextConfig(
                mode=context_mode,
                summarization_model=summarization_model,
                summarization_api_key=_optional_env("SUMMARIZATION_API_KEY"),
                tool_result_budget_chars=_int_env(
                    "CONTEXT_TOOL_RESULT_BUDGET_CHARS", 200_000
                ),
                single_tool_output_max_chars=_int_env(
                    "CONTEXT_SINGLE_TOOL_OUTPUT_MAX_CHARS", 80_000
                ),
                compact_threshold_chars=_int_env(
                    "CONTEXT_COMPACT_THRESHOLD_CHARS", 300_000
                ),
                summary_max_chars=_int_env("CONTEXT_SUMMARY_MAX_CHARS", 4_000),
                summary_max_tokens=_int_env("CONTEXT_SUMMARY_MAX_TOKENS", 8_192),
                command_output_max_chars=_int_env("CONTEXT_COMMAND_OUTPUT_MAX_CHARS", 12_000),
                read_reference_enabled=_bool_env("CONTEXT_READ_REFERENCE_ENABLED", True),
                transcript_dir=Path(
                    os.getenv("CONTEXT_TRANSCRIPT_DIR", ".transcripts")
                ),
                tool_output_dir=Path(
                    os.getenv(
                        "CONTEXT_TOOL_OUTPUT_DIR",
                        ".task_outputs/tool-results",
                    )
                ),
                reactive_retries=_int_env("CONTEXT_REACTIVE_RETRIES", 1),
                recency_messages=_int_env("CONTEXT_RECENCY_MESSAGES", 12),
                recency_rounds=_int_env("CONTEXT_RECENCY_ROUNDS", 2),
                min_fold_messages=_int_env("CONTEXT_MIN_FOLD_MESSAGES", 4),
                message_trigger_min_fold=_int_env("CONTEXT_MESSAGE_TRIGGER_MIN_FOLD", 16),
                round_trigger_min_fold=_int_env("CONTEXT_ROUND_TRIGGER_MIN_FOLD", 8),
                max_fold_messages=_int_env("CONTEXT_MAX_FOLD_MESSAGES", 200),
                max_fold_rounds=_int_env("CONTEXT_MAX_FOLD_ROUNDS", 12),
                summary_input_max_chars=_int_env("CONTEXT_SUMMARY_INPUT_MAX_CHARS", 120_000),
                context_window_tokens=_int_env("CONTEXT_WINDOW_TOKENS", 0),
                summary_context_window_tokens=_int_env("CONTEXT_SUMMARY_WINDOW_TOKENS", 0),
                failure_cooldown_seconds=_float_env("CONTEXT_FAILURE_COOLDOWN_SECONDS", 90.0),
                summary_timeout_seconds=_float_env("CONTEXT_SUMMARY_TIMEOUT_SECONDS", 45.0),
                tool_projection_enabled=_bool_env("CONTEXT_TOOL_PROJECTION_ENABLED", True),
                investigation_keep_rounds=_int_env("CONTEXT_INVESTIGATION_KEEP_ROUNDS", 2),
                command_keep_rounds=_int_env("CONTEXT_COMMAND_KEEP_ROUNDS", 1),
                write_keep_rounds=_int_env("CONTEXT_WRITE_KEEP_ROUNDS", 2),
                tool_clear_min_chars=_int_env("CONTEXT_TOOL_CLEAR_MIN_CHARS", 8_000),
                write_clear_min_chars=_int_env("CONTEXT_WRITE_CLEAR_MIN_CHARS", 8_000),
                summary_text_preview_chars=_int_env("CONTEXT_SUMMARY_TEXT_PREVIEW_CHARS", 4_000),
                summary_argument_preview_chars=_int_env("CONTEXT_SUMMARY_ARGUMENT_PREVIEW_CHARS", 2_000),
                model_context_windows=_model_windows_env(),
                near_context_ratio=_float_env("CONTEXT_NEAR_CONTEXT_RATIO", 0.8),
                cache_policy=os.getenv("CONTEXT_CACHE_POLICY", "auto"),
                cache_soft_ratio=_float_env("CONTEXT_CACHE_SOFT_RATIO", 0.8),
                cache_boundary_growth_ratio=_float_env("CONTEXT_CACHE_BOUNDARY_GROWTH_RATIO", 0.1),
            ),
            memory_config=MemoryConfig(
                enabled=_bool_env("ENABLE_MEMORY", True),
                memory_dir=Path(os.getenv("MEMORY_DIR", ".memory")),
                max_items_in_prompt=_int_env("MEMORY_MAX_ITEMS_IN_PROMPT", 50),
                max_loaded_items=_int_env("MEMORY_MAX_LOADED_ITEMS", 5),
                session_budget_chars=_int_env("MEMORY_SESSION_BUDGET_CHARS", 60_000),
                max_memory_bytes=_int_env("MEMORY_MAX_MEMORY_BYTES", 50_000),
                selection_mode=os.getenv("MEMORY_SELECTION_MODE", "llm"),
                auto_extract=_bool_env("MEMORY_AUTO_EXTRACT", False),
                extract_recent_messages=_int_env("MEMORY_EXTRACT_RECENT_MESSAGES", 12),
                consolidate_threshold=_int_env("MEMORY_CONSOLIDATE_THRESHOLD", 30),
                consolidate_mode=os.getenv("MEMORY_CONSOLIDATE_MODE", "simple"),
                allow_subagent_write=_bool_env("MEMORY_ALLOW_SUBAGENT_WRITE", False),
                retrieval_mode=os.getenv("MEMORY_RETRIEVAL_MODE", "indexed"),
                index_verify_seconds=_float_env("MEMORY_INDEX_VERIFY_SECONDS", 60.0),
            ),
            prompt_config=PromptConfig(
                template_dir=_optional_path_env("PROMPT_TEMPLATE_DIR"),
                system_budget_chars=_int_env("SYSTEM_PROMPT_BUDGET_CHARS", 120_000),
                static_budget_chars=_int_env(
                    "SYSTEM_PROMPT_STATIC_BUDGET_CHARS", 50_000
                ),
                dynamic_budget_chars=_int_env(
                    "SYSTEM_PROMPT_DYNAMIC_BUDGET_CHARS", 70_000
                ),
                skill_catalog_budget_chars=_int_env(
                    "SKILL_CATALOG_BUDGET_CHARS", 12_000
                ),
                emit_trace=_bool_env("PROMPT_TRACE", False),
            ),
            recovery_config=RecoveryConfig(
                enabled=_bool_env("RECOVERY_ENABLED", True),
                max_retries=_int_env("RECOVERY_MAX_RETRIES", 10),
                base_delay_ms=_int_env("RECOVERY_BASE_DELAY_MS", 500),
                max_delay_ms=_int_env("RECOVERY_MAX_DELAY_MS", 32_000),
                jitter_ratio=_float_env("RECOVERY_JITTER_RATIO", 0.25),
                max_continuations=_int_env("RECOVERY_MAX_CONTINUATIONS", 3),
                escalated_max_tokens=_int_env(
                    "RECOVERY_ESCALATED_MAX_TOKENS", 64_000
                ),
                overload_fallback_after=_int_env(
                    "RECOVERY_OVERLOAD_FALLBACK_AFTER", 3
                ),
                fallback_model=os.getenv("FALLBACK_MODEL_ID", ""),
                side_query_max_retries=_int_env("RECOVERY_SIDE_QUERY_MAX_RETRIES", 2),
                trace=_bool_env("RECOVERY_TRACE", False),
            ),
            loop_guard_config=LoopGuardConfig(
                window_size=_int_env("CODEAGENT_LOOP_WINDOW", 12),
                repeat_failure_limit=_int_env("CODEAGENT_LOOP_REPEAT_FAILURE_LIMIT", 3),
                parameter_error_limit=_int_env("CODEAGENT_LOOP_PARAMETER_ERROR_LIMIT", 2),
                blocked_attempt_limit=_int_env("CODEAGENT_LOOP_BLOCKED_ATTEMPT_LIMIT", 3),
                empty_response_limit=_int_env("CODEAGENT_LOOP_EMPTY_RESPONSE_LIMIT", 2),
                max_model_calls=_int_env("CODEAGENT_RUN_MAX_MODEL_CALLS", 80),
                max_tool_calls=_int_env("CODEAGENT_RUN_MAX_TOOL_CALLS", 200),
                max_total_tokens=_int_env("CODEAGENT_RUN_MAX_TOTAL_TOKENS", 0),
                max_active_seconds=_float_env("CODEAGENT_RUN_MAX_ACTIVE_SECONDS", 1800.0),
                tool_max_retries=_int_env("CODEAGENT_LOOP_TOOL_MAX_RETRIES", 2),
                retry_delay_seconds=_float_env("CODEAGENT_LOOP_RETRY_DELAY_SECONDS", 0.25),
            ),
            data_dir=Path(os.getenv("CODEAGENT_DATA_DIR") or default_runtime_data_dir()),
            planning_mode=PlanningBackend.parse(
                os.getenv("CODEAGENT_PLANNING_MODE", "auto")
            ),
            mcp_config_path=Path(os.getenv("MCP_CONFIG", "mcp.json")),
            team_runtime_enabled=_bool_env("TEAM_RUNTIME_ENABLED", False),
            team_write_enabled=_bool_env("TEAM_WRITE_ENABLED", False),
            web_max_concurrent_runs=_int_env("CODEAGENT_WEB_MAX_CONCURRENT_RUNS", 4),
            parallel_config=ParallelConfig(
                enabled=_bool_env("CODEAGENT_PARALLEL_ENABLED", True),
                max_tools=_int_env("CODEAGENT_PARALLEL_TOOLS", 4),
                max_subagents=_int_env("CODEAGENT_PARALLEL_SUBAGENTS", 3),
                max_pending_subagents=_int_env("CODEAGENT_SUBAGENT_QUEUE_LIMIT", 16),
            ),
            team_model_response_timeout=_float_env("TEAM_MODEL_RESPONSE_TIMEOUT", 300.0),
            team_model_call_timeout=_float_env("TEAM_MODEL_CALL_TIMEOUT", 600.0),
            team_worktree_root=Path(
                os.getenv("TEAM_WORKTREE_ROOT", ".codeagent-worktrees")
            ),
        )

        return apply_settings(config, saved["services"]) if saved else config

    def to_agent_config(
        self,
        *,
        planning_backend: PlanningBackend | None = None,
    ) -> AgentConfig:
        return AgentConfig(
            model=self.model_id,
            max_tokens=self.max_tokens,
            max_iterations=self.max_iterations,
            planning_backend=planning_backend or self.planning_mode,
            loop_guard=self.loop_guard_config,
            parallel=self.parallel_config,
        )

    def create_anthropic_client(
        self,
        *,
        stream: bool | None = None,
        on_text: Callable[[str], None] | None = None,
        event_emitter: Any | None = None,
        usage_tracker: Any | None = None,
        call_kind: str = "main",
        reasoning_effort: str | None = None,
    ) -> AnthropicModelClient:
        """Legacy factory name, retained for integrations; respects MODEL_PROTOCOL."""
        client_type = AnthropicModelClient if self.model_protocol == "anthropic" else OpenAIModelClient
        options = {} if self.model_protocol == "anthropic" else {"protocol": self.model_protocol, "token_parameter": self.chat_token_parameter}
        client = client_type(
            **options,
            api_key=self.api_key,
            base_url=self.base_url,
            stream=self.stream if stream is None else stream,
            on_text=on_text,
            event_emitter=event_emitter,
            usage_tracker=usage_tracker,
            call_kind=call_kind,
            reasoning_effort=(reasoning_effort if reasoning_effort is not None else self.reasoning_effort)
            if call_kind in {"main", "subagent"} else "default",
        )
        client.input_modalities = self.input_modalities
        return client

    def create_model_client(self, **kwargs: Any) -> ModelClient:
        """Create the shared conversation model used by all Agent roles."""
        return self.create_anthropic_client(**kwargs)


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def embedding_config_from_env() -> EmbeddingConfig:
    """Read embedding settings only; do not require a chat/summary model."""
    from codeagent.model_settings import read_settings
    saved = read_settings(default_runtime_data_dir())
    if saved:
        return EmbeddingConfig(**saved["services"]["embedding"], max_chunks=_int_env("CODEAGENT_EMBEDDING_MAX_CHUNKS", 2000))
    return EmbeddingConfig(
        enabled=_bool_env("CODEAGENT_EMBEDDING_ENABLED", False),
        base_url=_optional_env("CODEAGENT_EMBEDDING_BASE_URL") or "",
        api_key=_optional_env("CODEAGENT_EMBEDDING_API_KEY") or "",
        model=_optional_env("CODEAGENT_EMBEDDING_MODEL") or "",
        dimensions=_int_env("CODEAGENT_EMBEDDING_DIMENSIONS", 0),
        timeout_seconds=_float_env("CODEAGENT_EMBEDDING_TIMEOUT", 10.),
        max_chunks=_int_env("CODEAGENT_EMBEDDING_MAX_CHUNKS", 2000),
        batch_size=_int_env("CODEAGENT_EMBEDDING_BATCH_SIZE", 10),
    )


def _optional_env(name: str) -> str | None:
    value = os.getenv(name)
    return value or None


def _optional_path_env(name: str) -> Path | None:
    value = _optional_env(name)
    return Path(value) if value is not None else None


def _first_optional_env(*names: str) -> str | None:
    for name in names:
        value = _optional_env(name)
        if value is not None:
            return value
    return None


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got: {value}") from exc


def _model_windows_env() -> dict[str, int]:
    try:
        value = json.loads(os.getenv("CONTEXT_MODEL_WINDOWS_JSON") or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("CONTEXT_MODEL_WINDOWS_JSON must be a JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("CONTEXT_MODEL_WINDOWS_JSON must be a JSON object")
    return value


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a float, got: {value}") from exc


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if not value:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean, got: {value}")


def _path_list_env(name: str, default: tuple[Path, ...]) -> tuple[Path, ...]:
    value = os.getenv(name)
    if not value:
        return default
    paths = [Path(part.strip()) for part in value.split(os.pathsep) if part.strip()]
    return tuple(paths) if paths else default
