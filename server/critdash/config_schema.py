"""Validation for config/layout.json and config/theme.json documents."""

from __future__ import annotations

from .tzutil import is_valid_timezone


def validate_layout(doc: dict) -> list[str]:
    """Rejects anything that would make the dashboard unrenderable. Unknown
    top-level keys (e.g. a future "timezone" or settings-panel addition) are
    NOT rejected -- forward compatibility -- only the specific shapes below
    that would break rendering: a panel missing required geometry/identity
    fields, two panels overlapping, a panel extending past the configured
    grid width, a non-string title/timezone, or (see the 'panels' check
    below) an empty panel list that would silently wipe every panel."""
    errors = []
    if not isinstance(doc, dict):
        return ["layout must be a JSON object"]
    if "version" not in doc:
        errors.append("missing 'version'")

    grid = doc.get("grid")
    columns = None
    if "grid" not in doc or not isinstance(grid, dict):
        errors.append("missing or invalid 'grid'")
    else:
        for key in ("columns", "row_height", "gap"):
            if key not in grid:
                errors.append(f"grid missing '{key}'")
        if isinstance(grid.get("columns"), int) and not isinstance(grid.get("columns"), bool):
            if grid["columns"] > 0:
                columns = grid["columns"]
            else:
                errors.append("grid.columns must be a positive integer")

    tz = doc.get("timezone")
    if tz is not None:
        if not isinstance(tz, str):
            errors.append("'timezone' must be a string")
        elif not is_valid_timezone(tz):
            errors.append(f"'timezone' is not a recognized IANA timezone name: {tz!r}")

    panels = doc.get("panels")
    if not isinstance(panels, list):
        errors.append("missing or invalid 'panels' (must be a list)")
    elif not panels:
        # Same data-loss shape as validate_theme's empty-colors bug: an empty
        # list IS a list, so `isinstance(panels, list)` alone lets a request
        # that (accidentally or otherwise) drops every panel overwrite
        # layout.json with a dashboard that has none, with no error. A saved
        # layout must keep at least one panel.
        errors.append("'panels' must not be empty")
    else:
        seen_ids = set()
        rects: list[tuple[int, str, float, float, float, float]] = []
        for idx, panel in enumerate(panels):
            if not isinstance(panel, dict):
                errors.append(f"panels[{idx}] must be an object")
                continue
            for key in ("id", "type", "x", "y", "w", "h"):
                if key not in panel:
                    errors.append(f"panels[{idx}] missing '{key}'")
            pid = panel.get("id")
            if pid in seen_ids:
                errors.append(f"duplicate panel id '{pid}'")
            seen_ids.add(pid)

            title = panel.get("title")
            if title is not None and not isinstance(title, str):
                errors.append(f"panels[{idx}] (id={pid!r}) 'title' must be a string")

            x, y, w, h = panel.get("x"), panel.get("y"), panel.get("w"), panel.get("h")
            geometry_ok = all(
                isinstance(v, int | float) and not isinstance(v, bool) for v in (x, y, w, h)
            )
            if geometry_ok:
                if columns is not None and x + w > columns:
                    errors.append(
                        f"panels[{idx}] (id={pid!r}) extends past grid columns "
                        f"({x}+{w} > {columns})"
                    )
                rects.append((idx, pid, x, y, w, h))

        for i in range(len(rects)):
            idx_a, id_a, xa, ya, wa, ha = rects[i]
            for j in range(i + 1, len(rects)):
                idx_b, id_b, xb, yb, wb, hb = rects[j]
                overlaps = xa < xb + wb and xb < xa + wa and ya < yb + hb and yb < ya + ha
                if overlaps:
                    errors.append(
                        f"panels[{idx_a}] (id={id_a!r}) overlaps panels[{idx_b}] (id={id_b!r})"
                    )
    return errors


#  Every `--color-<token>` custom property the frontend actually reads --
#  either directly via `var(--color-<token>)` in web/css/style.css, or
#  JS-constructed (utils.js statusColorVar/severityColorVar/priorityColorVar,
#  interpolated into an inline `style` attribute) in web/js/*.js. Derived
#  with:
#    grep -ohE -- '--color-[a-zA-Z0-9_-]+' web/js/*.js web/css/style.css \
#      | sort -u
#  This is exactly the 28-key set config/theme.json's "colors" object ships
#  with today -- if the frontend starts reading a new token, add it here too,
#  and if a token stops being read anywhere, it can come out.
_REQUIRED_COLOR_TOKENS = (
    "accent-amber", "accent-amber-dim", "accent-cyan", "accent-cyan-dim",
    "agent-glow", "bg", "bg-elevated", "bg-panel", "bg-panel-hover",
    "border", "border-bright", "grid-line",
    "priority-0", "priority-1", "priority-2", "priority-3",
    "severity-crit", "severity-info", "severity-warn",
    "status-crit", "status-done", "status-idle", "status-ok",
    "status-warn", "status-working",
    "text", "text-dim", "text-faint",
)


def validate_theme(doc: dict) -> list[str]:
    """Same "cannot brick the dashboard" contract as validate_layout, scaled
    to theme.json's shape: unknown top-level keys are allowed (forward
    compatibility), but a known section with the wrong type -- one that the
    frontend would fail to apply -- is rejected.

    `colors` is REQUIRED (bug fix): every check used to be gated behind
    `if <field> is not None`, so a document that omitted a field validated
    it, requiring nothing. `POST /api/config/theme` with `{}` therefore
    passed with zero errors and silently overwrote theme.json, wiping every
    colour token and both presets -- while the page kept rendering, because
    style.css's own `:root` block still has a hardcoded value for each
    token, so the loss wasn't visible. `colors` must now be present, be a
    non-empty object, and contain every token in _REQUIRED_COLOR_TOKENS (the
    set the frontend actually consumes -- nothing invented, nothing that
    isn't read somewhere in web/). `fonts`/`radius`/`density`/`motion`/
    `reload_banner`/`_presets` stay optional and forward-compatible, exactly
    as before: type-checked only when present."""
    errors = []
    if not isinstance(doc, dict):
        return ["theme must be a JSON object"]

    colors = doc.get("colors")
    if colors is None:
        errors.append("'colors' is required")
    elif not isinstance(colors, dict):
        errors.append("'colors' must be an object")
    elif not colors:
        errors.append("'colors' must not be empty")
    else:
        for key, value in colors.items():
            if not isinstance(value, str):
                errors.append(f"colors.{key} must be a string")
        missing = [t for t in _REQUIRED_COLOR_TOKENS if t not in colors]
        if missing:
            errors.append("colors missing required token(s): " + ", ".join(missing))

    fonts = doc.get("fonts")
    if fonts is not None:
        if not isinstance(fonts, dict):
            errors.append("'fonts' must be an object")
        else:
            for key, value in fonts.items():
                if not isinstance(value, str):
                    errors.append(f"fonts.{key} must be a string")

    reload_banner = doc.get("reload_banner")
    if reload_banner is not None:
        if not isinstance(reload_banner, dict):
            errors.append("'reload_banner' must be an object")
        elif "enabled" in reload_banner and not isinstance(reload_banner["enabled"], bool):
            errors.append("reload_banner.enabled must be a boolean")

    presets = doc.get("_presets")
    if presets is not None and not isinstance(presets, dict):
        errors.append("'_presets' must be an object")

    return errors

