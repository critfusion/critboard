# CritBoard — build contract

Mission control for a fleet of coding agents. Runs on whatever machine you install it on,
served at http://<host>:9999/ (127.0.0.1 by default — see config/sources.json:bind_host).

## Hard constraints

1. **Zero token cost at runtime.** The dashboard makes NO LLM API calls, ever. It is pure
   subprocess + file parsing + SQLite. No `claude`, no `anthropic`, no network to any model API.
2. **Read-only by default.** Collectors never mutate beads, git state, or agent panes.
   Any write action goes behind an explicit opt-in flag. The one exception is the bead-reply
   feature (`POST /api/bead/{id}/reply` — see below): off by default (`bead_reply.enabled: false`
   in `config/sources.json`), and even when enabled it only ever runs when a person clicks
   Send back/Close in the bead popup — nothing in this codebase calls it on its own.
3. **Layout is data, not code.** Panels are declared in `config/layout.json`. Changing the
   dashboard shape must require editing JSON only — no JS edits, no rebuild.
4. **Degrade gracefully.** Any collector that fails marks itself `stale` with an error string
   and the rest of the dashboard keeps working. One dead source never blanks the page.

## Layout

```
critdash/
  server/            # Python 3.13, FastAPI + uvicorn, managed by uv
    critdash/
      __init__.py
      main.py        # FastAPI app, static mount, SSE
      state.py       # Snapshot dataclasses + in-memory store
      store.py       # SQLite history (timeseries, events)
      config.py      # loads config/*.json, env overrides
      collectors/
        __init__.py  # BaseCollector, registry, scheduler
        beads.py
        agents.py
        worktrees.py
        usage.py
        dispatch.py
        system.py
    tests/
    pyproject.toml
  web/               # static, no build step. ES modules + plain CSS.
    index.html
    css/
    js/
      app.js         # boot, SSE client, state store
      registry.js    # widget-type -> render fn
      widgets/*.js   # one file per widget type
  config/
    layout.json      # grid + panel declarations  (USER-EDITABLE)
    theme.json       # colors, fonts, density      (USER-EDITABLE)
    sources.json     # paths, intervals, repo roots (USER-EDITABLE)
  deploy/
    critdash.service
  README.md
```

## Data sources (ground truth, already verified on this host)

| key | command / path | interval |
|---|---|---|
| beads | `. ~/.config/beads/env; bd list --json`, `bd stats --json`, `bd ready --json` | 30s |
| agents | `herdr agent list` (JSON on stdout, one line) | 5s |
| worktrees | `git -C <dir>` over dirs in `sources.json:repo_roots` | 60s |
| usage | `~/.claude/projects/*/*.jsonl`, incremental by byte offset | 10s |
| dispatch | `~/.overlord/fleet-dispatch.log`, `~/.overlord/routes.conf`, `~/.overlord/pause/` | 30s |
| system | /proc loadavg, meminfo, `df` on / and /srv | 15s |

Notes on real shapes observed on this host:

- `herdr agent list` prints ONE line of JSON:
  `{"id":"cli:agent:list","result":{"agents":[{...}],"type":"agent_list"}}`
  Each agent: `agent` (claude|codex|grok|…), `agent_status` (idle|working|done),
  `cwd`, `foreground_cwd`, `pane_id`, `tab_id`, `workspace_id`, `terminal_title`,
  `focused` (bool), `agent_session.value` (= the Claude sessionId, joins to jsonl).
- Session jsonl lines are one JSON object per line with top-level
  `type` (user|assistant|system), `timestamp` (ISO8601 Z), `sessionId`, `cwd`, `gitBranch`,
  `version`, `isSidechain` (true = subagent), and for assistant lines
  `message.model` and `message.usage` with
  `input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens`,
  `cache_creation.{ephemeral_1h_input_tokens,ephemeral_5m_input_tokens}`,
  `server_tool_use.{web_search_requests,web_fetch_requests}`.
  **`message.model` can be the literal `<synthetic>` with all-zero usage — skip those rows.**
