"""HTTP routes, one module per area. build_app() in main.py calls each
module's register(app, deps) in a fixed order; tests/test_route_map.py
pins that order."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..collectors import Scheduler
from ..config import Config
from ..ctx import AppContext
from ..state import SnapshotStore
from ..store import Store
from ..version import VersionTracker


@dataclass
class Deps:
    """Shared objects build_app() creates once per app instance."""

    config: Config
    store: Store
    app_ctx: AppContext
    snap: SnapshotStore
    scheduler: Scheduler
    inactive_health: dict[str, dict]
    resolve_bd_bin: Callable[[], str]
    beads_dir: str
    version_tracker: VersionTracker
