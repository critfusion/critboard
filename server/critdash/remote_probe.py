"""CritBoard remote probe -- STDLIB ONLY, no third-party imports.

This file is never installed on a remote host. The `remote` collector
(critdash/collectors/remote.py) reads this file's source fresh on every call
and pipes it to `ssh <host> python3 -`, with a positional `host` argument and
`--flag` overrides on the command line (argparse args after the `-`). That
means editing this file takes effect on the *next* probe call, no deployment
step, no code installed on any remote host.

Prints exactly one JSON object to stdout: {host, generated_at, agents,
worktrees, system, usage_buckets, tool_buckets, error_buckets,
error_examples, trouble_file_buckets, api_error_buckets, session_buckets,
productivity_buckets}. See SPEC.md and the briefing (Task 2) for the frozen
shape. The wave-2 analytics buckets (tool_buckets through session_buckets)
are all pre-aggregated -- never raw per-message rows -- day-granularity where
usage_buckets is hour-granularity, for the same reason: a bounded, cheap
thing to ship and upsert on RemoteCollector's replace-semantics tables.

Design notes:
  - Every section (worktrees / agents / system / usage) is collected inside
    its own try/except so one broken subsystem (e.g. no herdr, an
    unsupported OS for system metrics) never blanks the others -- same
    "degrade gracefully" rule as the local collectors.
  - `agents`/`worktrees` reuse the exact same parsing rules as the local
    collectors (critdash/collectors/agents.py, worktrees.py), duplicated here
    intentionally rather than imported, because this file must survive being
    copied alone to a machine with no critdash package installed.
  - `usage_buckets` carries TOKEN COUNTS ONLY, never a computed cost -- all
    pricing is applied centrally on the local host from config/pricing.json, so a
    rate correction fixes every host at once. This also means fast-mode billing
    (message.usage.speed == "fast") cannot be reflected in a bucket the way
    the local collector's per-row `speed` column can; remote token buckets are
    costed at each model's standard rate on the local host. Documented gap, not a
    bug: SPEC's usage_buckets shape has no `speed` field.
  - Backfilling 1000+ jsonl files on every call would make this collector too
    slow to run on any reasonable interval. `collect_usage()` caches each
    file's aggregated (hour, model, project) bucket contribution in a small
    JSON state file under ~/.cache/critdash-probe/, keyed by
    (path, inode, size, mtime). An unchanged file is skipped entirely; a
    changed file is re-read in full (not byte-offset incremental) so
    message_id dedup is trivial (an in-memory set scoped to that one parse).
    "Today" filtering is deliberately NOT done here -- these are historical
    hour buckets; the caller (which knows the real "now") decides what counts
    as "today" when it reads them back out of remote_usage_buckets.
  - agents' tokens_today/cost_today_usd/msg_count_today are left at zero here
    on purpose: computing them would mean also caching per-session "today"
    aggregates, which have a real staleness bug (a file untouched since
    yesterday would keep reporting yesterday's date as "today" forever). A
    remote host with no herdr installed reports agents as [] here in
    practice -- this is a documented, deliberately deferred gap for a future
    host that both runs herdr AND needs live per-agent spend.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

# -- shared helpers -----------------------------------------------------------


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def default_state_path() -> str:
    return os.path.expanduser("~/.cache/critdash-probe/state.json")


def load_state(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return {"files": {}}


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


# -- usage: decode project dir + parse jsonl (mirrors collectors/usage.py) ---

_PROJECT_DIR_CACHE: dict[str, str] = {}


def decode_project_dir(dirname: str) -> str:
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


def project_name_from_dir(dirname: str) -> str:
    full_path = decode_project_dir(dirname)
    return os.path.basename(full_path.rstrip("/")) or dirname


# -- wave-2 analytics (mirrors collectors/analytics.py) ----------------------
# Duplicated here rather than imported -- same reason as agents/worktrees
# above: this file must survive being copied alone to a host with no critdash
# package installed. If you change the classification rules or extraction
# logic in collectors/analytics.py, mirror the change here too -- a test
# (tests/test_remote_probe.py) checks both copies classify the same fixed
# sample set identically, as a drift detector.

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
    # Kept in sync with collectors/analytics.py's _ERROR_RULES -- see that
    # module's comment on why Kimi's quota exhaustion is its own kind rather
    # than folded into rate_limit.
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
    if doc.get("apiErrorStatus") == 529:
        return "overloaded_error"
    return doc.get("error") or _DEFAULT_ERROR_KIND


import re as _re  # noqa: E402 -- kept next to its one use, mirrors analytics.py layout

_SECRET_RE = _re.compile(
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


def extract_usage_fields(doc: dict) -> dict | None:
    """Same extraction rules as collectors/usage.py's extract_usage_row, minus
    cost (no pricing on the remote side -- see module docstring)."""
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

    return {
        "message_id": message_id,
        "ts": ts,
        "model": model,
        "input": input_tokens,
        "output": output_tokens,
        "cache_read": cache_read,
        "cache_write_5m": cw_5m,
        "cache_write_1h": cw_1h,
    }


_BUCKET_FIELDS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h", "messages")
_SEP = "\x1f"


def aggregate_file(path: str, project: str) -> dict:
    """Full ONE-PASS parse of one jsonl file, producing every bucket category
    the probe ships (usage token buckets, plus wave-2 tool/error/api_error/
    session buckets -- same single read, no second pass over the same bytes,
    which matters because a changed file is re-read in full, never
    byte-offset incremental, see collect_usage_and_analytics's docstring).

    Returns {"usage": {...}, "tools": {...}, "errors": {...},
    "error_examples": {...}, "trouble_files": {...}, "api_errors": {...},
    "sessions": {...}} -- each an internal dict keyed by a _SEP-joined tuple,
    unpacked by the caller (mirrors the pre-existing usage-only shape so a
    cache entry written before this feature just contributes empty dicts for
    the new categories until that file next changes -- see module docstring
    at the top of the wave-2 analytics section)."""
    usage_buckets: dict[tuple[str, str], dict] = {}
    tool_buckets: dict[tuple[str, str], dict] = {}
    error_buckets: dict[tuple[str, str, str], dict] = {}
    error_examples: dict[str, dict] = {}
    trouble_file_buckets: dict[tuple[str, str, str], dict] = {}
    api_error_buckets: dict[tuple[str, str], dict] = {}
    session_buckets: dict[tuple[str, str, str, str], dict] = {}

    seen_ids: set[str] = set()
    pending_tool_use: dict[str, dict] = {}

    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return {
            "usage": {}, "tools": {}, "errors": {}, "error_examples": {},
            "trouble_files": {}, "api_errors": {}, "sessions": {},
        }

    for raw_line in data.split(b"\n"):
        line = raw_line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue

        row = extract_usage_fields(doc)
        if row is not None and row["message_id"] not in seen_ids:
            seen_ids.add(row["message_id"])
            hour = row["ts"][:13]
            key = (hour, row["model"])
            b = usage_buckets.setdefault(key, dict.fromkeys(_BUCKET_FIELDS, 0))
            b["input"] += row["input"]
            b["output"] += row["output"]
            b["cache_read"] += row["cache_read"]
            b["cache_write_5m"] += row["cache_write_5m"]
            b["cache_write_1h"] += row["cache_write_1h"]
            b["messages"] += 1

            model = row["model"]
            session_id = doc.get("sessionId") or doc.get("session_id")
            is_sidechain = "1" if doc.get("isSidechain") else "0"
            if session_id:
                day = row["ts"][:10]
                skey = (day, session_id, is_sidechain, model)
                sb = session_buckets.setdefault(
                    skey, {"project": project, **dict.fromkeys(_BUCKET_FIELDS, 0)}
                )
                sb["input"] += row["input"]
                sb["output"] += row["output"]
                sb["cache_read"] += row["cache_read"]
                sb["cache_write_5m"] += row["cache_write_5m"]
                sb["cache_write_1h"] += row["cache_write_1h"]
                sb["messages"] += 1

        uuid_ = doc.get("uuid")
        ts = doc.get("timestamp")
        if not uuid_ or not ts:
            continue

        if doc.get("isApiErrorMessage"):
            day = ts[:10]
            kind = classify_api_error(doc)
            ab = api_error_buckets.setdefault((day, kind), {"count": 0, "last_seen": ts})
            ab["count"] += 1
            if ts > ab["last_seen"]:
                ab["last_seen"] = ts
            continue

        message = doc.get("message") or {}
        content = message.get("content")
        if not isinstance(content, list):
            continue
        day = ts[:10]

        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                tool = block.get("name") or "unknown"
                tb = tool_buckets.setdefault((day, tool), {"calls": 0, "errors": 0})
                tb["calls"] += 1
                tool_use_id = block.get("id")
                if tool_use_id:
                    pending_tool_use[tool_use_id] = {
                        "day": day, "tool": tool, "file_path": extract_file_path(block.get("input")),
                    }
            elif btype == "tool_result" and block.get("is_error"):
                tool_use_id = block.get("tool_use_id")
                matched = pending_tool_use.pop(tool_use_id, None) if tool_use_id else None
                tool = matched["tool"] if matched else "unknown"
                file_path = matched["file_path"] if matched else None
                text = tool_result_text(block.get("content"))
                kind = classify_error(text)
                excerpt = make_example(text)

                tb = tool_buckets.setdefault((day, tool), {"calls": 0, "errors": 0})
                tb["errors"] += 1

                eb = error_buckets.setdefault((day, kind, tool), {"count": 0, "last_seen": ts})
                eb["count"] += 1
                if ts > eb["last_seen"]:
                    eb["last_seen"] = ts

                if excerpt:
                    ex = error_examples.get(kind)
                    if ex is None or ts >= ex["last_seen"]:
                        error_examples[kind] = {"example": excerpt, "last_seen": ts}

                if file_path:
                    fb = trouble_file_buckets.setdefault(
                        (day, file_path, tool), {"errors": 0}
                    )
                    fb["errors"] += 1

    return {
        "usage": {f"{h}{_SEP}{m}": v for (h, m), v in usage_buckets.items()},
        "tools": {f"{d}{_SEP}{t}": v for (d, t), v in tool_buckets.items()},
        "errors": {f"{d}{_SEP}{k}{_SEP}{t}": v for (d, k, t), v in error_buckets.items()},
        "error_examples": error_examples,
        "trouble_files": {f"{d}{_SEP}{p}{_SEP}{t}": v for (d, p, t), v in trouble_file_buckets.items()},
        "api_errors": {f"{d}{_SEP}{k}": v for (d, k), v in api_error_buckets.items()},
        "sessions": {
            f"{d}{_SEP}{sid}{_SEP}{sc}{_SEP}{m}": v for (d, sid, sc, m), v in session_buckets.items()
        },
    }


def collect_usage_and_analytics(projects_glob: str, state_file: str | None = None) -> dict:
    """Backfills/incrementally-caches every jsonl file once (see
    aggregate_file's one-pass docstring), then merges the per-file bucket
    dicts into flat lists ready to ship in the probe payload. An UNCHANGED
    file reuses whatever bucket categories its cache entry has -- a cache
    entry written before this feature shipped only has "buckets" (the old
    usage-only key, read via .get("usage") or the legacy "buckets" key for
    back-compat) and no tool/error/session data; that file's wave-2
    contribution stays empty until it next changes and gets a full
    recompute. This is a deliberate trade-off to avoid a one-time full
    re-read of a host's entire jsonl history (multiple GB on a busy fleet)
    inside one probe call's `ssh_timeout_s` budget -- see the delivery
    report for the real number measured against a live remote host."""
    state_file = state_file or default_state_path()
    state = load_state(state_file)
    old_files = state.get("files", {})
    pattern = os.path.expanduser(projects_glob)
    paths = glob.glob(pattern)

    new_files: dict[str, dict] = {}
    usage_merged: dict[tuple[str, str, str], dict] = {}
    tool_merged: dict[tuple[str, str], dict] = {}
    error_merged: dict[tuple[str, str, str], dict] = {}
    error_examples_merged: dict[str, dict] = {}
    trouble_merged: dict[tuple[str, str, str], dict] = {}
    api_error_merged: dict[tuple[str, str], dict] = {}
    session_merged: dict[tuple[str, str, str, str], dict] = {}

    for path in paths:
        try:
            st = os.stat(path)
        except OSError:
            continue
        inode, size, mtime = st.st_ino, st.st_size, st.st_mtime
        cached = old_files.get(path)
        unchanged = (
            cached
            and cached.get("inode") == inode
            and cached.get("size") == size
            and cached.get("mtime") == mtime
        )
        if unchanged:
            buckets = cached
            project = cached.get("project") or "unknown"
            # carry the cache entry forward -- without this, an unchanged
            # file's entry is read from old_files this call but never
            # written back into new_files, so it silently drops out of the
            # persisted state on save. The next call then finds it "not
            # cached" and does a full re-parse, which finds it unchanged
            # again and drops it again: perpetual full-rebuild every other
            # call instead of the one-time backfill this cache exists for.
            new_files[path] = cached
        else:
            dirname = os.path.basename(os.path.dirname(path))
            project = project_name_from_dir(dirname)
            buckets = aggregate_file(path, project)
            new_files[path] = {"inode": inode, "size": size, "mtime": mtime, "project": project, **buckets}

        # back-compat: a pre-wave-2 cache entry stored usage buckets under
        # the old key "buckets" instead of "usage".
        usage_buckets = buckets.get("usage") or buckets.get("buckets") or {}
        for k, v in usage_buckets.items():
            hour, model = k.split(_SEP)
            agg = usage_merged.setdefault((hour, model, project), dict.fromkeys(_BUCKET_FIELDS, 0))
            for f in _BUCKET_FIELDS:
                agg[f] += v[f]

        for k, v in (buckets.get("tools") or {}).items():
            day, tool = k.split(_SEP)
            agg = tool_merged.setdefault((day, tool), {"calls": 0, "errors": 0})
            agg["calls"] += v["calls"]
            agg["errors"] += v["errors"]

        for k, v in (buckets.get("errors") or {}).items():
            day, kind, tool = k.split(_SEP)
            agg = error_merged.setdefault((day, kind, tool), {"count": 0, "last_seen": v["last_seen"]})
            agg["count"] += v["count"]
            if v["last_seen"] > agg["last_seen"]:
                agg["last_seen"] = v["last_seen"]

        for kind, v in (buckets.get("error_examples") or {}).items():
            ex = error_examples_merged.get(kind)
            if ex is None or v["last_seen"] >= ex["last_seen"]:
                error_examples_merged[kind] = v

        for k, v in (buckets.get("trouble_files") or {}).items():
            day, fpath, tool = k.split(_SEP)
            agg = trouble_merged.setdefault((day, fpath, tool), {"errors": 0})
            agg["errors"] += v["errors"]

        for k, v in (buckets.get("api_errors") or {}).items():
            day, kind = k.split(_SEP)
            agg = api_error_merged.setdefault((day, kind), {"count": 0, "last_seen": v["last_seen"]})
            agg["count"] += v["count"]
            if v["last_seen"] > agg["last_seen"]:
                agg["last_seen"] = v["last_seen"]

        for k, v in (buckets.get("sessions") or {}).items():
            day, sid, sidechain, model = k.split(_SEP)
            default = {"project": v.get("project"), **dict.fromkeys(_BUCKET_FIELDS, 0)}
            agg = session_merged.setdefault((day, sid, sidechain, model), default)
            for f in _BUCKET_FIELDS:
                agg[f] += v[f]

    # Merge onto the CURRENT on-disk state rather than overwrite wholesale --
    # collect_kimi() below shares this same state file (its own "kimi_files"
    # top-level key), and each function only knows its own key. A plain
    # `save_state(state_file, {"files": new_files})` would silently wipe out
    # whatever the other one just wrote, depending on call order.
    save_state(state_file, {**load_state(state_file), "files": new_files})

    return {
        "usage_buckets": [
            {"hour": hour, "model": model, "project": project, **totals}
            for (hour, model, project), totals in usage_merged.items()
        ],
        "tool_buckets": [
            {"day": day, "tool": tool, **totals} for (day, tool), totals in tool_merged.items()
        ],
        "error_buckets": [
            {"day": day, "kind": kind, "tool": tool, **totals}
            for (day, kind, tool), totals in error_merged.items()
        ],
        "error_examples": [
            {"kind": kind, **v} for kind, v in error_examples_merged.items()
        ],
        "trouble_file_buckets": [
            {"day": day, "path": fpath, "tool": tool, **totals}
            for (day, fpath, tool), totals in trouble_merged.items()
        ],
        "api_error_buckets": [
            {"day": day, "kind": kind, **totals} for (day, kind), totals in api_error_merged.items()
        ],
        "session_buckets": [
            {"day": day, "session_id": sid, "is_sidechain": int(sidechain), "model": model, **totals}
            for (day, sid, sidechain, model), totals in session_merged.items()
        ],
    }


def collect_usage(projects_glob: str, state_file: str | None = None) -> list[dict]:
    """Back-compat wrapper: pre-wave-2 callers (and tests) that only want the
    usage_buckets list. build_result() calls collect_usage_and_analytics()
    directly since it needs every category from the same pass."""
    return collect_usage_and_analytics(projects_glob, state_file)["usage_buckets"]


# -- Kimi (second provider, mirrors collectors/kimi.py) -----------------------
# Duplicated here rather than imported -- same "must survive being copied
# alone to a host with no critdash package installed" reason as the rest of
# this file. Kimi may not exist on every configured remote host (some lack
# ~/.kimi-code) -- this section makes a
# missing kimi_dir a silent no-op (empty agents/buckets, no error) so a host
# that later grows a Kimi install is picked up automatically, no config
# change. See collectors/kimi.py's module docstring for the two constraints
# (single tokens-per-turn total, no cost -- Kimi bills by subscription
# quota) this section must not "fix" around.

def epoch_ms_to_iso(ms) -> str | None:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000.0, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    except (TypeError, ValueError, OSError):
        return None


def load_session_index(kimi_dir: str) -> list[dict]:
    path = os.path.join(kimi_dir, "session_index.jsonl")
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return out
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = doc.get("sessionId")
        sdir = doc.get("sessionDir")
        if not sid or not sdir:
            continue
        out.append({"session_id": sid, "session_dir": sdir, "work_dir": doc.get("workDir")})
    return out


def load_workspaces(kimi_dir: str) -> dict[str, dict]:
    path = os.path.join(kimi_dir, "workspaces.json")
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(doc, dict):
        return {}
    workspaces = doc.get("workspaces")
    return workspaces if isinstance(workspaces, dict) else {}


def workspace_id_from_session_dir(session_dir: str) -> str | None:
    if not session_dir:
        return None
    return os.path.basename(os.path.dirname(session_dir.rstrip("/"))) or None


def load_session_state(session_dir: str) -> dict | None:
    path = os.path.join(session_dir, "state.json")
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def discover_kimi_sessions(kimi_dir: str) -> list[dict]:
    index = load_session_index(kimi_dir)
    if not index:
        return []
    workspaces = load_workspaces(kimi_dir)

    out: list[dict] = []
    for entry in index:
        session_dir = entry["session_dir"]
        state = load_session_state(session_dir)
        if state is None:
            continue
        cwd = state.get("cwd") or entry.get("work_dir")
        if not cwd:
            wsid = workspace_id_from_session_dir(session_dir)
            cwd = (workspaces.get(wsid) or {}).get("root")
        agents = state.get("agents") or {}
        wire_paths = {
            agent_id: os.path.join(info["homedir"], "wire.jsonl")
            for agent_id, info in agents.items()
            if isinstance(info, dict) and info.get("homedir")
        }
        out.append({
            "session_id": entry["session_id"],
            "session_dir": session_dir,
            "cwd": cwd,
            "updated_at_ms": state.get("updatedAt"),
            "agent_wire_paths": wire_paths,
        })
    return out


_KIMI_TAIL_READ_BYTES = 65536

# ---------------------------------------------------------------------------
# Session-transcript bead extraction -- a bounded, stateless-per-call
# duplicate of critdash/collectors/bead_sessions.py's logic (never imported:
# this whole file is piped to `ssh host python3 -` fresh on every call, so it
# has to survive being copied ALONE to a machine with no critdash package
# installed -- same rule the module docstring already states for the
# agents/worktrees duplication above). See bead_sessions.py's own docstring
# for the two verified transcript formats (Claude Code jsonl, Kimi
# wire.jsonl) and the bd output markers this reads.
#
# Unlike the local collector, this has NO incremental cache across ticks --
# each `ssh host python3 -` invocation is a fresh interpreter with no state
# from the last probe (see this module's own docstring: "stateless per
# call"). Every live session's transcript(s) are re-scanned in full on every
# probe, bounded by `_BD_SCAN_TAIL_BYTES`: a transcript larger than that is
# read from its TAIL only, not from byte 0 -- a documented, bounded
# fallback (an active bead claimed further back than the tail window, with
# no bd activity at all since, would be missed -- judged acceptable: a
# session with megabytes of non-bd activity since its last claim is not the
# common case this dashboard needs to catch). Measured against this
# module's own real remote hosts; see the timing note in the collector's
# test/verification report.
_BD_SCAN_TAIL_BYTES = 2_000_000
_BEAD_TRACKED_KINDS = frozenset({"claude", "kimi"})
# Cap on how many unreleased-claim ids one session ships off this host --
# keeps the probe payload small (this is the only thing about a session's
# bd activity that ever leaves the host: ids, nothing else).
_BD_MAX_CLAIMS = 10

_BD_ALREADY_CLAIMED_RE = re.compile(r"already claimed", re.IGNORECASE)
_BD_ERROR_RE = re.compile(r"(?m)^Error:")
_BD_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_BD_TIMEOUT_DURATION_RE = re.compile(r"^\d+[smh]?$")
_BD_VALUE_FLAGS = {
    "--acceptance", "--add-label", "-a", "--assignee", "--append-notes",
    "--await-id", "--body-file", "-d", "--description", "--design",
    "--design-file", "--due", "--defer", "-e", "--estimate",
    "--external-ref", "--metadata", "--notes", "--parent", "-p",
    "--priority", "--remove-label", "-r", "--reason", "--reason-file",
    "--session", "--set-labels", "--set-metadata", "--spec-id", "-s",
    "--status", "--title", "-t", "--type", "--unset-metadata", "--actor",
    "--db", "-C", "--directory", "--dolt-auto-commit",
}
# Same strict allow-list as collectors/beads.py's validate_bead_id --
# duplicated (not imported) for the same "must survive being copied alone"
# reason as the rest of this file.
_BD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _bd_validate_id(bead_id: str) -> bool:
    return bool(bead_id) and bool(_BD_ID_RE.match(bead_id))


_BD_HEREDOC_OPEN_RE = re.compile(r"<<(-)?")
_BD_HEREDOC_WORD_RE = re.compile(r"[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _bd_preprocess(command: str) -> str | None:
    """Quote-aware pass over the whole command, mirroring
    bead_sessions.py's _preprocess_command: drops heredoc BODIES entirely
    (from the line after an unquoted `<<[-]DELIM` marker through the
    terminator line), and drops the CONTENTS of any unquoted `$( ... )` or
    backtick substitution. Everything else -- crucially, the text of any
    quoted argument -- passes through byte for byte, so the tokenizer below
    sees exactly the quoting a real shell would. Returns None on an
    unterminated quote/substitution/heredoc; see that module for the full
    rationale (Defect 1)."""
    out: list[str] = []
    i, n = 0, len(command)
    in_squote = in_dquote = False
    while i < n:
        c = command[i]
        if in_squote:
            out.append(c)
            i += 1
            if c == "'":
                in_squote = False
            continue
        if in_dquote:
            if c == "\\" and i + 1 < n:
                out.append(c)
                out.append(command[i + 1])
                i += 2
                continue
            out.append(c)
            i += 1
            if c == '"':
                in_dquote = False
            continue
        if c == "'":
            in_squote = True
            out.append(c)
            i += 1
            continue
        if c == '"':
            in_dquote = True
            out.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            out.append(c)
            out.append(command[i + 1])
            i += 2
            continue
        if c == "`":
            j = i + 1
            while j < n and command[j] != "`":
                j += 2 if command[j] == "\\" and j + 1 < n else 1
            if j >= n:
                return None
            i = j + 1
            continue
        if c == "$" and i + 1 < n and command[i + 1] == "(":
            depth = 1
            j = i + 2
            sq = dq = False
            while j < n and depth > 0:
                cj = command[j]
                if sq:
                    if cj == "'":
                        sq = False
                    j += 1
                    continue
                if dq:
                    if cj == "\\" and j + 1 < n:
                        j += 2
                        continue
                    if cj == '"':
                        dq = False
                    j += 1
                    continue
                if cj == "'":
                    sq = True
                elif cj == '"':
                    dq = True
                elif cj == "\\" and j + 1 < n:
                    j += 1
                elif cj == "(":
                    depth += 1
                elif cj == ")":
                    depth -= 1
                j += 1
            if depth != 0:
                return None
            i = j
            continue
        m = _BD_HEREDOC_OPEN_RE.match(command, i)
        if m:
            wm = _BD_HEREDOC_WORD_RE.match(command, m.end())
            if wm:
                delim = wm.group(2)
                strip_tabs = bool(m.group(1))
                out.append(command[i:wm.end()])
                i = wm.end()
                eol = command.find("\n", i)
                if eol == -1:
                    out.append(command[i:])
                    i = n
                    break
                out.append(command[i:eol + 1])
                i = eol + 1
                found = False
                while i < n:
                    line_end = command.find("\n", i)
                    line_end_excl = line_end if line_end != -1 else n
                    line = command[i:line_end_excl]
                    cmp_line = line.lstrip("\t") if strip_tabs else line
                    i = (line_end + 1) if line_end != -1 else n
                    if cmp_line == delim:
                        found = True
                        break
                if not found:
                    return None
                continue
        out.append(c)
        i += 1
    if in_squote or in_dquote:
        return None
    return "".join(out)


_BD_CONTROL_TOKENS = frozenset({"&&", "||", ";", "|", "&"})
_BD_NEWLINE_ONLY_RE = re.compile(r"^\n+$")
_BD_REDIR_OP_RE = re.compile(r"^&?[<>]{1,2}&?$")
_BD_REDIR_FD_RE = re.compile(r"^\d{1,2}$")


def _bd_strip_redirections(tokens: list[str]) -> list[str]:
    out: list[str] = []
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        if _BD_REDIR_FD_RE.match(tok) and i + 1 < n and _BD_REDIR_OP_RE.match(tokens[i + 1]):
            i += 1
            continue
        if _BD_REDIR_OP_RE.match(tok):
            i += 1
            if i < n:
                i += 1
            continue
        out.append(tok)
        i += 1
    return out


def _bd_split_segments(command: str) -> list[list[str]] | None:
    """Mirrors bead_sessions.py's _split_bd_segments: tokenize the WHOLE
    command respecting shell quoting first, then split on control operators
    (&&, ||, ;, |, &, unquoted newlines) that occur outside quotes, with
    redirections stripped from each segment. Returns None -- never a naive
    fallback -- if the command can't be tokenized at all."""
    pre = _bd_preprocess(command)
    if pre is None:
        return None
    try:
        lex = shlex.shlex(pre, posix=True, punctuation_chars=";&|()<>\n")
        lex.whitespace_split = True
        lex.whitespace = lex.whitespace.replace("\n", "")
        tokens = list(lex)
    except ValueError:
        return None
    segments: list[list[str]] = []
    seg: list[str] = []
    for tok in tokens:
        if tok in _BD_CONTROL_TOKENS or _BD_NEWLINE_ONLY_RE.match(tok):
            if seg:
                segments.append(_bd_strip_redirections(seg))
            seg = []
        else:
            seg.append(tok)
    if seg:
        segments.append(_bd_strip_redirections(seg))
    return [s for s in segments if s]


def _bd_find_invocation(segment: list[str]) -> list[str] | None:
    i, n = 0, len(segment)
    while i < n:
        tok = segment[i]
        if tok == "env":
            i += 1
            continue
        if tok == "timeout":
            i += 1
            if i < n and _BD_TIMEOUT_DURATION_RE.match(segment[i]):
                i += 1
            continue
        if _BD_ENV_ASSIGN_RE.match(tok):
            i += 1
            continue
        break
    if i >= n or segment[i] == "export":
        return None
    base = segment[i].rsplit("/", 1)[-1]
    if base != "bd":
        return None
    return segment[i + 1 :]


def _bd_parse_call(argv: list[str]) -> tuple[str, list[str], dict] | None:
    """Mirrors bead_sessions.py's _parse_bd_call, including `bd assign <id>
    <name>` (shorthand for `bd update <id> --assignee <name>`, verified via
    `bd assign --help`) -- its second positional is a name, NEVER a bead id."""
    if not argv:
        return None
    if argv[0] == "assign":
        positionals = [t for t in argv[1:] if t != "--"]
        if len(positionals) < 2:
            return None
        return "assign", [positionals[0]], {"assignee": positionals[1]}
    if argv[0] not in ("update", "close", "done"):
        return None
    subcmd = argv[0]
    ids: list[str] = []
    flags: dict = {}
    i, n = 1, len(argv)
    while i < n:
        tok = argv[i]
        if tok == "--":
            i += 1
            continue
        if tok.startswith("--") and "=" in tok:
            name, _sep, val = tok.partition("=")
            flags[name] = val
            i += 1
            continue
        if len(tok) > 1 and tok[0] == "-" and not re.match(r"^-\d", tok):
            if tok in _BD_VALUE_FLAGS:
                flags[tok] = argv[i + 1] if i + 1 < n else ""
                i += 2
            else:
                flags[tok] = True
                i += 1
            continue
        ids.append(tok)
        i += 1
    return subcmd, ids, flags


def _bd_classify_call(subcmd: str, ids: list[str], flags: dict) -> str | None:
    """`bd assign <id> <name>` is ALWAYS a release for this session,
    whatever `<name>` is -- see bead_sessions.py's _classify_bd_call."""
    if subcmd == "assign":
        return "release" if ids else None
    if subcmd in ("close", "done"):
        return "release" if ids else None
    if flags.get("--claim"):
        return "claim"
    status = flags.get("--status", flags.get("-s"))
    if status is not None:
        return "claim" if str(status).strip().lower().replace("-", "_") == "in_progress" else "release"
    assignee = flags.get("--assignee", flags.get("-a"))
    if assignee == "":
        return "release"
    return None


def _bd_result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if isinstance(p, str))
    return ""


def _bd_resolve_success_ids(result_text: str, is_error: bool, attempted_ids: list[str]) -> list[str]:
    """Mirrors bead_sessions.py's _resolve_success_ids: accepts/rejects the
    already-literal `attempted_ids` based on is_error and known
    failure-text markers only -- NEVER infers/substitutes an id from
    `result_text` (Defect 2)."""
    if is_error:
        return []
    if _BD_ALREADY_CLAIMED_RE.search(result_text) or _BD_ERROR_RE.search(result_text):
        return []
    return attempted_ids


def _bd_apply_command(
    events: list[tuple], seq_box: list[int], command: str, is_error: bool, result_text: str, ts: float | None
) -> None:
    segments = _bd_split_segments(command)
    if segments is None:
        return
    for seg in segments:
        argv = _bd_find_invocation(seg)
        if argv is None:
            continue
        parsed = _bd_parse_call(argv)
        if parsed is None:
            continue
        subcmd, ids, flags = parsed
        if not ids or not all(_bd_validate_id(i) for i in ids):
            continue
        action = _bd_classify_call(subcmd, ids, flags)
        if action is None:
            continue
        for bid in _bd_resolve_success_ids(result_text, is_error, ids):
            seq_box[0] += 1
            events.append((seq_box[0], action, bid, ts))


def _bd_event_key(seq: int, ts: float | None) -> tuple[float, int]:
    return (ts if ts is not None else float("-inf"), seq)


def _bd_unreleased_claims(events: list[tuple]) -> list[tuple[str, float | None]]:
    """Mirrors bead_sessions.py's _unreleased_claims: every bead this
    session has claimed and not (yet) released, most-recent-claim-first
    (Defect 3)."""
    latest_claim: dict[str, tuple[int, float | None]] = {}
    for seq, action, bid, ts in events:
        if action != "claim":
            continue
        prev = latest_claim.get(bid)
        if prev is None or _bd_event_key(seq, ts) > _bd_event_key(*prev):
            latest_claim[bid] = (seq, ts)
    unreleased: list[tuple[str, float | None, tuple[float, int]]] = []
    for bid, (seq, ts) in latest_claim.items():
        key = _bd_event_key(seq, ts)
        released_after = any(
            a == "release" and rid == bid and _bd_event_key(rseq, rts) > key
            for rseq, a, rid, rts in events
        )
        if not released_after:
            unreleased.append((bid, ts, key))
    unreleased.sort(key=lambda c: c[2], reverse=True)
    return [(bid, ts) for bid, ts, _key in unreleased]


def _bd_scan_claude_file(path: str, events: list[tuple], seq_box: list[int]) -> None:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > _BD_SCAN_TAIL_BYTES:
                f.seek(size - _BD_SCAN_TAIL_BYTES)
            data = f.read()
    except OSError:
        return
    pending: dict[str, str] = {}
    for raw in data.split(b"\n"):
        if not raw:
            continue
        has_bash = b'"Bash"' in raw
        has_result = b'"tool_result"' in raw
        if not has_bash and not (has_result and pending):
            continue
        try:
            doc = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(doc, dict):
            continue
        msg = doc.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "tool_use" and has_bash and item.get("name") == "Bash":
                tool_id = item.get("id")
                command = (item.get("input") or {}).get("command")
                if tool_id and isinstance(command, str) and "bd" in command:
                    pending[tool_id] = command
            elif itype == "tool_result" and has_result:
                tool_id = item.get("tool_use_id")
                command = pending.pop(tool_id, None) if tool_id else None
                if command is None:
                    continue
                is_error = bool(item.get("is_error"))
                text = _bd_result_text(item.get("content"))
                ts_raw = doc.get("timestamp")
                ts = None
                if isinstance(ts_raw, str):
                    try:
                        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        ts = None
                _bd_apply_command(events, seq_box, command, is_error, text, ts)


def _bd_scan_kimi_file(path: str, events: list[tuple], seq_box: list[int]) -> None:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > _BD_SCAN_TAIL_BYTES:
                f.seek(size - _BD_SCAN_TAIL_BYTES)
            data = f.read()
    except OSError:
        return
    pending: dict[str, str] = {}
    for raw in data.split(b"\n"):
        if not raw:
            continue
        has_bash = b'"Bash"' in raw
        has_tool_role = b'"role"' in raw and b'"tool"' in raw
        if not has_bash and not (has_tool_role and pending):
            continue
        try:
            doc = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(doc, dict) or doc.get("type") != "agent.message.appended":
            continue
        msg = (doc.get("message") or {}).get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        ts_ms = doc.get("time")
        ts = (ts_ms / 1000.0) if isinstance(ts_ms, int | float) else None
        if role == "assistant" and has_bash:
            for tc in msg.get("toolCalls") or []:
                if not isinstance(tc, dict) or tc.get("name") != "Bash":
                    continue
                tool_id = tc.get("id")
                args_raw = tc.get("arguments")
                args = None
                if isinstance(args_raw, str):
                    try:
                        args = json.loads(args_raw)
                    except ValueError:
                        args = None
                elif isinstance(args_raw, dict):
                    args = args_raw
                command = args.get("command") if isinstance(args, dict) else None
                if tool_id and isinstance(command, str) and "bd" in command:
                    pending[tool_id] = command
        elif role == "tool" and has_tool_role:
            tool_id = msg.get("toolCallId")
            command = pending.pop(tool_id, None) if tool_id else None
            if command is None:
                continue
            text = _bd_result_text(msg.get("content"))
            _bd_apply_command(events, seq_box, command, False, text, ts)


def resolve_remote_session_bead(kind: str, paths: list[str]) -> list[str]:
    """Stateless, full-tail-bounded unreleased-claims resolution for one
    LIVE session on this remote host. Returns ONLY bead ids -- the ordered
    (most-recent-claim-first) list this session has an unreleased claim on,
    capped at _BD_MAX_CLAIMS -- nothing else about the transcript (no
    command text, no raw output, no timestamp) ever leaves this function,
    and this module never writes anything but that id list into the
    probe's JSON output. The local server-side cross-check
    (agents.py's _apply_bead_cross_check) applies the same in_progress
    selection rule to this list that it applies to a local session's own
    (bead_id, ts) candidates."""
    if kind not in _BEAD_TRACKED_KINDS or not paths:
        return []
    events: list[tuple] = []
    seq_box = [0]
    scanner = _bd_scan_claude_file if kind == "claude" else _bd_scan_kimi_file
    for path in paths:
        scanner(path, events, seq_box)
    return [bid for bid, _ts in _bd_unreleased_claims(events)][:_BD_MAX_CLAIMS]


def _claude_projects_dir_from_glob(projects_glob: str) -> str:
    """projects_glob is always "<claude_projects_dir>/*/*.jsonl" (see
    RemoteCollector._probe_args, collectors/remote.py) -- strip that fixed
    two-segment glob suffix to recover the base directory a by-session-id
    transcript lookup needs (glob.glob("<dir>/*/<session_id>.jsonl")),
    expanding "~" the same way every other section of this file does."""
    expanded = os.path.expanduser(projects_glob)
    suffix = "/*/*.jsonl"
    if expanded.endswith(suffix):
        return expanded[: -len(suffix)]
    return os.path.dirname(os.path.dirname(expanded))


def find_claude_transcript_by_session_id(claude_projects_dir: str, session_id: str) -> list[str]:
    """Mirrors collectors/agents.py's _lookup_claude_transcript: a session's
    jsonl lives at <claude_projects_dir>/<project-dir>/<session-id>.jsonl,
    and `project-dir` is not derivable from a pane's cwd (it can be a
    dash-encoded path for a different, symlinked path than the cwd herdr
    reports), so this looks the file up by filename across every project dir
    instead. One readdir of claude_projects_dir plus one stat per project
    dir -- not a walk of any project dir's own contents."""
    if not claude_projects_dir:
        return []
    pattern = os.path.join(claude_projects_dir, "*", f"{session_id}.jsonl")
    return glob.glob(pattern)[:1]


def find_kimi_wire_paths_by_session_id(kimi_dir: str, session_id: str) -> list[str]:
    """Mirrors collectors/kimi.py's find_kimi_session_wire_paths (same
    session_index.jsonl -> state.json chain discover_kimi_sessions uses),
    scoped to one session id rather than reading every session's state.json.
    Duplicated, not imported -- see this file's module docstring."""
    for entry in load_session_index(kimi_dir):
        if entry["session_id"] != session_id:
            continue
        state = load_session_state(entry["session_dir"])
        if state is None:
            return []
        agents = state.get("agents") or {}
        return [
            os.path.join(info["homedir"], "wire.jsonl")
            for info in agents.values()
            if isinstance(info, dict) and info.get("homedir")
        ]
    return []


def backfill_herdr_only_bead_claims(
    agents: list[dict], claude_projects_dir: str, kimi_dir: str,
) -> None:
    """For every merged agent that came from herdr ALONE (no session/Kimi
    record merged onto it -- e.g. a pane herdr still lists as "done"/"idle"
    long after its transcript's mtime/updatedAt fell outside
    session_window_s), look its transcript up BY SESSION ID instead of by
    recent file activity, and resolve its unreleased bead claims from that.
    Mutates `agents` in place, adding a "bead_claims" key only when a
    transcript was actually found -- mirrors collectors/agents.py's
    _resolve_transcript_paths fallback, but this module has no cross-call
    cache to keep (a fresh `ssh host python3 -` interpreter starts, does
    this once, and exits -- see the module docstring's "stateless per
    call"), so there is nothing to cache here."""
    for agent in agents:
        if agent.get("source") != "herdr" or "bead_claims" in agent:
            continue
        kind = agent.get("kind")
        if kind not in _BEAD_TRACKED_KINDS:
            continue
        session_id = agent.get("session_id")
        if not session_id:
            continue
        if kind == "claude":
            paths = find_claude_transcript_by_session_id(claude_projects_dir, session_id)
        else:
            paths = find_kimi_wire_paths_by_session_id(kimi_dir, session_id)
        if paths:
            agent["bead_claims"] = resolve_remote_session_bead(kind, paths)


def tail_last_request(wire_path: str, tail_bytes: int = _KIMI_TAIL_READ_BYTES) -> tuple[str, int] | None:
    try:
        size = os.path.getsize(wire_path)
        with open(wire_path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
            chunk = f.read()
    except OSError:
        return None
    for raw_line in reversed(chunk.split(b"\n")):
        line = raw_line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(doc, dict) or doc.get("type") != "llm.request":
            continue
        model = doc.get("modelAlias") or doc.get("model")
        time_ms = doc.get("time")
        if model and isinstance(time_ms, int):
            return model, time_ms
    return None


def tail_last_model(wire_path: str, tail_bytes: int = _KIMI_TAIL_READ_BYTES) -> str | None:
    result = tail_last_request(wire_path, tail_bytes)
    return result[0] if result else None


def tail_last_model_across_agents(
    wire_paths: dict[str, str], tail_bytes: int = _KIMI_TAIL_READ_BYTES,
) -> str | None:
    """A session can have more than one live agent (main + subagents it
    dispatched) -- see collectors/kimi.py's docstring on this same function,
    verified live on the maintainer's dev host 2026-09-18. Every agent's wire.jsonl is
    tail-read; the model from whichever has the most recent llm.request
    `time` wins, not just whichever file is listed first."""
    best_model: str | None = None
    best_time = -1
    for wire_path in wire_paths.values():
        result = tail_last_request(wire_path, tail_bytes)
        if result is None:
            continue
        model, time_ms = result
        if time_ms > best_time:
            best_time = time_ms
            best_model = model
    return best_model


def classify_kimi_error(message: str | None, code: str | None) -> str:
    return classify_error(message or code or "")


def build_kimi_session_agent(rec: dict, worktrees: list[dict], now: datetime, window_s: float) -> dict | None:
    """None if the session is outside the active window -- mirrors
    scan_session_agents' age filter, done here instead since Kimi session
    discovery isn't file-mtime-driven (state.json's own updatedAt is the
    liveness signal, per the briefing)."""
    updated_ms = rec.get("updated_at_ms")
    if updated_ms is None:
        return None
    updated_dt = datetime.fromtimestamp(updated_ms / 1000.0, tz=UTC)
    age_s = (now - updated_dt).total_seconds()
    if age_s < 0 or age_s > window_s:
        return None
    session = rec["session_id"]
    cwd = rec.get("cwd")
    wt = best_worktree_match(cwd, worktrees) if cwd else None
    repo = wt.get("repo") if wt else None
    branch = wt.get("branch") if wt else None
    model = tail_last_model_across_agents(rec["agent_wire_paths"])
    updated_iso = epoch_ms_to_iso(updated_ms)
    status = "working" if age_s <= SESSION_WORKING_THRESHOLD_S else "idle"
    return {
        "id": session,
        "kind": "kimi",
        "status": status,
        "cwd": cwd,
        "repo": repo,
        "branch": branch,
        "pane": None,
        "workspace": None,
        "title": None,
        "label": repo or (os.path.basename(cwd.rstrip("/")) if cwd else None) or "kimi",
        "focused": False,
        "session_id": session,
        # ordered (most-recent-first) unreleased-claim ids -- the local
        # cross-check (agents.py's _apply_bead_cross_check) resolves this
        # down to a single "bead", same as it does for local sessions.
        "bead_claims": resolve_remote_session_bead("kimi", list(rec["agent_wire_paths"].values())),
        "bead_tracked": True,
        "last_activity": updated_iso,
        "status_since": None,
        "tokens_today": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 0},
        # null, not 0.0 -- Kimi bills by subscription quota, see module docstring.
        "cost_today_usd": None,
        "msg_count_today": 0,
        "subagents_active": max(0, len(rec["agent_wire_paths"]) - 1),
        "model": model,
        "source": "session",
    }


def aggregate_kimi_wire_file(path: str) -> dict:
    """One-pass full parse of one Kimi wire.jsonl into day-granularity
    buckets -- mirrors aggregate_file's per-file caching contract (an
    unchanged file is skipped entirely by the caller; a changed file is
    re-read in full here, never byte-offset incremental, since dedup across
    partial re-reads would need per-line identity Kimi's schema doesn't
    reliably provide -- see collectors/kimi.py's docstring on the two
    token_counting.* record variants)."""
    usage: dict[tuple[str, str], dict] = {}
    errors: dict[tuple[str, str], dict] = {}
    model = None
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return {"usage": {}, "errors": {}}
    for raw_line in data.split(b"\n"):
        line = raw_line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(doc, dict):
            continue
        rtype = doc.get("type") or ""
        if rtype == "llm.request":
            m = doc.get("modelAlias") or doc.get("model")
            if m:
                model = m
            continue
        if rtype.startswith("token_counting."):
            ts = epoch_ms_to_iso(doc.get("time"))
            if ts is None:
                continue
            key = (ts[:10], model or "unknown")
            b = usage.setdefault(key, {"tokens": 0, "turns": 0})
            b["tokens"] += doc.get("tokens") or 0
            b["turns"] += 1
            continue
        if rtype == "turn.ended" and doc.get("reason") == "failed":
            ts = epoch_ms_to_iso(doc.get("time"))
            if ts is None:
                continue
            error = doc.get("error") or {}
            kind = classify_kimi_error(error.get("message"), error.get("code"))
            key = (ts[:10], kind)
            eb = errors.setdefault(key, {"count": 0, "last_seen": ts})
            eb["count"] += 1
            if ts > eb["last_seen"]:
                eb["last_seen"] = ts
    return {
        "usage": {f"{d}{_SEP}{m}": v for (d, m), v in usage.items()},
        "errors": {f"{d}{_SEP}{k}": v for (d, k), v in errors.items()},
    }


def collect_kimi(
    kimi_dir: str, worktrees: list[dict], window_s: float = 900.0, state_file: str | None = None,
) -> dict:
    """Returns {"agents": [...], "kimi_usage_buckets": [...],
    "kimi_error_buckets": [...]}. A missing kimi_dir (the host has no Kimi
    install) is a silent no-op -- empty lists, never an error, per the
    briefing's explicit requirement."""
    if not os.path.isdir(kimi_dir):
        return {"agents": [], "kimi_usage_buckets": [], "kimi_error_buckets": []}

    state_file = state_file or default_state_path()
    state = load_state(state_file)
    old_kimi_files = state.get("kimi_files", {})

    sessions = discover_kimi_sessions(kimi_dir)
    now = datetime.now(UTC)

    agents = []
    new_kimi_files: dict[str, dict] = {}
    usage_merged: dict[tuple[str, str], dict] = {}
    error_merged: dict[tuple[str, str], dict] = {}

    for rec in sessions:
        agent = build_kimi_session_agent(rec, worktrees, now, window_s)
        if agent is not None:
            agents.append(agent)

        for wire_path in rec["agent_wire_paths"].values():
            try:
                st = os.stat(wire_path)
            except OSError:
                continue
            inode, size, mtime = st.st_ino, st.st_size, st.st_mtime
            cached = old_kimi_files.get(wire_path)
            unchanged = (
                cached and cached.get("inode") == inode
                and cached.get("size") == size and cached.get("mtime") == mtime
            )
            buckets = cached if unchanged else {
                "inode": inode, "size": size, "mtime": mtime, **aggregate_kimi_wire_file(wire_path),
            }
            new_kimi_files[wire_path] = buckets

            for k, v in (buckets.get("usage") or {}).items():
                day, bmodel = k.split(_SEP)
                agg = usage_merged.setdefault((day, bmodel), {"tokens": 0, "turns": 0})
                agg["tokens"] += v["tokens"]
                agg["turns"] += v["turns"]
            for k, v in (buckets.get("errors") or {}).items():
                day, kind = k.split(_SEP)
                agg = error_merged.setdefault((day, kind), {"count": 0, "last_seen": v["last_seen"]})
                agg["count"] += v["count"]
                if v["last_seen"] > agg["last_seen"]:
                    agg["last_seen"] = v["last_seen"]

    # Same merge-onto-current-state discipline as collect_usage_and_analytics
    # above -- this shares one state file with it via a disjoint top-level
    # key ("kimi_files" vs "files").
    save_state(state_file, {**load_state(state_file), "kimi_files": new_kimi_files})

    return {
        "agents": agents,
        "kimi_usage_buckets": [
            {"day": day, "model": model, **totals} for (day, model), totals in usage_merged.items()
        ],
        "kimi_error_buckets": [
            {"day": day, "kind": kind, **totals} for (day, kind), totals in error_merged.items()
        ],
    }


# -- worktrees (mirrors collectors/worktrees.py) -------------------------------

DEFAULT_PRUNE_DIRS = frozenset({
    "node_modules", ".venv", "venv", "vendor", "site-packages", ".cache",
    ".pub-cache", "build", "dist", "target", ".tox", ".gradle",
})
_DEFAULT_MAX_DEPTH = 4


def find_git_dirs(root: str, max_depth: int = _DEFAULT_MAX_DEPTH, prune_dirs=None) -> list[str]:
    prune = set(prune_dirs) if prune_dirs is not None else set(DEFAULT_PRUNE_DIRS)
    found: list[str] = []
    if not os.path.isdir(root):
        return found
    root_depth = root.rstrip("/").count("/")
    for dirpath, dirnames, _filenames in os.walk(root):
        depth = dirpath.rstrip("/").count("/") - root_depth
        is_repo = ".git" in dirnames or os.path.isfile(os.path.join(dirpath, ".git"))
        if is_repo:
            found.append(dirpath)
            dirnames[:] = []
            continue
        if depth >= max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in prune and not d.startswith(".")]
    return found


