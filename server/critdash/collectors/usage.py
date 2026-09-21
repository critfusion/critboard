"""usage collector: incrementally tail ~/.claude/projects/*/*.jsonl and roll up cost/tokens.

Per SPEC:
  - Only type == "assistant" lines with a real message.model (skip the literal
    "<synthetic>") and a non-null message.usage are counted.
  - Dedup on message.id (the same id can appear more than once in the log).
  - (path, inode, offset) tracked in SQLite so a restart doesn't re-read
    everything; first run does a full backfill.
  - Cost model per SPEC ## Cost model: input_tokens / cache_read_input_tokens /
    cache_creation_input_tokens each at pricing.json's rate for that model
    (longest-id-prefix match). Internally cache-creation tokens are stored split
    by TTL bucket (ephemeral_5m / ephemeral_1h from message.usage.cache_creation)
    for future flexibility, but costed at the single frozen `cache_write` rate
    from pricing.json -- SPEC's cost model has one cache_write rate, not two.
"""

from __future__ import annotations

import glob
import json
import os
from datetime import UTC, datetime, timedelta

from ..pricing import compute_cost_usd
from ..store import BLOCK_DURATION, zero_fill_hourly
from . import BaseCollector

_PROJECT_DIR_CACHE: dict[str, str] = {}


def decode_project_dir(dirname: str) -> str:
    """Best-effort decode of a Claude project directory name back to a real
    filesystem path. Project dirs are formed by replacing '/' with '-', which
    is lossy when a path component itself contains '-' (e.g. a repo named
    demo-web). Greedily match the longest existing directory
    at each step so real hyphenated names resolve correctly when the path
    exists on disk; falls back to a naive '-' -> '/' replace otherwise."""
    if dirname in _PROJECT_DIR_CACHE:
        return _PROJECT_DIR_CACHE[dirname]
    tokens = dirname.split("-")
    if tokens and tokens[0] == "":
        tokens = tokens[1:]
    path = ""
    i = 0
    n = len(tokens)
    while i < n:
        matched = False
        for j in range(n, i, -1):
            candidate = "-".join(tokens[i:j])
            test_path = f"{path}/{candidate}"
            if os.path.isdir(test_path):
                path = test_path
                i = j
                matched = True
                break
        if not matched:
            path = f"{path}/{tokens[i]}"
            i += 1
    result = path or "/"
    _PROJECT_DIR_CACHE[dirname] = result
    return result


def project_name_from_dir(dirname: str) -> tuple[str, str]:
    full_path = decode_project_dir(dirname)
    name = os.path.basename(full_path.rstrip("/")) or dirname
    return name, full_path


def parse_jsonl_bytes(data: bytes) -> list[dict]:
    """Parse complete lines from a byte blob. Malformed lines are skipped."""
    out = []
    for raw_line in data.split(b"\n"):
        line = raw_line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def extract_usage_row(doc: dict, project: str, project_path: str, pricing: dict) -> dict | None:
    if doc.get("type") != "assistant":
        return None
    message = doc.get("message") or {}
    model = message.get("model")
    if not model or model == "<synthetic>":
        return None
    usage = message.get("usage")
    if usage is None:
        return None
    message_id = message.get("id")
    if not message_id:
        return None
    ts = doc.get("timestamp")
    if not ts:
        return None
    session_id = doc.get("sessionId") or doc.get("session_id")

    input_tokens = usage.get("input_tokens") or 0
    output_tokens = usage.get("output_tokens") or 0
    cache_read = usage.get("cache_read_input_tokens") or 0
    cache_creation = usage.get("cache_creation") or {}
    cw_5m = cache_creation.get("ephemeral_5m_input_tokens")
    cw_1h = cache_creation.get("ephemeral_1h_input_tokens")
    if cw_5m is None and cw_1h is None:
        cw_5m = usage.get("cache_creation_input_tokens") or 0
        cw_1h = 0
    else:
        cw_5m = cw_5m or 0
        cw_1h = cw_1h or 0

    web_searches = ((usage.get("server_tool_use") or {}).get("web_search_requests")) or 0
    is_sidechain = 1 if doc.get("isSidechain") else 0
    speed = usage.get("speed")

    cost = compute_cost_usd(
        pricing, model, input_tokens, output_tokens, cache_read, cw_5m, cw_1h, speed=speed
    )

    return {
        "message_id": message_id,
        "ts": ts,
        "session_id": session_id,
        "project": project,
        "project_path": project_path,
        "model": model,
        "input": input_tokens,
        "output": output_tokens,
        "cache_read": cache_read,
        "cache_write_5m": cw_5m,
        "cache_write_1h": cw_1h,
        "speed": speed,
        "is_sidechain": is_sidechain,
        "web_searches": web_searches,
        "cost_usd": cost,
    }