- beads env lives at `~/.config/beads/env`; source it and set `BEADS_ACTOR=critdash`
  before every `bd` call. `bd` is configured via `config/sources.json:bd_bin`
  (default `~/.local/bin/bd`).
- Repo roots to scan come from `config/sources.json:repo_roots` (default `~/repos`,
  `~/src`, `~/work` — see `config/sources.example.json`).

## API contract (frozen — both sides code against this)

### `GET /api/snapshot` -> `Snapshot`

```jsonc
{
  "generated_at": "2026-09-18T13:00:00Z",
  "host": "localhost",
  "uptime_s": 1234,
  "sources": {                        // one entry per collector
    "beads":   {"ok": true,  "last_ok": "…", "last_run": "…", "duration_ms": 41, "error": null, "stale": false},
    "agents":  {"ok": false, "last_ok": "…", "last_run": "…", "duration_ms": 12, "error": "herdr: exit 1", "stale": true}
  },
  "beads": {
    "stats": {"open": 12, "in_progress": 3, "blocked": 2, "closed_today": 5, "ready": 7},
    "items": [{
      "id": "demo-fleet-78jr.10",
      "title": "…",
      "status": "in_progress",        // open|in_progress|blocked|closed
      "priority": 2,
      "type": "task",                 // task|bug|feature|epic|chore
      "assignee": "localhost-claude",
      "labels": ["needs-codex"],
      "created_at": "…", "updated_at": "…", "closed_at": null,
      "age_s": 86400,
      "blocked_by": ["demo-fleet-x2l4"],
      "blocks": [],
      "parent": "demo-fleet-78jr",
      "repo": "demo-web",   // best-effort, from labels/title/assignee
      "url": null
    }],
    "lanes": {"ready": ["id"], "in_progress": ["id"], "blocked": ["id"], "review": ["id"]}
  },
  "agents": [{
    "id": "ac025f78-…",               // agent_session.value, stable key
    "kind": "claude",
    "status": "working",              // working|idle|done|unknown
    "cwd": "/home/user/work/dashboard",
    "repo": "dashboard",
    "branch": "main",                 // joined from worktrees by cwd
    "pane": "w5:p1", "workspace": "w5",
    "title": "CritBoard",
    "focused": true,
    "session_id": "ac025f78-…",
    // "bead"/"bead_title": derived from THIS session's own transcript (the
    // exact bd commands it ran, tokenized respecting shell quoting, and
    // their results), never from actor name (one actor claims for many
    // sessions) and never a guess -- and never inferred from a command's
    // OUTPUT text, only from literal id arguments in the command itself.
    // Only ever set for a kind in bead_sessions.BEAD_TRACKED_KINDS
    // ("claude", "kimi" today). A session may hold several unreleased
    // claims at once; "bead" is the most recent of them that is CURRENTLY
    // in_progress (not simply the most recent claim outright) -- null when
    // none of them are. See critdash/collectors/bead_sessions.py for the
    // extraction rules and critdash/collectors/agents.py's
    // _apply_bead_cross_check for the in_progress selection and
    // cross-session dedupe.
    "bead": "demo-fleet-xxxx",
    "bead_title": "…",                // null when bead is null
    // "bead_tracked": true when this agent's `kind` has a transcript
    // extractor at all (independent of whether `bead` itself is null right
    // now) -- the UI uses this to say "bead not tracked for <kind>"
    // instead of the misleading "no active bead" for a kind (codex, grok,
    // cursor, …) this dashboard doesn't parse transcripts for yet.
    "bead_tracked": true,
    "last_activity": "2026-09-18T12:59:00Z",   // from jsonl tail
    "status_since": "2026-09-18T12:40:00Z",
    "tokens_today": {"input": 1, "output": 2, "cache_read": 3, "cache_write": 4, "total": 10},
    "cost_today_usd": 1.23,
    "msg_count_today": 42,
    "subagents_active": 2,
    "model": "claude-opus-5"
  }],
  "worktrees": [{
    "path": "/home/user/repos/demo-api",
    "repo": "demo-api",
    "root": "/home/user/repos",
    "branch": "feat/x", "head": "abc1234",
    "dirty": 3, "untracked": 1, "staged": 0,
    "ahead": 2, "behind": 0,
    "upstream": "origin/main",
    "last_commit_at": "…", "last_commit_msg": "…", "last_commit_author": "…",
    "agents": ["ac025f78-…"],          // agent ids whose cwd is inside
    "stale_days": 4
  }],
  "usage": {
    "totals": {                        // rolling windows
      "today":  {"input":0,"output":0,"cache_read":0,"cache_write":0,"total":0,"cost_usd":0.0,"messages":0},
      "7d":     {...}, "30d": {...}, "all": {...}
    },
    "by_model":   [{"model":"claude-opus-5","window":"today","input":0,"output":0,"cache_read":0,"cache_write":0,"cost_usd":0.0,"messages":0}],
    "by_project": [{"project":"demo-web","window":"today","total":0,"cost_usd":0.0,"messages":0}],
    "by_agent":   [{"agent_id":"…","window":"today","total":0,"cost_usd":0.0}],
    "timeline":   [{"t":"2026-09-18T12:00:00Z","input":0,"output":0,"cache_read":0,"cache_write":0,"cost_usd":0.0}], // hourly, last 48h
    "burn": {"usd_per_hour_1h": 0.0, "usd_per_hour_24h": 0.0, "projected_month_usd": 0.0, "tokens_per_min_5m": 0.0},
    "cache_hit_ratio_today": 0.87
  },
  "dispatch": {
    "routes": [{"label":"needs-codex","kind":"codex","paused":false,"precheck":null}],
    "paused_all": false,
    "recent": [{"t":"…","line":"woke codex for demo-fleet-abc"}]   // last 50
  },
  "system": {
    "load1":0.4,"load5":0.5,"load15":0.6,"cpu_count":16,
    "mem_used_gb":12.1,"mem_total_gb":64.0,
    "disks":[{"mount":"/","used_gb":100,"total_gb":500,"pct":20}]
  },
  "events": [{"t":"…","kind":"bead_status","severity":"info","text":"…","ref":"demo-fleet-abc"}]
}
```