def _git(dir_: str, args: list[str], timeout: float) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", dir_, "--no-optional-locks", *args],
            capture_output=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode(errors="replace").rstrip("\n")


def _parse_status_v2(porcelain: str):
    dirty = untracked = staged = ahead = behind = 0
    branch = upstream = None
    if not porcelain:
        return dirty, untracked, staged, branch, upstream, ahead, behind
    for line in porcelain.splitlines():
        if not line:
            continue
        if line.startswith("# branch.head "):
            head_field = line[len("# branch.head "):]
            branch = "HEAD" if head_field == "(detached)" else head_field
            continue
        if line.startswith("# branch.upstream "):
            upstream = line[len("# branch.upstream "):]
            continue
        if line.startswith("# branch.ab "):
            for part in line[len("# branch.ab "):].split():
                if part.startswith("+"):
                    ahead = int(part[1:])
                elif part.startswith("-"):
                    behind = int(part[1:])
            continue
        if line.startswith("#"):
            continue
        if line.startswith("? "):
            untracked += 1
            continue
        if line.startswith("! "):
            continue
        if len(line) < 4:
            continue
        x, y = line[2], line[3]
        if x != ".":
            staged += 1
        if y != ".":
            dirty += 1
    return dirty, untracked, staged, branch, upstream, ahead, behind


