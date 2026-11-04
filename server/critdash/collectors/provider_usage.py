"""Codex and Grok token usage (critfusion-fleet-q733.11 phase 2).

Formats, mapped from real files on the dev host (no transcript text is kept):

Codex -- ~/.codex/sessions/YYYY/MM/DD/rollout-*-<session>.jsonl, append-only.
  session_meta{payload:{id,cwd}}            session id, working directory
  turn_context{payload:{turn_id,model,cwd}} the model, per turn
  token_usage_record{timestamp, payload:{response_id, turn_id, session_id,
      usage:{input_tokens,cached_input_tokens,cache_write_input_tokens,
             output_tokens,reasoning_output_tokens}}}
      one record per model response; `usage` is that response alone (the
      turn_/thread_ siblings are running totals and are NOT read).
  `input_tokens` INCLUDES the cached and cache-written tokens and
  `output_tokens` INCLUDES the reasoning tokens (total_tokens == input +
  output in every record checked), so the uncached input is
  input - cached - cache_write and reasoning is not added again.
  (event_msg/token_count carries the same numbers without an id; unused.)

Grok -- ~/.grok/sessions/<url-quoted-cwd>/<session>/usage.json, rewritten
  whole each turn: {sessionId, turns:[{turnNumber, endedAt, modelUsage:{
  <model>:{inputTokens,outputTokens,cachedReadTokens,cacheCreationTokens,
  reasoningTokens}}}]}. Each turn entry is that turn alone (sums to the
  session totals). inputTokens includes cachedReadTokens, outputTokens
  includes reasoningTokens, same as Codex. `costUsdTicks` is NOT read: its
  unit is unverified and Grok is an OAuth login, so cost stays unknown.

Billing basis (Bryan-approved recommendation, bead q733.11): Codex is an API
key -> real per-token cost from pricing.json `codex_models` (None when a
model has no price); Grok -> tokens only, cost None. The database stores
tokens only; cost is derived at read time (pricing.compute_provider_cost_usd).

remote_probe.py carries a stdlib-only copy of these parsers (it is piped to
`ssh host python3 -` and cannot import critdash); test_provider_usage.py
asserts the two agree on the same fixtures.
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import re
from datetime import UTC, datetime
from urllib.parse import unquote

from . import BaseCollector

_OFFSET_PREFIX = "provider::"
_FRACTION_RE = re.compile(r"(\.\d{1,6})\d*")


def _num(v) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def split_tokens(input_total, cached, cache_write, output) -> dict:
    """Normalise OpenAI/xAI-style counts (input includes cached) to the
    Claude-style split the store uses (input excludes cached)."""
    cached, cache_write = _num(cached), _num(cache_write)
    return {
        "input": max(0, _num(input_total) - cached - cache_write),
        "output": _num(output),
        "cache_read": cached,
        "cache_write": cache_write,
    }


def grok_ts(value) -> str | None:
    """Grok writes nanosecond ISO timestamps; normalise to millisecond Z."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(_FRACTION_RE.sub(r"\1", value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _basename(cwd) -> str | None:
    return os.path.basename(cwd.rstrip("/")) or None if isinstance(cwd, str) else None


def new_codex_state(session_id: str | None = None) -> dict:
    return {"session_id": session_id, "project": None, "model": None, "turn_models": {}}


def parse_codex_lines(data: bytes, state: dict) -> list[dict]:
    """Usage events from complete rollout lines. `state` carries the session
    id, project and turn->model map across calls for one file and is mutated."""
    out: list[dict] = []
    for raw in data.split(b"\n"):
        line = raw.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(doc, dict) or not isinstance(doc.get("payload"), dict):
            continue
        payload = doc["payload"]
        kind = doc.get("type")
        if kind == "session_meta":
            state["session_id"] = payload.get("id") or state["session_id"]
            state["project"] = _basename(payload.get("cwd")) or state["project"]
        elif kind == "turn_context":
            model = payload.get("model")
            if model:
                state["model"] = model
                if payload.get("turn_id"):
                    state["turn_models"][payload["turn_id"]] = model
            state["project"] = _basename(payload.get("cwd")) or state["project"]
        elif kind == "token_usage_record":
            usage = payload.get("usage")
            response_id = payload.get("response_id")
            ts = doc.get("timestamp")
            session_id = payload.get("session_id") or state["session_id"]
            if not isinstance(usage, dict) or not response_id or not ts or not session_id:
                continue
            out.append(
                {
                    "provider": "codex",
                    "session_id": session_id,
                    "event_key": response_id,
                    "ts": ts,
                    "model": state["turn_models"].get(payload.get("turn_id")) or state["model"] or "unknown",
                    "project": state["project"],
                    **split_tokens(
                        usage.get("input_tokens"),
                        usage.get("cached_input_tokens"),
                        usage.get("cache_write_input_tokens"),
                        usage.get("output_tokens"),
                    ),
                }
            )
    return out


