"""Cost model per SPEC ## Cost model / briefing Task 1.

`config/pricing.json` shape:

    models.<model-id-prefix> -> {input, output, cache_write_5m, cache_write_1h, cache_read}
    fast_mode.<model-id>     -> same shape, used when message.usage.speed == "fast"
    monthly_budget_usd       -> number

All rates are USD per 1,000,000 tokens. Keys beginning with "_" are comments
and must be ignored when matching.

Cost per message:

    input_tokens             / 1e6 * input
    output_tokens             / 1e6 * output
    cache_read_input_tokens  / 1e6 * cache_read
    cache_creation.ephemeral_5m_input_tokens / 1e6 * cache_write_5m
    cache_creation.ephemeral_1h_input_tokens / 1e6 * cache_write_1h

The 5m and 1h cache-write rates are charged separately -- they differ by 1.6x
and this fleet runs 1h-TTL caching, so collapsing them into one bucket
understates spend. `input_tokens` already excludes cached tokens, so it is
never double counted against the cache buckets.
"""

from __future__ import annotations

_FALLBACK_RATE = {
    "input": 3.0,
    "output": 15.0,
    "cache_write_5m": 3.75,
    "cache_write_1h": 6.0,
    "cache_read": 0.30,
}


def _longest_prefix_match(table: dict, model: str) -> dict | None:
    """Longest-prefix match of `model` against `table`'s keys. Keys starting
    with "_" (comments) and the literal "default" key are never matched as
    prefixes -- "default" is only ever used as the explicit fallback."""
    best_key = None
    best_len = -1
    for key in table:
        if key.startswith("_") or key == "default":
            continue
        if model.startswith(key) and len(key) > best_len:
            best_key = key
            best_len = len(key)
    return table[best_key] if best_key is not None else None


def rate_for_model(pricing: dict, model: str, speed: str | None = None) -> dict:
    """Resolve the rate dict for `model`.

    If `speed == "fast"` and a `fast_mode` entry matches `model` (by longest
    prefix), that rate wins. Otherwise match `models` by longest prefix,
    falling back to `models.default`, then to a hardcoded fallback rate if
    even that is missing.
    """
    models = pricing.get("models", {})

    if speed == "fast":
        fast_mode = pricing.get("fast_mode", {})
        rate = _longest_prefix_match(fast_mode, model)
        if rate is not None:
            return rate

    rate = _longest_prefix_match(models, model)
    if rate is not None:
        return rate

    default = models.get("default")
    if default is not None:
        return default

    return _FALLBACK_RATE


def compute_cost_usd(
    pricing: dict,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_5m_tokens: int,
    cache_write_1h_tokens: int,
    speed: str | None = None,
) -> float:
    rate = rate_for_model(pricing, model, speed)
    return (
        input_tokens / 1_000_000 * rate.get("input", 0.0)
        + output_tokens / 1_000_000 * rate.get("output", 0.0)
        + cache_read_tokens / 1_000_000 * rate.get("cache_read", 0.0)
        + cache_write_5m_tokens / 1_000_000 * rate.get("cache_write_5m", 0.0)
        + cache_write_1h_tokens / 1_000_000 * rate.get("cache_write_1h", 0.0)
    )