def _row_to_totals(row) -> dict:
    return {
        "input": row["input"],
        "output": row["output"],
        "cache_read": row["cache_read"],
        "cache_write": row["cache_write"],
        "total": row["input"] + row["output"] + row["cache_read"] + row["cache_write"],
        "cost_usd": round(row["cost_usd"], 6),
        "messages": row["messages"],
    }


def _remote_row_cost(pricing: dict, row) -> float:
    return compute_cost_usd(
        pricing, row["model"], row["input"], row["output"], row["cache_read"],
        row["cache_write_5m"], row["cache_write_1h"],
    )


def _merge_totals_with_remote(local_totals: dict, remote_model_rows, pricing: dict) -> dict:
    """local_totals is a _row_to_totals()-shaped dict (already has combined
    cache_write and stored cost_usd). remote_model_rows is
    store.remote_usage_grouped(("model",), since) -- token sums per model
    across every remote host in the window, with no cost yet (pricing is
    applied centrally here, not on the remote side -- see remote_probe.py)."""
    merged = dict(local_totals)
    remote_cost = 0.0
    for r in remote_model_rows:
        merged["input"] += r["input"]
        merged["output"] += r["output"]
        merged["cache_read"] += r["cache_read"]
        merged["cache_write"] += r["cache_write_5m"] + r["cache_write_1h"]
        merged["messages"] += r["messages"]
        remote_cost += _remote_row_cost(pricing, r)
    merged["total"] = merged["input"] + merged["output"] + merged["cache_read"] + merged["cache_write"]
    merged["cost_usd"] = round(local_totals["cost_usd"] + remote_cost, 6)
    return merged


def _merge_by_model(local_rows, remote_model_rows, pricing: dict) -> dict[str, dict]:
    acc: dict[str, dict] = {}
    for row in local_rows:
        acc[row["model"]] = {
            "input": row["input"], "output": row["output"], "cache_read": row["cache_read"],
            "cache_write": row["cache_write"], "cost_usd": round(row["cost_usd"], 6),
            "messages": row["messages"],
        }
    for r in remote_model_rows:
        a = acc.setdefault(r["model"], {
            "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost_usd": 0.0, "messages": 0,
        })
        a["input"] += r["input"]
        a["output"] += r["output"]
        a["cache_read"] += r["cache_read"]
        a["cache_write"] += r["cache_write_5m"] + r["cache_write_1h"]
        a["messages"] += r["messages"]
        a["cost_usd"] = round(a["cost_usd"] + _remote_row_cost(pricing, r), 6)
    return acc


def _merge_by_project(local_rows, remote_project_model_rows, pricing: dict) -> dict[str, dict]:
    acc: dict[str, dict] = {}
    for row in local_rows:
        acc[row["project"]] = {
            "total": row["total"], "cost_usd": round(row["cost_usd"], 6), "messages": row["messages"],
        }
    for r in remote_project_model_rows:
        a = acc.setdefault(r["project"], {"total": 0, "cost_usd": 0.0, "messages": 0})
        a["total"] += r["input"] + r["output"] + r["cache_read"] + r["cache_write_5m"] + r["cache_write_1h"]
        a["messages"] += r["messages"]
        a["cost_usd"] = round(a["cost_usd"] + _remote_row_cost(pricing, r), 6)
    return acc


def _merge_timeline(local_timeline: list[dict], remote_hour_model_rows, pricing: dict) -> list[dict]:
    per_hour: dict[str, dict] = {}
    for r in remote_hour_model_rows:
        h = per_hour.setdefault(r["hour"], {
            "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost_usd": 0.0, "messages": 0,
        })
        h["input"] += r["input"]
        h["output"] += r["output"]
        h["cache_read"] += r["cache_read"]
        h["cache_write"] += r["cache_write_5m"] + r["cache_write_1h"]
        h["messages"] += r["messages"]
        h["cost_usd"] = round(h["cost_usd"] + _remote_row_cost(pricing, r), 6)

    merged = []
    for row in local_timeline:
        add = per_hour.get(row["t"][:13])
        if add:
            row = dict(row)
            row["input"] += add["input"]
            row["output"] += add["output"]
            row["cache_read"] += add["cache_read"]
            row["cache_write"] += add["cache_write"]
            row["messages"] += add["messages"]
            row["cost_usd"] = round(row["cost_usd"] + add["cost_usd"], 6)
        merged.append(row)
    return merged


