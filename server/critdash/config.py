"""Load config/*.json with env overrides prefixed CRITDASH_.

config/sources.json is machine-local (repo roots, hostnames, credential
paths) and is gitignored -- it is never shipped. config/sources.example.json
ships instead, with generic, documented defaults. On startup, if
config/sources.json is missing, it is created from the example (see
_ensure_sources_file below) and the file already on disk is never touched or
overwritten.

config/layout.json is personal (title, human_labels, and a user's own panel
arrangement) and is gitignored the same way. config/layout.example.json
ships instead, with those personal values neutralised (see
_ensure_layout_file below); an existing layout.json is likewise never
touched or overwritten.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("critdash.config")

# server/critdash/config.py -> dashboard root is two parents up
SERVER_DIR = Path(__file__).resolve().parent.parent
DASHBOARD_ROOT = SERVER_DIR.parent
# CRITDASH_CONFIG_DIR lets a caller point at a different config directory
# entirely (e.g. a throwaway dir for a fresh-install smoke test, or a
# non-standard install layout) without touching the real one -- unset in
# normal operation, where this is just DASHBOARD_ROOT/config.
CONFIG_DIR = Path(os.environ.get("CRITDASH_CONFIG_DIR") or (DASHBOARD_ROOT / "config"))

# Generic, host-agnostic fallback -- used only if config/sources.json is
# missing AND config/sources.example.json is also missing/unreadable (see
# _ensure_sources_file), or if the on-disk file fails to parse as JSON.
DEFAULT_SOURCES = {
    # Empty by default: ~/.config/beads/env is a site-specific convention
    # (the original fleet's own way of sourcing credentials for a shared
    # beads server), not something `bd` requires. It is optional -- sourced
    # only if it exists (see collectors/beads.py's bd_shell_prefix) -- and a
    # generic `bd init`/BEADS_DIR workspace needs no such file at all. Set
    # this only if your `bd` setup actually uses an env file like this.
    "beads_env": "",
    # Empty by default. When set, the collector exports BEADS_DIR to this
    # path before every `bd` call -- this is what lets the collector find a
    # workspace `bd` itself would find fine from a normal shell, since it
    # runs `bd` from CritBoard's own directory and does not inherit a user's
    # shell environment. Takes precedence over whatever beads_env's sourced
    # file exports, which in turn takes precedence over bd's own resolution.
    # Must be the .beads directory itself (e.g. /path/to/project/.beads),
    # not its parent -- an easy mistake; `bd where --json`'s "path" field is
    # the documented way to find the right value. See collectors/beads.py's
    # bd_shell_prefix/validate_beads_dir.
    "beads_dir": "",
    "beads_actor": "critdash",
    "bd_bin": "~/.local/bin/bd",
    "herdr_bin": "herdr",
    "claude_projects_dir": "~/.claude/projects",
    "kimi_dir": "~/.kimi-code",
    "overlord_dir": "~/.overlord",
    "repo_roots": [
        "~/repos",
        "~/src",
        "~/work",
    ],
    "db_path": "server/data/critdash.db",
    "excluded_repos": [],
    "intervals_s": {
        "beads": 30,
        "agents": 5,
        "worktrees": 60,
        "usage": 10,
        "kimi": 10,
        "dispatch": 30,
        "system": 15,
        "analytics": 90,
        "productivity": 600,
    },
    "worktree_worker_pool": 32,
    "worktree_git_timeout_s": 5,
    "repo_scan_depth": 4,
    "worktree_skip_dirs": [
        "node_modules", ".venv", "venv", "vendor", "site-packages", ".cache",
        ".pub-cache", "build", "dist", "target", ".tox", ".gradle",
    ],
    "host": "localhost",
    # 127.0.0.1, not 0.0.0.0 -- a safer public default. Installs that need to
    # listen on all interfaces (e.g. to reach the dashboard from other
    # machines on a LAN) set bind_host explicitly in config/sources.json.
    "bind_host": "127.0.0.1",
    "bind_port": 9999,
    "hosts": [
        {"name": "localhost", "mode": "local", "enabled": True},
    ],
    # Disk mounts the system panel reports on, local and remote -- "/" plus
    # whatever else you add here (e.g. "/srv" or "/mnt/data"). A configured
    # mount that doesn't exist on a given host is skipped, not a failure --
    # see collectors/system.py.
    "disk_mounts": ["/"],
    # Per-collector enablement override (Bug 2). Empty/absent = every
    # collector auto-detects: one whose optional dependency is missing (no
    # `bd`, no ~/.overlord, no ssh-mode host) starts inactive and is
    # re-checked every collector_redetect_interval_s rather than scheduled
    # to fail every cycle. Force one on or off regardless of detection with
    # e.g. {"beads": {"enabled": false}}.
    "collectors": {},
    "collector_redetect_interval_s": 60,
    "session_active_window_s": 900,
    "remote_interval_s": 120,
    "ssh_timeout_s": 45,
    # AI-provider quota check (critdash/quota.py) -- credential file paths,
    # never the credentials themselves. See that module's docstring for how
    # each is used and why.
    "opencode_auth_path": "~/.local/share/opencode/auth.json",
    "grok_auth_path": "~/.grok/auth.json",
    "codex_auth_path": "~/.codex/auth.json",
    "kimi_usage_url": "https://api.kimi.ai/coding/v1/usages",
    "quota_timeout_s": 10,
    "quota_refresh_min_interval_s": 30,
    "quota_stale_after_s": 3600,
    # Self-update (critdash/update.py). Off by default -- it executes
    # `git pull` from update_repo/update_branch and restarts the service, so
    # a fresh install must opt in explicitly (see that module's docstring).
    "allow_self_update": False,
    # critfusion/critboard is public again, so an unauthenticated
    # GET /repos/critfusion/critboard returns 200 and the periodic/manual
    # check works out of the box. A fork should point this at its own
    # "owner/repo" instead.
    "update_repo": "critfusion/critboard",
    "update_branch": "main",
    "update_check_min_interval_s": 300,
    # Periodic background check (critdash/update.py's periodic_update_check,
    # briefing Task 5): read-only and safe, so it's on by default, unlike
    # allow_self_update/update_auto_apply, which actually change the
    # checkout. Interval floor is enforced by POST /api/settings/updates
    # (not below 300s) so the settings UI can't configure a rate-limit
    # violation; conditional (ETag) requests are what make a 900s default
    # free against GitHub's unauthenticated 60/hour limit either way.
    "update_check_enabled": True,
    "update_check_interval_s": 900,
    # Off by default -- true lets the periodic check pull a fast-forward
    # update on its own with no human click, still gated behind
    # allow_self_update and every safety check in update.apply_update.
    "update_auto_apply": False,
    "ssh_opts": [
        "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new",
    ],
    # Collector cadence preset (briefing Task 5): "normal" (today's
    # intervals_s numbers) or "relaxed" (~3x longer, for a laptop on
    # battery). See Config.refresh_multiplier().
    "refresh_preset": "normal",
    # Settings UI writes config/layout.json and config/theme.json via
    # POST /api/config/layout|theme -- that's the point of the settings
    # panel. An operator exposing this dashboard beyond 127.0.0.1 (e.g.
    # bind_host: 0.0.0.0 on a LAN) can set this false to make the dashboard
    # read-only from the browser; both POSTs then return 403.
    "allow_config_writes": True,
}

# Fallback used only if config/pricing.json is missing or fails to parse.
# Shape must match the real file: models.<prefix> -> rate dict, fast_mode,
# monthly_budget_usd. Rates here are deliberately conservative "default"-tier
# numbers, not real per-model rates -- config/pricing.json is authoritative.
DEFAULT_PRICING = {
    "models": {
        "default": {
            "input": 3.0, "output": 15.0,
            "cache_write_5m": 3.75, "cache_write_1h": 6.0, "cache_read": 0.30,
        },
    },
    "fast_mode": {},
    "monthly_budget_usd": 2000,
}


def _load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return dict(default)
    try:
        with path.open() as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return dict(default)


def _ensure_config_file_from_example(config_dir: Path, name: str) -> None:
    """Generic first-run bootstrap: config/<name>.json is gitignored (it
    holds machine-local or personal values) and is never shipped. If it's
    missing, seed it from config/<name>.example.json (which IS shipped,
    with generic/neutral defaults) so a fresh install has a working config
    with zero manual steps. An existing config/<name>.json is NEVER
    overwritten -- this only ever runs the copy once, the first time.
    Shared by both config/sources.json (machine-local repo roots,
    hostnames, credential paths -- see _ensure_sources_file) and
    config/layout.json (a user's panel arrangement and personal settings
    like human_labels/title -- see _ensure_layout_file); an upstream
    `git pull` must not silently overwrite either."""
    target = config_dir / f"{name}.json"
    if target.exists():
        return
    example = config_dir / f"{name}.example.json"
    if not example.exists():
        return
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(example.read_text())
        logger.info(
            "config/%s.json not found -- created from config/%s.example.json", name, name
        )
    except OSError as exc:
        logger.warning("could not create config/%s.json from the example: %s", name, exc)


def _ensure_sources_file(config_dir: Path) -> None:
    """First-run bootstrap: config/sources.json is gitignored (it holds
    machine-local repo roots, hostnames, and credential paths) and is never
    shipped. If it's missing, seed it from config/sources.example.json (which
    IS shipped, with generic defaults) so a fresh install has a working
    config with zero manual steps. An existing sources.json is NEVER
    overwritten -- this only ever runs the copy once, the first time."""
    _ensure_config_file_from_example(config_dir, "sources")


def _ensure_layout_file(config_dir: Path) -> None:
    """First-run bootstrap for config/layout.json, same never-overwrite
    logic as _ensure_sources_file (see _ensure_config_file_from_example).
    config/layout.json now holds personal settings (title, human_labels,
    timezone) alongside panel geometry, so it is gitignored like
    sources.json; config/layout.example.json ships instead, with those
    personal values neutralised (title "CritBoard", human_labels [],
    timezone "UTC") and the product-default panel layout otherwise
    unchanged. An existing layout.json -- including one with a real
    title/human_labels already in it -- is NEVER touched: a user's panel
    arrangement is theirs, and an upstream `git pull` must not silently
    rearrange it."""
    _ensure_config_file_from_example(config_dir, "layout")


def _env_override(key: str):
    env_key = f"CRITDASH_{key.upper()}"
    return os.environ.get(env_key)


@dataclass
class Config:
    sources: dict = field(default_factory=dict)
    pricing: dict = field(default_factory=dict)
    dashboard_root: Path = DASHBOARD_ROOT
    config_dir: Path = CONFIG_DIR
    server_dir: Path = SERVER_DIR

    @property
    def layout_path(self) -> Path:
        return self.config_dir / "layout.json"

    @property
    def theme_path(self) -> Path:
        return self.config_dir / "theme.json"

    @property
    def sources_path(self) -> Path:
        return self.config_dir / "sources.json"

    @property
    def pricing_path(self) -> Path:
        return self.config_dir / "pricing.json"

    @property
    def db_path(self) -> Path:
        raw = self.sources.get("db_path", "server/data/critdash.db")
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = self.dashboard_root / p
        return p

    def expand(self, key: str) -> str:
        val = self.sources.get(key, "")
        if val is None:
            # A key present with an explicit JSON `null` (e.g. install.sh
            # recording "genuinely not found anywhere") must behave the
            # same as the key being absent -- str(None) == "None" would
            # otherwise silently become a truthy, nonsense path.
            return ""
        return os.path.expanduser(os.path.expandvars(str(val)))

    def interval(self, name: str) -> float:
        override = _env_override(f"interval_{name}")
        if override:
            try:
                return float(override)
            except ValueError:
                pass
        base = float(self.sources.get("intervals_s", {}).get(name, 30))
        return base * self.refresh_multiplier()

    def remote_interval(self) -> float:
        base = float(self.sources.get("remote_interval_s", 120))
        return base * self.refresh_multiplier()

    def refresh_multiplier(self) -> float:
        """Settings panel cadence preset (briefing Task 5): "normal" (1x,
        today's intervals_s numbers unchanged) or "relaxed" (~3x longer --
        for a laptop on battery). Applied as a multiplier over the existing
        per-collector numbers rather than a second set of interval values, so
        config/sources.example.json's documented defaults stay the single
        source of truth. An unrecognized value is treated as "normal" (1x),
        never an error -- a typo'd preset must not silently slow every
        collector to nothing."""
        preset = self.sources.get("refresh_preset", "normal")
        return 3.0 if preset == "relaxed" else 1.0


def load_config(create: bool = True) -> Config:
    """create=False reads config/*.json if present but never creates or
    modifies anything on disk -- no config/sources.json or
    config/layout.json bootstrap from the .example files. Used by
    critdash.doctor (make doctor / install.sh --doctor / --probe), which
    must be side-effect-free even against a fresh clone that has neither
    file yet; every other caller (the running app) keeps the default
    create=True first-run bootstrap."""
    if create:
        _ensure_sources_file(CONFIG_DIR)
        _ensure_layout_file(CONFIG_DIR)
    sources = _load_json(CONFIG_DIR / "sources.json", DEFAULT_SOURCES)
    pricing = _load_json(CONFIG_DIR / "pricing.json", DEFAULT_PRICING)

    bind_host = _env_override("bind_host") or sources.get("bind_host", "127.0.0.1")
    bind_port_raw = _env_override("bind_port") or sources.get("bind_port", 9999)
    sources = dict(sources)
    sources["bind_host"] = bind_host
    sources["bind_port"] = int(bind_port_raw)

    db_override = _env_override("db_path")
    if db_override:
        sources["db_path"] = db_override

    # Bug fix: Config.config_dir/dashboard_root/server_dir are dataclass
    # fields whose declared defaults (`= CONFIG_DIR`, `= DASHBOARD_ROOT`,
    # `= SERVER_DIR`) are bound ONCE, at class-definition time (this
    # module's first import) -- not re-evaluated per call. Relying on those
    # defaults here silently ignored CRITDASH_CONFIG_DIR and any test's
    # `monkeypatch.setattr(config_mod, "CONFIG_DIR", ...)` for config_dir
    # specifically (load_config()'s own CONFIG_DIR-based lookups above, and
    # sources["bind_host"] etc., were unaffected -- they read the module
    # global fresh on every call). A Config built via the old
    # `Config(sources=sources, pricing=pricing)` therefore had a CORRECTLY
    # isolated `.sources` dict but a `.config_dir` (and `.layout_path`/
    # `.theme_path`) still pointing at the REAL, un-isolated config
    # directory -- exactly the kind of test-isolation gap that lets a test
    # believe it's writing to a tmp_path config dir while actually writing
    # to a real machine's config/layout.json or config/theme.json. Passing
    # these three explicitly makes every Config instance reflect whatever
    # CONFIG_DIR/DASHBOARD_ROOT/SERVER_DIR are AT CALL TIME, matching every
    # other CONFIG_DIR-derived value load_config() already computes.
    return Config(sources=sources, pricing=pricing, config_dir=CONFIG_DIR,
                  dashboard_root=DASHBOARD_ROOT, server_dir=SERVER_DIR)