`events` is the unified activity feed. `kind` in:
`bead_created|bead_status|bead_closed|bead_claimed|agent_status|commit|dispatch|alert`.
`severity` in `info|warn|crit`.

### `GET /api/stream` — Server-Sent Events

- `event: snapshot` — full `Snapshot` JSON, sent once on connect and every 60s as a resync.
- `event: patch`   — `{"paths": {"agents": [...], "usage.burn": {...}}}` — a shallow map of
  dotted top-level keys to their new value. Client merges by replacing at that path.
- `event: ping`    — `{}` every 15s so proxies keep the connection open.

### Other endpoints

- `GET /api/history/usage?window=24h|7d|30d&bucket=hour|day` -> `[{t, input, output, cache_read, cache_write, cost_usd, messages}]`
- `GET /api/history/agents?window=24h` -> `[{t, agent_id, status}]` status transitions
- `GET /api/config/layout` / `GET /api/config/theme` -> the JSON files, live-reloaded from disk
- `POST /api/config/layout` -> writes `config/layout.json` (validated) so the UI can save layout edits
- `GET /api/healthz` -> `{"ok":true,"collectors":{...}}`
- `GET /api/version` -> `{"build":"455b0242","started_at":"…","commit":"837ff64…"|null,"branch":"main"|null,"dirty":true|false|null}`.
  `build` is a content hash of `web/`+`config/` (existing "new version, reload" banner signal, unrelated to git).
  `commit`/`branch`/`dirty` are this checkout's git identity, all `null` outside a git checkout (e.g. a tarball
  install) -- never an error for that case.

### Bead reply — send back / close (off by default)

Lets the bead popup add a reply comment to a human-labelled bead and either **send it back** (wake
an agent) or **close it**. Off by default: `config/sources.json`'s `bead_reply.enabled` (default
`false`) gates both endpoints below with a `403 {"detail": {"reason": "bead_reply_disabled", ...}}`
— when disabled, neither endpoint ever runs a `bd` command. The write endpoint additionally
requires `allow_config_writes` (same flag POST `/api/config/layout|theme` use) — same reasoning:
a dashboard bound beyond 127.0.0.1 must be able to be made fully read-only. `GET /api/snapshot`'s
`settings.bead_reply_enabled` tells the frontend whether to show the reply UI at all.