def collect_one_repo(dir_: str, root: str, timeout: float) -> dict | None:
    status_raw = _git(dir_, ["status", "--porcelain=v2", "--branch"], timeout)
    if status_raw is None:
        return None
    dirty, untracked, staged, branch, upstream, ahead, behind = _parse_status_v2(status_raw)

    log = _git(dir_, ["log", "-1", "--format=%h\x1f%H\x1f%ct\x1f%an\x1f%s"], timeout)
    head = last_commit_at = last_commit_msg = last_commit_author = None
    stale_days = None
    if log:
        parts = log.split("\x1f")
        if len(parts) == 5:
            head, _sha, epoch_s, last_commit_author, last_commit_msg = parts
            try:
                dt = datetime.fromtimestamp(int(epoch_s), tz=UTC)
                last_commit_at = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                stale_days = (datetime.now(UTC) - dt).days
            except ValueError:
                pass

    if branch is None:
        branch = head or "HEAD"

    return {
        "path": dir_,
        "repo": os.path.basename(dir_.rstrip("/")),
        "root": root,
        "branch": branch,
        "head": head,
        "dirty": dirty,
        "untracked": untracked,
        "staged": staged,
        "ahead": ahead,
        "behind": behind,
        "upstream": upstream,
        "last_commit_at": last_commit_at,
        "last_commit_msg": last_commit_msg,
        "last_commit_author": last_commit_author,
        "agents": [],
        "stale_days": stale_days,
    }


