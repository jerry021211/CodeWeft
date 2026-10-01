"""User-supplied off-peak estimate; independent of actual request time."""
from decimal import Decimal


PRICING = {
    "id": "user-off-peak-cny-v1",
    "currency": "CNY",
    "period": "off_peak",
    "source": "user_provided_pricing_screenshot",
    "per_million_tokens": {"cache_hit_input": "0.02", "cache_miss_input": "1", "output": "4"},
    "usage_format": "anthropic_messages_disjoint_input_categories",
    "cache_creation_policy": "count_as_cache_miss_input",
    "scope": "recorded_model_calls_only",
}
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def money_number(value):
    # The cheapest single token is CNY 0.00000002; never round at each call.
    return float(Decimal(value).quantize(Decimal("0.00000001")))


def price_usage(usage):
    if not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0 for k in TOKEN_FIELDS):
        return None
    counts = {"cache_hit_input": usage["cache_read_input_tokens"],
              "cache_miss_input": usage["input_tokens"] + usage["cache_creation_input_tokens"],
              "output": usage["output_tokens"]}
    parts = {k: Decimal(count) * Decimal(PRICING["per_million_tokens"][k]) / 1_000_000
             for k, count in counts.items()}
    return {**{k: money_number(v) for k, v in parts.items()}, "total": money_number(sum(parts.values()))}


def summarize_cost(rows):
    measured = [r["cost_components_cny"] for r in rows if r.get("cost_components_cny") is not None]
    complete = bool(rows) and len(measured) == len(rows)
    known = {k: money_number(sum((Decimal(str(r[k])) for r in measured), Decimal(0))) if measured else None
             for k in ("cache_hit_input", "cache_miss_input", "output", "total")}
    return {"cost": known["total"] if complete else None,
            "cost_average_per_trial": money_number(Decimal(str(known["total"])) / len(rows)) if complete else None,
            "cost_complete": complete, "cost_measured_trials": len(measured),
            "cost_known_subtotal": known["total"],
            "cost_components_cny": {k: v if complete else None for k, v in known.items()},
            "cost_known_components_cny": known,
            "cost_note": "按用户指定空闲单价估算记录内模型调用，缓存写入按未命中输入计费；不按实际调用时间切换价格，不含未接入记录的其他服务费用。"}


def format_money(value):
    return "未知" if value is None else f"¥{value:.8f}"
