#!/usr/bin/env bash
# install.sh -- one-command setup for CritBoard (critfusion/critboard).
#
# Usage:
#   ./install.sh [--port N] [--bind ADDR] [--service] [--start] [--help]
#   ./install.sh --doctor
#
#   --port N      Port to listen on. Default: 9999. Ignored if
#                 config/sources.json already exists (see below).
#   --bind ADDR   Address to bind. Default: 127.0.0.1 (loopback only -- safer
#                 default than 0.0.0.0; pass --bind 0.0.0.0 to listen on
#                 every interface, e.g. to reach it from another machine).
#   --service     Also install and enable a systemd --user unit
#                 (~/.config/systemd/user/critdash.service) so the dashboard
#                 survives a reboot/logout. Requires systemd --user to be
#                 available; skipped with a message otherwise. Unit name is
#                 kept as `critdash.service` -- see INSTALL.md "legacy name".
#   --start       Start the dashboard directly (no systemd) as a background
#                 process, and wait for /api/healthz to answer. Useful for a
#                 one-shot smoke test or a non-systemd environment. PID file:
#                 server/data/critdash.pid.
#   --doctor      Print configured vs. detected tool/data paths (bd_bin,
#                 herdr_bin, beads_env, claude_projects_dir, kimi_dir,
#                 overlord_dir, the quota auth paths, plus ssh/git/uv) and
#                 exit. Non-zero exit means a configured path is missing
#                 while critdash.detect found a working one elsewhere --
#                 the "I have bd installed but the dashboard disagrees" bug.
#                 Runs nothing else; does not touch config/sources.json.
#
# Idempotent: safe to re-run. A second run never overwrites an existing
# config/sources.json, never clobbers an already-installed systemd unit
# with different content, and re-running the dependency install step is a
# no-op if nothing changed.
#
# Prerequisites: python3 (>=3.11) OR `uv` (https://docs.astral.sh/uv/,
# preferred -- it provisions a matching Python itself if the system one is
# too old) and `git`. ssh/bd/herdr are optional -- see INSTALL.md.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$SCRIPT_DIR"
SERVER_DIR="$ROOT/server"
CONFIG_DIR="${CRITDASH_CONFIG_DIR:-$ROOT/config}"

PORT=9999
BIND=127.0.0.1
DO_SERVICE=0
DO_START=0
DO_DOCTOR=0

usage() {
    sed -n '2,34p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --port=*) PORT="${1#*=}"; shift ;;
        --bind) BIND="$2"; shift 2 ;;
        --bind=*) BIND="${1#*=}"; shift ;;
        --service) DO_SERVICE=1; shift ;;
        --start) DO_START=1; shift ;;
        --doctor) DO_DOCTOR=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "install.sh: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

log()  { printf 'install.sh: %s\n' "$*"; }
fail() { printf 'install.sh: ERROR: %s\n' "$*" >&2; exit 1; }

# Runs `python -m critdash.doctor` (see server/critdash/doctor.py) with
# whatever Python this install actually has -- the venv if it exists yet
# (normal case: step 2 below always runs before this is ever called),
# `uv run` if not, else a bare python3 (doctor.py is stdlib-only, so this
# always works even before `make install`/./install.sh has run at all).
# Propagates doctor's own exit code (0 = no mismatch, 1 = mismatch found).
run_doctor() {
    if [ -x "$SERVER_DIR/.venv/bin/python" ]; then
        (cd "$SERVER_DIR" && "$SERVER_DIR/.venv/bin/python" -m critdash.doctor)
    elif command -v uv >/dev/null 2>&1; then
        (cd "$SERVER_DIR" && uv run python -m critdash.doctor)
    else
        (cd "$SERVER_DIR" && python3 -m critdash.doctor)
    fi
}

if [ "$DO_DOCTOR" -eq 1 ]; then
    run_doctor
    exit $?
fi

# -- 1. prerequisites ---------------------------------------------------------

command -v git >/dev/null 2>&1 || fail "git is required and was not found on PATH."

HAVE_UV=0
command -v uv >/dev/null 2>&1 && HAVE_UV=1

PY_FLOOR_OK=0
if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' && PY_FLOOR_OK=1
fi