def collect_worktrees(
    repo_roots: list[str], worker_pool: int = 16, git_timeout_s: float = 5.0,
    max_depth: int = _DEFAULT_MAX_DEPTH, prune_dirs=None,
) -> list[dict]:
    seen: dict[str, str] = {}
    for root in sorted(repo_roots, key=len, reverse=True):
        for d in find_git_dirs(root, max_depth=max_depth, prune_dirs=prune_dirs):
            seen.setdefault(d, root)
    dirs = list(seen.items())
    if not dirs:
        return []
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, worker_pool)) as ex:
        futs = [ex.submit(collect_one_repo, d, r, git_timeout_s) for d, r in dirs]
        for fut in futs:
            r = fut.result()
            if r is not None:
                results.append(r)
    return results


# -- agents (mirrors collectors/agents.py) -------------------------------------

# Same recency split as collectors/agents.py's SESSION_WORKING_THRESHOLD_S --
# duplicated, not imported, for the same "must survive being copied alone to
# a host with no critdash package installed" reason as the rest of this file.
SESSION_WORKING_THRESHOLD_S = 120.0
_SESSION_TAIL_READ_BYTES = 65536


def _read_last_session_record(path: str, tail_bytes: int = _SESSION_TAIL_READ_BYTES) -> dict | None:
    """Mirrors collectors/agents.py's _read_last_session_record: scans the
    tail backward and picks the freshest non-null value for
    sessionId/cwd/gitBranch/message.model independently -- the most recent
    lines are often housekeeping events (e.g. "system", "queue-operation")
    that carry the sessionId but an explicit null cwd/gitBranch and no model
    at all. Returns None if no line in the tail carries a sessionId."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
            chunk = f.read()
    except OSError:
        return None
    session_id = cwd = git_branch = model = None
    for raw_line in reversed(chunk.split(b"\n")):
        line = raw_line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(doc, dict):
            continue
        if session_id is None and doc.get("sessionId"):
            session_id = doc["sessionId"]
        if cwd is None and doc.get("cwd"):
            cwd = doc["cwd"]
        if git_branch is None and doc.get("gitBranch"):
            git_branch = doc["gitBranch"]
        if model is None:
            m = (doc.get("message") or {}).get("model")
            if m and m != "<synthetic>":
                model = m
        if session_id is not None and cwd is not None and git_branch is not None and model is not None:
            break
    if session_id is None:
        return None
    return {"session_id": session_id, "cwd": cwd, "git_branch": git_branch, "model": model}


def scan_session_agents(
    projects_glob: str, window_s: float, working_threshold_s: float = SESSION_WORKING_THRESHOLD_S,
) -> list[dict]:
    """Mirrors collectors/agents.py's scan_session_agents: a session whose
    jsonl was written within `window_s` is an active agent on this host, even
    when it was started outside herdr (plain tmux, a cron job, a manual
    shell) and is therefore invisible to collect_agents() below -- the Bug 2
    fix, extended fleet-wide."""
    now = datetime.now(UTC)
    pattern = os.path.expanduser(projects_glob)
    out: list[dict] = []
    for path in glob.glob(pattern):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        mtime_dt = datetime.fromtimestamp(mtime, tz=UTC)
        age_s = (now - mtime_dt).total_seconds()
        if age_s < 0 or age_s > window_s:
            continue
        rec = _read_last_session_record(path)
        if rec is None:
            continue
        out.append({
            "session_id": rec["session_id"],
            "cwd": rec["cwd"],
            "git_branch": rec["git_branch"],
            "model": rec["model"],
            "mtime": mtime_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "working" if age_s <= working_threshold_s else "idle",
            "path": path,
        })
    return out


def build_session_agent(rec: dict, worktrees: list[dict]) -> dict:
    """Mirrors collectors/agents.py's _build_session_agent: this record was
    built by scanning a Claude Code session log under claude_projects_dir, so
    `kind` is known -- "claude" -- even without herdr. merge_remote_agent_sources
    overwrites this with herdr's kind when herdr also reports the session,
    since herdr stays authoritative there."""
    session = rec["session_id"]
    cwd = rec.get("cwd")
    wt = best_worktree_match(cwd, worktrees) if cwd else None
    repo = wt.get("repo") if wt else None
    branch = rec.get("git_branch") or (wt.get("branch") if wt else None)
    return {
        "id": session,
        "kind": "claude",
        "status": rec["status"],
        "cwd": cwd,
        "repo": repo,
        "branch": branch,
        "pane": None,
        "workspace": None,
        "title": None,
        "label": repo or (os.path.basename(cwd.rstrip("/")) if cwd else None) or "claude",
        "focused": False,
        "session_id": session,
        # ordered (most-recent-first) unreleased-claim ids -- see
        # build_kimi_session_agent's identical field for the rationale.
        "bead_claims": resolve_remote_session_bead("claude", [rec["path"]] if rec.get("path") else []),
        "bead_tracked": True,
        "last_activity": rec["mtime"],
        "status_since": None,
        "tokens_today": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 0},
        "cost_today_usd": 0.0,
        "msg_count_today": 0,
        "subagents_active": 0,
        "model": rec.get("model"),
        "source": "session",
    }


def merge_remote_agent_sources(herdr_agents: list[dict], session_agents: list[dict]) -> list[dict]:
    """Merge by session id -- mirrors collectors/agents.py's
    merge_agent_sources. A session known to both sources produces ONE entry
    (source="both"), with herdr's pane/workspace/title/status/focused/kind
    enriching the session-derived base (cwd/branch/model come from the
    jsonl)."""
    session_by_id = {a["session_id"]: a for a in session_agents if a.get("session_id")}
    consumed: set[str] = set()
    merged: list[dict] = []

    for h in herdr_agents:
        sid = h.get("session_id")
        if sid and sid in session_by_id:
            base = dict(session_by_id[sid])
            base.update({
                "pane": h["pane"], "workspace": h["workspace"], "title": h["title"],
                "status": h["status"], "focused": h["focused"], "kind": h["kind"],
                "source": "both",
            })
            base["label"] = base["repo"] or h["title"] or h["kind"] or base["label"]
            merged.append(base)
            consumed.add(sid)
        else:
            merged.append(h)

    for sid, base in session_by_id.items():
        if sid not in consumed:
            merged.append(base)

    return merged


def find_herdr(herdr_bin: str) -> str | None:
    # "" (explicit "this host has no herdr", shipped by RemoteCollector for a
    # sources.json herdr_bin: null override) must short-circuit to "no herdr"
    # -- never a PATH search, which could accidentally resolve to an
    # unrelated binary on a host that was deliberately configured to have
    # none. A caller that truly wants the PATH-search default passes "herdr"
    # explicitly (see main()'s argparse default).
    if not herdr_bin:
        return None
    if os.path.sep in herdr_bin:
        return herdr_bin if os.path.isfile(herdr_bin) and os.access(herdr_bin, os.X_OK) else None
    return shutil.which(herdr_bin)


def parse_herdr_output(text: str) -> list[dict]:
    line = text.strip().splitlines()[-1] if text.strip() else ""
    if not line:
        return []
    try:
        doc = json.loads(line)
    except json.JSONDecodeError:
        return []
    return doc.get("result", {}).get("agents", [])


def best_worktree_match(cwd: str, worktrees: list[dict]) -> dict | None:
    best = None
    best_len = -1
    for wt in worktrees:
        path = wt.get("path") or ""
        if cwd == path or cwd.startswith(path.rstrip("/") + "/"):
            if len(path) > best_len:
                best = wt
                best_len = len(path)
    return best


def collect_agents(herdr_bin: str, worktrees: list[dict]) -> list[dict]:
    resolved = find_herdr(herdr_bin)
    if not resolved:
        return []
    try:
        proc = subprocess.run([resolved, "agent", "list"], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    raw_agents = parse_herdr_output(proc.stdout.decode(errors="replace"))

    agents = []
    for raw in raw_agents:
        session = (raw.get("agent_session") or {}).get("value")
        cwd = raw.get("cwd") or raw.get("foreground_cwd")
        status_raw = raw.get("agent_status")
        status = status_raw if status_raw in ("idle", "working", "done") else "unknown"
        wt = best_worktree_match(cwd, worktrees) if cwd else None
        repo = wt.get("repo") if wt else None
        title = raw.get("terminal_title_stripped") or raw.get("terminal_title")
        kind = raw.get("agent")
        agents.append({
            "id": session,
            "kind": kind,
            "status": status,
            "cwd": cwd,
            "repo": repo,
            "branch": wt.get("branch") if wt else None,
            "pane": raw.get("pane_id"),
            "workspace": raw.get("workspace_id"),
            "title": title,
            "label": repo or title or kind,
            "focused": bool(raw.get("focused")),
            "session_id": session,
            # herdr's own output has no transcript path for this session --
            # merge_remote_agent_sources fills bead in from the session-
            # derived record when one also exists for this session id.
            "bead": None,
            "bead_tracked": kind in _BEAD_TRACKED_KINDS,
            "last_activity": None,
            "status_since": None,
            # not computed remotely -- see module docstring
            "tokens_today": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 0},
            "cost_today_usd": 0.0,
            "msg_count_today": 0,
            "subagents_active": 0,
            "model": None,
            "source": "herdr",
        })
    return agents


def join_agents_to_worktrees(worktrees: list[dict], agents: list[dict]) -> None:
    for wt in worktrees:
        matched = []
        for agent in agents:
            cwd = agent.get("cwd") or ""
            if cwd == wt["path"] or cwd.startswith(wt["path"].rstrip("/") + "/"):
                matched.append((len(wt["path"]), agent.get("id")))
        wt["agents"] = [aid for _len, aid in matched if aid]


# -- system (mirrors collectors/system.py -- cross-platform: Linux + macOS.
# See that module's docstring for what's verified on Linux vs
# implemented-but-unverified on macOS; the logic is duplicated here rather
# than imported for the same reason every other section of this file
# duplicates the local collectors' parsing rules -- this script must survive
# being copied alone to a host with no critdash package installed.) --------

_GB = 1024 ** 3
_SUPPORTED_SYSTEM_PLATFORMS = ("linux", "darwin")

_SYSCTL_MEMSIZE_RE = re.compile(r"(\d+)")
_VM_STAT_PAGE_SIZE_RE = re.compile(r"page size of (\d+) bytes")
_VM_STAT_LINE_RE = re.compile(r'^"?([^":]+)"?:\s*([\d,]+)\.?\s*$')


def _read_loadavg():
    try:
        return os.getloadavg()
    except OSError:
        return 0.0, 0.0, 0.0


def _read_meminfo_linux():
    total_kb = avail_kb = 0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total_kb = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    avail_kb = int(line.split()[1])
    except OSError:
        pass
    total_gb = total_kb / 1024 / 1024
    used_gb = (total_kb - avail_kb) / 1024 / 1024
    return used_gb, total_gb


def _parse_sysctl_memsize(output: str):
    m = _SYSCTL_MEMSIZE_RE.search(output)
    return int(m.group(1)) if m else None


def _parse_vm_stat(output: str):
    page_size_m = _VM_STAT_PAGE_SIZE_RE.search(output)
    page_size = int(page_size_m.group(1)) if page_size_m else 4096
    pages: dict[str, int] = {}
    for raw_line in output.splitlines():
        m = _VM_STAT_LINE_RE.match(raw_line.strip())
        if not m:
            continue
        key = m.group(1).strip().lower()
        try:
            pages[key] = int(m.group(2).replace(",", ""))
        except ValueError:
            continue
    return pages, page_size


def _macos_mem_used_gb(vm_stat_output: str):
    pages, page_size = _parse_vm_stat(vm_stat_output)
    try:
        used_pages = (
            pages["pages active"] + pages["pages wired down"]
            + pages.get("pages occupied by compressor", 0)
        )
    except KeyError:
        return None
    return used_pages * page_size / _GB


def _run_command(args, timeout: float = 5.0):
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _read_meminfo():
    """(used_gb, total_gb), both possibly None -- see collectors/system.py's
    read_meminfo docstring: total-only (used=None) when used memory can't be
    determined reliably, both None on a platform this has no support for."""
    plat = sys.platform
    if plat == "darwin":
        sysctl_out = _run_command(["sysctl", "hw.memsize"])
        total_bytes = _parse_sysctl_memsize(sysctl_out) if sysctl_out else None
        total_gb = total_bytes / _GB if total_bytes is not None else None
        vm_stat_out = _run_command(["vm_stat"])
        used_gb = _macos_mem_used_gb(vm_stat_out) if vm_stat_out else None
        return used_gb, total_gb
    if plat == "linux":
        return _read_meminfo_linux()
    return None, None


def _disk(mount: str) -> dict | None:
    if not os.path.exists(mount):
        return None
    usage = shutil.disk_usage(mount)
    used_gb = usage.used / _GB
    total_gb = usage.total / _GB
    pct = round((usage.used / usage.total) * 100, 1) if usage.total else 0.0
    return {"mount": mount, "used_gb": round(used_gb, 1), "total_gb": round(total_gb, 1), "pct": pct}


def collect_system(disk_mounts: list[str]) -> dict:
    if sys.platform not in _SUPPORTED_SYSTEM_PLATFORMS:
        return {}
    load1, load5, load15 = _read_loadavg()
    mem_used_gb, mem_total_gb = _read_meminfo()
    disks = [d for d in (_disk(m) for m in disk_mounts) if d is not None]
    return {
        "load1": load1, "load5": load5, "load15": load15,
        "cpu_count": os.cpu_count() or 0,
        "mem_used_gb": round(mem_used_gb, 1) if mem_used_gb is not None else None,
        "mem_total_gb": round(mem_total_gb, 1) if mem_total_gb is not None else None,
        "disks": disks,
    }


# -- productivity (mirrors collectors/productivity.py) -------------------------

_PRODUCTIVITY_SEP = "\x1f"
_PRODUCTIVITY_LOG_FORMAT = f"COMMIT{_PRODUCTIVITY_SEP}%H{_PRODUCTIVITY_SEP}%ct{_PRODUCTIVITY_SEP}%an"


def _git_log_numstat(path: str, since: str, timeout: float) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", path, "--no-optional-locks", "log",
             f"--since={since}", "--numstat", f"--format={_PRODUCTIVITY_LOG_FORMAT}"],
            capture_output=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode(errors="replace")


def parse_log_numstat(output: str) -> list[dict]:
    commits: list[dict] = []
    current: dict | None = None
    for line in output.splitlines():
        if line.startswith("COMMIT" + _PRODUCTIVITY_SEP):
            parts = line.split(_PRODUCTIVITY_SEP)
            if len(parts) != 4:
                continue
            _, sha, epoch_s, author = parts
            try:
                epoch = int(epoch_s)
            except ValueError:
                continue
            current = {"sha": sha, "epoch_s": epoch, "author": author,
                       "lines_added": 0, "lines_removed": 0, "files_changed": 0}
            commits.append(current)
            continue
        if current is None or "\t" not in line:
            continue
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        added, removed, _path = fields[0], fields[1], fields[2]
        current["files_changed"] += 1
        if added.isdigit():
            current["lines_added"] += int(added)
        if removed.isdigit():
            current["lines_removed"] += int(removed)
    return commits


def collect_productivity(
    worktrees: list[dict], worker_pool: int = 16, git_timeout_s: float = 15.0,
    since: str = "30 days ago",
) -> list[dict]:
    """Per-(real commit day, repo) commit/line-churn aggregates -- bucketed by
    each commit's OWN date (%ct), never by "today", so this is a true
    per-day aggregate like remote_usage_buckets' per-hour buckets: safe to
    upsert with replace semantics and sum across days without double
    counting. (A rolling-window total re-stated under "today"'s bucket every
    probe call would double count once more than one day's snapshot exists
    in the table -- the bug this avoids.) `since` bounds the git log query;
    the caller (RemoteCollector -> analytics/productivity rollup) only ever
    reads the last 7 days of these buckets, so shipping up to 30 days of
    history here is headroom, not overreach."""
    if not worktrees:
        return []
    with ThreadPoolExecutor(max_workers=max(1, worker_pool)) as ex:
        futs = {ex.submit(_git_log_numstat, wt["path"], since, git_timeout_s): wt for wt in worktrees}
        outputs = [(futs[fut], fut.result()) for fut in futs]

    agg: dict[tuple[str, str], dict] = {}
    for wt, output in outputs:
        if not output:
            continue
        repo = wt.get("repo") or os.path.basename(wt["path"].rstrip("/"))
        for c in parse_log_numstat(output):
            day = datetime.fromtimestamp(c["epoch_s"], tz=UTC).strftime("%Y-%m-%d")
            default = {"commits": 0, "lines_added": 0, "lines_removed": 0, "files_changed": 0}
            a = agg.setdefault((day, repo), default)
            a["commits"] += 1
            a["lines_added"] += c["lines_added"]
            a["lines_removed"] += c["lines_removed"]
            a["files_changed"] += c["files_changed"]

    return [{"day": day, "repo": repo, **totals} for (day, repo), totals in agg.items()]


# -- entrypoint -----------------------------------------------------------------


_EMPTY_ANALYTICS_BUCKETS = {
    "usage_buckets": [], "tool_buckets": [], "error_buckets": [], "error_examples": [],
    "trouble_file_buckets": [], "api_error_buckets": [], "session_buckets": [],
}
_EMPTY_KIMI_BUCKETS = {"kimi_usage_buckets": [], "kimi_error_buckets": []}


def build_result(
    host_name: str, projects_glob: str, repo_roots: list[str], disk_mounts: list[str],
    herdr_bin: str, worker_pool: int, git_timeout_s: float, max_depth: int,
    state_file: str | None, productivity_since: str = "30 days ago",
    session_window_s: float = 900.0, kimi_dir: str = "~/.kimi-code",
) -> dict:
    worktrees: list[dict] = []
    agents: list[dict] = []
    system: dict = {}
    analytics_buckets = dict(_EMPTY_ANALYTICS_BUCKETS)
    productivity_buckets: list[dict] = []
    kimi_buckets = dict(_EMPTY_KIMI_BUCKETS)

    try:
        worktrees = collect_worktrees(repo_roots, worker_pool, git_timeout_s, max_depth)
    except Exception:  # noqa: BLE001 -- one broken section must not blank the others
        worktrees = []

    kimi_agents: list[dict] = []
    try:
        kimi_result = collect_kimi(os.path.expanduser(kimi_dir), worktrees, session_window_s, state_file)
        kimi_agents = kimi_result.pop("agents")
        kimi_buckets = kimi_result
    except Exception:  # noqa: BLE001
        kimi_agents = []
        kimi_buckets = dict(_EMPTY_KIMI_BUCKETS)

    try:
        herdr_agents = collect_agents(herdr_bin, worktrees)
        session_scanned = scan_session_agents(projects_glob, session_window_s)
        session_agents = [build_session_agent(rec, worktrees) for rec in session_scanned]
        # Kimi agents merge into the SAME pass as the Claude session records
        # -- a Kimi session herdr also happens to report (point A of the
        # briefing) gets folded onto its session id exactly like a Claude one
        # does, no duplicate entry.
        agents = merge_remote_agent_sources(herdr_agents, session_agents + kimi_agents)
        # The pane-went-quiet gap (see backfill_herdr_only_bead_claims'
        # docstring): a herdr-only entry has no session record, so it has no
        # bead_claims from the merge above -- back-fill those by looking its
        # transcript up by session id instead of by recent activity.
        backfill_herdr_only_bead_claims(
            agents, _claude_projects_dir_from_glob(projects_glob), os.path.expanduser(kimi_dir),
        )
        join_agents_to_worktrees(worktrees, agents)
    except Exception:  # noqa: BLE001
        agents = []

    for wt in worktrees:
        wt.setdefault("agents", [])

    try:
        system = collect_system(disk_mounts)
    except Exception:  # noqa: BLE001
        system = {}

    try:
        analytics_buckets = collect_usage_and_analytics(projects_glob, state_file)
    except Exception:  # noqa: BLE001
        analytics_buckets = dict(_EMPTY_ANALYTICS_BUCKETS)

    try:
        productivity_buckets = collect_productivity(worktrees, worker_pool, git_timeout_s, productivity_since)
    except Exception:  # noqa: BLE001
        productivity_buckets = []

    return {
        "host": host_name,
        "generated_at": now_iso(),
        "agents": agents,
        "worktrees": worktrees,
        "system": system,
        "productivity_buckets": productivity_buckets,
        **analytics_buckets,
        **kimi_buckets,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="critdash-remote-probe")
    p.add_argument("host")
    p.add_argument("--projects-glob", default="~/.claude/projects/*/*.jsonl")
    p.add_argument("--repo-root", action="append", default=None)
    p.add_argument("--disk-mount", action="append", default=None)
    p.add_argument("--herdr-bin", default="herdr")
    p.add_argument("--worker-pool", type=int, default=16)
    p.add_argument("--git-timeout", type=float, default=5.0)
    p.add_argument("--max-depth", type=int, default=4)
    p.add_argument("--state-file", default=None)
    p.add_argument("--productivity-since", default="30 days ago")
    p.add_argument("--session-window", type=float, default=900.0)
    p.add_argument("--kimi-dir", default="~/.kimi-code")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    repo_roots = args.repo_root or ["~/repos", "~/src", "~/work"]
    repo_roots = [os.path.expanduser(r) for r in repo_roots]
    disk_mounts = args.disk_mount or ["/"]

    try:
        result = build_result(
            args.host, args.projects_glob, repo_roots, disk_mounts,
            args.herdr_bin, args.worker_pool, args.git_timeout, args.max_depth,
            args.state_file, args.productivity_since, args.session_window, args.kimi_dir,
        )
    except Exception as exc:  # noqa: BLE001 -- always print valid JSON, never a traceback
        result = {
            "host": args.host, "generated_at": now_iso(),
            "agents": [], "worktrees": [], "system": {}, "productivity_buckets": [],
            **_EMPTY_ANALYTICS_BUCKETS,
            **_EMPTY_KIMI_BUCKETS,
            "error": f"{type(exc).__name__}: {exc}",
        }

    sys.stdout.write(json.dumps(result, separators=(",", ":")))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
