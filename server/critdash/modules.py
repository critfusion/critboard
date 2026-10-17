"""Opt-in modules (Infrastructure / Workflow / Knowledge).

A default install has none of these: the board is agents + beads only. A
module exists only when config/sources.json (machine-local, never tracked)
has `modules.<name>.enabled` set to the JSON boolean `true`. Anything else
-- key absent, `false`, a string, `null` -- means off. See the `_readme` in
config/sources.example.json.
"""

from __future__ import annotations

# Nav order. "live" is not a module; it is always present.
KNOWN_MODULES = ("infrastructure", "workflow", "knowledge")


def module_config(sources: dict, name: str) -> dict | None:
    """The module's config dict if it is enabled, else None."""
    modules = sources.get("modules")
    if not isinstance(modules, dict) or name not in KNOWN_MODULES:
        return None
    cfg = modules.get(name)
    if isinstance(cfg, dict) and cfg.get("enabled") is True:
        return cfg
    return None


def enabled_modules(sources: dict) -> list[str]:
    return [n for n in KNOWN_MODULES if module_config(sources, n) is not None]