if [ "$HAVE_UV" -eq 0 ] && [ "$PY_FLOOR_OK" -eq 0 ]; then
    fail "need Python >=3.11 on PATH, or 'uv' installed (https://docs.astral.sh/uv/) -- uv can provision a matching Python itself. Neither was found."
fi

for bin in ssh bd herdr; do
    if ! command -v "$bin" >/dev/null 2>&1; then
        log "optional: '$bin' not found on PATH -- $(
            case "$bin" in
                ssh) echo "multi-host fleet collection stays inactive (single-host mode still works fully)." ;;
                bd) echo "the beads panel stays inactive." ;;
                herdr) echo "pane-level agent detection stays inactive (session-based agent detection from ~/.claude/projects still works)." ;;
            esac
        )"
    fi
done

# -- 2. venv + deps ------------------------------------------------------------

if [ "$HAVE_UV" -eq 1 ]; then
    log "using uv to create the venv and install dependencies (this also provisions a compatible Python if needed)..."
    (cd "$SERVER_DIR" && uv sync) || fail "uv sync failed."
else
    if [ ! -x "$SERVER_DIR/.venv/bin/python" ]; then
        log "creating venv with python3 (no uv found)..."
        python3 -m venv "$SERVER_DIR/.venv" || fail "python3 -m venv failed."
    else
        log "venv already exists at server/.venv -- reusing it."
    fi
    "$SERVER_DIR/.venv/bin/pip" install --upgrade pip >/dev/null || fail "pip upgrade failed."
    log "installing dependencies with pip..."
    "$SERVER_DIR/.venv/bin/pip" install -e "$SERVER_DIR" || fail "pip install failed."
fi

# -- 3. config ------------------------------------------------------------------

SOURCES_JSON="$CONFIG_DIR/sources.json"
SOURCES_EXAMPLE="$CONFIG_DIR/sources.example.json"

if [ -f "$SOURCES_JSON" ]; then
    log "config/sources.json already exists -- leaving it untouched (--port/--bind ignored; edit that file directly to change them)."
    log "checking its tool/data paths against what's actually on this machine (full table: 'make doctor' or './install.sh --doctor')..."
    if run_doctor; then
        log "doctor: no mismatches -- every configured path either exists or has no working alternative anyway."
    else
        log "doctor: WARNING -- see the MISMATCH row(s) printed above. A path in config/sources.json is missing, but critdash.detect found a working one at a different location (its DETECTED column) -- that tool IS installed, just not where sources.json says. Update the matching key in config/sources.json to the DETECTED value, or that panel stays inactive even though the tool works."
    fi
else
    [ -f "$SOURCES_EXAMPLE" ] || fail "config/sources.example.json is missing -- cannot bootstrap a config."
    mkdir -p "$CONFIG_DIR"
    PORT="$PORT" BIND="$BIND" SRC="$SOURCES_EXAMPLE" DST="$SOURCES_JSON" PYTHONPATH="$SERVER_DIR" python3 - <<'PYEOF'
import json
import os
import sys

sys.path.insert(0, os.environ["PYTHONPATH"])
from critdash import detect  # noqa: E402

with open(os.environ["SRC"]) as f:
    doc = json.load(f)
doc["bind_port"] = int(os.environ["PORT"])
doc["bind_host"] = os.environ["BIND"]

# Resolve absolute paths for the machine running this install instead of
# shipping whatever machine config/sources.example.json was last edited
# on -- see critdash/detect.py's module docstring for the bug this fixes
# (a Mac install where beads WAS installed, just not at the Debian path
# baked into the example file). Left unchanged (the example's original
# value) when nothing is found anywhere -- that's a normal "not installed,
# panel stays inactive" state, not a reason to write a broken value.
found, not_found = [], []
for key, name in (("bd_bin", "bd"), ("herdr_bin", "herdr")):
    resolved = detect.resolve_binary(doc.get(key), name)
    if resolved:
        doc[key] = resolved
        found.append(f"{key}={resolved}")
    else:
        not_found.append(key)

with open(os.environ["DST"], "w") as f:
    json.dump(doc, f, indent=2)
    f.write("\n")

if found:
    print("install.sh: resolved for this machine: " + ", ".join(found))
if not_found:
    print(
        "install.sh: not found on this machine (their panels stay inactive -- fine "
        "if you don't use them): " + ", ".join(not_found)
    )
