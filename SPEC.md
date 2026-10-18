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
- Subagent transcripts live at `<projects>/<project>/<parent-session-id>/subagents/agent-<agentId>.jsonl`.
  Their lines carry `isSidechain: true`, `sessionId` = the PARENT session id, `agentId`,
  and `message.usage`. The usage collector ingests them (stored with `is_sidechain=1`
  and `agent_id`, deduped by message id like any transcript, so their cost counts in
  every total and in the parent session's `cost_today_usd`). They never create an
  agent/session record: the agents and analytics collectors glob `<projects>/*/*.jsonl`,
  which does not match them.
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
    // closed_today counts beads closed since local midnight (layout timezone).
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
    // ("claude", "kimi", "codex", "grok", "cursor" today). A session may
    // hold several unreleased claims at once; "bead" is the most recent of
    // them that is CURRENTLY in_progress (not simply the most recent claim
    // outright) -- null when none of them are. A bead bound to the agent's
    // herdr pane (herdr's per-pane metadata `tokens.bead`, written by the
    // `bd-claim <id>` / `bd-herdr claim <id>` wrappers) wins when that bead is
    // in_progress; otherwise the transcript decides. Those two wrappers also
    // count as claims in a transcript (exactly one literal id, same success
    // rules as `bd update <id> --claim`); `bd-herdr clear` is not a release.
    // For remote hosts only the bound bead id leaves the host. See
    // critdash/collectors/bead_sessions.py for the
    // extraction rules and critdash/collectors/agents.py's
    // _apply_bead_cross_check for the in_progress selection and
    // cross-session dedupe.
    "bead": "demo-fleet-xxxx",
    "bead_title": "…",                // null when bead is null
    // "bead_tracked": true when this agent's `kind` has a transcript
    // extractor at all (independent of whether `bead` itself is null right
    // now) -- the UI uses this to say "bead not tracked for <kind>"
    // instead of the misleading "no active bead" for a kind (opencode,
    // …) this dashboard doesn't parse transcripts for yet.
    "bead_tracked": true,
    "last_activity": "2026-09-18T12:59:00Z",   // from jsonl tail
    "status_since": "2026-09-18T12:40:00Z",
    "tokens_today": {"input": 1, "output": 2, "cache_read": 3, "cache_write": 4, "total": 10},
    "cost_today_usd": 1.23,
    "msg_count_today": 42,
    "subagents_active": 2,            // DISTINCT subagents (agentId) of this session with
                                      // a usage message in the last 5 minutes
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
    // "today" (totals.today, per-agent tokens_today/cost_today_usd/msg_count_today,
    // cache_hit_ratio_today, per-host tokens_today) starts at LOCAL midnight in
    // config/layout.json's "timezone" (IANA name; DST-safe; missing/invalid = UTC),
    // converted to the UTC instant the store compares against. Same for
    // GET /api/history/usage?window=today. 7d/30d are rolling; the budget's
    // month-to-date stays on the UTC calendar month.
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

### Opt-in modules: Infrastructure / Workflow / Knowledge / Context (all off by default)

A default install is the board with agents and beads, exactly as before. Extra views exist only
when `config/sources.json` (machine-local, never tracked) sets `modules.<name>.enabled` to the JSON
boolean `true`. `config/sources.example.json` ships every module `false`. Config is re-read per
request, so a change needs no restart.

- `GET /api/modules` -> `{"enabled": ["knowledge", ...]}`, in nav order (infrastructure, workflow,
  knowledge, context; `context` has no view). `[]` on a default install.
- UI: with `[]` there is no navigation at all and the page is unchanged. With one or more, a view bar
  `Live | <enabled views>` appears. The view is the `?view=<name>` query parameter (never the hash:
  the hash is the bead/agent deep link). Switching only toggles `hidden`; it never rebuilds the Live
  grid or closes the detail popup, so unsaved text survives. The TODAY/7D/30D filter belongs to Live
  and is hidden on other views. Each enabled view has its own panel; `workflow` is described below.
- `modules.knowledge` is a read-only window onto BookStack. Server-side only: the API token id and
  secret come from `bookstack.token_file` (one line `<id>:<secret>`, mode 600; refused if group or
  others can read it) and never reach the browser, a response body or a log. Redirects are not
  followed. No page content is cached: every response is a live fetch and carries `fetched_at`.
  - `GET /api/knowledge/status` -> probes BookStack. `{"ok":true,"status":{...}}` or an error body.
  - `GET /api/knowledge/search?q=` (max 200 chars) -> `{"results":[{id,name,book,updated_at,preview}]}`.
  - `GET /api/knowledge/page/{id}` -> `{id,name,book,text,updated_at,provenance:{tags,created_by,
    updated_by,revision_count},edit_url,open_url}`. `text` is plain text made from BookStack's HTML;
    page tags are the provenance block. No tags is shown as "origin unknown".
  - Failure body: `{"ok":false,"error":{"code","message","http_status"},"status":{"ok","last_success",
    "down_since","error"}}`. Codes: `unreachable`, `auth_failed` (BookStack 401/403), `upstream_error`
    -> HTTP 502. `not_found` -> 404 (not an outage). `not_configured`, `token_file_missing`,
    `token_file_permissions`, `token_file_invalid` -> 503 (operator setup, not an outage clock).
    `down_since` is the first failure since the last success; `last_success` is null until one works.
  - Module off -> all three answer 404. A request with an `Origin` host that differs from `Host` gets
    403 (CORS elsewhere is open; this keeps another site from reading wiki content).
  - Read-only: editing is the "Edit in BookStack" link to BookStack's own editor.
- `modules.infrastructure` is a read-only view of the pages in one BookStack book. Config:
  `{"enabled": true, "book_slug": "infrastructure", "stale_after_days": 90}` (`book_slug` default
  `infrastructure`; `stale_after_days` an integer 1 to 3650, anything else means 90). It reads BookStack
  through `modules.knowledge` (base_url, token_file, timeout_s, `allowed_hosts`, one status block), so
  `modules.knowledge` must be enabled and configured; if not, the endpoint answers `not_configured`
  (HTTP 503). Infrastructure on with Knowledge off does not open the Knowledge routes, and the reverse.
  - `GET /api/infrastructure` -> `{"ok":true,"book":{name,slug,url},"records":[...],"total","truncated",
    "stale_after_days","fetched_at","status":{...}}`. Same gate as `/api/knowledge/*` (module off -> 404;
    foreign Host or Origin -> 403 `host_not_allowed` / `origin_not_allowed`), same `ok:false` error shape
    and `status` block (`ok`, `last_success`, `down_since`, `error`). No token or token path in any body.
  - Reads: `GET /api/books?filter[slug]=`, `GET /api/pages?filter[book_id]=&count=50&sort=+name`, then
    `GET /api/pages/{id}` per page (5 at a time). At most 50 pages; `truncated` is true and `total`
    says how many exist when there are more. One overall deadline of 2 x `timeout_s` covers the whole
    read. Any failure (outage, auth, oversize page) fails the whole call: a partial list is never returned
    as the inventory. A page deleted between the listing and its read is skipped. No book with the slug, or a 404 on the book lookup or page listing ->
    `book_not_found` (503, setup problem, not an outage; also kept in `status.error` until the next good read). The shared `status` block records a success only
    when the whole read worked: a failed page is not cleared by a later page that answered. Nothing is cached.
  - Records are sorted by `kind`, then title. `kind` `unknown` sorts after the named kinds.

- `modules.workflow` is a read-only view of the written workflow beside the observed one. Config:
  `{"enabled": true, "policy_book_slug": "workflow-policy-projections", "stale_after_days": 90,
  "availability_ttl_s": 1800, "owner_reported": []}`. Policy pages come from BookStack through
  `modules.knowledge` (same adapter, limits and deadline as Infrastructure). Workflow on with Knowledge off
  does not open the Knowledge routes.
  - `GET /api/workflow` -> `{"ok":true,"policy":{...},"observed":{...},"assignments":{...},
    "availability":{...},"fetched_at","status":{...}}`. Same gate as `/api/infrastructure` (module off -> 404;
    foreign Host or Origin -> 403 `host_not_allowed` / `origin_not_allowed`, `ok:false` body). The four
    sections are built apart and never merged. A failure in `policy` (wiki off, down, no such book) is
    reported inside `policy` and does not change `ok` or the other sections. No call goes to any AI provider.
  - `policy`: `{state, label:"intended policy", book, pages[], total, truncated, stale_after_days, error}`.
    `state` is `ok`, or the error code (`not_configured`, `unreachable`, `auth_failed`, `book_not_found`, ...)
    with `pages` empty. Each page: `id, title, url, status, status_reason, status_conflict,
    source{repo, repo_href, path, git_sha}, last_verified, updated_at`. Fields and status rules are the
    Infrastructure record contract below (`unknown` / `needs-verification` pass through unchanged).
  - `observed`: `{label, snapshot_at, sources{agents,beads:{ok,stale}}, sessions[], cost_note}`. Each session:
    `id, role, runner, model, provider, account, host, coordinated, status, repo, bead, subagents_active,
    last_activity, cost_estimate_today_usd, cost_basis:"estimate"`. Each of `role`, `runner`, `model`,
    `provider`, `account`, `host`, `coordinated` is its own field and is the string `unknown` when the data
    does not state it; one unknown never changes another. `role` is read only from a session title that starts with
    `role:<name>` (orchestrator, coder, reviewer, worker) that ends at whitespace or at the end of the title
    (`role:coder-x` is `unknown`); a role word elsewhere in a title is not read. `provider` is read from the model id
    (`provider/model`, or a known family prefix). `account` and `coordinated` have no collector yet and are
    always `unknown`. `cost_estimate_today_usd` is priced from local token counts: an estimate, not spend,
    not remaining quota.
  - `assignments`: `{label, items[]}` for beads with status `in_progress`. Each item: `id, title, assignee,
    coder, coder_basis (label|assignee|none), reviewers[], approvers[], reviewed_refs[], needs_review,
    review_gate, gate_missing[], sessions[]`. People are read only from bead labels `coder:<name>`,
    `reviewer:<name>`, `approver:<name>`, `reviewed:<ref>`; `coder` falls back to the assignee. A reviewer
    is never taken from the assignee. `needs_review` is true when a label other than those contains
    `review` (for example `waiting-review`). `review_gate`: `no_review_marked` (no review label; this is
    not a claim that none is needed), `named` (a reviewer and an approver are named; the review itself is
    not verified), or `missing` (review needed and `gate_missing` lists `reviewer`, `approver` or
    `independent_reviewer` (a reviewer is the coder), `independent_approver` (an approver is the coder:
    self-approval is not allowed) or `coder_unknown`). The coder names are every `coder:` label value (several labels and comma lists all count) and the
    trimmed assignee (blank means none); a reviewer or approver equal to either is not independent. Label values may be comma
    separated (`reviewer:a,b`) and each name is checked. No coder name at all is `coder_unknown`.
    Bounds: each label value is split first; a name is at most 200 characters and each of coder, reviewer,
    approver and reviewed keeps at most 20 names. A dropped name (too many, or too long) adds
    `too_many_names` to `gate_missing` on a bead that needs review: the gate fails closed. Missing items sort first. The view treats
    any unknown gate value as missing.
  - `availability`: `{ttl_s, rows[], note}`. Each row: `provider, basis, state, values, observed_at,
    expires_at, expired, detail`. `expires_at = observed_at + ttl_s`. `basis` is `provider-observed` (the
    last manual quota check got an answer from the provider API), `owner-reported` (typed into
    `modules.workflow.owner_reported`), or `unknown` (never checked, check failed, no provider endpoint,
    no readable time, or a time in the future). At or after `expires_at` the row is `state:"stale"`,
    `expired:true`, `values:null`. The browser repeats the check against its own clock from `expires_at`
    when it renders, and every 60 seconds while the view is shown (the dimmed last-good copy too), so a
    reading that expires on an open page turns stale without a refetch. `values` (remaining, limit, used, unit, period, resets_at) exist only on
    a fresh provider-observed row. Rows come from the cached quota check (`/api/quota`); the view makes no
    provider call. `ttl_s` is `availability_ttl_s` (integer 60 to 86400, otherwise 1800).
  - UI: a `Workflow` tab with four panels (Intended policy, Observed runtime, Assignments, Availability).
    `REVIEW GATE MISSING` is a solid red badge with a thick left border and a count above the list; `STALE`
    and `UNKNOWN` are dashed. Every state is a word, not colour alone. Links are made only from http(s) URLs.

#### Context endpoint (`modules.context`, off by default, API only)

One small answer for an agent starting a session. It has no view and no nav entry. It reads the same
records as the UI: BookStack-curated pages (through `modules.knowledge`) and the bead collector. It
sends no request to any AI provider and writes nothing.

- Config: `modules.context` = `{"enabled": true, "projects_book_slug": "projects", "runbooks_book_slug":
  "runbooks", "max_bytes": 16384, "stale_after_days": 90, "cache_ttl_s": 60}`. The policy book is
  `modules.workflow.policy_book_slug` (default `workflow-policy-projections`); `modules.workflow` need not
  be enabled. `max_bytes` is an integer clamped to 8192 to 262144 (any other value: 16384). `cache_ttl_s`
  (integer 0 to 3600, default 60; 0 turns the cache off) is how long a successful read of each of the three
  books is reused. Only a success is cached: an error is read again on the next request. Within the TTL a
  page edit is not visible, and an outage is not seen; `revision.pages[].updated_at` shows the page version
  that was read, and `revision.fetched_at` is the time of the request, not of the read.
- `GET /api/context?project=<slug>`. Same gate as `/api/workflow`: module off -> 404; foreign Host or
  Origin -> 403 `host_not_allowed` / `origin_not_allowed`. Then `project` must match
  `^[a-z0-9][a-z0-9._-]{0,63}$`, else 400 `{"ok":false,"error":{"code":"invalid_project"}}`. `Cache-Control:
  no-store`. Nothing is cached.
- With Knowledge off or down the wiki-backed sections report `state: "not_configured"` or
  `"unavailable"` with `error: <code>` and nothing else; `ownership` and `availability` still answer. A
  fact the data does not state is the string `unknown`.
- Body: `{ok, project, entrypoint, standards, runbooks, ownership, availability, revision, degraded}`.
  Every section but `revision` and `degraded` carries `source` (`bookstack:<book slug>`, `beads`, `quota`).
  - `entrypoint`: `state` is `ok`, `no_match`, `not_configured` or `unavailable`. Match order: a page in
    the projects book tagged `project:<slug>`, then a page whose slug starts with `<slug>-`. First in
    book order wins. On `ok`: `match` (`tag` or `slug_prefix`), `id, title, url, updated_at`,
    `provenance{repo, path, git_sha, status, [path_note], [git_sha_note]}`, `canonical_repo`,
    `canonical_location`, `dev_prod_boundary`. The last three come from the rows `Canonical repo`,
    `Canonical location` and `Dev/prod boundary` of a `| Field | Value |` table on the page (the
    Provenance table or a Facts table); absent -> `unknown`. `url` is `unknown` when the page has none.
    The slug-prefix rule matches a longer slug: `proj` matches `proj-a-notes`. `match` says which rule was
    used (`tag` or `slug_prefix`), so a caller can tell an explicit tag from a prefix guess.
    `state: "incomplete"` means the projects book has more pages than were read (the read stops at 50); no
    match is not claimed then.
  - `standards`: the pages of the workflow policy book: `items[]` of `{id, title, url, updated_at,
    status}`, `truncated`. These are the intended review and approval roles, not observed behaviour.
  - `runbooks`: pages of the runbooks book tagged `project:<slug>` or `scope:all`: `items[]` of `{id, title,
    url, updated_at}`, `truncated`. No match -> `items: []` and `runbooks_note: "untagged"`, except when the
    book was cut short: then `truncated` is true and the note is not given. The whole book is never listed.
  - `ownership`: in-progress beads with a label equal to the slug: `items[]` of `{id, title, assignee,
    coder, reviewers[], approvers[], review_gate}` (rules as in `assignments` above), `total`, `truncated`.
  - `availability`: `ttl_s` and `rows[]` of `{provider, basis, state, observed_at, expires_at, expired}`.
    Freshness only: no values, spend, cost or detail text. `state` is one of `observed`, `stale`, `unknown`,
    `available`, `unavailable`, `degraded`, `limited`; any other (owner-typed) text is shown as `unknown`.
  - `revision`: `{critboard, pages[{id, updated_at}], fetched_at}`. `critboard` is the commit the update
    banner reads (`unknown` outside a git checkout). `pages` lists every BookStack page id in the body.
  - `degraded`: reasons, in this order: `knowledge_not_configured`, `knowledge_unavailable`,
    `no_entrypoint`, `projects_truncated`, `runbooks_truncated` (the book has more pages than the 50 read),
    `beads_stale` (the beads source is not ok, or stale, or missing), then `dropped:<section>` for each
    section removed by the cap, and `entrypoint_dropped` and `over_cap` for the stub below.
- Lists hold at most 20 items. Each text field is cut at 200 BYTES of UTF-8, at a character boundary. A
  lone surrogate becomes `?` and a control character a space. Page bodies are never returned.
- Size cap: the bytes sent (compact JSON, UTF-8) never exceed `max_bytes`. When the body is over, whole
  sections are dropped in this order, stopping at the first that fits: `runbooks`, `standards`,
  `ownership`, `availability`. A dropped section stays in the body as `{source, state: "dropped",
  items: []}` (`rows: []` for availability), its pages leave `revision.pages`, and `degraded` names it.
  If the body is still over `max_bytes` with those four dropped (the entrypoint alone is too large), a stub
  is served: `{ok, project, state: "over_cap", revision{critboard, pages: [], fetched_at}, degraded}`, with
  every section removed and `entrypoint_dropped`, `over_cap` and all four `dropped:` reasons in `degraded`.
  The bytes sent never exceed `max_bytes`; `server/tests/test_context.py` asserts this for ASCII, CJK, emoji,
  control-character and escape-heavy text at 8192 and at the default.

##### Context data boundary (checkable)

The response may hold only BookStack-curated records and bead assignment fields. The boundary is the
allowlist projection in `context.py`: each section copies named fields and nothing else. The forbidden-key
scrub below is a second layer, not the boundary. Free text passes through as written: page titles, bead
titles, provenance values and the page-stated canonical location and boundary. CritBoard does not judge
whether that text is sensitive; keep it out of the curated pages. Each line below is true of every
response; `server/tests/test_context.py` checks them.

- [ ] Keys: no key named `session`, `sessions`, `agents`, `transcript`, `memory`, `cost`, `spend`, `token`,
  `token_file`, `auth`, `auth_path`, `values`, `detail`, `local_path`, `cwd`, `host`, `hosts`,
  `cost_estimate_today_usd`, `cost_basis`, `authorization`, `secret`, `api_key`, `apikey`, `password`,
  `passwd`, `credential`, `credentials`, `session_id`, `sessionid`, `token_path`, `file_path`, `filepath`,
  `memory_path`, `transcript_path` or `home`, at any depth (case-insensitive). `context.scrub`
  removes them from the final body even if a section starts to pass one.
- [ ] Ownership holds only `id, title, assignee, coder, reviewers, approvers, review_gate`. Observed
  sessions are never read.
- [ ] Availability holds only `provider, basis, state, observed_at, expires_at, expired`.
- [ ] BookStack data is limited to title, url, id, `updated_at`, status, provenance and the three canonical
  facts. No page body, no tag list, no editor name, no error message text (only the error code).
- [ ] No BookStack token, token id, token path or Authorization header, and no host path of the
  CritBoard machine.
- [ ] The beads and sessions of other projects do not appear: ownership matches the slug label exactly.

#### Infrastructure record contract

One page = one record. A field the page does not state is the string `unknown` (lists are empty
and the view shows `unknown`). The server never guesses a value.

- Provenance table: the first table on the page, with header `| Field | Value |`. Rows (field names
  match without regard to case): `Source repo`, `Source path`, `Source git SHA`, `Live check`,
  `Class`, `Status`. A page with no `markdown` (saved in BookStack's WYSIWYG editor) has no readable
  table: its provenance shows as `unknown`. No such table, a missing row, an empty cell, or `-`, `n/a`, `none`, `tbd`, `?`
  -> `unknown`. A row with fewer than two cells is skipped.
- Tags (name/value, name matches without regard to case): `kind` (host, service, repo, ...), `owner`,
  `environment`, `purpose`, `status`, `class` (first non-empty wins), and `runbook`, `backup`, `related` (may repeat;
  duplicates collapse). A `runbook` or `backup` value is a link only if it is an `http(s)` URL or a
  path that starts with a single `/` (joined to the wiki base_url). Anything else is shown as text.
  `related` is text: it names other assets, services or repos.
- Bounds: each table cell, tag value, title and editor name is cut at 500 characters; only the first 50
  tags of a page are read. A page that cannot be parsed becomes a record with status `unknown` and reason
  `unreadable record`; it never fails the read. Each page is parsed (in a worker thread) as it arrives and the raw page is dropped. `id` is the listing's integer;
  `updated_at` and the page slug are cut at 500 characters too.
- Also returned: `title`, `url` (`<base_url>/books/<book_slug>/page/<page slug>`), `updated_at`,
  `updated_by` (name).
- `status` is one of `current`, `needs-verification`, `contradicted`, `stale`, `unknown`:
  - The table `Status` is read without regard to case; spaces and `_` count as `-`. Other text -> `unknown`.
  - The `status` tag is read the same way, and may repeat. All readable values (table and every tag) are
    merged. If only the tags are readable, they are used. If the readable values differ, the worst one wins (`contradicted` > `stale` > `needs-verification` > `unknown` >
    `current`) and the record carries `status_conflict: "status conflict: table X, tag Y"` (one `tag Z` per differing
    value), which the view shows. A status written as `unknown` is a real
    value (worse than `current`, so `current` + `unknown` gives `unknown` and a conflict note); an empty,
    `tbd` or unrecognised status is absent and is ignored. `Class` falls back to the `class` tag the same way (table first).
  - Age counts from the `Live check` date (`verified_basis: "live_check"`). With no readable date
    (absent, `unknown`, or text that does not start with `YYYY-MM-DD`, or a date more than one day
    ahead) it counts from the page's `updated_at` (`verified_basis: "page_updated"`); with neither,
    `"none"`.
  - Older than `stale_after_days` -> `stale`, whatever the table says, except `contradicted`, which is kept.
  - `current` without a readable Live check date -> `needs-verification`: a page edit is not a check.
  - `last_verified` is the Live check time, or `unknown`. It is never the page edit time.
  - `status_reason` says why a status differs from the table.
- The view shows status as a word in a badge (not colour alone); `stale` and `unknown` cards have a dashed
  border. On an outage it keeps the records from the last good fetch in the page, dimmed, with the fetch
  time, the time BookStack went down and the last success.

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
