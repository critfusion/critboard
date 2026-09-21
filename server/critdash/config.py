"""Load config/*.json with env overrides prefixed CRITDASH_.

config/sources.json is machine-local (repo roots, hostnames, credential
paths) and is gitignored -- it is never shipped. config/sources.example.json
ships instead, with generic, documented defaults. On startup, if
config/sources.json is missing, it is created from the example (see
_ensure_sources_file below) and the file already on disk is never touched or
overwritten.
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
    "beads_env": "~/.config/beads/env",
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
    "update_repo": "critfusion/critboard",
    "update_branch": "main",
    "update_check_min_interval_s": 300,
    "ssh_opts": [
        "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new",
    ],
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


def _ensure_sources_file(config_dir: Path) -> None:
    """First-run bootstrap: config/sources.json is gitignored (it holds
    machine-local repo roots, hostnames, and credential paths) and is never
    shipped. If it's missing, seed it from config/sources.example.json (which
    IS shipped, with generic defaults) so a fresh install has a working
    config with zero manual steps. An existing sources.json is NEVER
    overwritten -- this only ever runs the copy once, the first time."""
    target = config_dir / "sources.json"
    if target.exists():
        return
    example = config_dir / "sources.example.json"
    if not example.exists():
        return
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(example.read_text())
        logger.info("config/sources.json not found -- created from config/sources.example.json")
    except OSError as exc:
        logger.warning("could not create config/sources.json from the example: %s", exc)


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
        return os.path.expanduser(os.path.expandvars(str(val)))

    def interval(self, name: str) -> float:
        override = _env_override(f"interval_{name}")
        if override:
            try:
                return float(override)
            except ValueError:
                pass
        return float(self.sources.get("intervals_s", {}).get(name, 30))


def load_config() -> Config:
    _ensure_sources_file(CONFIG_DIR)
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

    return Config(sources=sources, pricing=pricing)
