"""SQLite history store: usage events, file offsets, agent/bead status history, events feed."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path

from .tzutil import safe_zoneinfo

# Widened lookback used whenever a day-bucketed query needs to be re-bucketed
# by a non-UTC local calendar day (see fold_hour_rows_to_local_day below): no
# IANA timezone offset exceeds +-14h, so a flat 24h widen always pulls in
# every hour that could shift into the requested local-day range. The caller
# discards any bucket outside what it actually asked for (a bucket_set filter
# in history.py, or zero_fill_daily's own dense-key lookup), so over-fetching
# here is free -- it can never leak an extra day into a response.
_TZ_WIDEN_HOURS = 24


def _widen_since_hour(since_iso: str, hours: int = _TZ_WIDEN_HOURS) -> str:
    dt = datetime.fromisoformat(since_iso.replace("Z", "+00:00")) - timedelta(hours=hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fold_hour_rows_to_local_day(
    rows: Iterable[sqlite3.Row], tz_name: str, key_cols: tuple[str, ...] = ()
) -> list[dict]:
    """Re-buckets hour-granularity rows (each with a 'bucket' column of the
    form 'YYYY-MM-DDTHH:00:00Z') into local-calendar-day buckets for
    `tz_name`, summing every other numeric column. `key_cols` (e.g.
    ('host', 'model')) are carried through and included in the fold key, for
    grouped queries -- pass () for a flat (ungrouped) timeline. This is what
    lets day-bucketed history honor a non-UTC timezone: SQLite has no IANA
    timezone database, so the day boundary is computed in Python instead of
    SQL, over rows already fetched at hour granularity (which needs no
    timezone -- an hour boundary is the same instant everywhere)."""
    tz = safe_zoneinfo(tz_name)
    acc: dict[tuple, dict] = {}
    for r in rows:
        dt = datetime.fromisoformat(r["bucket"].replace("Z", "+00:00"))
        day_key = dt.astimezone(tz).date().isoformat()
        fold_key = (day_key, *(r[c] for c in key_cols))
        entry = acc.get(fold_key)
        if entry is None:
            entry = {"bucket": day_key, **{c: r[c] for c in key_cols}}
            for col in r.keys():
                if col in ("bucket", *key_cols):
                    continue
                entry[col] = 0.0 if isinstance(r[col], float) else 0
            acc[fold_key] = entry
        for col in r.keys():
            if col in ("bucket", *key_cols):
                continue
            entry[col] += r[col]
    return list(acc.values())

# The Anthropic rate-limit block duration. A block starts at some message and
# ends exactly this long after -- see current_usage_block_start() below.
BLOCK_DURATION = timedelta(hours=5)

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    session_id TEXT,
    project TEXT,
    project_path TEXT,
    model TEXT,
    input INTEGER NOT NULL DEFAULT 0,
    output INTEGER NOT NULL DEFAULT 0,
    cache_read INTEGER NOT NULL DEFAULT 0,
    cache_write_5m INTEGER NOT NULL DEFAULT 0,
    cache_write_1h INTEGER NOT NULL DEFAULT 0,
    speed TEXT,
    is_sidechain INTEGER NOT NULL DEFAULT 0,
    web_searches INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0.0,
    host TEXT NOT NULL DEFAULT 'localhost',
    UNIQUE(host, message_id)
);
CREATE INDEX IF NOT EXISTS idx_usage_events_ts ON usage_events(ts);
CREATE INDEX IF NOT EXISTS idx_usage_events_session ON usage_events(session_id);
CREATE INDEX IF NOT EXISTS idx_usage_events_project ON usage_events(project);

-- Pre-aggregated (host, hour, model, project) token buckets shipped by the
-- remote probe. No cost_usd here on purpose: pricing is applied centrally on
-- the local host from config/pricing.json at query time (see collectors/usage.py),
-- so a single rate correction fixes every host's historical numbers at once
-- instead of requiring a re-cost pass per remote host.
CREATE TABLE IF NOT EXISTS remote_usage_buckets (
    host TEXT NOT NULL,
    hour TEXT NOT NULL,
    model TEXT NOT NULL,
    project TEXT NOT NULL,
    input INTEGER NOT NULL DEFAULT 0,
    output INTEGER NOT NULL DEFAULT 0,
    cache_read INTEGER NOT NULL DEFAULT 0,
    cache_write_5m INTEGER NOT NULL DEFAULT 0,
    cache_write_1h INTEGER NOT NULL DEFAULT 0,
    messages INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (host, hour, model, project)
);
CREATE INDEX IF NOT EXISTS idx_remote_usage_buckets_hour ON remote_usage_buckets(hour);

CREATE TABLE IF NOT EXISTS file_offsets (
    path TEXT PRIMARY KEY,
    inode INTEGER,
    offset INTEGER NOT NULL DEFAULT 0,
    mtime REAL
);

CREATE TABLE IF NOT EXISTS agent_status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_status_history_ts ON agent_status_history(ts);
CREATE INDEX IF NOT EXISTS idx_agent_status_history_agent ON agent_status_history(agent_id);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    text TEXT NOT NULL,
    ref TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS bead_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    bead_id TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bead_history_ts ON bead_history(ts);
CREATE INDEX IF NOT EXISTS idx_bead_history_bead ON bead_history(bead_id);

-- Wave-2 analytics (briefing Task 2). One row per tool_use/tool_result block
-- found while incrementally tailing the same *.jsonl files usage.py already
-- reads (this table's offset is tracked separately, see
-- analytics.py:ANALYTICS_OFFSET_PREFIX, so the two collectors never fight
-- over file_offsets). Dedup key is (host, uuid, block_index): `uuid` is the
-- jsonl line's own id (one per transcript entry), `block_index` is that
-- line's position within message.content -- a single line can carry more
-- than one tool_use/tool_result block.
CREATE TABLE IF NOT EXISTS tool_call_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host TEXT NOT NULL DEFAULT 'localhost',
    uuid TEXT NOT NULL,
    block_index INTEGER NOT NULL,
    ts TEXT NOT NULL,
    session_id TEXT,
    is_sidechain INTEGER NOT NULL DEFAULT 0,
    tool TEXT,
    tool_use_id TEXT,
    is_error INTEGER NOT NULL DEFAULT 0,
    error_kind TEXT,
    error_excerpt TEXT,
    file_path TEXT,
    UNIQUE(host, uuid, block_index)
);
CREATE INDEX IF NOT EXISTS idx_tool_call_events_ts ON tool_call_events(ts);
CREATE INDEX IF NOT EXISTS idx_tool_call_events_host ON tool_call_events(host);
CREATE INDEX IF NOT EXISTS idx_tool_call_events_tool_use_id ON tool_call_events(tool_use_id);
CREATE INDEX IF NOT EXISTS idx_tool_call_events_tool ON tool_call_events(tool);

-- isApiErrorMessage lines (rate limits, overloaded, auth failures, ...).
-- One row per jsonl line -- these are whole-message errors, not per-block.
CREATE TABLE IF NOT EXISTS api_error_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host TEXT NOT NULL DEFAULT 'localhost',
    uuid TEXT NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT,
    UNIQUE(host, uuid)
);
CREATE INDEX IF NOT EXISTS idx_api_error_events_ts ON api_error_events(ts);

-- Remote-host analytics buckets, all day-granularity, all replace-semantics
-- upserts (same pattern as remote_usage_buckets -- each probe call ships that
-- host's full current aggregate for a bucket, which overwrites rather than
-- accumulates). Kept separate per sub-metric rather than one wide table so
-- each stays a simple GROUP BY at query time.
CREATE TABLE IF NOT EXISTS remote_tool_buckets (
    host TEXT NOT NULL, day TEXT NOT NULL, tool TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (host, day, tool)
);
CREATE INDEX IF NOT EXISTS idx_remote_tool_buckets_day ON remote_tool_buckets(day);

CREATE TABLE IF NOT EXISTS remote_error_buckets (
    host TEXT NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL, tool TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0, last_seen TEXT,
    PRIMARY KEY (host, day, kind, tool)
);
CREATE INDEX IF NOT EXISTS idx_remote_error_buckets_day ON remote_error_buckets(day);

-- One representative (redacted, already-capped) example string per
-- (host, kind), overwritten with the most recent occurrence on every probe.
CREATE TABLE IF NOT EXISTS remote_error_examples (
    host TEXT NOT NULL, kind TEXT NOT NULL,
    example TEXT, last_seen TEXT,
    PRIMARY KEY (host, kind)
);

CREATE TABLE IF NOT EXISTS remote_trouble_file_buckets (
    host TEXT NOT NULL, day TEXT NOT NULL, path TEXT NOT NULL, tool TEXT NOT NULL,
    errors INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (host, day, path, tool)
);
CREATE INDEX IF NOT EXISTS idx_remote_trouble_file_buckets_day ON remote_trouble_file_buckets(day);

CREATE TABLE IF NOT EXISTS remote_api_error_buckets (
    host TEXT NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0, last_seen TEXT,
    PRIMARY KEY (host, day, kind)
);
CREATE INDEX IF NOT EXISTS idx_remote_api_error_buckets_day ON remote_api_error_buckets(day);

-- Per-session token buckets split by is_sidechain, day granularity. No cost
-- here for the same reason remote_usage_buckets has none: pricing is applied
-- centrally on the local host so a rate correction fixes every host at once.
CREATE TABLE IF NOT EXISTS remote_session_buckets (
    host TEXT NOT NULL, day TEXT NOT NULL, session_id TEXT NOT NULL,
    is_sidechain INTEGER NOT NULL, model TEXT NOT NULL,
    project TEXT,
    input INTEGER NOT NULL DEFAULT 0, output INTEGER NOT NULL DEFAULT 0,
    cache_read INTEGER NOT NULL DEFAULT 0,
    cache_write_5m INTEGER NOT NULL DEFAULT 0, cache_write_1h INTEGER NOT NULL DEFAULT 0,
    messages INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (host, day, session_id, is_sidechain, model)
);
CREATE INDEX IF NOT EXISTS idx_remote_session_buckets_day ON remote_session_buckets(day);

CREATE TABLE IF NOT EXISTS remote_productivity_buckets (
    host TEXT NOT NULL, day TEXT NOT NULL, repo TEXT NOT NULL,
    commits INTEGER NOT NULL DEFAULT 0,
    lines_added INTEGER NOT NULL DEFAULT 0, lines_removed INTEGER NOT NULL DEFAULT 0,
    files_changed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (host, day, repo)
);
CREATE INDEX IF NOT EXISTS idx_remote_productivity_buckets_day ON remote_productivity_buckets(day);

-- Kimi Code (second provider, briefing 2026-09-18). Kept in dedicated tables
-- rather than folded into usage_events/api_error_events: Kimi has no
-- per-message cost (subscription quota, not per-token billing -- see
-- collectors/kimi.py module docstring), only a single `tokens` total per
-- turn (no input/output/cache split), so it does not share usage_events'
-- shape or its NOT NULL cost_usd column. Keeping it separate means the
-- existing Claude tables/queries are never touched by this feature -- the
-- "Claude totals numerically unchanged" requirement holds structurally, not
-- by convention. One row per Kimi turn (token_counting.* event in a
-- session's wire.jsonl); ts_ms (the event's own epoch-ms `time` field) is
-- the dedup key component since Kimi's wire.jsonl schema does not always
-- carry a turnId (see collectors/kimi.py).
CREATE TABLE IF NOT EXISTS kimi_turn_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host TEXT NOT NULL DEFAULT 'localhost',
    session_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    model TEXT,
    tokens INTEGER NOT NULL DEFAULT 0,
    project TEXT,
    UNIQUE(host, session_id, ts_ms)
);
CREATE INDEX IF NOT EXISTS idx_kimi_turn_events_ts ON kimi_turn_events(ts);
CREATE INDEX IF NOT EXISTS idx_kimi_turn_events_session ON kimi_turn_events(session_id);

-- One row per Kimi turn.ended{reason:"failed"} event -- the Kimi analogue of
-- api_error_events (whole-turn errors, not per-tool-call).
CREATE TABLE IF NOT EXISTS kimi_error_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host TEXT NOT NULL DEFAULT 'localhost',
    session_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    kind TEXT,
    code TEXT,
    example TEXT,
    UNIQUE(host, session_id, ts_ms)
);
CREATE INDEX IF NOT EXISTS idx_kimi_error_events_ts ON kimi_error_events(ts);

-- Remote-host Kimi buckets, day-granularity, replace-semantics upsert --
-- same pattern as the other remote_* bucket tables.
CREATE TABLE IF NOT EXISTS remote_kimi_usage_buckets (
    host TEXT NOT NULL, day TEXT NOT NULL, model TEXT NOT NULL,
    tokens INTEGER NOT NULL DEFAULT 0, turns INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (host, day, model)
);
CREATE INDEX IF NOT EXISTS idx_remote_kimi_usage_buckets_day ON remote_kimi_usage_buckets(day);

CREATE TABLE IF NOT EXISTS remote_kimi_error_buckets (
    host TEXT NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0, last_seen TEXT,
    PRIMARY KEY (host, day, kind)
);
CREATE INDEX IF NOT EXISTS idx_remote_kimi_error_buckets_day ON remote_kimi_error_buckets(day);

-- Manually-triggered AI-provider quota check cache (critdash/quota.py). One
-- row per provider, replaced wholesale on every POST /api/quota/refresh.
-- `payload` is the full JSON result dict quota.py builds -- numbers and
-- timestamps only, NEVER a credential (see quota.py's module docstring) --
-- so persisting this table verbatim is safe.
CREATE TABLE IF NOT EXISTS quota_cache (
    provider TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    checked_at TEXT NOT NULL
);
"""