def _by_host_rollup(store, since_iso: str | None, local_host: str, pricing: dict) -> list[dict]:
    local_totals = store.usage_totals(since_iso, host=local_host)
    out = [{
        "host": local_host, "total": local_totals["total"],
        "cost_usd": local_totals["cost_usd"], "messages": local_totals["messages"],
    }]

    per_host: dict[str, dict] = {}
    for r in store.remote_usage_grouped(("host", "model"), since_iso=since_iso):
        h = per_host.setdefault(r["host"], {"total": 0, "cost_usd": 0.0, "messages": 0})
        h["total"] += r["input"] + r["output"] + r["cache_read"] + r["cache_write_5m"] + r["cache_write_1h"]
        h["messages"] += r["messages"]
        h["cost_usd"] = round(h["cost_usd"] + _remote_row_cost(pricing, r), 6)
    for host_name, v in per_host.items():
        out.append({"host": host_name, **v})
    return out


class UsageCollector(BaseCollector):
    name = "usage"
    interval_s = 10.0

    def __init__(self, ctx=None, projects_glob: str = "~/.claude/projects/*/*.jsonl", store=None,
                 pricing: dict | None = None, host: str = "localhost"):
        super().__init__(ctx)
        self.projects_glob = projects_glob
        self.store = store
        self._pricing = pricing or {}
        self.host = host

    def _current_pricing(self) -> dict:
        if self.ctx is not None and self.ctx.pricing:
            return self.ctx.pricing
        return self._pricing

    async def collect(self) -> dict:
        pattern = os.path.expanduser(self.projects_glob)
        files = glob.glob(pattern)
        pricing = self._current_pricing()

        new_rows: list[dict] = []
        for path in files:
            try:
                st = os.stat(path)
            except OSError:
                continue
            inode = st.st_ino
            size = st.st_size
            prev = self.store.get_offset(path) if self.store else None
            offset = 0
            if prev is not None:
                prev_inode, prev_offset, _prev_mtime = prev
                if prev_inode == inode and prev_offset <= size:
                    offset = prev_offset
                # else: file replaced/rotated/truncated -> full re-read from 0

            if offset >= size:
                if self.store:
                    self.store.set_offset(path, inode, size, st.st_mtime)
                continue

            dirname = os.path.basename(os.path.dirname(path))
            project, project_path = project_name_from_dir(dirname)

            with open(path, "rb") as f:
                f.seek(offset)
                chunk = f.read()

            # only advance the offset past the last complete line
            last_nl = chunk.rfind(b"\n")
            if last_nl == -1:
                if self.store:
                    self.store.set_offset(path, inode, offset, st.st_mtime)
                continue
            usable = chunk[: last_nl + 1]
            new_offset = offset + len(usable)

            for doc in parse_jsonl_bytes(usable):
                row = extract_usage_row(doc, project, project_path, pricing)
                if row is not None:
                    row["host"] = self.host
                    new_rows.append(row)

            if self.store:
                self.store.set_offset(path, inode, new_offset, st.st_mtime)

        inserted = self.store.insert_usage_events(new_rows) if self.store else 0

        usage_data = self._compute_rollups()
        self._update_ctx_session_map()

        return {"usage": usage_data, "_inserted": inserted}

    def _windows(self) -> dict[str, str | None]:
        now = datetime.now(UTC)
        today_start = now.strftime("%Y-%m-%dT00:00:00Z")
        return {
            "today": today_start,
            "7d": (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "30d": (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "all": None,
        }

    def _compute_rollups(self) -> dict:
        pricing = self._current_pricing()
        if self.store is None:
            empty_block = {
                "started_at": None, "ends_at": None, "tokens": 0,
                "cost_usd": 0.0, "pct_elapsed": 0.0, "active": False,
            }
            empty_budget = {
                "monthly_usd": pricing.get("monthly_budget_usd", 0.0),
                "spent_mtd_usd": 0.0, "pct": 0.0, "projected_month_usd": 0.0,
            }
            return {
                "totals": {}, "by_model": [], "by_project": [], "by_agent": [], "by_host": [],
                "by_provider": [],
                "timeline": zero_fill_hourly([], datetime.now(UTC), 48),
                "burn": {}, "cache_hit_ratio_today": 0.0,
                "block": empty_block, "budget": empty_budget,
            }

        windows = self._windows()
        totals = {}
        by_model = []
        by_project = []
        by_agent = []
        by_host = []
        # Fleet-wide merge: totals/by_model/by_project/by_host/timeline below
        # combine this host's usage_events (per-message, cost already stored)
        # with every other host's remote_usage_buckets (pre-aggregated token
        # counts, costed here from the live pricing table -- see module
        # docstring). burn/cache_hit_ratio_today/block/budget stay LOCAL ONLY
        # by design: a 5h rate-limit block and a monthly budget are properties
        # of whichever Anthropic account plan a given host's Claude Code
        # session uses, so summing them across hosts that may be on different
        # accounts would conflate numbers that don't actually add up to one
        # thing. Reported explicitly in the delivery report.
        for wname, since in windows.items():
            local_totals_row = self.store.usage_totals(since)
            remote_model_rows = self.store.remote_usage_grouped(("model",), since_iso=since)
            merged_totals = _merge_totals_with_remote(
                _row_to_totals(local_totals_row), remote_model_rows, pricing
            )
            totals[wname] = merged_totals

            local_model_rows = self.store.usage_by_model(since)
            merged_by_model = _merge_by_model(local_model_rows, remote_model_rows, pricing)
            for model, agg in merged_by_model.items():
                by_model.append({"model": model, "window": wname, **agg})

            local_project_rows = self.store.usage_by_project(since)
            remote_project_rows = self.store.remote_usage_grouped(("project", "model"), since_iso=since)
            merged_by_project = _merge_by_project(local_project_rows, remote_project_rows, pricing)
            for project, agg in merged_by_project.items():
                by_project.append({"project": project, "window": wname, **agg})

            for row in self.store.usage_by_session(since):
                by_agent.append({
                    "agent_id": row["session_id"], "window": wname,
                    "total": row["total"], "cost_usd": round(row["cost_usd"], 6),
                })

            for entry in _by_host_rollup(self.store, since, self.host, pricing):
                by_host.append({**entry, "window": wname})

        now = datetime.now(UTC)
        since_48h = (now - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
        local_timeline = self.store.usage_timeline_hourly(since_48h)
        remote_timeline_rows = self.store.remote_usage_grouped(("hour", "model"), since_iso=since_48h)
        timeline = _merge_timeline(
            zero_fill_hourly(local_timeline, now, 48), remote_timeline_rows, pricing
        )

        since_1h = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        since_24h = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        since_5m = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cost_1h = self.store.usage_totals(since_1h)["cost_usd"]
        totals_24h = self.store.usage_totals(since_24h)
        cost_24h = totals_24h["cost_usd"]
        usd_per_hour_24h = cost_24h / 24.0
        tokens_5m = self.store.usage_totals(since_5m)["total"]

        burn = {
            "usd_per_hour_1h": round(cost_1h, 6),
            "usd_per_hour_24h": round(usd_per_hour_24h, 6),
            "projected_month_usd": round(usd_per_hour_24h * 24 * 30, 4),
            "tokens_per_min_5m": round(tokens_5m / 5.0, 2),
        }

        today_totals_row = self.store.usage_totals(windows["today"])
        denom = today_totals_row["input"] + today_totals_row["cache_read"]
        cache_hit_ratio_today = round(today_totals_row["cache_read"] / denom, 4) if denom else 0.0

        now_iso_s = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        block_start = self.store.current_usage_block_start(now_iso_s)
        block = {
            "started_at": None, "ends_at": None, "tokens": 0, "cost_usd": 0.0,
            "pct_elapsed": 0.0, "active": False,
        }
        if block_start:
            start_dt = datetime.fromisoformat(block_start.replace("Z", "+00:00"))
            end_dt = start_dt + BLOCK_DURATION
            block_totals = self.store.usage_totals(block_start)
            elapsed = (now - start_dt).total_seconds()
            pct = max(0.0, min(1.0, elapsed / BLOCK_DURATION.total_seconds()))
            block = {
                "started_at": block_start,
                "ends_at": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "tokens": block_totals["total"],
                "cost_usd": block_totals["cost_usd"],
                "pct_elapsed": round(pct, 4),
                "active": True,
            }

        monthly_usd = pricing.get("monthly_budget_usd", 0.0)
        month_start = now.strftime("%Y-%m-01T00:00:00Z")
        spent_mtd_usd = round(self.store.usage_totals(month_start)["cost_usd"], 6)

        # Budget projection: actual spend so far this calendar month, plus the
        # remainder of the month extrapolated at the current 24h burn rate.
        # This must always be >= spent_mtd_usd (a month-end projection can
        # never be below actual spend already incurred), unlike
        # burn["projected_month_usd"] which is a naive full-month
        # extrapolation of the 24h rate and answers a different question.
        # Uses the same UTC calendar-month basis as spent_mtd_usd above, so
        # the two figures cannot disagree.
        if now.month == 12:
            next_month_start_dt = datetime(now.year + 1, 1, 1, tzinfo=UTC)
        else:
            next_month_start_dt = datetime(now.year, now.month + 1, 1, tzinfo=UTC)
        hours_remaining_in_month = max(0.0, (next_month_start_dt - now).total_seconds() / 3600.0)
        budget_projected_month_usd = max(
            spent_mtd_usd,
            spent_mtd_usd + usd_per_hour_24h * hours_remaining_in_month,
        )

        budget = {
            "monthly_usd": monthly_usd,
            "spent_mtd_usd": spent_mtd_usd,
            "pct": round(spent_mtd_usd / monthly_usd, 4) if monthly_usd else 0.0,
            "projected_month_usd": round(budget_projected_month_usd, 4),
        }

        by_provider = self._rollup_by_provider(windows, totals)

        return {
            "totals": totals,
            "by_model": by_model,
            "by_project": by_project,
            "by_agent": by_agent,
            "by_host": by_host,
            "by_provider": by_provider,
            "timeline": timeline,
            "burn": burn,
            "cache_hit_ratio_today": cache_hit_ratio_today,
            "block": block,
            "budget": budget,
        }

    def _rollup_by_provider(self, windows: dict[str, str | None], totals: dict) -> list[dict]:
        """{provider, window, tokens, messages, cost_usd} rows -- `totals`
        (Claude's merged local+remote figures, computed above) is reused
        VERBATIM for the "claude" rows so this can never diverge from the
        existing totals it is presented alongside. Kimi's row is built from
        kimi_turn_events (this host's own ingestion) plus
        remote_kimi_usage_buckets (every other host's, once one grows a Kimi
        install -- see remote_probe.py). cost_usd is always None for Kimi:
        it bills by subscription quota, not per token -- see
        collectors/kimi.py's module docstring."""
        rows: list[dict] = []
        for wname, since in windows.items():
            claude_totals = totals[wname]
            rows.append({
                "provider": "claude", "window": wname,
                "tokens": claude_totals["total"], "messages": claude_totals["messages"],
                "cost_usd": claude_totals["cost_usd"],
            })
            if self.store is None:
                rows.append({
                    "provider": "kimi", "window": wname, "tokens": 0, "messages": 0, "cost_usd": None,
                })
                continue
            local_kimi = self.store.kimi_usage_totals(since)
            remote_kimi = self.store.remote_kimi_usage_totals(since)
            rows.append({
                "provider": "kimi", "window": wname,
                "tokens": local_kimi["tokens"] + remote_kimi["tokens"],
                "messages": local_kimi["messages"] + remote_kimi["messages"],
                "cost_usd": None,
            })
        return rows

    def _update_ctx_session_map(self) -> None:
        if self.ctx is None or self.store is None:
            return
        now = datetime.now(UTC)
        today_start = now.strftime("%Y-%m-%dT00:00:00Z")
        since_5m = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

        today_by_session: dict[str, dict] = {}
        for row in self.store.usage_by_session(today_start):
            sid = row["session_id"]
            if not sid:
                continue
            today_by_session[sid] = row

        session_category_rows = self.store.query_usage(since_iso=today_start)
        category_by_session: dict[str, dict] = {}
        for r in session_category_rows:
            sid = r["session_id"]
            if not sid:
                continue
            default_agg = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
            agg = category_by_session.setdefault(sid, default_agg)
            agg["input"] += r["input"]
            agg["output"] += r["output"]
            agg["cache_read"] += r["cache_read"]
            agg["cache_write"] += r["cache_write_5m"] + r["cache_write_1h"]

        last_activity_rows = self.store.usage_last_activity_by_session()
        sidechain_rows = self.store.usage_sidechain_recent_counts(since_5m)
        sidechain_by_session = {r["session_id"]: r["n"] for r in sidechain_rows}

        session_map: dict[str, dict] = {}
        for r in last_activity_rows:
            sid = r["session_id"]
            cats = category_by_session.get(sid, {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0})
            total_today = today_by_session.get(sid)
            session_map[sid] = {
                "last_activity": r["ts"],
                "last_model": r["model"],
                "tokens_today": {**cats, "total": sum(cats.values())},
                "cost_today_usd": round(total_today["cost_usd"], 6) if total_today else 0.0,
                "msg_count_today": total_today["messages"] if total_today else 0,
                "subagents_active": sidechain_by_session.get(sid, 0),
            }
        self.ctx.usage_by_session = session_map
