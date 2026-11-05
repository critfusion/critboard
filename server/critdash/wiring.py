"""Collector wiring: build, register, or park every collector, per
its dependency check and sources.json overrides."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import detect
from .collectors import CollectorIssue, Scheduler, SourceHealth
from .collectors.agents import AgentsCollector
from .collectors.analytics import AnalyticsCollector
from .collectors.beads import BeadsCollector
from .collectors.beads import availability_issue as beads_availability_issue
from .collectors.dispatch import DispatchCollector
from .collectors.dispatch import availability_issue as dispatch_availability_issue
from .collectors.kimi import KimiCollector
from .collectors.productivity import ProductivityCollector
from .collectors.provider_usage import ProviderUsageCollector
from .collectors.remote import RemoteCollector
from .collectors.remote import availability_issue as remote_availability_issue
from .collectors.system import SystemCollector
from .collectors.usage import UsageCollector
from .collectors.worktrees import WorktreesCollector
from .config import Config
from .ctx import AppContext
from .state import SnapshotStore
from .store import Store

logger = logging.getLogger("critdash")


def _collector_override(config: Config, name: str) -> bool | None:
    """Explicit True/False from sources.json's "collectors": {"<name>":
    {"enabled": true|false}} block. None means no override -- fall back to
    auto-detection (an absent or empty "collectors" block, the default)."""
    entry = (config.sources.get("collectors") or {}).get(name) or {}
    val = entry.get("enabled")
    return val if isinstance(val, bool) else None


def _resolve_enablement(
    name: str, override: bool | None, issue: CollectorIssue | None
) -> tuple[bool, CollectorIssue | None]:
    """Combine an auto-detected availability issue (None = the dependency
    was found) with an explicit sources.json override, which always wins
    either way. Returns (enabled, issue-to-report-when-inactive)."""
    if override is True:
        return True, None
    if override is False:
        return False, CollectorIssue(
            "config_missing",
            f"{name} disabled via collectors.{name}.enabled=false in sources.json",
            remedy=f"Set collectors.{name}.enabled to true in config/sources.json to re-enable.",
            optional=True,
        )
    return issue is None, issue


def _inactive_health_dict(issue: CollectorIssue) -> dict:
    return SourceHealth(
        ok=False, stale=True, optional=issue.optional,
        reason_code=issue.reason_code, detail=issue.detail, remedy=issue.remedy,
        error=f"{issue.reason_code}: {issue.detail}",
    ).to_dict()


@dataclass
class Wiring:
    scheduler: Scheduler
    # collector name -> its current inactive SourceHealth dict.
    inactive_health: dict[str, dict]
    # Collectors inactive under auto-detection, re-checked by
    # background.collector_redetect_loop.
    redetect_specs: list[dict]
    resolve_bd_bin: Callable[[], str]
    beads_dir: str


def wire_collectors(config: Config, store: Store, app_ctx: AppContext, snap: SnapshotStore) -> Wiring:
    def on_result(name: str, data: dict) -> None:
        for key, value in data.items():
            if key.startswith("_"):
                continue
            snap.update_path(key, value)
        # unified activity feed, refreshed after any collector that can emit events
        if name in ("beads", "worktrees", "agents", "dispatch"):
            events = [
                {
                    "t": r["ts"], "kind": r["kind"], "severity": r["severity"],
                    "text": r["text"], "ref": r["ref"],
                }
                for r in store.recent_events(50)
            ]
            snap.update_path("events", events)

    def on_health(_name: str, _health) -> None:
        snap.update_sources({**_inactive_health, **scheduler.health_snapshot()})

    scheduler = Scheduler(on_result=on_result, on_health=on_health)

    # Per-collector enablement (Bug 2): a collector whose optional dependency
    # is absent (no `bd`, no ~/.overlord, no ssh-mode host configured) starts
    # INACTIVE and is never registered with the scheduler -- no repeated
    # failing subprocess every cycle on a fresh install. It still shows up in
    # /api/healthz and the "sources" snapshot key (via _inactive_health,
    # merged in below and in on_health/the healthz endpoint) with
    # optional=true and a reason, so the UI can say "not configured" instead
    # of the panel silently vanishing -- same sourceIssueNotice() contract
    # the frontend already uses for beads (see web/js/utils.js). An explicit
    # "collectors.<name>.enabled" in sources.json overrides the
    # auto-detection either way (see _resolve_enablement).
    # collector name -> its current inactive SourceHealth dict.
    _inactive_health: dict[str, dict] = {}
    # Specs for collectors currently inactive under auto-detection (not a
    # hard `enabled: false`) -- periodically re-checked (collector_redetect_
    # loop below) so e.g. installing `bd` after the dashboard started brings
    # the panel alive with no restart needed.
    _redetect_specs: list[dict] = []

    def _apply_enablement(name, detect_fn, build_fn) -> None:
        issue = detect_fn()
        override = _collector_override(config, name)
        enabled, report_issue = _resolve_enablement(name, override, issue)
        if enabled:
            scheduler.register(build_fn())
            return
        _inactive_health[name] = _inactive_health_dict(report_issue)
        logger.info("collector %s: inactive (%s: %s)", name, report_issue.reason_code, report_issue.detail)
        if override is not False:
            _redetect_specs.append({"name": name, "detect": detect_fn, "build": build_fn})

    repo_roots = config.sources.get("repo_roots", [])

    _resolve_bd_bin, beads_dir = _wire_beads(config, app_ctx, store, _apply_enablement)

    local_host = config.sources.get("host", "localhost")

    # herdr_bin: kept as the raw configured value for RemoteCollector below
    # (an explicit "" -- or JSON null, normalized to "" here -- means "this
    # host has no herdr, don't PATH-search" -- see collectors/remote.py's
    # find_herdr -- and a value resolved against THIS host's filesystem
    # would be actively wrong for a remote ssh host). AgentsCollector only
    # ever runs locally, so it alone gets the same detect.py-backed
    # fallback as bd_bin above -- but only when herdr_bin isn't the
    # explicit "no herdr" sentinel, which must never trigger a PATH search.
    herdr_bin = config.sources.get("herdr_bin", "herdr")
    if herdr_bin is None:
        herdr_bin = ""
    herdr_bin_local = (detect.resolve_binary(herdr_bin, "herdr") or herdr_bin) if herdr_bin else ""
    session_active_window_s = config.sources.get("session_active_window_s", 900)
    _register_local_collectors(
        config, app_ctx, store, scheduler,
        local_host=local_host, repo_roots=repo_roots,
        herdr_bin_local=herdr_bin_local, session_active_window_s=session_active_window_s,
    )

    overlord_dir = config.expand("overlord_dir")

    def _build_dispatch_collector() -> DispatchCollector:
        c = DispatchCollector(ctx=app_ctx, overlord_dir=overlord_dir)
        c.interval_s = config.interval("dispatch")
        return c

    _apply_enablement(
        "dispatch",
        lambda: dispatch_availability_issue(Path(overlord_dir).expanduser()),
        _build_dispatch_collector,
    )

    # No more hardcoded "/srv" (Bug 1) -- "/" plus whatever the install
    # configures in sources.json's "disk_mounts". A configured mount that
    # doesn't exist on this host is skipped by SystemCollector/RemoteCollector
    # (see collectors/system.py's _disk()), not a failure.
    disk_mounts = config.sources.get("disk_mounts") or ["/"]

    system_collector = SystemCollector(ctx=app_ctx, disk_mounts=disk_mounts)
    system_collector.interval_s = config.interval("system")
    scheduler.register(system_collector)

    hosts_cfg = config.sources.get("hosts", [])

    def _build_remote_collector() -> RemoteCollector:
        return _remote_collector(
            config, app_ctx, store, hosts=hosts_cfg, local_host=local_host, repo_roots=repo_roots,
            herdr_bin=herdr_bin, disk_mounts=disk_mounts, session_active_window_s=session_active_window_s,
        )

    _apply_enablement("remote", lambda: remote_availability_issue(hosts_cfg), _build_remote_collector)

    _register_analytics_collectors(config, app_ctx, store, scheduler, local_host)

    # Seed "sources" with every inactive collector's reason immediately --
    # otherwise GET /api/snapshot (or the first /api/stream frame) before any
    # collector has completed a cycle would show nothing for e.g. beads on a
    # fresh install, instead of "not configured" right away.
    snap.update_sources({**_inactive_health, **scheduler.health_snapshot()})

    return Wiring(
        scheduler=scheduler,
        inactive_health=_inactive_health,
        redetect_specs=_redetect_specs,
        resolve_bd_bin=_resolve_bd_bin,
        beads_dir=beads_dir,
    )


def _register_local_collectors(
    config: Config, app_ctx: AppContext, store: Store, scheduler: Scheduler, *,
    local_host: str, repo_roots: list, herdr_bin_local: str, session_active_window_s: int,
) -> None:
    """Always-on local collectors: agents, worktrees, usage, kimi, provider usage."""
    agents_collector = AgentsCollector(
        ctx=app_ctx, herdr_bin=herdr_bin_local, store=store, host=local_host,
        session_projects_glob=config.expand("claude_projects_dir") + "/*/*.jsonl",
        session_active_window_s=session_active_window_s,
        # By-session-id transcript lookup fallback for a herdr-listed pane
        # with no session record (see AgentsCollector.__init__/module
        # docstring) -- same claude_projects_dir/kimi_dir config values the
        # session-derived collectors above already use.
        claude_projects_dir=config.expand("claude_projects_dir"),
        kimi_dir=config.sources.get("kimi_dir", "~/.kimi-code"),
        codex_dir=config.sources.get("codex_dir", "~/.codex"),
        grok_dir=config.sources.get("grok_dir", "~/.grok"),
        cursor_dir=config.sources.get("cursor_dir", "~/.cursor"),
    )
    agents_collector.interval_s = config.interval("agents")
    scheduler.register(agents_collector)

    worktrees_collector = WorktreesCollector(
        ctx=app_ctx, repo_roots=repo_roots,
        worker_pool=config.sources.get("worktree_worker_pool", 32),
        git_timeout_s=config.sources.get("worktree_git_timeout_s", 5),
        store=store,
        max_depth=config.sources.get("repo_scan_depth", 4),
        prune_dirs=set(config.sources.get("worktree_skip_dirs", [])) or None,
        host=local_host,
    )
    worktrees_collector.interval_s = config.interval("worktrees")
    scheduler.register(worktrees_collector)

    usage_collector = UsageCollector(
        ctx=app_ctx,
        projects_glob=config.expand("claude_projects_dir") + "/*/*.jsonl",
        store=store,
        pricing=config.pricing,
        host=local_host,
    )
    usage_collector.interval_s = config.interval("usage")
    scheduler.register(usage_collector)

    kimi_collector = KimiCollector(
        ctx=app_ctx, store=store, host=local_host,
        kimi_dir=config.sources.get("kimi_dir", "~/.kimi-code"),
        session_active_window_s=session_active_window_s,
    )
    kimi_collector.interval_s = config.interval("kimi")
    scheduler.register(kimi_collector)

    provider_usage_collector = ProviderUsageCollector(
        ctx=app_ctx, store=store, host=local_host,
        codex_dir=config.sources.get("codex_dir", "~/.codex"),
        grok_dir=config.sources.get("grok_dir", "~/.grok"),
    )
    provider_usage_collector.interval_s = config.interval("provider_usage")
    scheduler.register(provider_usage_collector)


def _remote_collector(
    config: Config, app_ctx: AppContext, store: Store, *, hosts: list, local_host: str,
    repo_roots: list, herdr_bin: str, disk_mounts: list, session_active_window_s: int,
) -> RemoteCollector:
    c = RemoteCollector(
        ctx=app_ctx,
        hosts=hosts,
        ssh_opts=config.sources.get("ssh_opts", []),
        ssh_timeout_s=config.sources.get("ssh_timeout_s", 45),
        store=store,
        local_host=local_host,
        repo_roots=repo_roots,
        # NOT config.expand() here -- a remote host's "~" must be
        # expanded on THAT host (remote_probe.py does this), never
        # pre-expanded against the local host's own home directory. See
        # RemoteCollector._probe_args.
        claude_projects_dir=config.sources.get("claude_projects_dir", "~/.claude/projects"),
        herdr_bin=herdr_bin,
        disk_mounts=disk_mounts,
        worker_pool=config.sources.get("worktree_worker_pool", 32),
        git_timeout_s=config.sources.get("worktree_git_timeout_s", 5),
        max_depth=config.sources.get("repo_scan_depth", 4),
        session_active_window_s=session_active_window_s,
        kimi_dir=config.sources.get("kimi_dir", "~/.kimi-code"),
        codex_dir=config.sources.get("codex_dir", "~/.codex"),
        grok_dir=config.sources.get("grok_dir", "~/.grok"),
        cursor_dir=config.sources.get("cursor_dir", "~/.cursor"),
    )
    c.interval_s = config.sources.get("remote_interval_s", 120)
    return c


def _register_analytics_collectors(
    config: Config, app_ctx: AppContext, store: Store, scheduler: Scheduler, local_host: str
) -> None:
    # ProductivityCollector must be registered before AnalyticsCollector so
    # ctx.latest_productivity has a first value as soon as possible -- not
    # load-bearing for correctness (AnalyticsCollector's productivity default
    # is an honest all-zero dict either way), just avoids one empty poll.
    productivity_collector = ProductivityCollector(
        ctx=app_ctx, store=store, host=local_host,
        worker_pool=config.sources.get("worktree_worker_pool", 32),
        excluded_repos=config.sources.get("excluded_repos"),
    )
    productivity_collector.interval_s = config.interval("productivity")
    scheduler.register(productivity_collector)

    analytics_collector = AnalyticsCollector(
        ctx=app_ctx,
        projects_glob=config.expand("claude_projects_dir") + "/*/*.jsonl",
        store=store,
        host=local_host,
    )
    analytics_collector.interval_s = config.interval("analytics")
    scheduler.register(analytics_collector)


def _wire_beads(
    config: Config, app_ctx: AppContext, store: Store, _apply_enablement: Callable[..., None]
) -> tuple[Callable[[], str], str]:
    """Register (or park) the beads collector. Returns the bd resolver and
    beads_dir, which the bead routes also use."""
    beads_env = config.expand("beads_env")
    beads_dir = config.expand("beads_dir")
    bd_bin_configured = config.expand("bd_bin") or "bd"
    beads_actor = config.sources.get("beads_actor", "critdash")

    def _resolve_bd_bin() -> str:
        """Configured value first, exactly as before; if that doesn't
        resolve, fall back to the full candidate scan (PATH, then every
        known install-manager location -- see detect.py). This is what
        finds `bd` on a Mac where sources.json still has the Debian path
        baked in, both here (startup) and in collector_redetect_loop below
        (re-called fresh on every tick, so installing bd later -- or fixing
        a wrong-machine config without restarting -- brings the panel alive
        with no restart)."""
        return detect.resolve_binary(bd_bin_configured, "bd") or bd_bin_configured

    def _build_beads_collector() -> BeadsCollector:
        c = BeadsCollector(
            ctx=app_ctx, beads_env=beads_env, bd_bin=_resolve_bd_bin(), actor=beads_actor, store=store,
            beads_dir=beads_dir,
        )
        c.interval_s = config.interval("beads")
        return c

    _apply_enablement(
        "beads", lambda: beads_availability_issue(_resolve_bd_bin(), beads_dir), _build_beads_collector
    )

    return _resolve_bd_bin, beads_dir
