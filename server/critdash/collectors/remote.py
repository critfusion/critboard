"""remote collector: pull per-host data (agents, worktrees, system, usage
buckets) from configured Tailscale peers over SSH, running the self-contained
stdlib-only probe (critdash/remote_probe.py) shipped fresh on stdin every
call. Nothing is installed on the remote; editing remote_probe.py takes
effect on the *next* probe call, no remote deployment step.

Architecture -- who owns which snapshot key:
  This collector is the only thing that talks to SSH, and it is the sole
  writer of the new `hosts` snapshot key (one entry per configured host,
  local and remote). It deliberately does NOT return "agents"/"worktrees"/
  "usage" itself -- those keys already have one owning collector each
  (AgentsCollector / WorktreesCollector / UsageCollector), and the collector
  framework's `on_result` replaces a whole top-level key per collector run,
  so two collectors writing the same key would stomp each other. Instead this
  collector stashes each host's raw probe result on `ctx.remote_hosts[name]`
  and persists usage_buckets into the `remote_usage_buckets` SQLite table;
  the three local collectors read `ctx.remote_hosts` on their own interval
  (agents every 5s, worktrees every 60s, usage every 10s) and fold remote
  rows into the merged view they already own -- the same pattern the
  codebase already uses for agents<->worktrees cross-joins via ctx.

Failure isolation: every host's probe runs concurrently under a bounded
semaphore with its own hard `ssh_timeout_s` wrapped around the whole ssh
call (connect + probe execution). One host timing out or refusing a
connection can never block or slow another host's collection, and never
raises out of collect() -- a failing host becomes an `ok: false` entry with
the real error text.

Stale-but-present, not vanished: on a failed probe, `ctx.remote_hosts[name]`
keeps its LAST GOOD agents/worktrees payload (only `ok`/`error`/`last_run`
are overwritten) rather than being cleared to empty. AgentsCollector/
WorktreesCollector tag every remote-sourced row with `stale` computed live
from the host's current `ok` state, so a dashboard reader sees "that host's
last known worktrees, marked stale" instead of its rows silently
disappearing from the fleet view. Usage numbers need no such handling: they
are persisted into remote_usage_buckets on every successful probe, so a
down host's past contribution is simply whatever is already in SQLite --
rollup queries don't need to know the host is currently unreachable to keep
including its history.
"""

from __future__ import annotations

import asyncio
import json
import shlex
from datetime import UTC, datetime
from pathlib import Path

from ..pricing import compute_cost_usd
from . import BaseCollector, CollectorIssue, now_iso

_PROBE_PATH = Path(__file__).resolve().parent.parent / "remote_probe.py"

_REMOTE_REMEDY = (
    'Add a host with "mode": "ssh" to the "hosts" list in config/sources.json '
    "to probe another machine over SSH, or ignore this panel if this is your "
    "only machine."
)


def availability_issue(hosts: list[dict] | None) -> CollectorIssue | None:
    """None if at least one enabled ssh-mode host is configured -- a single
    laptop with only the default "localhost" (mode: local) entry has nothing
    to probe over SSH, which is a normal, expected state, not a failure
    (same "optional, auto-detected" treatment as beads/dispatch). Unlike
    those two, this is not something collect() itself needs to check --
    zero ssh hosts already makes collect() a fast no-op -- this only decides
    whether main.py schedules the collector at all, so the `remote` panel
    shows "not configured" instead of quietly running an idle task forever."""
    for h in hosts or []:
        if h.get("enabled", True) and h.get("mode") == "ssh":
            return None
    return CollectorIssue(
        "config_missing",
        "no ssh-mode host is configured in sources.json's \"hosts\" list",
        remedy=_REMOTE_REMEDY,
        optional=True,
    )


