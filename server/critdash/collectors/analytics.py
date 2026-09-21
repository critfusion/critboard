"""Wave-2 analytics collector (briefing Task 2): error-pattern mining, tool
usage, and subagent delegation cost -- all sourced from the same
`~/.claude/projects/*/*.jsonl` files usage.py already tails, at zero LLM
cost. Productivity (2d, git log) lives in collectors/productivity.py on its
own slower interval -- see that module's docstring for why.

Ingestion is incremental by byte offset, same pattern as UsageCollector, but
tracked under its OWN key in the shared `file_offsets` table (prefixed
`analytics::`) so the two collectors never contend over the same file's
offset -- each reads the same bytes independently, at its own pace.

Tool-call rows are keyed by the *assistant* line's (uuid, block_index) at the
tool_use block -- one row per invocation. A matching tool_result error found
later (possibly in a LATER poll, since a slow tool call can straddle a poll
boundary) updates that SAME row via `tool_use_id`, rather than inserting a
second row, so "calls" and "errors" count the same unit. `_pending_tool_use`
only covers same-poll matches (kept small, cleared every poll); a match
against an earlier poll's already-inserted row goes through
Store.update_tool_call_error(); if that finds nothing (the tool_use line was
malformed/dropped), a fallback row is inserted so the error is never silently
lost, at the cost of `tool: null` for that one row.
"""

from __future__ import annotations

import glob
import os
import re
from datetime import UTC, datetime, timedelta

from ..pricing import compute_cost_usd
from . import BaseCollector
from .usage import parse_jsonl_bytes, project_name_from_dir

_ANALYTICS_OFFSET_PREFIX = "analytics::"

# -- error classification: small, ordered rule table -------------------------
# First matching rule wins. Order matters: more specific patterns (e.g. "string
# to replace not found", which is itself a kind of "not found") are checked
# before generic ones. Add a new failure mode by adding one tuple here.
_ERROR_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("string_not_found", (
        "string to replace not found", "old_string not found", "no such string",
    )),
    ("file_not_found", (
        "no such file or directory", "file does not exist", "cannot access", "enoent",
    )),
    ("permission_denied", (
        "permission denied", "permission for this action was denied",
        "eacces", "the user doesn't want to proceed",
    )),
    ("timeout", (
        "timed out", "command timed out", "timeout",
    )),
    ("rate_limit", (
        "rate limit", "concurrent subagent limit reached",
        "temporarily unavailable", "overloaded", " 529",
    )),
    # Kimi's quota exhaustion (verified live: "403 You've reached your
    # monthly usage limit for this billing cycle... purchase extra usage or
    # upgrade your plan") is a distinct failure mode from a transient
    # rate_limit above -- it does not clear on retry, only on the next
    # billing cycle or a plan change, so it gets its own kind rather than
    # being folded into rate_limit.
    ("quota_exceeded", (
        "usage limit", "monthly usage limit", "billing cycle",
        "upgrade your plan", "purchase extra usage", "quota will be refreshed",
    )),
    ("invalid_json", (
        "invalid json", "jsondecodeerror", "unexpected token", "expecting value",
    )),
    ("command_failed", (
        "exit code", "non-zero exit", "command failed",
    )),
)
_DEFAULT_ERROR_KIND = "other"


def classify_error(text: str) -> str:
    low = f" {(text or '').lower()} "
    for kind, patterns in _ERROR_RULES:
        if any(p in low for p in patterns):
            return kind
    return _DEFAULT_ERROR_KIND


def classify_api_error(doc: dict) -> str:
    """isApiErrorMessage rows carry their own `error` field (rate_limit,
    invalid_request, authentication_failed, server_error -- verified against
    real errors on this host 2026-09-18), which is a better signal than text
    matching. The one gap: apiErrorStatus 529 (real, observed) is reported
    with error="server_error" too, indistinguishable from a real 5xx without
    the status code, so 529 is special-cased to the more useful
    "overloaded_error" kind the briefing asks for."""
    if doc.get("apiErrorStatus") == 529:
        return "overloaded_error"
    return doc.get("error") or _DEFAULT_ERROR_KIND