Config shape (`config/sources.json`, see `config/sources.example.json`'s `_readme` for the prose):

```json
"bead_reply": {
  "enabled": false,
  "actor": "",
  "routes": [["claude", "needs-claude"], ["codex", "needs-codex"], ["grok", "needs-grok"]],
  "default_route": "needs-claude"
}
```

`actor` is the `BEADS_ACTOR` a write runs as (empty → first entry of `config/layout.json`'s
`human_labels`, else `"critboard-human"`). `routes` is an ordered `[substring, route_label]` list
matched case-insensitively against a bead's `created_by`, first match wins; `default_route` is used
when nothing matches — this is how SEND BACK decides which `needs-*` label to add for a fleet's own
actor-naming convention (this repo ships no fleet-specific defaults).

- `GET /api/bead/{id}/comments` -> `200 {"comments": [{"id","author","text","created_at"}], "status": "open", "labels": [...], "human_labels_present": [...], "route_preview": "needs-claude"}`.
  `human_labels_present` is the subset of `config/layout.json`'s `human_labels` that this bead
  currently carries (empty means "not human-owned" — the frontend hides the reply form).
  `route_preview` is the label SEND BACK would add right now, computed server-side so the frontend
  never duplicates the routing logic. `400` on a malformed bead id, `403` while disabled.

- `POST /api/bead/{id}/reply` — body `{"action": "send_back"|"close", "text": "..."}`.
  Before acting, the bead is always re-fetched fresh via `bd show` (never the cached snapshot) and
  the write is refused with `409 {"detail": {"reason": "not_actionable", ...}}` unless it is
  currently `open` and carries at least one human label — this stops a stale popup from
  reopening/relabelling a bead an agent already took.
  - `send_back`: `text` is required (400 if empty after stripping). Adds `text` as a comment, then
    removes every human label present, adds the chosen route label, clears the assignee, and sets
    status to `open` — all in one `bd update` call. Returns
    `{"action":"send_back","route":"needs-claude","removed_labels":[...],"assignee_cleared":true,"status":"open"}`.
  - `close`: `text` is optional — added as a comment first if non-empty, then `bd close -r <text or
    a fixed default reason>`. Returns
    `{"action":"close","comment_added":true|false,"reason":"...","status":"closed"}`.
  - `400` on a malformed bead id, empty `text` on `send_back`, or an unknown `action`. `403` while
    disabled or while `allow_config_writes` is false. `409` per the not-open/no-human-label rule
    above. `502` if the underlying `bd` command fails.
  - Security: the reply text, bead id, and every label are validated (id/label against a strict
    `[A-Za-z0-9_.-]` pattern) and travel to `bd` as an argv list via
    `asyncio.create_subprocess_exec` (`server/critdash/collectors/beads.py`'s `bd_exec_argv`/
    `bd_exec_env`/`run_bd_exec`) — never interpolated into a shell string, unlike the read-only
    collector's `bd_shell_prefix` path. On success, triggers an immediate re-collect of the beads
    collector so the card updates without waiting for its normal 30s tick.

- `GET`/`POST /api/settings/bead-reply` — the gear-menu Settings panel's toggle for
  `bead_reply.enabled`, so turning this on/off never requires hand-editing `config/sources.json`.
  `GET` -> `200 {"enabled": true|false, "actor": "...", "default_route": "...", "beads_configured":
  true|false}`. `actor`/`default_route` are the EFFECTIVE values (`bead_reply.resolve_config` /
  `resolve_actor`), reported read-only — `routes`/`actor`/`default_route` stay hand-edit-JSON-only
  (this is a public repo; a fork's route labels and actor name are site-specific). `beads_configured`
  is `false` when `bd` isn't installed or no workspace is set up on this host, so the panel can say so
  instead of silently no-opping. `POST` accepts only `{"enabled": bool}` — any other key, or a
  non-boolean `enabled`, is a `400`. The write is a nested merge into the `bead_reply` object (never
  `doc["bead_reply"] = {...}`), so an existing `actor`/`routes`/`default_route` survive a toggle even
  when `bead_reply` was previously absent from `config/sources.json` entirely. `403` while
  `allow_config_writes` is false, same as the config POSTs above. Mutates `config.sources` in place on
  success, so `GET /api/bead/{id}/comments`, `POST /api/bead/{id}/reply`, and `GET /api/snapshot`'s
  `settings.bead_reply_enabled` all see the change on the very next request, no restart.

### Self-update (briefing Task 4 — frontend owns the UI, backend owns these two endpoints)

Off by default: `POST /api/update/apply` always 403s with `reason: "self_update_disabled"` unless
`"allow_self_update": true` is set in `config/sources.json`. Nothing in the backend calls either endpoint on a
timer or on startup -- both are meant to be triggered by a person, `check` at most on a long interval.

- `GET /api/update/check` -> `200 {"current": "<sha>"|null, "latest": "<sha>"|null, "behind": <int>|null,
  "relation": "behind"|"ahead"|"identical"|"diverged"|"unknown", "checked_at": "…", "update_available": true|false}`.
  `current` is this checkout's HEAD (`null` outside a git checkout, in which case `update_available` is always
  `false`). `latest` is the tip of `update_repo`'s `update_branch` on GitHub (defaults `critfusion/critboard` /
  `main`, both configurable in `config/sources.json`). `relation` is this checkout's relation to `latest`, from
  GitHub's compare API (`compare/{current}...{latest}`, `status` field); `update_available` is `true` **only**
  when `relation` is `"behind"` (this checkout is strictly behind `latest`). Being ahead of GitHub (an unpushed
  local commit), identical, or diverged is never an "update" -- see the self-restart-loop incident this fixes: a
  local commit not yet pushed used to register as "update available" and every auto-apply cycle re-pulled
  nothing and restarted anyway. `relation` is `"unknown"` (and `update_available` false) whenever the compare
  can't be resolved, including a 404 -- notably when `current` only exists locally and was never pushed, GitHub's
  compare API 404s on it, and a checkout carrying a commit GitHub doesn't know about can never be proven
  strictly behind. `behind` is the commit count `current` is behind `latest` (GitHub's `ahead_by`; `null` if
  `relation` is `"unknown"`, which does not block reporting `current`/`latest`). The result is cached server-side
  for `update_check_min_interval_s` (default 300s) -- calling this endpoint often is safe, but the frontend
  should still poll it rarely, not on every snapshot tick. Each successful check also updates the same state
  `/api/snapshot`'s `update` object (and so the banner) reads, so a manual check right after a push clears the
  banner immediately instead of waiting for the next periodic tick. On a GitHub API failure, returns `502
  {"detail": {"reason": "github_error"|"github_unreachable"|"github_bad_response", "message": "…"}}`, never a bare 500.
