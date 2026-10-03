"""Provider-verified reasoning options for the Anthropic Messages transport."""

from __future__ import annotations

from urllib.parse import urlsplit

from codeagent.model_metadata import model_windows


# Official fallback only; never infer a provider from a model-name substring.
# https://api-docs.deepseek.com/guides/thinking_mode/
_DEEPSEEK_MODELS = frozenset({
    "deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp", "deepseek-v4-pro",
})


def reasoning_capabilities(*, base_url: str | None, model: str, api_key: str | None = None) -> dict:
    result = {"supported_levels": [], "default_level": None, "supports_disabled": False,
              "source": "unavailable"}
    if urlsplit(base_url or "").hostname != "api.deepseek.com":
        return result
    metadata = model_windows.resolve(base_url=base_url, model=model, api_key=api_key or "")
    levels = metadata.get("reasoning_supported_levels")
    if levels:
        return {"supported_levels": list(levels), "default_level": metadata.get("reasoning_default_level"),
                "supports_disabled": True, "source": "model_api"}
    if model in _DEEPSEEK_MODELS:
        return {"supported_levels": ["low", "high", "max"], "default_level": "high",
                "supports_disabled": True, "source": "official_docs"}
    return result


def environment_capabilities(env) -> dict:
    if getattr(env, "model_protocol", "anthropic") != "anthropic":
        levels = list(getattr(env, "reasoning_levels", ()))
        return {"supported_levels": [level for level in levels if level != "none"], "default_level": None,
                "supports_disabled": "none" in levels, "source": "configuration" if levels else "unavailable"}
    return reasoning_capabilities(base_url=getattr(env, "base_url", None),
                                  model=getattr(env, "model_id", ""), api_key=getattr(env, "api_key", None))


def validate_effort(effort: str | None, capabilities: dict) -> None:
    if effort is None or effort == "default":
        return
    allowed = list(capabilities["supported_levels"])
    if capabilities["supports_disabled"]:
        allowed.append("none")
    if effort not in allowed:
        raise ValueError(f"当前模型/接口不支持推理等级 {effort!r}；可选值：{', '.join(['default', *allowed])}")


def reasoning_parameters(effort: str | None, capabilities: dict) -> dict:
    validate_effort(effort, capabilities)
    if effort is None or effort == "default":
        return {}
    if effort == "none":
        return {"thinking": {"type": "disabled"}}
    return {"thinking": {"type": "enabled"}, "output_config": {"effort": effort}}