_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|bearer|authorization)\b\s*[:=]\s*\S+"
)
_MAX_EXAMPLE_LEN = 200


def redact_secrets(text: str) -> str:
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}=<redacted>", text)


def make_example(text: str | None) -> str | None:
    if not text:
        return None
    collapsed = " ".join(redact_secrets(text).split())
    return collapsed[:_MAX_EXAMPLE_LEN] if collapsed else None


def tool_result_text(content) -> str:
    """tool_result's `content` is either a plain string or a list of blocks
    (text/image/...). Only text is ever surfaced in an example excerpt."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        return " ".join(parts)
    return ""


_FILE_PATH_INPUT_KEYS = ("file_path", "path", "notebook_path")


def extract_file_path(tool_input) -> str | None:
    if not isinstance(tool_input, dict):
        return None
    for key in _FILE_PATH_INPUT_KEYS:
        val = tool_input.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def parse_doc_for_analytics(
    doc: dict, host: str, pending_tool_use: dict[str, dict]
) -> tuple[list[dict], list[dict]]:
    """Extract this jsonl line's contribution: (new_call_rows, api_error_rows).
    A tool_result error that matches a tool_use already staged this poll
    (`pending_tool_use`) updates that row IN PLACE and contributes no new
    row; the caller handles cross-poll matches (see module docstring)."""
    call_rows: list[dict] = []
    api_error_rows: list[dict] = []

    uuid_ = doc.get("uuid")
    ts = doc.get("timestamp")
    if not uuid_ or not ts:
        return call_rows, api_error_rows

    if doc.get("isApiErrorMessage"):
        api_error_rows.append({"host": host, "uuid": uuid_, "ts": ts, "kind": classify_api_error(doc)})
        return call_rows, api_error_rows

    message = doc.get("message") or {}
    content = message.get("content")
    if not isinstance(content, list):
        return call_rows, api_error_rows

    session_id = doc.get("sessionId") or doc.get("session_id")
    is_sidechain = 1 if doc.get("isSidechain") else 0

    for idx, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_use":
            tool_use_id = block.get("id")
            row = {
                "host": host, "uuid": uuid_, "block_index": idx, "ts": ts,
                "session_id": session_id, "is_sidechain": is_sidechain,
                "tool": block.get("name"), "tool_use_id": tool_use_id,
                "is_error": 0, "error_kind": None,
                "error_excerpt": None, "file_path": extract_file_path(block.get("input")),
            }
            call_rows.append(row)
            if tool_use_id:
                pending_tool_use[tool_use_id] = row
        elif btype == "tool_result" and block.get("is_error"):
            tool_use_id = block.get("tool_use_id")
            text = tool_result_text(block.get("content"))
            kind = classify_error(text)
            excerpt = make_example(text)
            matched = pending_tool_use.pop(tool_use_id, None) if tool_use_id else None
            if matched is not None:
                matched["is_error"] = 1
                matched["error_kind"] = kind
                matched["error_excerpt"] = excerpt
            else:
                call_rows.append({
                    "host": host, "uuid": uuid_, "block_index": idx, "ts": ts,
                    "session_id": session_id, "is_sidechain": is_sidechain,
                    "tool": None, "is_error": 1, "error_kind": kind,
                    "error_excerpt": excerpt, "file_path": None,
                    "_fallback_tool_use_id": tool_use_id,
                })

    return call_rows, api_error_rows


# -- rollup: truncate-with-flag helper ----------------------------------------


def _truncate(rows: list, n: int) -> tuple[list, bool]:
    if len(rows) <= n:
        return rows, False
    return rows[:n], True


_TOP_ERRORS_N = 20
_BY_TOOL_N = 40
_TROUBLE_FILES_N = 20
_API_ERRORS_N = 10
_SUBAGENT_SESSIONS_N = 30


def _max_ts(a: str | None, b: str | None) -> str | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


class AnalyticsCollector(BaseCollector):
    name = "analytics"
    interval_s = 90.0

    def __init__(
        self, ctx=None, projects_glob: str = "~/.claude/projects/*/*.jsonl",
        store=None, host: str = "localhost", window_days: int = 7,
    ):
        super().__init__(ctx)
        self.projects_glob = projects_glob
        self.store = store
        self.host = host
        self.window_days = window_days

    async def collect(self) -> dict:
        if self.store is not None:
            await self._ingest()
        return {"analytics": self._rollup()}

    # -- ingestion ------------------------------------------------------------

    async def _ingest(self) -> None:
        pattern = os.path.expanduser(self.projects_glob)
        files = glob.glob(pattern)

        new_call_rows: list[dict] = []
        api_error_rows: list[dict] = []
        pending_tool_use: dict[str, dict] = {}

        for path in files:
            try:
                st = os.stat(path)
            except OSError:
                continue
            inode, size = st.st_ino, st.st_size
            offset_key = _ANALYTICS_OFFSET_PREFIX + path
            prev = self.store.get_offset(offset_key)
            offset = 0
            if prev is not None:
                prev_inode, prev_offset, _prev_mtime = prev
                if prev_inode == inode and prev_offset <= size:
                    offset = prev_offset

            if offset >= size:
                self.store.set_offset(offset_key, inode, size, st.st_mtime)
                continue

            with open(path, "rb") as f:
                f.seek(offset)
                chunk = f.read()

            last_nl = chunk.rfind(b"\n")
            if last_nl == -1:
                self.store.set_offset(offset_key, inode, offset, st.st_mtime)
                continue
            usable = chunk[: last_nl + 1]
            new_offset = offset + len(usable)

            for doc in parse_jsonl_bytes(usable):
                calls, api_errs = parse_doc_for_analytics(doc, self.host, pending_tool_use)
                new_call_rows.extend(calls)
                api_error_rows.extend(api_errs)

            self.store.set_offset(offset_key, inode, new_offset, st.st_mtime)

        # cross-poll error matches: a tool_use inserted in an earlier poll,
        # its error only showing up now. Try to correct that already-stored
        # row in place; if it can't be found (dropped/malformed tool_use
        # line), fall back to inserting this row standalone so the error is
        # still counted, just without a resolved tool name.
        fallback_rows = [r for r in new_call_rows if r.get("_fallback_tool_use_id")]
        clean_rows = [r for r in new_call_rows if not r.get("_fallback_tool_use_id")]
        if clean_rows:
            self.store.insert_tool_call_events(clean_rows)
        for r in fallback_rows:
            tool_use_id = r.pop("_fallback_tool_use_id")
            updated = self.store.update_tool_call_error(
                self.host, tool_use_id, r["error_kind"], r["error_excerpt"]
            )
            if not updated:
                self.store.insert_tool_call_events([r])
        if api_error_rows:
            self.store.insert_api_error_events(api_error_rows)

    # -- rollup -----------------------------------------------------------

    _EMPTY_PRODUCTIVITY_SHAPE = {
        "commits_7d": 0, "commits_30d": 0, "lines_added_7d": 0, "lines_removed_7d": 0,
        "files_changed_7d": 0, "by_repo": [], "truncated": False,
    }
    _EMPTY_PRODUCTIVITY = {
        **_EMPTY_PRODUCTIVITY_SHAPE,
        "excluding_fixtures": dict(_EMPTY_PRODUCTIVITY_SHAPE),
        "fixtures": dict(_EMPTY_PRODUCTIVITY_SHAPE),
        "excluded_repo_patterns": [],
    }

    def _productivity(self) -> dict:
        # populated by ProductivityCollector on its own slower interval --
        # see that module's docstring for why this isn't computed here.
        if self.ctx is not None and self.ctx.latest_productivity:
            return self.ctx.latest_productivity
        return dict(self._EMPTY_PRODUCTIVITY)

    def _rollup(self) -> dict:
        if self.store is None:
            return {
                "errors": {"window": f"{self.window_days}d", "top_errors": [], "by_tool": [],
                            "trouble_files": [], "api_errors": [], "total_errors": 0},
                "tools": {"window": f"{self.window_days}d", "usage": [],
                           "decisions": {"accepted": 0, "rejected": 0, "total": 0}},
                "subagents": {
                    "window": f"{self.window_days}d", "sessions": [],
                    "totals": {"main_cost_usd": 0.0, "sidechain_cost_usd": 0.0, "sidechain_share": 0.0},
                },
                "productivity": self._productivity(),
            }

        now = datetime.now(UTC)
        since_iso = (now - timedelta(days=self.window_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        since_day = (now - timedelta(days=self.window_days)).strftime("%Y-%m-%d")
        window = f"{self.window_days}d"
        pricing = self.ctx.pricing if self.ctx is not None else {}

        return {
            "errors": self._rollup_errors(since_iso, since_day, window),
            "tools": self._rollup_tools(since_iso, since_day, window),
            "subagents": self._rollup_subagents(since_iso, since_day, window, pricing),
            "productivity": self._productivity(),
        }

    def _rollup_errors(self, since_iso: str, since_day: str, window: str) -> dict:
        agg: dict[tuple[str, str], dict] = {}
        for r in self.store.tool_error_top_kinds(since_iso, host=self.host):
            key = (r["kind"] or _DEFAULT_ERROR_KIND, r["tool"] or "unknown")
            a = agg.setdefault(key, {"count": 0, "last_seen": None})
            a["count"] += r["count"]
            a["last_seen"] = _max_ts(a["last_seen"], r["last_seen"])
        for r in self.store.remote_error_top_kinds(since_day):
            key = (r["kind"] or _DEFAULT_ERROR_KIND, r["tool"] or "unknown")
            a = agg.setdefault(key, {"count": 0, "last_seen": None})
            a["count"] += r["count"]
            a["last_seen"] = _max_ts(a["last_seen"], r["last_seen"])

        total_errors = sum(a["count"] for a in agg.values())
        top = sorted(
            ({"kind": k, "tool": t, "count": a["count"], "last_seen": a["last_seen"]}
             for (k, t), a in agg.items()),
            key=lambda r: r["count"], reverse=True,
        )
        for r in top:
            pct = round(r["count"] / total_errors, 4) if total_errors else 0.0
            r["pct"] = pct
            local_ex = self.store.tool_error_example(since_iso, r["kind"], host=self.host)
            example = local_ex["error_excerpt"] if local_ex else None
            if example is None:
                remote_ex = self.store.remote_error_example(r["kind"])
                example = remote_ex["example"] if remote_ex else None
            r["example"] = example
        top, top_truncated = _truncate(top, _TOP_ERRORS_N)

        tool_agg: dict[str, dict] = {}
        for r in self.store.tool_call_by_tool(since_iso, host=self.host):
            a = tool_agg.setdefault(r["tool"] or "unknown", {"calls": 0, "errors": 0})
            a["calls"] += r["calls"]
            a["errors"] += r["errors"] or 0
        for r in self.store.remote_tool_by_tool(since_day):
            a = tool_agg.setdefault(r["tool"] or "unknown", {"calls": 0, "errors": 0})
            a["calls"] += r["calls"]
            a["errors"] += r["errors"]
        by_tool = sorted(
            ({"tool": t, "errors": a["errors"], "calls": a["calls"],
              "error_rate": round(a["errors"] / a["calls"], 4) if a["calls"] else 0.0}
             for t, a in tool_agg.items()),
            key=lambda r: r["errors"], reverse=True,
        )
        by_tool, by_tool_truncated = _truncate(by_tool, _BY_TOOL_N)

        file_agg: dict[str, dict] = {}
        for r in self.store.tool_error_trouble_files(since_iso, host=self.host):
            a = file_agg.setdefault(r["file_path"], {"errors": 0, "tools": set()})
            a["errors"] += r["errors"]
            a["tools"].add(r["tool"] or "unknown")
        for r in self.store.remote_trouble_files(since_day):
            a = file_agg.setdefault(r["path"], {"errors": 0, "tools": set()})
            a["errors"] += r["errors"]
            a["tools"].add(r["tool"] or "unknown")
        trouble_files = sorted(
            ({"path": p, "errors": a["errors"], "tools": sorted(a["tools"])} for p, a in file_agg.items()),
            key=lambda r: r["errors"], reverse=True,
        )
        trouble_files, trouble_truncated = _truncate(trouble_files, _TROUBLE_FILES_N)

        # keyed by (kind, provider) rather than kind alone -- a Kimi
        # "quota_exceeded" and a hypothetical future Claude error of the same
        # kind name must never be silently summed into one row; see the
        # briefing's "tag them with the provider so Claude and Kimi failures
        # are distinguishable" requirement.
        api_agg: dict[tuple[str, str], dict] = {}
        for r in self.store.api_error_counts(since_iso, host=self.host):
            key = (r["kind"] or _DEFAULT_ERROR_KIND, "claude")
            a = api_agg.setdefault(key, {"count": 0, "last_seen": None})
            a["count"] += r["count"]
            a["last_seen"] = _max_ts(a["last_seen"], r["last_seen"])
        for r in self.store.remote_api_errors(since_day):
            key = (r["kind"] or _DEFAULT_ERROR_KIND, "claude")
            a = api_agg.setdefault(key, {"count": 0, "last_seen": None})
            a["count"] += r["count"]
            a["last_seen"] = _max_ts(a["last_seen"], r["last_seen"])
        for r in self.store.kimi_error_counts(since_iso, host=self.host):
            key = (r["kind"] or _DEFAULT_ERROR_KIND, "kimi")
            a = api_agg.setdefault(key, {"count": 0, "last_seen": None})
            a["count"] += r["count"]
            a["last_seen"] = _max_ts(a["last_seen"], r["last_seen"])
        for r in self.store.remote_kimi_error_counts(since_day):
            key = (r["kind"] or _DEFAULT_ERROR_KIND, "kimi")
            a = api_agg.setdefault(key, {"count": 0, "last_seen": None})
            a["count"] += r["count"]
            a["last_seen"] = _max_ts(a["last_seen"], r["last_seen"])
        api_errors = sorted(
            ({"kind": k, "provider": p, "count": a["count"], "last_seen": a["last_seen"]}
             for (k, p), a in api_agg.items()),
            key=lambda r: r["count"], reverse=True,
        )
        api_errors, api_truncated = _truncate(api_errors, _API_ERRORS_N)

        return {
            "window": window,
            "top_errors": top,
            "by_tool": by_tool,
            "trouble_files": trouble_files,
            "api_errors": api_errors,
            "total_errors": total_errors,
            "truncated": top_truncated or by_tool_truncated or trouble_truncated or api_truncated,
        }

    def _rollup_tools(self, since_iso: str, since_day: str, window: str) -> dict:
        tool_agg: dict[str, dict] = {}
        total_calls = 0
        for r in self.store.tool_call_by_tool(since_iso, host=self.host):
            a = tool_agg.setdefault(r["tool"] or "unknown", {"calls": 0, "errors": 0})
            a["calls"] += r["calls"]
            a["errors"] += r["errors"] or 0
            total_calls += r["calls"]
        for r in self.store.remote_tool_by_tool(since_day):
            a = tool_agg.setdefault(r["tool"] or "unknown", {"calls": 0, "errors": 0})
            a["calls"] += r["calls"]
            a["errors"] += r["errors"]
            total_calls += r["calls"]

        def _tool_row(t, a):
            pct = round(a["calls"] / total_calls, 4) if total_calls else 0.0
            return {
                "tool": t, "calls": a["calls"], "pct": pct,
                # tool call duration is never recorded in the transcript (no
                # start/end pairing available) -- always null, not estimated.
                "avg_duration_ms": None, "errors": a["errors"],
            }

        usage = sorted(
            (_tool_row(t, a) for t, a in tool_agg.items()),
            key=lambda r: r["calls"], reverse=True,
        )
        usage, truncated = _truncate(usage, _BY_TOOL_N)

        # "decisions" (accept/reject rate on edits and permission prompts):
        # the transcript has no event that means "the user approved this
        # tool call" -- a successful tool_result is equally consistent with
        # "no permission prompt was ever shown" (auto-approved by settings)
        # as with "shown and approved". Only *rejections* have a distinct,
        # unambiguous signal (a tool_result whose error text is the
        # permission-denial message), so counting those against a fabricated
        # "accepted" number would report a rate with no honest denominator.
        # Left at zero per the briefing's explicit instruction rather than
        # inventing a heuristic.
        return {
            "window": window,
            "usage": usage,
            "decisions": {"accepted": 0, "rejected": 0, "total": 0},
            "truncated": truncated,
        }

    def _rollup_subagents(self, since_iso: str, since_day: str, window: str, pricing: dict) -> dict:
        sessions: dict[str, dict] = {}

        def _bucket(key: str, host: str, session_id: str, project: str | None):
            return sessions.setdefault(key, {
                "session_id": session_id, "host": host, "project": project,
                "main_cost_usd": 0.0, "sidechain_cost_usd": 0.0, "sidechain_messages": 0,
                "models": set(),
            })

        for r in self.store.usage_by_session_sidechain(since_iso, host=self.host):
            s = _bucket(r["session_id"], self.host, r["session_id"], r["project"])
            if r["is_sidechain"]:
                s["sidechain_cost_usd"] += r["cost_usd"]
                s["sidechain_messages"] += r["messages"]
            else:
                s["main_cost_usd"] += r["cost_usd"]
            if r["models"]:
                s["models"].update(m for m in r["models"].split(",") if m)

        for r in self.store.remote_session_buckets_grouped(since_day):
            key = f"{r['host']}::{r['session_id']}"
            s = _bucket(key, r["host"], r["session_id"], r["project"])
            cost = compute_cost_usd(
                pricing, r["model"], r["input"], r["output"], r["cache_read"],
                r["cache_write_5m"], r["cache_write_1h"],
            )
            if r["is_sidechain"]:
                s["sidechain_cost_usd"] += cost
                s["sidechain_messages"] += r["messages"]
            else:
                s["main_cost_usd"] += cost
            if r["model"]:
                s["models"].add(r["model"])

        out = []
        for s in sessions.values():
            total = s["main_cost_usd"] + s["sidechain_cost_usd"]
            out.append({
                "session_id": s["session_id"],
                "host": s["host"],
                "project": s["project"],
                "main_cost_usd": round(s["main_cost_usd"], 6),
                "sidechain_cost_usd": round(s["sidechain_cost_usd"], 6),
                "sidechain_share": round(s["sidechain_cost_usd"] / total, 4) if total else 0.0,
                "sidechain_messages": s["sidechain_messages"],
                "model_mix": sorted(s["models"]),
            })
        totals = {
            "main_cost_usd": round(sum(s["main_cost_usd"] for s in out), 6),
            "sidechain_cost_usd": round(sum(s["sidechain_cost_usd"] for s in out), 6),
        }
        grand_total = totals["main_cost_usd"] + totals["sidechain_cost_usd"]
        totals["sidechain_share"] = (
            round(totals["sidechain_cost_usd"] / grand_total, 4) if grand_total else 0.0
        )

        out.sort(key=lambda s: s["sidechain_cost_usd"], reverse=True)
        out, truncated = _truncate(out, _SUBAGENT_SESSIONS_N)

        return {"window": window, "sessions": out, "totals": totals, "truncated": truncated}


__all__ = [
    "AnalyticsCollector",
    "classify_api_error",
    "classify_error",
    "extract_file_path",
    "make_example",
    "parse_doc_for_analytics",
    "project_name_from_dir",
    "redact_secrets",
    "tool_result_text",
]