- `POST /api/update/apply` -> on success, `200 {"applied": true, "commit": "<new sha>", "reinstalled": true|false, "restart_requested": true|false, "restart_method": "systemd"|"self"|"none", "restart_hint": "<command>"|"", "applied_at": "…"}`.
  Performs `git fetch` + `git pull --ff-only` from the checkout's own `origin` remote (refuses if it doesn't
  point at `github.com/<update_repo>`). If the pull moves `HEAD` nowhere (checkout was already up to date --
  e.g. `update_available` was a false positive, or two triggers raced), it returns `200 {"applied": false,
  "commit": "<unchanged sha>", "reinstalled": false, "restart_requested": false, "restart_method": "none",
  "restart_hint": "", "applied_at": "…", "reason": "already_up_to_date", "message": "already up to date --
  nothing to apply"}` -- no reinstall, no restart. Otherwise it reinstalls dependencies only if `server/uv.lock`
  changed, then restarts whatever is actually supervising this process and returns immediately (the response is
  sent before the restart kills the process): a systemd --user unit if one is detected (by THIS process's own
  cgroup/`INVOCATION_ID`, never a hardcoded unit name -- `restart_method: "systemd"`), else the PID-file process
  install.sh `--start` manages -- the only mechanism on a host with no systemd, e.g. macOS -- restarted via a
  detached helper (`restart_method: "self"`), else nothing (`restart_method: "none"`). `restart_requested` is
  `true` only when a restart actually fired; `restart_method: "none"` means the pulled code is on disk but this
  process is still running the OLD code, and `restart_hint` is the command to restart it manually (e.g.
  `./install.sh --start`, or `systemctl --user restart <unit>` if systemd was detected but the restart itself
  failed). Every refusal is a specific 4xx/5xx with `{"detail": {"reason": "<code>",
  "message": "<human string>"}}`, never a generic 500 -- reason codes: `self_update_disabled` (403),
  `not_a_git_checkout` (409), `dirty_working_tree` (409), `no_origin_remote` (409), `origin_mismatch` (403),
  `fetch_failed` (502), `not_fast_forward` (409), `reinstall_failed` (500). The frontend should surface
  `message` verbatim -- it is already the human-readable explanation, not a code to re-translate. The periodic
  auto-apply path (`update_auto_apply`) only ever calls this when its own check's `update_available` is true --
  i.e. only when strictly behind, never when merely diverged or ahead.