class Store:
    """Thread-safety note: sqlite3 connections are per-thread; this app runs
    a single asyncio event loop and collectors call the store synchronously
    from that loop's thread, so one connection with check_same_thread=True
    (the default) is safe. A lock guards against accidental concurrent use."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
            # migration: usage_events predates the `speed` column on DBs created
            # before fast-mode billing was tracked. CREATE TABLE IF NOT EXISTS is
            # a no-op against an existing table, so add it explicitly.
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(usage_events)")}
            if "speed" not in cols:
                self._conn.execute("ALTER TABLE usage_events ADD COLUMN speed TEXT")
                self._conn.commit()
            # migration: usage_events predates multi-host fleet collection. A
            # DB created before this change keeps its original column-level
            # UNIQUE(message_id) constraint -- SQLite can't alter a
            # constraint in place -- which is harmless since every row in
            # such a DB is implicitly host='localhost' already (only the local
            # collector ever wrote to this table before remote hosts
            # existed), so uniqueness-per-message_id and
            # uniqueness-per-(host,message_id) agree for all pre-existing
            # rows. Fresh DBs get the composite UNIQUE(host, message_id) from
            # SCHEMA above, which is what lets two distinct hosts legitimately
            # share a message_id without a dedupe collision.
            if "host" not in cols:
                self._conn.execute(
                    "ALTER TABLE usage_events ADD COLUMN host TEXT NOT NULL DEFAULT 'localhost'"
                )
                self._conn.commit()
            # created here (not in the static SCHEMA block above) because a
            # pre-migration DB doesn't have the `host` column yet at the
            # point SCHEMA's executescript runs -- by this line the ALTER
            # above (or the fresh CREATE TABLE) guarantees it exists.
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_events_host ON usage_events(host)")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- file offsets -----------------------------------------------------
    def get_offset(self, path: str) -> tuple[int, int, float] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT inode, offset, mtime FROM file_offsets WHERE path = ?", (path,)
            ).fetchone()
        if row is None:
            return None
        return row["inode"], row["offset"], row["mtime"]

    def set_offset(self, path: str, inode: int, offset: int, mtime: float) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO file_offsets(path, inode, offset, mtime) VALUES (?, ?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET inode=excluded.inode,
                       offset=excluded.offset, mtime=excluded.mtime""",
                (path, inode, offset, mtime),
            )
            self._conn.commit()

    # -- usage events -------------------------------------------------------
    def insert_usage_events(self, rows: Iterable[dict]) -> int:
        # `host` defaults to 'localhost' when a caller doesn't set it, so
        # pre-existing single-host call sites (and their tests) are
        # unaffected -- UsageCollector stamps the configured local host name
        # explicitly, everything else falls back to the historical default.
        rows = [{**r, "host": r.get("host") or "localhost"} for r in rows]
        if not rows:
            return 0
        with self._lock:
            cur = self._conn.executemany(
                """INSERT OR IGNORE INTO usage_events
                   (message_id, ts, session_id, project, project_path, model,
                    input, output, cache_read, cache_write_5m, cache_write_1h,
                    speed, is_sidechain, web_searches, cost_usd, host)
                   VALUES (:message_id, :ts, :session_id, :project, :project_path, :model,
                           :input, :output, :cache_read, :cache_write_5m, :cache_write_1h,
                           :speed, :is_sidechain, :web_searches, :cost_usd, :host)""",
                rows,
            )
            self._conn.commit()
            return cur.rowcount

    # -- remote usage buckets -------------------------------------------------
    def upsert_remote_usage_buckets(self, rows: Iterable[dict]) -> int:
        """Replace-semantics upsert keyed by (host, hour, model, project).
        Each probe call recomputes a host's FULL current aggregate for every
        (hour, model, project) it has data for (see remote_probe.py's
        file-level cache), so a fresh call's numbers are authoritative and
        should overwrite -- not accumulate on top of -- whatever was stored
        for that key before."""
        rows = list(rows)
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                """INSERT INTO remote_usage_buckets
                   (host, hour, model, project, input, output, cache_read,
                    cache_write_5m, cache_write_1h, messages)
                   VALUES (:host, :hour, :model, :project, :input, :output, :cache_read,
                           :cache_write_5m, :cache_write_1h, :messages)
                   ON CONFLICT(host, hour, model, project) DO UPDATE SET
                       input=excluded.input, output=excluded.output,
                       cache_read=excluded.cache_read,
                       cache_write_5m=excluded.cache_write_5m,
                       cache_write_1h=excluded.cache_write_1h,
                       messages=excluded.messages""",
                rows,
            )
            self._conn.commit()
            return len(rows)

    _REMOTE_GROUP_COLS = {"host", "hour", "model", "project"}

    def remote_usage_grouped(
        self, group_cols: tuple[str, ...], since_iso: str | None = None, host: str | None = None,
    ) -> list[sqlite3.Row]:
        """Token sums from remote_usage_buckets grouped by `group_cols` (any
        of host/hour/model/project -- an internal fixed whitelist, never
        caller-supplied text, so building the GROUP BY/SELECT list by string
        join is safe). `since_iso` is compared against the bucket's hour
        prefix (its first 13 chars, e.g. "2026-09-18T14") since buckets are
        hour-granularity, not full-timestamp."""
        cols = tuple(group_cols)
        if not cols or not all(c in self._REMOTE_GROUP_COLS for c in cols):
            raise ValueError(f"invalid group_cols: {group_cols!r}")
        clauses = []
        params: list = []
        if since_iso:
            clauses.append("hour >= ?")
            params.append(since_iso[:13])
        if host:
            clauses.append("host = ?")
            params.append(host)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        col_list = ", ".join(cols)
        with self._lock:
            return self._conn.execute(
                f"""SELECT {col_list},
                        COALESCE(SUM(input),0) AS input,
                        COALESCE(SUM(output),0) AS output,
                        COALESCE(SUM(cache_read),0) AS cache_read,
                        COALESCE(SUM(cache_write_5m),0) AS cache_write_5m,
                        COALESCE(SUM(cache_write_1h),0) AS cache_write_1h,
                        COALESCE(SUM(messages),0) AS messages
                    FROM remote_usage_buckets {where}
                    GROUP BY {col_list}""",  # noqa: S608 -- col_list/where built from a fixed whitelist above
                params,
            ).fetchall()

    def recost_all(self, cost_fn) -> int:
        """Recompute and persist cost_usd for every stored row from its token
        columns, using `cost_fn(model, input, output, cache_read,
        cache_write_5m, cache_write_1h, speed) -> float`. Used once at
        startup so historical rows reflect the current pricing.json rates
        rather than whatever was in effect at ingest time. Rows ingested
        before the `speed` column existed have speed=NULL (treated as
        standard rate) -- their original fast-mode status, if any, was never
        persisted and cannot be recovered without re-reading rotated jsonl
        files, which is out of scope here."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, model, input, output, cache_read, cache_write_5m,
                          cache_write_1h, speed FROM usage_events"""
            ).fetchall()
            updates = [
                (
                    cost_fn(
                        r["model"], r["input"], r["output"], r["cache_read"],
                        r["cache_write_5m"], r["cache_write_1h"], r["speed"],
                    ),
                    r["id"],
                )
                for r in rows
            ]
            self._conn.executemany("UPDATE usage_events SET cost_usd = ? WHERE id = ?", updates)
            self._conn.commit()
            return len(updates)

    def query_usage(self, since_iso: str | None = None, until_iso: str | None = None) -> list[sqlite3.Row]:
        clauses = []
        params: list = []
        if since_iso:
            clauses.append("ts >= ?")
            params.append(since_iso)
        if until_iso:
            clauses.append("ts < ?")
            params.append(until_iso)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            return self._conn.execute(
                f"SELECT * FROM usage_events {where} ORDER BY ts ASC", params  # noqa: S608
            ).fetchall()

    def usage_totals(self, since_iso: str | None = None, host: str | None = None) -> dict:
        clauses = []
        params: list = []
        if since_iso:
            clauses.append("ts >= ?")
            params.append(since_iso)
        if host:
            clauses.append("host = ?")
            params.append(host)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            row = self._conn.execute(
                f"""SELECT
                        COALESCE(SUM(input),0) AS input,
                        COALESCE(SUM(output),0) AS output,
                        COALESCE(SUM(cache_read),0) AS cache_read,
                        COALESCE(SUM(cache_write_5m + cache_write_1h),0) AS cache_write,
                        COALESCE(SUM(cost_usd),0.0) AS cost_usd,
                        COUNT(*) AS messages
                    FROM usage_events {where}""",  # noqa: S608
                params,
            ).fetchone()
        total = row["input"] + row["output"] + row["cache_read"] + row["cache_write"]
        return {
            "input": row["input"],
            "output": row["output"],
            "cache_read": row["cache_read"],
            "cache_write": row["cache_write"],
            "total": total,
            "cost_usd": round(row["cost_usd"], 6),
            "messages": row["messages"],
        }

    def usage_by_model(self, since_iso: str | None = None) -> list[sqlite3.Row]:
        where = "WHERE ts >= ?" if since_iso else ""
        params = [since_iso] if since_iso else []
        with self._lock:
            return self._conn.execute(
                f"""SELECT model,
                        COALESCE(SUM(input),0) AS input,
                        COALESCE(SUM(output),0) AS output,
                        COALESCE(SUM(cache_read),0) AS cache_read,
                        COALESCE(SUM(cache_write_5m + cache_write_1h),0) AS cache_write,
                        COALESCE(SUM(cost_usd),0.0) AS cost_usd,
                        COUNT(*) AS messages
                    FROM usage_events {where} GROUP BY model""",  # noqa: S608
                params,
            ).fetchall()

    def usage_by_project(self, since_iso: str | None = None) -> list[sqlite3.Row]:
        where = "WHERE ts >= ?" if since_iso else ""
        params = [since_iso] if since_iso else []
        with self._lock:
            return self._conn.execute(
                f"""SELECT project,
                        COALESCE(SUM(input+output+cache_read+cache_write_5m+cache_write_1h),0) AS total,
                        COALESCE(SUM(cost_usd),0.0) AS cost_usd,
                        COUNT(*) AS messages
                    FROM usage_events {where} GROUP BY project""",  # noqa: S608
                params,
            ).fetchall()

    def usage_by_session(self, since_iso: str | None = None) -> list[sqlite3.Row]:
        where = "WHERE ts >= ?" if since_iso else ""
        params = [since_iso] if since_iso else []
        with self._lock:
            return self._conn.execute(
                f"""SELECT session_id,
                        COALESCE(SUM(input+output+cache_read+cache_write_5m+cache_write_1h),0) AS total,
                        COALESCE(SUM(cost_usd),0.0) AS cost_usd,
                        COUNT(*) AS messages
                    FROM usage_events {where} GROUP BY session_id""",  # noqa: S608
                params,
            ).fetchall()

    def usage_timeline_hourly(self, since_iso: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                """SELECT substr(ts, 1, 13) || ':00:00Z' AS bucket,
                        COALESCE(SUM(input),0) AS input,
                        COALESCE(SUM(output),0) AS output,
                        COALESCE(SUM(cache_read),0) AS cache_read,
                        COALESCE(SUM(cache_write_5m + cache_write_1h),0) AS cache_write,
                        COALESCE(SUM(cost_usd),0.0) AS cost_usd,
                        COUNT(*) AS messages
                    FROM usage_events WHERE ts >= ?
                    GROUP BY bucket ORDER BY bucket ASC""",
                (since_iso,),
            ).fetchall()

    def usage_timeline_daily(self, since_iso: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                """SELECT substr(ts, 1, 10) AS bucket,
                        COALESCE(SUM(input),0) AS input,
                        COALESCE(SUM(output),0) AS output,
                        COALESCE(SUM(cache_read),0) AS cache_read,
                        COALESCE(SUM(cache_write_5m + cache_write_1h),0) AS cache_write,
                        COALESCE(SUM(cost_usd),0.0) AS cost_usd,
                        COUNT(*) AS messages
                    FROM usage_events WHERE ts >= ?
                    GROUP BY bucket ORDER BY bucket ASC""",
                (since_iso,),
            ).fetchall()

    def usage_timeline_daily_tz(self, since_iso: str, tz_name: str = "UTC") -> list[dict]:
        """Same shape as usage_timeline_daily, but day buckets are the local
        calendar day in `tz_name` rather than UTC's. tz_name="UTC" (or a
        falsy value) takes the plain SQL path above unchanged -- byte-
        identical to the pre-existing behavior. Any other timezone re-buckets
        from the existing hourly query (widened so a local day near the
        window edge is never cut short) via fold_hour_rows_to_local_day, so
        SQLite never needs to know about IANA timezones."""
        if not tz_name or tz_name == "UTC":
            return [dict(r) for r in self.usage_timeline_daily(since_iso)]
        hourly = self.usage_timeline_hourly(_widen_since_hour(since_iso))
        return fold_hour_rows_to_local_day(hourly, tz_name)

    # -- grouped timeline queries (history.py: GET /api/history/usage
    # group_by=model/provider/host) -----------------------------------------
    # Each of the four source tables gets one bucketed-by-(bucket, host,
    # model) query, at whatever grain that table natively supports. history.py
    # unions all four in Python (same union-with-no-double-counting shape the
    # existing collectors/usage.py rollups already use: local usage_events is
    # this host's own per-message rows, remote_usage_buckets/
    # remote_kimi_usage_buckets are every OTHER host's pre-aggregated
    # buckets -- RemoteCollector only probes hosts with mode == "ssh", never
    # the local one, so the two sets are always disjoint by host).
    def usage_events_grouped_timeline(
        self, since_iso: str, bucket: str, tz_name: str = "UTC"
    ) -> list[sqlite3.Row] | list[dict]:
        """(bucket, host, model) token+cost sums from local usage_events.
        cost_usd is the per-row value already stored at ingest time (see
        collectors/usage.py), safe to SUM directly -- no re-costing needed
        here, unlike the remote path below.

        bucket='day' with tz_name != 'UTC' re-buckets from the hour query
        (widened) via fold_hour_rows_to_local_day, same technique as
        usage_timeline_daily_tz -- SQLite has no IANA timezone database."""
        if bucket not in ("hour", "day"):
            raise ValueError(f"invalid bucket: {bucket!r}")
        if bucket == "day" and tz_name and tz_name != "UTC":
            hourly = self.usage_events_grouped_timeline(_widen_since_hour(since_iso), "hour")
            return fold_hour_rows_to_local_day(hourly, tz_name, key_cols=("host", "model"))
        bucket_expr = "substr(ts,1,10)" if bucket == "day" else "substr(ts,1,13) || ':00:00Z'"
        with self._lock:
            return self._conn.execute(
                f"""SELECT {bucket_expr} AS bucket, host, model,
                        COALESCE(SUM(input),0) AS input,
                        COALESCE(SUM(output),0) AS output,
                        COALESCE(SUM(cache_read),0) AS cache_read,
                        COALESCE(SUM(cache_write_5m + cache_write_1h),0) AS cache_write,
                        COALESCE(SUM(cost_usd),0.0) AS cost_usd,
                        COUNT(*) AS messages
                    FROM usage_events WHERE ts >= ?
                    GROUP BY bucket, host, model ORDER BY bucket ASC""",  # noqa: S608 -- bucket_expr from fixed whitelist
                (since_iso,),
            ).fetchall()

    def remote_usage_grouped_timeline(
        self, since_iso: str, bucket: str, tz_name: str = "UTC"
    ) -> list[sqlite3.Row] | list[dict]:
        """Same grain as usage_events_grouped_timeline but from
        remote_usage_buckets (every other host). cache_write_5m/1h are kept
        SEPARATE (not combined like the local query above) because the two
        TTL buckets price differently -- see pricing.compute_cost_usd -- and
        no cost_usd is stored on this table (pricing is applied centrally on
        the local host at query time), so the caller needs the split figures to
        cost each row correctly.

        bucket='day' with tz_name != 'UTC': same hour-widen-and-fold
        technique as usage_events_grouped_timeline above -- remote_usage_buckets
        is hour-granularity, so this is exact, not a further approximation."""
        if bucket not in ("hour", "day"):
            raise ValueError(f"invalid bucket: {bucket!r}")
        if bucket == "day" and tz_name and tz_name != "UTC":
            hourly = self.remote_usage_grouped_timeline(_widen_since_hour(since_iso), "hour")
            return fold_hour_rows_to_local_day(hourly, tz_name, key_cols=("host", "model"))
        bucket_expr = "substr(hour,1,10)" if bucket == "day" else "hour || ':00:00Z'"
        with self._lock:
            return self._conn.execute(
                f"""SELECT {bucket_expr} AS bucket, host, model,
                        COALESCE(SUM(input),0) AS input,
                        COALESCE(SUM(output),0) AS output,
                        COALESCE(SUM(cache_read),0) AS cache_read,
                        COALESCE(SUM(cache_write_5m),0) AS cache_write_5m,
                        COALESCE(SUM(cache_write_1h),0) AS cache_write_1h,
                        COALESCE(SUM(messages),0) AS messages
                    FROM remote_usage_buckets WHERE hour >= ?
                    GROUP BY bucket, host, model ORDER BY bucket ASC""",  # noqa: S608
                (since_iso[:13],),
            ).fetchall()

    def kimi_turn_grouped_timeline(
        self, since_iso: str, bucket: str, tz_name: str = "UTC"
    ) -> list[sqlite3.Row] | list[dict]:
        """(bucket, host, model) sums from local kimi_turn_events. Kimi has
        no input/output/cache split (one `tokens` total per turn -- see
        collectors/kimi.py) and no cost (subscription billing, not
        per-token).

        bucket='day' with tz_name != 'UTC': same hour-widen-and-fold
        technique as usage_events_grouped_timeline above."""
        if bucket not in ("hour", "day"):
            raise ValueError(f"invalid bucket: {bucket!r}")
        if bucket == "day" and tz_name and tz_name != "UTC":
            hourly = self.kimi_turn_grouped_timeline(_widen_since_hour(since_iso), "hour")
            return fold_hour_rows_to_local_day(hourly, tz_name, key_cols=("host", "model"))
        bucket_expr = "substr(ts,1,10)" if bucket == "day" else "substr(ts,1,13) || ':00:00Z'"
        with self._lock:
            return self._conn.execute(
                f"""SELECT {bucket_expr} AS bucket, host, model,
                        COALESCE(SUM(tokens),0) AS tokens,
                        COUNT(*) AS messages
                    FROM kimi_turn_events WHERE ts >= ?
                    GROUP BY bucket, host, model ORDER BY bucket ASC""",  # noqa: S608
                (since_iso,),
            ).fetchall()

    def remote_kimi_grouped_timeline(self, since_day: str) -> list[sqlite3.Row]:
        """Day granularity only -- remote_kimi_usage_buckets (every other
        host's Kimi data) has no finer resolution than a day, unlike
        remote_usage_buckets above. Callers only use this when the requested
        bucket is 'day'; an hour-bucketed grouped query cannot place a
        remote host's Kimi tokens within the day, so it is left out there
        (a real resolution gap, not fabricated placement -- see history.py).

        NOT timezone-aware: this table is pre-aggregated to a UTC calendar
        day by the remote probe before it ever reaches this host, so the
        day boundary it was bucketed at cannot be recovered here -- a real
        architectural limitation (see history.py's _accumulate), not an
        oversight."""
        with self._lock:
            return self._conn.execute(
                """SELECT day AS bucket, host, model,
                        COALESCE(SUM(tokens),0) AS tokens,
                        COALESCE(SUM(turns),0) AS messages
                    FROM remote_kimi_usage_buckets WHERE day >= ?
                    GROUP BY bucket, host, model ORDER BY bucket ASC""",
                (since_day,),
            ).fetchall()

    # -- earliest-data-per-series (history.py coverage metadata) -------------
    def usage_events_first_seen(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT host, model, MIN(ts) AS first_ts FROM usage_events GROUP BY host, model"
            ).fetchall()

    def remote_usage_buckets_first_seen(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT host, model, MIN(hour) AS first_ts FROM remote_usage_buckets GROUP BY host, model"
            ).fetchall()

    def kimi_turn_events_first_seen(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT host, model, MIN(ts) AS first_ts FROM kimi_turn_events GROUP BY host, model"
            ).fetchall()

    def remote_kimi_usage_buckets_first_seen(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT host, model, MIN(day) AS first_ts FROM remote_kimi_usage_buckets GROUP BY host, model"
            ).fetchall()

    def usage_last_activity_by_session(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                """SELECT session_id, model, ts FROM (
                       SELECT session_id, model, ts,
                              ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY ts DESC) AS rn
                       FROM usage_events WHERE session_id IS NOT NULL
                   ) WHERE rn = 1"""
            ).fetchall()

    def usage_sidechain_recent_counts(self, since_iso: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                """SELECT session_id, COUNT(*) AS n FROM usage_events
                   WHERE is_sidechain = 1 AND ts >= ? AND session_id IS NOT NULL
                   GROUP BY session_id""",
                (since_iso,),
            ).fetchall()

    def usage_since(self, since_iso: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM usage_events WHERE ts >= ? ORDER BY ts ASC", (since_iso,)
            ).fetchall()

    def current_usage_block_start(self, now_iso: str) -> str | None:
        """Anchor of the currently active 5h rate-limit block, or None if no
        block is active right now (ccusage's model, not a pure activity-gap
        detector -- see collectors/usage.py's Bug 1 fix notes).

        A block starts at the first message after the previous block ended.
        A block ends the earlier of: (a) BLOCK_DURATION after it started, or
        (b) never, if activity continues -- but (a) always fires first,
        because a >=BLOCK_DURATION gap between two messages inside a block
        implies the later message is already >=BLOCK_DURATION past the
        block's start (the block's start is always <= every message inside
        it), so a bare "did 5h elapse since this block started" check on each
        message, scanned in order, captures both a gap-triggered rollover and
        a continuously-busy-fleet rollover with one rule. The next message at
        or after a block's end begins a new block (>=, matching "the next
        message... begins a new block").

        If the most recently observed block has already ended and no message
        has arrived since, there is no active block: returns None rather than
        a stale started_at/ends_at pair, so a caller never reports pct_elapsed
        clamped at a past-due window as if it were still counting up.
        """
        with self._lock:
            rows = self._conn.execute("SELECT ts FROM usage_events ORDER BY ts ASC").fetchall()
        if not rows:
            return None

        def parse(t: str) -> datetime:
            return datetime.fromisoformat(t.replace("Z", "+00:00"))

        block_start = parse(rows[0]["ts"])
        block_end = block_start + BLOCK_DURATION
        for r in rows[1:]:
            cur = parse(r["ts"])
            if cur >= block_end:
                block_start = cur
                block_end = block_start + BLOCK_DURATION

        now = parse(now_iso)
        if now >= block_end:
            return None
        return block_start.strftime("%Y-%m-%dT%H:%M:%SZ")

    # -- wave-2 analytics: tool/error ingestion ------------------------------
    def insert_tool_call_events(self, rows: Iterable[dict]) -> int:
        rows = [
            {**r, "host": r.get("host") or "localhost", "tool_use_id": r.get("tool_use_id")} for r in rows
        ]
        if not rows:
            return 0
        with self._lock:
            cur = self._conn.executemany(
                """INSERT OR IGNORE INTO tool_call_events
                   (host, uuid, block_index, ts, session_id, is_sidechain, tool, tool_use_id,
                    is_error, error_kind, error_excerpt, file_path)
                   VALUES (:host, :uuid, :block_index, :ts, :session_id, :is_sidechain, :tool, :tool_use_id,
                           :is_error, :error_kind, :error_excerpt, :file_path)""",
                rows,
            )
            self._conn.commit()
            return cur.rowcount

    def update_tool_call_error(
        self, host: str, tool_use_id: str | None, error_kind: str | None, error_excerpt: str | None
    ) -> bool:
        """Correct an already-inserted tool_use row (from an earlier poll)
        once its matching tool_result error is seen. Returns False if no row
        with that tool_use_id exists (e.g. the original tool_use line was
        malformed/dropped) -- caller falls back to inserting a standalone
        error row so the error is never silently lost."""
        if not tool_use_id:
            return False
        with self._lock:
            cur = self._conn.execute(
                """UPDATE tool_call_events SET is_error = 1, error_kind = ?, error_excerpt = ?
                   WHERE host = ? AND tool_use_id = ?""",
                (error_kind, error_excerpt, host, tool_use_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def insert_api_error_events(self, rows: Iterable[dict]) -> int:
        rows = [{**r, "host": r.get("host") or "localhost"} for r in rows]
        if not rows:
            return 0
        with self._lock:
            cur = self._conn.executemany(
                """INSERT OR IGNORE INTO api_error_events (host, uuid, ts, kind)
                   VALUES (:host, :uuid, :ts, :kind)""",
                rows,
            )
            self._conn.commit()
            return cur.rowcount

    def tool_error_top_kinds(self, since_iso: str, host: str | None = None) -> list[sqlite3.Row]:
        where = "WHERE is_error = 1 AND ts >= ?"
        params: list = [since_iso]
        if host:
            where += " AND host = ?"
            params.append(host)
        with self._lock:
            return self._conn.execute(
                f"""SELECT error_kind AS kind, tool,
                        COUNT(*) AS count, MAX(ts) AS last_seen
                    FROM tool_call_events {where}
                    GROUP BY error_kind, tool""",  # noqa: S608 -- where built from fixed clauses only
                params,
            ).fetchall()

    def tool_error_example(self, since_iso: str, kind: str, host: str | None = None) -> sqlite3.Row | None:
        where = "WHERE is_error = 1 AND ts >= ? AND error_kind = ? AND error_excerpt IS NOT NULL"
        params: list = [since_iso, kind]
        if host:
            where += " AND host = ?"
            params.append(host)
        with self._lock:
            return self._conn.execute(
                f"""SELECT error_excerpt, ts FROM tool_call_events {where}
                    ORDER BY ts DESC LIMIT 1""",  # noqa: S608
                params,
            ).fetchone()

    def tool_call_by_tool(self, since_iso: str, host: str | None = None) -> list[sqlite3.Row]:
        where = "WHERE ts >= ?"
        params: list = [since_iso]
        if host:
            where += " AND host = ?"
            params.append(host)
        with self._lock:
            return self._conn.execute(
                f"""SELECT tool, COUNT(*) AS calls,
                        SUM(is_error) AS errors
                    FROM tool_call_events {where} GROUP BY tool""",  # noqa: S608
                params,
            ).fetchall()

    def tool_error_trouble_files(self, since_iso: str, host: str | None = None) -> list[sqlite3.Row]:
        where = "WHERE is_error = 1 AND ts >= ? AND file_path IS NOT NULL"
        params: list = [since_iso]
        if host:
            where += " AND host = ?"
            params.append(host)
        with self._lock:
            return self._conn.execute(
                f"""SELECT file_path, tool, COUNT(*) AS errors
                    FROM tool_call_events {where} GROUP BY file_path, tool""",  # noqa: S608
                params,
            ).fetchall()

    def api_error_counts(self, since_iso: str, host: str | None = None) -> list[sqlite3.Row]:
        where = "WHERE ts >= ?"
        params: list = [since_iso]
        if host:
            where += " AND host = ?"
            params.append(host)
        with self._lock:
            return self._conn.execute(
                f"""SELECT kind, COUNT(*) AS count, MAX(ts) AS last_seen
                    FROM api_error_events {where} GROUP BY kind""",  # noqa: S608
                params,
            ).fetchall()

    def usage_by_session_sidechain(self, since_iso: str, host: str | None = None) -> list[sqlite3.Row]:
        """Local per-(session_id, is_sidechain) totals for the subagent-cost
        rollup: cost is already computed and stored per usage_events row, so
        this is a plain GROUP BY, no pricing needed here."""
        where = "WHERE ts >= ?"
        params: list = [since_iso]
        if host:
            where += " AND host = ?"
            params.append(host)
        with self._lock:
            return self._conn.execute(
                f"""SELECT session_id, is_sidechain,
                        COALESCE(SUM(cost_usd),0.0) AS cost_usd,
                        COUNT(*) AS messages,
                        GROUP_CONCAT(DISTINCT model) AS models,
                        MAX(project) AS project
                    FROM usage_events {where} AND session_id IS NOT NULL
                    GROUP BY session_id, is_sidechain""",  # noqa: S608
                params,
            ).fetchall()

    # -- wave-2 analytics: remote bucket upserts + rollup queries ------------
    _REMOTE_DAY_BUCKET_TABLES = {
        "remote_tool_buckets": ("host", "day", "tool"),
        "remote_error_buckets": ("host", "day", "kind", "tool"),
        "remote_trouble_file_buckets": ("host", "day", "path", "tool"),
        "remote_api_error_buckets": ("host", "day", "kind"),
        "remote_session_buckets": ("host", "day", "session_id", "is_sidechain", "model"),
        "remote_productivity_buckets": ("host", "day", "repo"),
    }

    def upsert_remote_tool_buckets(self, rows: Iterable[dict]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                """INSERT INTO remote_tool_buckets (host, day, tool, calls, errors)
                   VALUES (:host, :day, :tool, :calls, :errors)
                   ON CONFLICT(host, day, tool) DO UPDATE SET
                       calls=excluded.calls, errors=excluded.errors""",
                rows,
            )
            self._conn.commit()
            return len(rows)

    def upsert_remote_error_buckets(self, rows: Iterable[dict]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                """INSERT INTO remote_error_buckets (host, day, kind, tool, count, last_seen)
                   VALUES (:host, :day, :kind, :tool, :count, :last_seen)
                   ON CONFLICT(host, day, kind, tool) DO UPDATE SET
                       count=excluded.count, last_seen=excluded.last_seen""",
                rows,
            )
            self._conn.commit()
            return len(rows)

    def upsert_remote_error_examples(self, rows: Iterable[dict]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                """INSERT INTO remote_error_examples (host, kind, example, last_seen)
                   VALUES (:host, :kind, :example, :last_seen)
                   ON CONFLICT(host, kind) DO UPDATE SET
                       example=excluded.example, last_seen=excluded.last_seen
                   WHERE excluded.last_seen >= remote_error_examples.last_seen""",
                rows,
            )
            self._conn.commit()
            return len(rows)

    def upsert_remote_trouble_file_buckets(self, rows: Iterable[dict]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                """INSERT INTO remote_trouble_file_buckets (host, day, path, tool, errors)
                   VALUES (:host, :day, :path, :tool, :errors)
                   ON CONFLICT(host, day, path, tool) DO UPDATE SET errors=excluded.errors""",
                rows,
            )
            self._conn.commit()
            return len(rows)

    def upsert_remote_api_error_buckets(self, rows: Iterable[dict]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                """INSERT INTO remote_api_error_buckets (host, day, kind, count, last_seen)
                   VALUES (:host, :day, :kind, :count, :last_seen)
                   ON CONFLICT(host, day, kind) DO UPDATE SET
                       count=excluded.count, last_seen=excluded.last_seen""",
                rows,
            )
            self._conn.commit()
            return len(rows)

    def upsert_remote_session_buckets(self, rows: Iterable[dict]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                """INSERT INTO remote_session_buckets
                   (host, day, session_id, is_sidechain, model, project, input, output,
                    cache_read, cache_write_5m, cache_write_1h, messages)
                   VALUES (:host, :day, :session_id, :is_sidechain, :model, :project, :input, :output,
                           :cache_read, :cache_write_5m, :cache_write_1h, :messages)
                   ON CONFLICT(host, day, session_id, is_sidechain, model) DO UPDATE SET
                       project=excluded.project,
                       input=excluded.input, output=excluded.output,
                       cache_read=excluded.cache_read,
                       cache_write_5m=excluded.cache_write_5m,
                       cache_write_1h=excluded.cache_write_1h,
                       messages=excluded.messages""",
                rows,
            )
            self._conn.commit()
            return len(rows)

    def upsert_remote_productivity_buckets(self, rows: Iterable[dict]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                """INSERT INTO remote_productivity_buckets
                   (host, day, repo, commits, lines_added, lines_removed, files_changed)
                   VALUES (:host, :day, :repo, :commits, :lines_added, :lines_removed, :files_changed)
                   ON CONFLICT(host, day, repo) DO UPDATE SET
                       commits=excluded.commits, lines_added=excluded.lines_added,
                       lines_removed=excluded.lines_removed, files_changed=excluded.files_changed""",
                rows,
            )
            self._conn.commit()
            return len(rows)

    def remote_tool_by_tool(self, since_day: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                """SELECT tool, COALESCE(SUM(calls),0) AS calls, COALESCE(SUM(errors),0) AS errors
                   FROM remote_tool_buckets WHERE day >= ? GROUP BY tool""",
                (since_day,),
            ).fetchall()

    def remote_error_top_kinds(self, since_day: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                """SELECT kind, tool, COALESCE(SUM(count),0) AS count, MAX(last_seen) AS last_seen
                   FROM remote_error_buckets WHERE day >= ? GROUP BY kind, tool""",
                (since_day,),
            ).fetchall()

    def remote_error_example(self, kind: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                """SELECT example, last_seen FROM remote_error_examples
                   WHERE kind = ? ORDER BY last_seen DESC LIMIT 1""",
                (kind,),
            ).fetchone()

    def remote_trouble_files(self, since_day: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                """SELECT path, tool, COALESCE(SUM(errors),0) AS errors
                   FROM remote_trouble_file_buckets WHERE day >= ? GROUP BY path, tool""",
                (since_day,),
            ).fetchall()

    def remote_api_errors(self, since_day: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                """SELECT kind, COALESCE(SUM(count),0) AS count, MAX(last_seen) AS last_seen
                   FROM remote_api_error_buckets WHERE day >= ? GROUP BY kind""",
                (since_day,),
            ).fetchall()

    def remote_session_buckets_grouped(self, since_day: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                """SELECT host, session_id, is_sidechain, model, MAX(project) AS project,
                        COALESCE(SUM(input),0) AS input, COALESCE(SUM(output),0) AS output,
                        COALESCE(SUM(cache_read),0) AS cache_read,
                        COALESCE(SUM(cache_write_5m),0) AS cache_write_5m,
                        COALESCE(SUM(cache_write_1h),0) AS cache_write_1h,
                        COALESCE(SUM(messages),0) AS messages
                   FROM remote_session_buckets WHERE day >= ?
                   GROUP BY host, session_id, is_sidechain, model""",
                (since_day,),
            ).fetchall()

    def remote_productivity_grouped(self, since_day: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                """SELECT repo, COALESCE(SUM(commits),0) AS commits,
                        COALESCE(SUM(lines_added),0) AS lines_added,
                        COALESCE(SUM(lines_removed),0) AS lines_removed,
                        COALESCE(SUM(files_changed),0) AS files_changed
                   FROM remote_productivity_buckets WHERE day >= ? GROUP BY repo""",
                (since_day,),
            ).fetchall()

    # -- events feed --------------------------------------------------------
    def add_event(self, ts: str, kind: str, severity: str, text: str, ref: str | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events(ts, kind, severity, text, ref) VALUES (?, ?, ?, ?, ?)",
                (ts, kind, severity, text, ref),
            )
            self._conn.commit()

    def recent_events(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()

    def vacuum_old_events(self, days: int = 30) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM events WHERE ts < datetime('now', ?)", (f"-{days} days",)
            )
            self._conn.commit()
            self._conn.execute("VACUUM")

    # -- agent status history ------------------------------------------------
    def add_agent_status(self, ts: str, agent_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO agent_status_history(ts, agent_id, status) VALUES (?, ?, ?)",
                (ts, agent_id, status),
            )
            self._conn.commit()

    def agent_status_history(self, since_iso: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM agent_status_history WHERE ts >= ? ORDER BY ts ASC", (since_iso,)
            ).fetchall()

    # -- bead history ---------------------------------------------------------
    def add_bead_status(self, ts: str, bead_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO bead_history(ts, bead_id, status) VALUES (?, ?, ?)",
                (ts, bead_id, status),
            )
            self._conn.commit()

    # -- Kimi (second provider) turn/error ingestion -------------------------
    def insert_kimi_turn_events(self, rows: Iterable[dict]) -> int:
        rows = [{**r, "host": r.get("host") or "localhost"} for r in rows]
        if not rows:
            return 0
        with self._lock:
            cur = self._conn.executemany(
                """INSERT OR IGNORE INTO kimi_turn_events
                   (host, session_id, ts, ts_ms, model, tokens, project)
                   VALUES (:host, :session_id, :ts, :ts_ms, :model, :tokens, :project)""",
                rows,
            )
            self._conn.commit()
            return cur.rowcount

    def insert_kimi_error_events(self, rows: Iterable[dict]) -> int:
        rows = [{**r, "host": r.get("host") or "localhost"} for r in rows]
        if not rows:
            return 0
        with self._lock:
            cur = self._conn.executemany(
                """INSERT OR IGNORE INTO kimi_error_events
                   (host, session_id, ts, ts_ms, kind, code, example)
                   VALUES (:host, :session_id, :ts, :ts_ms, :kind, :code, :example)""",
                rows,
            )
            self._conn.commit()
            return cur.rowcount

    def kimi_usage_totals(self, since_iso: str | None = None, host: str | None = None) -> dict:
        clauses = []
        params: list = []
        if since_iso:
            clauses.append("ts >= ?")
            params.append(since_iso)
        if host:
            clauses.append("host = ?")
            params.append(host)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            row = self._conn.execute(
                f"""SELECT COALESCE(SUM(tokens),0) AS tokens, COUNT(*) AS messages
                    FROM kimi_turn_events {where}""",  # noqa: S608 -- where built from fixed clauses only
                params,
            ).fetchone()
        return {"tokens": row["tokens"], "messages": row["messages"]}

    def kimi_usage_by_session(
        self, since_iso: str | None = None, host: str | None = None,
    ) -> list[sqlite3.Row]:
        clauses = []
        params: list = []
        if since_iso:
            clauses.append("ts >= ?")
            params.append(since_iso)
        if host:
            clauses.append("host = ?")
            params.append(host)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            return self._conn.execute(
                f"""SELECT session_id, COALESCE(SUM(tokens),0) AS tokens, COUNT(*) AS messages,
                        MAX(ts) AS last_ts
                    FROM kimi_turn_events {where} GROUP BY session_id""",  # noqa: S608
                params,
            ).fetchall()

    def kimi_error_counts(self, since_iso: str | None = None, host: str | None = None) -> list[sqlite3.Row]:
        clauses = []
        params: list = []
        if since_iso:
            clauses.append("ts >= ?")
            params.append(since_iso)
        if host:
            clauses.append("host = ?")
            params.append(host)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            return self._conn.execute(
                f"""SELECT kind, COUNT(*) AS count, MAX(ts) AS last_seen
                    FROM kimi_error_events {where} GROUP BY kind""",  # noqa: S608
                params,
            ).fetchall()

    # -- Kimi remote bucket upserts + rollup queries --------------------------
    def upsert_remote_kimi_usage_buckets(self, rows: Iterable[dict]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                """INSERT INTO remote_kimi_usage_buckets (host, day, model, tokens, turns)
                   VALUES (:host, :day, :model, :tokens, :turns)
                   ON CONFLICT(host, day, model) DO UPDATE SET
                       tokens=excluded.tokens, turns=excluded.turns""",
                rows,
            )
            self._conn.commit()
            return len(rows)

    def upsert_remote_kimi_error_buckets(self, rows: Iterable[dict]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                """INSERT INTO remote_kimi_error_buckets (host, day, kind, count, last_seen)
                   VALUES (:host, :day, :kind, :count, :last_seen)
                   ON CONFLICT(host, day, kind) DO UPDATE SET
                       count=excluded.count, last_seen=excluded.last_seen""",
                rows,
            )
            self._conn.commit()
            return len(rows)

    def remote_kimi_usage_totals(self, since_iso: str | None = None) -> dict:
        where = "WHERE day >= ?" if since_iso else ""
        params = [since_iso[:10]] if since_iso else []
        with self._lock:
            row = self._conn.execute(
                f"""SELECT COALESCE(SUM(tokens),0) AS tokens, COALESCE(SUM(turns),0) AS messages
                    FROM remote_kimi_usage_buckets {where}""",  # noqa: S608
                params,
            ).fetchone()
        return {"tokens": row["tokens"], "messages": row["messages"]}

    def remote_kimi_error_counts(self, since_day: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                """SELECT kind, COALESCE(SUM(count),0) AS count, MAX(last_seen) AS last_seen
                   FROM remote_kimi_error_buckets WHERE day >= ? GROUP BY kind""",
                (since_day,),
            ).fetchall()

    # -- AI-provider quota cache (critdash/quota.py) -------------------------
    def get_quota_cache(self) -> dict[str, dict]:
        with self._lock:
            rows = self._conn.execute("SELECT provider, payload FROM quota_cache").fetchall()
        out: dict[str, dict] = {}
        for r in rows:
            try:
                out[r["provider"]] = json.loads(r["payload"])
            except json.JSONDecodeError:
                continue
        return out

    def set_quota_cache(self, provider: str, payload: dict) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO quota_cache (provider, payload, checked_at) VALUES (?, ?, ?)
                   ON CONFLICT(provider) DO UPDATE SET
                       payload=excluded.payload, checked_at=excluded.checked_at""",
                (provider, json.dumps(payload), str(payload.get("checked_at") or "")),
            )
            self._conn.commit()


# -- timeline zero-fill ------------------------------------------------------
# usage_timeline_hourly/daily only return buckets that have data, which draws
# a chart with gaps compressed away instead of shown flat. These turn a sparse
# row set into a dense, contiguous series of exactly `count` buckets ending at
# the bucket containing `end` (inclusive), with every missing bucket filled in
# as an explicit zero row -- real idleness rendered as a flat zero, not gone.


def _zero_fill_row(key: str, row: sqlite3.Row | None) -> dict:
    if row is None:
        return {
            "t": key, "input": 0, "output": 0, "cache_read": 0,
            "cache_write": 0, "cost_usd": 0.0, "messages": 0,
        }
    return {
        "t": key, "input": row["input"], "output": row["output"],
        "cache_read": row["cache_read"], "cache_write": row["cache_write"],
        "cost_usd": round(row["cost_usd"], 6), "messages": row["messages"],
    }


def dense_bucket_keys(end: datetime, bucket: str, count: int, tz_name: str = "UTC") -> list[str]:
    """Exactly `count` contiguous bucket keys ending at the bucket containing
    `end` (inclusive), in the same string format the *_grouped_timeline and
    usage_timeline_hourly/daily 'bucket' columns use ('YYYY-MM-DD' for day,
    'YYYY-MM-DDTHH:00:00Z' for hour). Shared by zero_fill_hourly/daily below
    and by history.py's grouped (group_by=model/provider/host) response, so
    the flat and grouped paths can never disagree on bucket boundaries.

    `tz_name` only affects the day-bucket case: `end` (always UTC-aware) is
    converted to that timezone's local calendar date before counting
    backward, so a user in UTC-6 gets day boundaries that fall at their own
    midnight, not UTC's. Day arithmetic is done on plain `date` objects
    (never `timedelta` against an aware datetime) so it can never double- or
    skip a day across a DST transition -- a calendar day is always exactly
    one date step, never a fixed 24h span. tz_name="UTC" (the default)
    reproduces the exact pre-existing UTC-only behavior. Hour buckets never
    need a timezone -- an hour boundary is the same instant everywhere."""
    if bucket == "day":
        end_local_date = end.astimezone(safe_zoneinfo(tz_name)).date()
        return [(end_local_date - timedelta(days=i)).isoformat() for i in range(count - 1, -1, -1)]
    end_b = end.replace(minute=0, second=0, microsecond=0)
    return [(end_b - timedelta(hours=i)).strftime("%Y-%m-%dT%H:00:00Z") for i in range(count - 1, -1, -1)]


def zero_fill_hourly(rows: Iterable[sqlite3.Row], end: datetime, count: int) -> list[dict]:
    """Return exactly `count` contiguous hourly buckets ending at the hour
    containing `end` (inclusive), formatted like usage_timeline_hourly's
    'bucket' column, zero-filled for any hour `rows` has no data for."""
    by_bucket = {row["bucket"]: row for row in rows}
    return [_zero_fill_row(key, by_bucket.get(key)) for key in dense_bucket_keys(end, "hour", count)]


def zero_fill_daily(
    rows: Iterable[sqlite3.Row], end: datetime, count: int, tz_name: str = "UTC"
) -> list[dict]:
    """Same as zero_fill_hourly but for day buckets ('YYYY-MM-DD'), matching
    usage_timeline_daily/usage_timeline_daily_tz's 'bucket' column. `rows`
    must already be bucketed in the same timezone as `tz_name` (see
    Store.usage_timeline_daily_tz) -- this function only decides which
    `count` local-day keys to render and zero-fills the gaps, it does not
    itself re-bucket anything."""
    by_bucket = {row["bucket"]: row for row in rows}
    return [
        _zero_fill_row(key, by_bucket.get(key)) for key in dense_bucket_keys(end, "day", count, tz_name)
    ]