class RemoteCollector(BaseCollector):
    name = "remote"
    interval_s = 120.0

    def __init__(
        self,
        ctx=None,
        hosts: list[dict] | None = None,
        ssh_opts: list[str] | None = None,
        ssh_timeout_s: float = 45.0,
        store=None,
        probe_path: str | None = None,
        max_parallel: int = 8,
        local_host: str = "localhost",
        repo_roots: list[str] | None = None,
        claude_projects_dir: str = "~/.claude/projects",
        herdr_bin: str = "herdr",
        disk_mounts: list[str] | None = None,
        worker_pool: int = 16,
        git_timeout_s: float = 5.0,
        max_depth: int = 4,
        session_active_window_s: float = 900.0,
        kimi_dir: str = "~/.kimi-code",
        codex_dir: str = "~/.codex",
        grok_dir: str = "~/.grok",
        cursor_dir: str = "~/.cursor",
    ):
        super().__init__(ctx)
        self.hosts = hosts or []
        self.ssh_opts = ssh_opts or []
        self.ssh_timeout_s = ssh_timeout_s
        self.store = store
        self.probe_path = Path(probe_path) if probe_path else _PROBE_PATH
        self.max_parallel = max_parallel
        self.local_host = local_host
        self.repo_roots = repo_roots or []
        self.claude_projects_dir = claude_projects_dir
        self.herdr_bin = herdr_bin
        self.disk_mounts = disk_mounts or ["/"]
        self.worker_pool = worker_pool
        self.git_timeout_s = git_timeout_s
        self.max_depth = max_depth
        self.session_active_window_s = session_active_window_s
        self.kimi_dir = kimi_dir
        self.codex_dir = codex_dir
        self.grok_dir = grok_dir
        self.cursor_dir = cursor_dir

    async def collect(self) -> dict:
        ssh_hosts = [h for h in self.hosts if h.get("enabled", True) and h.get("mode") == "ssh"]
        local_hosts = [h for h in self.hosts if h.get("enabled", True) and h.get("mode") == "local"]

        try:
            probe_source = self.probe_path.read_text()
            probe_read_error = None
        except OSError as exc:
            probe_source = None
            probe_read_error = f"{type(exc).__name__}: {exc}"

        sem = asyncio.Semaphore(max(1, self.max_parallel))

        async def bound(h):
            async with sem:
                return await self._collect_ssh_host(h, probe_source, probe_read_error)

        ssh_results = list(await asyncio.gather(*(bound(h) for h in ssh_hosts)))

        hosts_out = [self._local_host_entry(h["name"]) for h in local_hosts]
        hosts_out.extend(ssh_results)

        return {"hosts": hosts_out}

    # -- per-host probe run --------------------------------------------------

    def _host_value(self, host_cfg: dict, key: str, default):
        """Per-host override with global fallback. A key ABSENT from
        host_cfg falls back to `default` (the collector-level global from
        sources.json); a key PRESENT but explicitly null (Python None) is
        returned as None -- distinct from "absent" -- so a host can opt out
        of a global (see herdr_bin: null handling in _probe_args)."""
        return host_cfg[key] if key in host_cfg else default

    def _probe_args(self, host_cfg: dict) -> list[str]:
        name = host_cfg["name"]
        # claude_projects_dir must NOT be pre-expanded on the local host (this
        # machine's `~` != the remote host's `~`) -- it is shipped as-is
        # (e.g. "~/.claude/projects") and expanded by remote_probe.py's own
        # os.path.expanduser() call, which runs ON the remote host and
        # therefore resolves against THAT host's home directory. Passing a
        # locally-expanded absolute path here (the pre-fix bug) works only by
        # coincidence for a remote host that happens to share this machine's
        # username, and silently globs to nothing -- no error, zero
        # usage_buckets -- for any host with a different username.
        projects_dir = self._host_value(host_cfg, "claude_projects_dir", self.claude_projects_dir)
        repo_roots = self._host_value(host_cfg, "repo_roots", self.repo_roots)
        # herdr_bin: null (explicit, in config/sources.json) means "this host
        # has no herdr" -- ship "" so the probe's find_herdr() short-circuits
        # to "no agents" instead of falling back to the global herdr_bin path
        # (which wouldn't exist on that host) or doing a PATH search that
        # could accidentally match an unrelated binary.
        herdr_bin = self._host_value(host_cfg, "herdr_bin", self.herdr_bin) or ""
        session_window = self._host_value(host_cfg, "session_active_window_s", self.session_active_window_s)
        # Same per-host override mechanism as claude_projects_dir above, and
        # same NOT-pre-expanded rule: shipped as-is (e.g. "~/.kimi-code") and
        # expanded on the remote host by remote_probe.py's own
        # os.path.expanduser(), never against this machine's home directory.
        kimi_dir = self._host_value(host_cfg, "kimi_dir", self.kimi_dir)
        # Same per-host override + NOT-pre-expanded rule as kimi_dir above.
        codex_dir = self._host_value(host_cfg, "codex_dir", self.codex_dir)
        grok_dir = self._host_value(host_cfg, "grok_dir", self.grok_dir)
        cursor_dir = self._host_value(host_cfg, "cursor_dir", self.cursor_dir)

        args = [
            name,
            "--projects-glob", str(projects_dir).rstrip("/") + "/*/*.jsonl",
            "--herdr-bin", herdr_bin,
            "--worker-pool", str(self.worker_pool),
            "--git-timeout", str(self.git_timeout_s),
            "--max-depth", str(self.max_depth),
            "--session-window", str(session_window),
            "--kimi-dir", str(kimi_dir),
            "--codex-dir", str(codex_dir),
            "--grok-dir", str(grok_dir),
            "--cursor-dir", str(cursor_dir),
        ]
        for r in repo_roots:
            args += ["--repo-root", r]
        for m in self.disk_mounts:
            args += ["--disk-mount", m]
        return args

    async def _collect_ssh_host(self, host_cfg: dict, probe_source: str | None, probe_read_error: str | None):
        name = host_cfg["name"]
        target = host_cfg.get("target", name)
        loop = asyncio.get_event_loop()
        start = loop.time()

        if probe_source is None:
            return self._fail_entry(
                name, probe_read_error or "probe script unavailable", 0.0,
                reason_code="command_failed",
                remedy="This is a local file-read problem (remote_probe.py), "
                "not the remote host -- check the critdash install.",
            )

        # ssh reassembles trailing argv into one string and hands it to the
        # remote login shell, which re-parses and glob-expands it -- so each
        # argument must be independently shell-quoted for that remote shell,
        # not just passed as separate argv items locally (verified live: an
        # unquoted `*/*.jsonl` glob gets expanded on the remote side into
        # hundreds of stray argv entries before python ever runs).
        remote_cmd = "python3 - " + " ".join(shlex.quote(a) for a in self._probe_args(host_cfg))
        cmd = ["ssh", *self.ssh_opts, target, remote_cmd]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            # ssh itself is not installed / not executable -- an optional
            # dependency absent, same "normal on a fresh install" treatment
            # as beads/herdr (see collectors/__init__.py's CollectorIssue).
            return self._fail_entry(
                name, f"ssh is not available: {type(exc).__name__}: {exc}", 0.0,
                reason_code="dependency_missing",
                remedy="Install an ssh client on this host, or remove this host "
                "from config/sources.json if you don't use remote hosts.",
                optional=True,
            )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=probe_source.encode()), timeout=self.ssh_timeout_s
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            duration_ms = (loop.time() - start) * 1000.0
            return self._fail_entry(
                name, f"ssh timed out after {self.ssh_timeout_s}s", duration_ms,
                reason_code="unreachable",
                remedy=f"Check that {target} is powered on and reachable (try `ssh {target}` by hand).",
            )

        duration_ms = (loop.time() - start) * 1000.0

        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()[:500] or f"ssh exit {proc.returncode}"
            return self._fail_entry(name, err, duration_ms, reason_code="command_failed")

        try:
            lines = [ln for ln in stdout.decode(errors="replace").strip().splitlines() if ln.strip()]
            payload = json.loads(lines[-1]) if lines else {}
        except (json.JSONDecodeError, IndexError) as exc:
            return self._fail_entry(
                name, f"bad probe output: {type(exc).__name__}: {exc}", duration_ms,
                reason_code="command_failed",
            )

        if payload.get("error"):
            return self._fail_entry(name, str(payload["error"]), duration_ms, reason_code="command_failed")

        return self._ok_entry(name, payload, duration_ms)

    # -- host entry builders --------------------------------------------------

    def _local_host_entry(self, name: str) -> dict:
        agents = self.ctx.latest_agents if self.ctx is not None else []
        worktrees = self.ctx.latest_worktrees if self.ctx is not None else []
        n_agents = sum(1 for a in agents if (a.get("host") or self.local_host) == name)
        n_worktrees = sum(1 for w in worktrees if (w.get("host") or self.local_host) == name)
        tokens_today, cost_today = 0, 0.0
        if self.store is not None:
            today_start = datetime.now(UTC).strftime("%Y-%m-%dT00:00:00Z")
            totals = self.store.usage_totals(today_start, host=name)
            tokens_today = totals["total"]
            cost_today = totals["cost_usd"]
        return {
            "name": name, "mode": "local", "ok": True, "last_ok": now_iso(),
            "error": None, "duration_ms": 0.0, "reachable": True,
            "agents": n_agents, "worktrees": n_worktrees,
            "tokens_today": tokens_today, "cost_today_usd": cost_today,
            "reason_code": None, "detail": None, "remedy": None, "optional": False,
        }

    def _ok_entry(self, name: str, payload: dict, duration_ms: float) -> dict:
        ts = now_iso()
        agents = payload.get("agents") or []
        worktrees = payload.get("worktrees") or []
        system = payload.get("system") or {}
        usage_buckets = payload.get("usage_buckets") or []

        for a in agents:
            a["host"] = name
        for w in worktrees:
            w["host"] = name

        if self.ctx is not None:
            self.ctx.remote_hosts[name] = {
                "agents": agents, "worktrees": worktrees, "system": system,
                "ok": True, "error": None, "last_run": ts, "last_ok": ts,
            }

        if self.store is not None and usage_buckets:
            rows = [{**b, "host": name} for b in usage_buckets]
            self.store.upsert_remote_usage_buckets(rows)

        self._persist_analytics_buckets(name, payload)

        tokens_today, cost_today = self._host_today_totals(name)
        return {
            "name": name, "mode": "ssh", "ok": True, "last_ok": ts, "error": None,
            "duration_ms": round(duration_ms, 2), "reachable": True,
            "agents": len(agents), "worktrees": len(worktrees),
            "tokens_today": tokens_today, "cost_today_usd": cost_today,
            "reason_code": None, "detail": None, "remedy": None, "optional": False,
        }

    def _persist_analytics_buckets(self, name: str, payload: dict) -> None:
        """Wave-2 analytics (briefing Task 2): fold every bucket category the
        probe shipped into its store table, tagging each row with this host.
        All replace-semantics upserts (see store.py's SCHEMA comment on the
        remote_* bucket tables) -- a fresh probe call's numbers for a bucket
        it reports are authoritative, same pattern as remote_usage_buckets."""
        if self.store is None:
            return
        if rows := payload.get("tool_buckets"):
            self.store.upsert_remote_tool_buckets([{**r, "host": name} for r in rows])
        if rows := payload.get("error_buckets"):
            self.store.upsert_remote_error_buckets([{**r, "host": name} for r in rows])
        if rows := payload.get("error_examples"):
            self.store.upsert_remote_error_examples([
                {"host": name, "kind": r["kind"], "example": r["example"], "last_seen": r["last_seen"]}
                for r in rows
            ])
        if rows := payload.get("trouble_file_buckets"):
            self.store.upsert_remote_trouble_file_buckets([{**r, "host": name} for r in rows])
        if rows := payload.get("api_error_buckets"):
            self.store.upsert_remote_api_error_buckets([{**r, "host": name} for r in rows])
        if rows := payload.get("session_buckets"):
            self.store.upsert_remote_session_buckets([{**r, "host": name} for r in rows])
        if rows := payload.get("productivity_buckets"):
            self.store.upsert_remote_productivity_buckets([{**r, "host": name} for r in rows])
        if rows := payload.get("kimi_usage_buckets"):
            self.store.upsert_remote_kimi_usage_buckets([{**r, "host": name} for r in rows])
        if rows := payload.get("kimi_error_buckets"):
            self.store.upsert_remote_kimi_error_buckets([{**r, "host": name} for r in rows])

    def _fail_entry(
        self, name: str, error: str, duration_ms: float,
        reason_code: str | None = "command_failed", remedy: str | None = None, optional: bool = False,
    ) -> dict:
        """Same reason_code/detail/remedy/optional vocabulary CollectorIssue
        uses for beads/herdr (see collectors/__init__.py), applied per-host
        here instead of per-collector -- RemoteCollector.collect() itself
        never raises (one host's failure must never block another's probe),
        so there is no scheduler-level exception to classify; each host
        entry in the `hosts` snapshot key carries its own classification
        instead. `detail` is kept equal to `error` (the existing field every
        caller/test already reads) rather than a second free-text field."""
        ts = now_iso()
        prev = self.ctx.remote_hosts.get(name) if self.ctx is not None else None
        if self.ctx is not None:
            self.ctx.remote_hosts[name] = {
                **(prev or {"agents": [], "worktrees": [], "system": {}}),
                "ok": False, "error": error, "last_run": ts,
                "last_ok": (prev or {}).get("last_ok"),
            }
        agents_n = len((prev or {}).get("agents", [])) if prev else 0
        worktrees_n = len((prev or {}).get("worktrees", [])) if prev else 0
        tokens_today, cost_today = self._host_today_totals(name)
        return {
            "name": name, "mode": "ssh", "ok": False, "last_ok": (prev or {}).get("last_ok"),
            "error": error, "duration_ms": round(duration_ms, 2), "reachable": False,
            "agents": agents_n, "worktrees": worktrees_n,
            "tokens_today": tokens_today, "cost_today_usd": cost_today,
            "reason_code": reason_code, "detail": error, "remedy": remedy, "optional": optional,
        }

    def _host_today_totals(self, name: str) -> tuple[int, float]:
        if self.store is None:
            return 0, 0.0
        today_start = datetime.now(UTC).strftime("%Y-%m-%dT00:00:00Z")
        rows = self.store.remote_usage_grouped(("model",), since_iso=today_start, host=name)
        pricing = self.ctx.pricing if self.ctx is not None else {}
        total_tokens = 0
        cost = 0.0
        for r in rows:
            total_tokens += (
                r["input"] + r["output"] + r["cache_read"] + r["cache_write_5m"] + r["cache_write_1h"]
            )
            cost += compute_cost_usd(
                pricing, r["model"], r["input"], r["output"], r["cache_read"],
                r["cache_write_5m"], r["cache_write_1h"],
            )
        return total_tokens, round(cost, 6)