## Cost model

`config/pricing.json` is AUTHORITATIVE and already written — read it, do not invent rates.
Verified 2026-09-18 against the bundled `claude-api` skill model table.

Structure: `models.<model-id-prefix>` -> `{input, output, cache_write_5m, cache_write_1h, cache_read}`
in USD per 1,000,000 tokens. Match `message.model` by LONGEST PREFIX; fall back to `models.default`.
`fast_mode.<model>` overrides the rate when `message.usage.speed == "fast"` (Opus 5 / 4.8 fast mode
bills at Fable-tier rates). `monthly_budget_usd` drives the budget progress bar.

Cost per message:

```
input_tokens             / 1e6 * input
output_tokens            / 1e6 * output
cache_read_input_tokens  / 1e6 * cache_read
cache_creation.ephemeral_5m_input_tokens / 1e6 * cache_write_5m
cache_creation.ephemeral_1h_input_tokens / 1e6 * cache_write_1h
```

**The 5m and 1h cache-write rates differ by 1.6x — do not collapse them into one `cache_write`
bucket.** Only if `cache_creation` is absent or its two fields sum to zero while
`cache_creation_input_tokens > 0`, fall back to charging that total at the 5m rate.
`input_tokens` already EXCLUDES cached tokens — never double count.

Snapshot fields keep the flat names `cache_write` (= 5m + 1h tokens summed) for display, but the
SQLite `usage_events` table stores `cache_write_5m` and `cache_write_1h` separately so cost can be
recomputed if a rate changes.

## layout.json format (this is the extensibility surface)

```jsonc
{
  "version": 1,
  "title": "CritBoard",
  "grid": {"columns": 12, "row_height": 80, "gap": 14},
  "panels": [
    {
      "id": "fleet",
      "type": "agent_grid",         // must exist in web/js/registry.js
      "title": "FLEET",
      "x": 0, "y": 0, "w": 6, "h": 4,
      "options": {"show_idle": true, "sort": "status"}
    }
  ]
}
```

Widget types to implement in v1 (each a file in `web/js/widgets/`):

`agent_grid`, `bead_board`, `bead_table`, `spend_summary`, `spend_timeline`,
`model_split`, `worktree_table`, `activity_feed`, `system_gauges`, `source_health`,
`stat_row`, `burn_gauge`, `dispatch_routes`, `commit_feed`, `markdown_note`.

Unknown `type` renders a visible placeholder card naming the missing type — never a blank
or a crash.

## Quality bar

- Backend: pytest unit tests per collector against recorded fixture files (commit real
  captured samples under `server/tests/fixtures/`). Parser tests must cover the
  `<synthetic>` zero-usage row, a malformed jsonl line, and an empty file.
- Frontend: renders correctly with `sources.*.ok = false` for every collector, and at
  1280px and 1920px width. No horizontal scroll.
- `ruff` clean, no bare `except:`.
