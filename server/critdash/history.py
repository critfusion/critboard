"""GET /api/history/usage: window/bucket parsing and the group_by=model/
provider/host grouped response.

The `group_by=none` (default) flat response is NOT built here -- it stays
exactly as it was (see main.py), byte-compatible for the existing 48h
timeline widget. This module only backs the new grouped shape, plus the
shared window parser both paths use.

Union-with-no-double-counting: every grouped query below combines this
host's local tables (usage_events, kimi_turn_events) with every OTHER host's
pre-aggregated remote_* tables. RemoteCollector only probes hosts with
mode == "ssh" (never the local one -- see collectors/remote.py), so the two
sets never overlap by host and nothing is summed twice.

Kimi cost is always None (null), never 0.0 -- Kimi bills by subscription
quota, not per token (collectors/kimi.py). A `totals.cost_usd` or a
group_by=host series' cost_usd is therefore always a CLAUDE-ONLY figure: real
(possibly 0.0) wherever no Claude spend happened, never null, because "how
much did Claude cost here" is always a well-defined number -- it is only
Kimi's OWN per-message cost that is undefined.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from .pricing import compute_cost_usd
from .store import Store, dense_bucket_keys

_WINDOW_RE = re.compile(r"^(\d+)(h|d)$")
_TOP_N_SERIES = 10


class InvalidWindowError(ValueError):
    """A window string that cannot be parsed as 'today' or '<N>h'/'<N>d'."""


def parse_window(window: str, now: datetime) -> tuple[datetime, datetime]:
    """Returns (since, until) for a window string; `until` is always `now`.

    'today' is calendar-anchored (UTC midnight up to `now`). Any '<N>h' or
    '<N>d' (N > 0) -- including the previously-hardcoded '24h'/'7d'/'30d' and
    the previously-broken '90d' -- is a fixed-length lookback ending at
    `now`. Anything else raises InvalidWindowError rather than silently
    falling back to a default, so a caller asking for an unsupported window
    finds out immediately instead of quietly getting the wrong data."""
    if window == "today":
        since = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return since, now
    m = _WINDOW_RE.match(window)
    if m:
        n = int(m.group(1))
        if n <= 0:
            raise InvalidWindowError(f"window must be a positive number: {window!r}")
        delta = timedelta(hours=n) if m.group(2) == "h" else timedelta(days=n)
        return now - delta, now
    raise InvalidWindowError(
        f"unrecognized window {window!r}: expected 'today' or '<N>h'/'<N>d' (e.g. '24h', '7d', '30d', '90d')"
    )


def bucket_count(since_dt: datetime, until_dt: datetime, bucket: str, window: str) -> int:
    """Number of dense buckets to render. Identical to the pre-existing
    formula (max(1, delta // unit)) for every fixed-length window, so 24h/7d/
    30d/90d/<N>h/<N>d are unchanged from before this endpoint grew group_by.
    'today' is calendar-anchored, not fixed-length, so it gets its own rule:
    every hour from midnight through the current (partial) hour, inclusive."""
    span = (until_dt - since_dt).total_seconds()
    if bucket == "day":
        if window == "today":
            return 1
        return max(1, int(span // 86400))
    if window == "today":
        return int(span // 3600) + 1
    return max(1, int(span // 3600))


def _fold_cache_write(row) -> int:
    return row["cache_write_5m"] + row["cache_write_1h"]


def _empty_totals() -> dict:
    return {
        "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
        "cost": None, "tokens": 0, "messages": 0,
    }


def _accumulate(
    store: Store, pricing: dict, since_iso: str, since_day: str, bucket: str, bucket_set: set[str],
    tz_name: str = "UTC",
):
    """Returns acc: dict[(provider, host, model)][bucket_key] -> mutable
    totals dict. Every one of the four source queries feeds into the SAME
    accumulator keyed by (provider, host, model, bucket) -- union, not
    concatenation, so a (provider, host, model) that has both local and
    remote rows in the same bucket (impossible today since local/remote are
    disjoint by host, but true in general for two hosts sharing a model in
    one bucket) is summed correctly rather than overwritten. `bucket_set` is
    a defensive filter for the UTC (tz_name="UTC") case (build_grouped_history
    already anchors since_iso/since_day to the first dense bucket's own
    start, so this should never trim anything there in practice) -- but for
    bucket='day' with a non-UTC tz_name it does real work: the three local/
    remote-hourly sources below are queried with a widened lookback so a
    local day near the window edge is never cut short (see Store's
    *_grouped_timeline day+tz_name path), and bucket_set is what trims that
    deliberate over-fetch back down to exactly the requested range."""
    acc: dict[tuple[str, str, str], dict[str, dict]] = {}

    def cell(provider: str, host: str, model: str, bkey: str) -> dict:
        by_bucket = acc.setdefault((provider, host, model), {})
        return by_bucket.setdefault(bkey, _empty_totals())

    for r in store.usage_events_grouped_timeline(since_iso, bucket, tz_name):
        if r["bucket"] not in bucket_set:
            continue
        c = cell("claude", r["host"], r["model"] or "unknown", r["bucket"])
        c["input"] += r["input"]
        c["output"] += r["output"]
        c["cache_read"] += r["cache_read"]
        c["cache_write"] += r["cache_write"]
        c["messages"] += r["messages"]
        c["cost"] = (c["cost"] or 0.0) + r["cost_usd"]
        c["tokens"] += r["input"] + r["output"] + r["cache_read"] + r["cache_write"]

    for r in store.remote_usage_grouped_timeline(since_iso, bucket, tz_name):
        if r["bucket"] not in bucket_set:
            continue
        cost = compute_cost_usd(
            pricing, r["model"], r["input"], r["output"], r["cache_read"],
            r["cache_write_5m"], r["cache_write_1h"],
        )
        cw = _fold_cache_write(r)
        c = cell("claude", r["host"], r["model"] or "unknown", r["bucket"])
        c["input"] += r["input"]
        c["output"] += r["output"]
        c["cache_read"] += r["cache_read"]
        c["cache_write"] += cw
        c["messages"] += r["messages"]
        c["cost"] = (c["cost"] or 0.0) + cost
        c["tokens"] += r["input"] + r["output"] + r["cache_read"] + cw

    for r in store.kimi_turn_grouped_timeline(since_iso, bucket, tz_name):
        if r["bucket"] not in bucket_set:
            continue
        c = cell("kimi", r["host"], r["model"] or "unknown", r["bucket"])
        c["tokens"] += r["tokens"]
        c["messages"] += r["messages"]
        # cost intentionally never touched here -- stays None (Kimi bills by
        # subscription quota, not per token; see module docstring).

    if bucket == "day":
        # NOT timezone-aware -- remote_kimi_usage_buckets is pre-aggregated
        # to a UTC calendar day before it reaches this host (see
        # Store.remote_kimi_grouped_timeline's docstring); a real resolution
        # gap for a non-UTC tz_name, not an oversight.
        for r in store.remote_kimi_grouped_timeline(since_day):
            if r["bucket"] not in bucket_set:
                continue
            c = cell("kimi", r["host"], r["model"] or "unknown", r["bucket"])
            c["tokens"] += r["tokens"]
            c["messages"] += r["messages"]

    return acc


def _group_series(acc, group_by: str) -> dict[str, dict]:
    """Reduces the (provider, host, model)-keyed accumulator down to
    group_by's dimension. For group_by='host' a series can combine BOTH
    providers (one physical machine running Claude and Kimi); its cost_usd
    is then the CLAUDE-ONLY portion (a real, always-defined number -- see
    module docstring), and its token-category breakdown (input/output/
    cache_read/cache_write) only reflects Claude, since Kimi has no such
    breakdown -- Kimi still contributes to that series' `tokens` total."""
    series_map: dict[str, dict] = {}
    for (provider, host, model), by_bucket in acc.items():
        if group_by == "model":
            skey, sprovider = model, provider
        elif group_by == "provider":
            skey, sprovider = provider, provider
        else:  # host
            skey, sprovider = host, None

        s = series_map.setdefault(skey, {"provider": sprovider, "cells": {}})
        for bkey, v in by_bucket.items():
            c = s["cells"].setdefault(bkey, _empty_totals())
            if group_by == "host":
                c["tokens"] += v["tokens"]
                c["messages"] += v["messages"]
                if c["cost"] is None:
                    c["cost"] = 0.0
                if provider == "claude":
                    c["input"] += v["input"]
                    c["output"] += v["output"]
                    c["cache_read"] += v["cache_read"]
                    c["cache_write"] += v["cache_write"]
                    c["cost"] += v["cost"] or 0.0
            else:
                c["input"] += v["input"]
                c["output"] += v["output"]
                c["cache_read"] += v["cache_read"]
                c["cache_write"] += v["cache_write"]
                c["tokens"] += v["tokens"]
                c["messages"] += v["messages"]
                if v["cost"] is not None:
                    c["cost"] = (c["cost"] or 0.0) + v["cost"]
    return series_map


def _finalize(series_map: dict[str, dict], bucket_keys: list[str]) -> list[dict]:
    out = []
    for skey, s in series_map.items():
        provider = s["provider"]
        default_cost = None if provider == "kimi" else 0.0
        cells = s["cells"]
        tokens, cost_usd, inp, outp, cread, cwrite = [], [], [], [], [], []
        for bkey in bucket_keys:
            c = cells.get(bkey)
            if c is None:
                tokens.append(0)
                cost_usd.append(default_cost)
                inp.append(0)
                outp.append(0)
                cread.append(0)
                cwrite.append(0)
            else:
                tokens.append(c["tokens"])
                cost_usd.append(round(c["cost"], 6) if c["cost"] is not None else None)
                inp.append(c["input"])
                outp.append(c["output"])
                cread.append(c["cache_read"])
                cwrite.append(c["cache_write"])
        out.append({
            "key": skey, "provider": provider,
            "tokens": tokens, "cost_usd": cost_usd,
            "input": inp, "output": outp, "cache_read": cread, "cache_write": cwrite,
            "_total_tokens": sum(tokens),
        })
    return out


def _fold_other(folded: list[dict], bucket_keys: list[str]) -> dict:
    n = len(bucket_keys)
    tokens = [0] * n
    inp = [0] * n
    outp = [0] * n
    cread = [0] * n
    cwrite = [0] * n
    cost_usd: list[float | None] = [None] * n
    for e in folded:
        for i in range(n):
            tokens[i] += e["tokens"][i]
            inp[i] += e["input"][i]
            outp[i] += e["output"][i]
            cread[i] += e["cache_read"][i]
            cwrite[i] += e["cache_write"][i]
            if e["cost_usd"][i] is not None:
                cost_usd[i] = (cost_usd[i] or 0.0) + e["cost_usd"][i]
    return {
        "key": "other", "provider": None, "is_other": True, "folded_series_count": len(folded),
        "tokens": tokens, "cost_usd": [round(v, 6) if v is not None else None for v in cost_usd],
        "input": inp, "output": outp, "cache_read": cread, "cache_write": cwrite,
    }


def _cap_series(finalized: list[dict], bucket_keys: list[str]) -> tuple[list[dict], bool]:
    finalized.sort(key=lambda e: e["_total_tokens"], reverse=True)
    capped = len(finalized) > _TOP_N_SERIES
    kept = finalized[:_TOP_N_SERIES] if capped else finalized
    folded = finalized[_TOP_N_SERIES:] if capped else []
    for e in kept:
        e.pop("_total_tokens", None)
    if folded:
        for e in folded:
            e.pop("_total_tokens", None)
        kept.append(_fold_other(folded, bucket_keys))
    return kept, capped


def _first_seen_map(store: Store) -> dict[tuple[str, str, str], str]:
    """(provider, host, model) -> earliest-ever timestamp/day seen for that
    series, across the FULL history (not limited to the requested window) --
    this is what lets the coverage block tell a client apart "no data
    recorded yet" from "recorded and genuinely zero"."""
    fs: dict[tuple[str, str, str], str] = {}

    def note(key: tuple[str, str, str], ts: str) -> None:
        if key not in fs or ts < fs[key]:
            fs[key] = ts

    for r in store.usage_events_first_seen():
        note(("claude", r["host"], r["model"] or "unknown"), r["first_ts"])
    for r in store.remote_usage_buckets_first_seen():
        ts = r["first_ts"]
        ts = ts + ":00:00Z" if len(ts) == 13 else ts
        note(("claude", r["host"], r["model"] or "unknown"), ts)
    for r in store.kimi_turn_events_first_seen():
        note(("kimi", r["host"], r["model"] or "unknown"), r["first_ts"])
    for r in store.remote_kimi_usage_buckets_first_seen():
        ts = r["first_ts"]
        ts = ts + "T00:00:00Z" if len(ts) == 10 else ts
        note(("kimi", r["host"], r["model"] or "unknown"), ts)
    return fs


def _coverage(fs, group_by: str, series: list[dict], since_dt: datetime, until_dt: datetime) -> dict:
    """Per group_by='host'/'provider', coverage is reported for EVERY known
    host/provider (from the full, unwindowed first-seen map), not only ones
    with activity inside the requested window -- a host whose only data
    predates the window (e.g. a remote host's history starts well before a
    90d lookback) is exactly the kind of fleet-completeness fact this metadata
    exists to surface, so it must not disappear just because that host is
    currently silent. For group_by='model', the model namespace is
    unbounded and already capped in `series` (see _cap_series), so coverage
    is reported only for the models actually returned there -- an "other"
    fold has no single well-defined first_data and is skipped."""
    requested_from = since_dt.strftime("%Y-%m-%d")
    requested_to = until_dt.strftime("%Y-%m-%d")

    per_key: dict[str, str] = {}
    if group_by == "model":
        wanted = {s["key"] for s in series if not s.get("is_other")}
        for (_prov, _host, model), ts in fs.items():
            if model in wanted and (model not in per_key or ts < per_key[model]):
                per_key[model] = ts
    elif group_by == "provider":
        for (prov, _host, _model), ts in fs.items():
            if prov not in per_key or ts < per_key[prov]:
                per_key[prov] = ts
    else:  # host
        for (_prov, host, _model), ts in fs.items():
            if host not in per_key or ts < per_key[host]:
                per_key[host] = ts

    cov_series = []
    complete_froms = []
    for key, ts in sorted(per_key.items()):
        first_data = ts[:10]
        complete_from = max(first_data, requested_from)
        cov_series.append({
            "key": key, "first_data": first_data,
            "complete_from": complete_from, "partial": first_data > requested_from,
        })
        complete_froms.append(complete_from)
    fleet_complete_from = max(complete_froms) if complete_froms else requested_from
    return {
        "requested_from": requested_from, "requested_to": requested_to,
        "series": cov_series, "fleet_complete_from": fleet_complete_from,
    }


def build_grouped_history(
    store: Store, pricing: dict, window: str, bucket: str, group_by: str,
    since_dt: datetime, until_dt: datetime, tz_name: str = "UTC",
) -> dict:
    count = bucket_count(since_dt, until_dt, bucket, window)
    bucket_keys = dense_bucket_keys(until_dt, bucket, count, tz_name)
    bucket_set = set(bucket_keys)

    # Query from the FIRST DENSE BUCKET's own start, not from since_dt's raw
    # wall-clock time. since_dt carries today's time-of-day (e.g. a 7d window
    # queried at 14:32 has since_dt = 7 days ago at 14:32), which sits
    # strictly inside the oldest rendered day/hour bucket -- querying from
    # since_dt would pull in a sliver of data from just before that bucket's
    # start that then has nowhere to render (dense_bucket_keys doesn't
    # include a key for it), silently dropped from the series but NOT from
    # a naive totals sum over raw query rows, so totals and per-bucket series
    # would disagree. Anchoring the query to the first bucket's own boundary
    # keeps totals and series consistent by construction.
    first_bucket = bucket_keys[0]
    since_iso = first_bucket if bucket == "hour" else first_bucket + "T00:00:00Z"
    since_day = first_bucket if bucket == "day" else first_bucket[:10]

    acc = _accumulate(store, pricing, since_iso, since_day, bucket, bucket_set, tz_name)

    total_tokens = 0
    total_cost = 0.0
    total_messages = 0
    for by_bucket in acc.values():
        for v in by_bucket.values():
            total_tokens += v["tokens"]
            total_messages += v["messages"]
            if v["cost"] is not None:
                total_cost += v["cost"]

    series_map = _group_series(acc, group_by)
    finalized = _finalize(series_map, bucket_keys)
    kept, capped = _cap_series(finalized, bucket_keys)

    fs = _first_seen_map(store)
    coverage = _coverage(fs, group_by, kept, since_dt, until_dt)

    return {
        "window": window, "bucket": bucket, "group_by": group_by,
        "buckets": bucket_keys,
        "series": kept,
        "series_capped": capped,
        "totals": {
            "tokens": total_tokens, "cost_usd": round(total_cost, 6),
            "cost_usd_basis": "claude_only", "messages": total_messages,
        },
        "coverage": coverage,
    }


__all__ = ["InvalidWindowError", "build_grouped_history", "bucket_count", "parse_window"]
