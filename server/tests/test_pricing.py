from critdash.pricing import compute_cost_usd, rate_for_model


def _rate(input_, output, cw5m, cw1h, cache_read):
    return {
        "input": input_, "output": output,
        "cache_write_5m": cw5m, "cache_write_1h": cw1h, "cache_read": cache_read,
    }


PRICING = {
    "_comment": "ignore me, not a model key",
    "models": {
        "claude-opus-5": _rate(5.0, 25.0, 6.25, 10.0, 0.50),
        "claude-sonnet-5": _rate(2.0, 10.0, 2.50, 4.0, 0.20),
        "claude-haiku-4-5": _rate(1.0, 5.0, 1.25, 2.0, 0.10),
        "default": _rate(3.0, 15.0, 3.75, 6.0, 0.30),
    },
    "fast_mode": {
        "_comment": "ignore me too",
        "claude-opus-5": _rate(10.0, 50.0, 12.50, 20.0, 1.00),
    },
    "monthly_budget_usd": 2000,
}


def test_longest_prefix_match():
    assert rate_for_model(PRICING, "claude-opus-5")["input"] == 5.0
    # a hypothetical more-specific future key should win over the shorter one
    pricing2 = {"models": dict(PRICING["models"])}
    pricing2["models"]["claude-opus-5-fast"] = {
        "input": 30.0, "output": 100.0, "cache_write_5m": 0, "cache_write_1h": 0, "cache_read": 0,
    }
    assert rate_for_model(pricing2, "claude-opus-5-fast")["input"] == 30.0
    assert rate_for_model(pricing2, "claude-opus-5-slow")["input"] == 5.0  # falls back to shorter prefix


def test_unknown_model_falls_back_to_default():
    rate = rate_for_model(PRICING, "claude-mystery-9000")
    assert rate == PRICING["models"]["default"]


def test_model_id_with_date_suffix_still_prefix_matches():
    # real jsonl model ids sometimes carry a date suffix, e.g. from the live DB:
    # "claude-haiku-4-5-20251001" -- must still match the "claude-haiku-4-5" key.
    rate = rate_for_model(PRICING, "claude-haiku-4-5-20251001")
    assert rate == PRICING["models"]["claude-haiku-4-5"]


def test_underscore_keys_are_ignored_as_comments():
    # "_comment" must never be treated as a model prefix, even though every
    # model string technically doesn't start with "_" -- this guards against
    # a future key like "_default" or similar being matched by accident.
    pricing = {"models": {"_should_never_match": {"input": 999.0}, "default": PRICING["models"]["default"]}}
    rate = rate_for_model(pricing, "_should_never_match")
    assert rate == pricing["models"]["default"]


def test_fast_mode_used_only_when_speed_is_fast():
    standard = rate_for_model(PRICING, "claude-opus-5", speed="standard")
    fast = rate_for_model(PRICING, "claude-opus-5", speed="fast")
    assert standard["input"] == 5.0
    assert fast["input"] == 10.0
    assert fast is PRICING["fast_mode"]["claude-opus-5"]


def test_fast_mode_falls_back_to_standard_when_model_has_no_fast_entry():
    # claude-sonnet-5 has no fast_mode entry in PRICING
    rate = rate_for_model(PRICING, "claude-sonnet-5", speed="fast")
    assert rate == PRICING["models"]["claude-sonnet-5"]


def test_cost_math_hand_computed():
    # 1,000,000 input @ $5, 1,000,000 output @ $25, 1,000,000 cache_read @ $0.50,
    # 1,000,000 cache_write_5m @ $6.25, 1,000,000 cache_write_1h @ $10
    # => 5 + 25 + 0.5 + 6.25 + 10 = 46.75
    cost = compute_cost_usd(PRICING, "claude-opus-5", 1_000_000, 1_000_000, 1_000_000, 1_000_000, 1_000_000)
    assert round(cost, 2) == 46.75


def test_cost_math_5m_and_1h_are_not_collapsed():
    # same total cache-write tokens (1,000,000), but split differently between
    # the 5m and 1h buckets must produce different costs since the rates
    # differ by 1.6x (10.0 / 6.25) -- this is the bug this test guards against.
    all_5m = compute_cost_usd(PRICING, "claude-opus-5", 0, 0, 0, 1_000_000, 0)
    all_1h = compute_cost_usd(PRICING, "claude-opus-5", 0, 0, 0, 0, 1_000_000)
    assert all_5m != all_1h
    assert round(all_5m, 2) == 6.25
    assert round(all_1h, 2) == 10.0


def test_cost_math_real_sample():
    # from tests/fixtures/usage_real_assistant.jsonl: input=2, output=449,
    # cache_read=23978, cache_write_5m=0, cache_write_1h=29923, model claude-opus-5
    cost = compute_cost_usd(PRICING, "claude-opus-5", 2, 449, 23978, 0, 29923)
    rate = PRICING["models"]["claude-opus-5"]
    expected = (
        (2 / 1e6 * rate["input"])
        + (449 / 1e6 * rate["output"])
        + (23978 / 1e6 * rate["cache_read"])
        + (29923 / 1e6 * rate["cache_write_1h"])
    )
    assert abs(cost - expected) < 1e-9
    assert cost > 0


def test_speed_fast_uses_fast_mode_rate_in_full_cost():
    standard_cost = compute_cost_usd(PRICING, "claude-opus-5", 1_000_000, 0, 0, 0, 0, speed="standard")
    fast_cost = compute_cost_usd(PRICING, "claude-opus-5", 1_000_000, 0, 0, 0, 0, speed="fast")
    assert round(standard_cost, 2) == 5.0
    assert round(fast_cost, 2) == 10.0