def parse_grok_usage(doc, dirname: str | None = None) -> list[dict]:
    """One event per (turn, model) from a parsed usage.json. `dirname` is the
    url-quoted cwd directory the session lives in (-> project)."""
    if not isinstance(doc, dict) or not doc.get("sessionId"):
        return []
    project = _basename(unquote(dirname)) if dirname else None
    out: list[dict] = []
    for turn in doc.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        ts = grok_ts(turn.get("endedAt"))
        models = turn.get("modelUsage")
        if ts is None or not isinstance(models, dict):
            continue
        for model, u in models.items():
            if not isinstance(u, dict):
                continue
            out.append(
                {
                    "provider": "grok",
                    "session_id": doc["sessionId"],
                    # Contract: a finished turn is append-only -- usage.json only
                    # ever gains turns, never rewrites one. Rows are inserted
                    # OR IGNOREd on this key, so an edited turn would NOT update.
                    "event_key": f"{turn.get('turnNumber')}:{model}",
                    "ts": ts,
                    "model": model,
                    "project": project,
                    **split_tokens(
                        u.get("inputTokens"),
                        u.get("cachedReadTokens"),
                        u.get("cacheCreationTokens"),
                        u.get("outputTokens"),
                    ),
                }
            )
    return out


class ProviderUsageCollector(BaseCollector):
    """Tails Codex rollouts and re-reads changed Grok usage.json files into
    provider_usage_events. UsageCollector's rollups read the table."""

    name = "provider_usage"
    interval_s = 10.0

    def __init__(
        self,
        ctx=None,
        store=None,
        host: str = "localhost",
        codex_dir: str = "~/.codex",
        grok_dir: str = "~/.grok",
    ):
        super().__init__(ctx)
        self.store = store
        self.host = host
        self.codex_dir = codex_dir
        self.grok_dir = grok_dir
        # per-rollout parse state, in memory only: a restarted process has
        # none, so a file with a stored offset is re-read from byte 0 once
        # (INSERT OR IGNORE keeps that idempotent) to recover its models.
        self._codex_state: dict[str, dict] = {}

    async def collect(self) -> dict:
        if self.store is None:
            return {}
        inserted = await asyncio.to_thread(self._ingest)
        return {"_inserted": inserted}

    def _ingest(self) -> int:
        return self._ingest_codex() + self._ingest_grok()

    def _ingest_codex(self) -> int:
        pattern = os.path.join(
            os.path.expanduser(self.codex_dir), "sessions", "*", "*", "*", "rollout-*.jsonl"
        )
        inserted = 0
        for path in glob.glob(pattern):
            try:
                st = os.stat(path)
            except OSError:
                continue
            key = _OFFSET_PREFIX + path
            prev = self.store.get_offset(key)
            offset = 0
            if prev is not None:
                prev_inode, prev_offset, _ = prev
                if prev_inode == st.st_ino and prev_offset <= st.st_size and path in self._codex_state:
                    offset = prev_offset
            if offset >= st.st_size:
                self.store.set_offset(key, st.st_ino, st.st_size, st.st_mtime)
                continue
            state = self._codex_state.get(path) if offset else None
            if state is None:
                state = self._codex_state[path] = new_codex_state()
            with open(path, "rb") as f:
                f.seek(offset)
                chunk = f.read()
            last_nl = chunk.rfind(b"\n")
            if last_nl == -1:
                continue
            usable = chunk[: last_nl + 1]
            new_rows = parse_codex_lines(usable, state)
            # rows land before the offset moves (same rule as UsageCollector)
            inserted += self.store.insert_provider_usage_events([{**r, "host": self.host} for r in new_rows])
            self.store.set_offset(key, st.st_ino, offset + len(usable), st.st_mtime)
        return inserted

    def _ingest_grok(self) -> int:
        pattern = os.path.join(os.path.expanduser(self.grok_dir), "sessions", "*", "*", "usage.json")
        inserted = 0
        for path in glob.glob(pattern):
            try:
                st = os.stat(path)
            except OSError:
                continue
            key = _OFFSET_PREFIX + path
            prev = self.store.get_offset(key)
            if prev is not None and prev[0] == st.st_ino and prev[1] == st.st_size and prev[2] == st.st_mtime:
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    doc = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue  # mid-write or unreadable: retried next tick, offset not advanced
            dirname = os.path.basename(os.path.dirname(os.path.dirname(path)))
            inserted += self.store.insert_provider_usage_events(
                [{**r, "host": self.host} for r in parse_grok_usage(doc, dirname)]
            )
            self.store.set_offset(key, st.st_ino, st.st_size, st.st_mtime)
        return inserted
