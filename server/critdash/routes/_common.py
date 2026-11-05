"""Request and config-file helpers shared by the route modules."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from fastapi import HTTPException, Request

from ..config import Config
from ..tzutil import read_layout_timezone

# POST /api/config/layout|theme write server-side config from a browser
# request. Defaults bind to 127.0.0.1, but a user may bind 0.0.0.0 on a
# LAN -- see AGENTS.md briefing's security section. _MAX_CONFIG_BODY_BYTES
# caps the request body (these documents are a few KB; 1MB is generous
# headroom, not a real limit for a legitimate save) against someone on the
# same network sending an oversized body at an exposed instance.
_MAX_CONFIG_BODY_BYTES = 1_000_000

def _read_layout_timezone(config: Config) -> str:
    return read_layout_timezone(config)


def _read_human_labels(config: Config) -> list[str]:
    """config/layout.json's "human_labels" (the bead labels that mean "a
    person owns this, not the fleet") -- read fresh on every call, same
    reasoning as _read_layout_timezone: a hand edit or a settings-panel save
    must take effect on the very next bead-reply request, no restart. Any
    problem reading/parsing the file, or a malformed value, is treated as
    "no human labels configured" (empty list) rather than 500ing -- this
    gates a feature that is off by default anyway (see bead_reply.py)."""
    try:
        with config.layout_path.open() as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(doc, dict):
        return []
    labels = doc.get("human_labels")
    if not isinstance(labels, list):
        return []
    return [label.strip() for label in labels if isinstance(label, str) and label.strip()]


async def _read_config_body(request: Request) -> dict:
    """Shared body handling for the config POST endpoints: size-capped read,
    then JSON-object parse. Raises HTTPException (413/400) rather than
    returning an error value, so callers can just `body = await
    _read_config_body(request)`."""
    raw = await request.body()
    if len(raw) > _MAX_CONFIG_BODY_BYTES:
        raise HTTPException(status_code=413, detail="request body too large")
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"malformed JSON body: {exc.msg}") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    return body


def _check_config_writes_allowed(config: Config) -> None:
    if not config.sources.get("allow_config_writes", True):
        raise HTTPException(
            status_code=403,
            detail="config writes are disabled (allow_config_writes is false in sources.json)",
        )


def _safe_config_path(path: Path, config_dir: Path) -> Path:
    """Defense-in-depth: both config POSTs only ever pass a fixed constant
    path (config.layout_path / config.theme_path), so this can't be tripped
    today -- but it guarantees a write can never land outside config_dir even
    if that ever changes, without echoing a filesystem path back to the
    browser (see AGENTS.md briefing's security section)."""
    resolved = path.resolve()
    if not resolved.is_relative_to(config_dir.resolve()):
        raise HTTPException(status_code=400, detail="invalid config path")
    return resolved



def _atomic_write_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        bak = path.with_suffix(path.suffix + ".bak")
        bak.write_bytes(path.read_bytes())
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