PYEOF
    log "wrote config/sources.json (bind_host=$BIND, bind_port=$PORT)."
    log "full tool/data path table:"
    run_doctor || true
fi

# -- 4. optional systemd --user unit --------------------------------------------

if [ "$DO_SERVICE" -eq 1 ]; then
    if ! command -v systemctl >/dev/null 2>&1; then
        log "--service requested but systemctl was not found -- skipping. Run the app with 'make run' instead."
    elif ! systemctl --user show-environment >/dev/null 2>&1; then
        log "--service requested but a systemd --user session is not available here -- skipping. Run the app with 'make run' instead."
    else
        UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
        UNIT_PATH="$UNIT_DIR/critdash.service"
        mkdir -p "$UNIT_DIR"
        NEW_UNIT_CONTENT="$(cat <<UNITEOF
[Unit]
Description=CritBoard backend (legacy unit name critdash.service)
After=network.target

[Service]
Type=simple
WorkingDirectory=$SERVER_DIR
ExecStart=$SERVER_DIR/.venv/bin/uvicorn critdash.main:app --host $BIND --port $PORT
Restart=always
RestartSec=3
TimeoutStopSec=10
KillMode=mixed
Environment=PYTHONUNBUFFERED=1
Environment=PATH=$HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin

[Install]
WantedBy=default.target
UNITEOF
)"
        if [ -f "$UNIT_PATH" ] && [ "$(cat "$UNIT_PATH")" != "$NEW_UNIT_CONTENT" ]; then
            log "an existing $UNIT_PATH has different content -- leaving it untouched. Remove it first if you want install.sh to replace it."
        else
            printf '%s\n' "$NEW_UNIT_CONTENT" > "$UNIT_PATH"
            systemctl --user daemon-reload
            systemctl --user enable --now critdash.service && log "systemd --user unit installed, enabled, and started." \
                || log "systemd unit written but 'enable --now' failed -- check 'systemctl --user status critdash.service'."
        fi
    fi
fi

# -- 5. optional direct start (no systemd) --------------------------------------

if [ "$DO_START" -eq 1 ]; then
    mkdir -p "$SERVER_DIR/data"
    PIDFILE="$SERVER_DIR/data/critdash.pid"
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        log "already running (pid $(cat "$PIDFILE"))."
    else
        log "starting uvicorn in the background..."
        # No subshell/cd here on purpose: `--app-dir` points uvicorn at
        # server/ without changing this script's own cwd (nothing in the app
        # depends on cwd -- config.py resolves every path from __file__, not
        # cwd). `setsid` (falling back to plain nohup if setsid isn't
        # available) fully detaches the new process into its own session --
        # plain `nohup ... &` only ignores SIGHUP and redirects output, it
        # does NOT detach from this shell's process group/job table, and a
        # long-running child left attached there can make this script hang
        # at exit waiting on its own background job.
        DETACH=(nohup)
        command -v setsid >/dev/null 2>&1 && DETACH=(setsid nohup)
        CRITDASH_CONFIG_DIR="$CONFIG_DIR" \
            "${DETACH[@]}" "$SERVER_DIR/.venv/bin/uvicorn" critdash.main:app --app-dir "$SERVER_DIR" \
            --host "$BIND" --port "$PORT" \
            > "$SERVER_DIR/data/critdash.log" 2>&1 < /dev/null &
        UVICORN_PID=$!
        echo "$UVICORN_PID" > "$PIDFILE"
        disown "$UVICORN_PID" 2>/dev/null || disown 2>/dev/null || true
        for _ in $(seq 1 30); do
            if curl -fs "http://127.0.0.1:$PORT/api/healthz" >/dev/null 2>&1; then
                log "healthz OK."
                break
            fi
            sleep 0.5
        done
    fi
fi

# -- 6. done ----------------------------------------------------------------------

log "done. Dashboard URL: http://$([ "$BIND" = "0.0.0.0" ] && echo 127.0.0.1 || echo "$BIND"):$PORT/"
if [ "$DO_SERVICE" -eq 0 ] && [ "$DO_START" -eq 0 ]; then
    log "not started. Run 'make run' for the foreground process, or re-run with --service (systemd) or --start (background)."
fi
exit 0
