#!/usr/bin/env bash
# install.sh -- one-command setup for CritBoard (critfusion/critboard).
#
# Usage:
#   ./install.sh [--port N] [--bind ADDR] [--service] [--start] [--help]
#   ./install.sh --doctor
#   ./install.sh --probe --json
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
#                 herdr_bin, beads_env, beads_dir, claude_projects_dir,
#                 kimi_dir, overlord_dir, the quota auth paths, ssh/git/uv/curl)
#                 and exit. Non-zero exit means a configured path is missing
#                 while critdash.detect found a working one elsewhere --
#                 the "I have bd installed but the dashboard disagrees" bug.
#                 Runs nothing else; does not touch config/sources.json.
#   --probe       Machine-readable version of --doctor, for an installing
#                 agent instead of a human. Combine with --json (the only
#                 supported/intended use: `--probe --json`) to print ONE
#                 JSON object to stdout and nothing else, and touch nothing
#                 on disk -- no venv, no config/sources.json, no server
#                 start. Exits 0 if the JSON's "ready" field is true, 1
#                 otherwise. See INSTALL.md "For coding agents" for the
#                 schema and the discover -> override -> re-probe loop this
#                 is built for. `--probe` without `--json` prints the same
#                 human table as `--doctor`.
#
# -- Explicit overrides (skip detection for that one tool, used verbatim) --
#
#   An override is validated (existence, executable bit, and -- for
#   --python -- the version floor below) even though detection is skipped;
#   an override that fails validation is a hard error, not a silent
#   fallback to auto-detection. Use these when detection guessed wrong, or
#   to hand over a tool an installing agent just installed itself.
#
#   --python PATH   Exact Python interpreter to use. Must exist, be
#                   executable, and report version >= 3.11 when run --
#                   `PATH -c 'import sys; print(sys.version_info[:3])'`.
#   --git PATH      Exact `git` binary.
#   --uv PATH       Exact `uv` binary.
#   --ssh PATH      Exact `ssh` binary (optional feature -- multi-host).
#   --bd PATH       Exact `bd` (beads) binary (optional feature).
#   --herdr PATH    Exact `herdr` binary (optional feature).
#   --curl PATH     Exact `curl` binary (only used by --start's healthz
#                   check).
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
DO_PROBE=0
DO_JSON=0

# Explicit overrides (--python/--git/--uv/--ssh/--bd/--herdr/--curl): empty
# means "detect it"; non-empty is used verbatim (still validated -- see
# validate_binary_override()/validate_python_override() below), skipping
# find_binary()/select_python() entirely for that tool.
PYTHON_OVERRIDE=""
GIT_OVERRIDE=""
UV_OVERRIDE=""
SSH_OVERRIDE=""
BD_OVERRIDE=""
HERDR_OVERRIDE=""
CURL_OVERRIDE=""

# Binary search order, tried only after PATH (command -v) has already
# failed. MUST stay in sync with server/critdash/detect.py's
# BINARY_CANDIDATE_DIRS -- server/tests/test_install_candidate_dirs.py
# asserts these two lists agree (run `./install.sh --print-candidate-dirs`
# to see what this list resolves to). A non-interactive shell (the common
# case on macOS: cron, a systemd-less launch, an agent's shell) often does
# not have Homebrew's directories on PATH even though the tools are right
# there -- this is what finds them anyway instead of failing the install.
# shellcheck disable=SC2088  # literal ~ entries, expanded manually below via $HOME -- not shell tilde-expansion
BINARY_CANDIDATE_DIRS=(
    "~/.local/bin"
    "/opt/homebrew/bin"
    "/usr/local/bin"
    "/opt/local/bin"
    "/usr/bin"
)

# Python interpreter names to probe, in this order, at each search location
# (PATH, then each of BINARY_CANDIDATE_DIRS): the bare `python3` first,
# then versioned names newest to oldest, down to the floor (3.11 -- see
# PY_FLOOR checks below). Real bug this exists to fix: Homebrew's python
# formula installs the *versioned* binary (e.g. python3.12) into
# /opt/homebrew/bin and does not always place a `python3` symlink beside
# it, so a perfectly good interpreter meeting the floor was reported "not
# found" just because it wasn't named `python3`. MUST stay in sync with
# server/critdash/detect.py's PYTHON_INTERPRETER_NAMES --
# server/tests/test_install_candidate_dirs.py asserts the two lists agree
# (run `./install.sh --print-python-names` to see what this resolves to).
# `git`/`ssh` don't get this treatment: no versioned-binary convention.
PYTHON_INTERPRETER_NAMES=(
    "python3"
    "python3.14"
    "python3.13"
    "python3.12"
    "python3.11"
)

usage() {
    sed -n '2,67p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
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
        --probe) DO_PROBE=1; shift ;;
        --json) DO_JSON=1; shift ;;
        --python) PYTHON_OVERRIDE="$2"; shift 2 ;;
        --python=*) PYTHON_OVERRIDE="${1#*=}"; shift ;;
        --git) GIT_OVERRIDE="$2"; shift 2 ;;
        --git=*) GIT_OVERRIDE="${1#*=}"; shift ;;
        --uv) UV_OVERRIDE="$2"; shift 2 ;;
        --uv=*) UV_OVERRIDE="${1#*=}"; shift ;;
        --ssh) SSH_OVERRIDE="$2"; shift 2 ;;
        --ssh=*) SSH_OVERRIDE="${1#*=}"; shift ;;
        --bd) BD_OVERRIDE="$2"; shift 2 ;;
        --bd=*) BD_OVERRIDE="${1#*=}"; shift ;;
        --herdr) HERDR_OVERRIDE="$2"; shift 2 ;;
        --herdr=*) HERDR_OVERRIDE="${1#*=}"; shift ;;
        --curl) CURL_OVERRIDE="$2"; shift 2 ;;
        --curl=*) CURL_OVERRIDE="${1#*=}"; shift ;;
        # Internal/test-only: prints BINARY_CANDIDATE_DIRS, one per line, so
        # a test can assert it matches detect.py's BINARY_CANDIDATE_DIRS
        # without parsing this script's shell syntax from Python.
        --print-candidate-dirs)
            printf '%s\n' "${BINARY_CANDIDATE_DIRS[@]}"
            exit 0
            ;;
        # Internal/test-only: same idea, for PYTHON_INTERPRETER_NAMES, so a
        # test can assert it matches detect.py's PYTHON_INTERPRETER_NAMES.
        --print-python-names)
            printf '%s\n' "${PYTHON_INTERPRETER_NAMES[@]}"
            exit 0
            ;;
        -h|--help) usage; exit 0 ;;
        *) echo "install.sh: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

log()  { printf 'install.sh: %s\n' "$*"; }
fail() { printf 'install.sh: ERROR: %s\n' "$*" >&2; exit 1; }

# find_binary NAME -- prints the resolved absolute path to stdout and
# returns 0 (PATH first via `command -v`, then BINARY_CANDIDATE_DIRS in
# order); prints nothing and returns 1 if not found anywhere. Silent on
# purpose (no log output) so it can be used internally (e.g. by
# run_doctor(), before the prerequisites section below has run) without
# double-reporting -- see report_binary() for the version that logs.
find_binary() {
    local name="$1" found d expanded
    found="$(command -v "$name" 2>/dev/null)"
    if [ -n "$found" ]; then
        printf '%s\n' "$found"
        return 0
    fi
    for d in "${BINARY_CANDIDATE_DIRS[@]}"; do
        expanded="${d/#\~/$HOME}"
        if [ -f "$expanded/$name" ] && [ -x "$expanded/$name" ]; then
            printf '%s\n' "$expanded/$name"
            return 0
        fi
    done
    return 1
}

# find_python_candidates -- prints every python3* interpreter found, one
# absolute path per line, by filename only (no version check yet -- see
# select_python()): PATH first (for each name in PYTHON_INTERPRETER_NAMES,
# in that order), then BINARY_CANDIDATE_DIRS for each name in the same
# order. Deduplicated, order of first appearance preserved.
find_python_candidates() {
    local name d expanded path seen=" "
    for name in "${PYTHON_INTERPRETER_NAMES[@]}"; do
        path="$(command -v "$name" 2>/dev/null)"
        if [ -n "$path" ]; then
            case "$seen" in
                *" $path "*) ;;
                *) seen="$seen$path "; printf '%s\n' "$path" ;;
            esac
        fi
    done
    for name in "${PYTHON_INTERPRETER_NAMES[@]}"; do
        for d in "${BINARY_CANDIDATE_DIRS[@]}"; do
            expanded="${d/#\~/$HOME}"
            path="$expanded/$name"
            if [ -f "$path" ] && [ -x "$path" ]; then
                case "$seen" in
                    *" $path "*) ;;
                    *) seen="$seen$path "; printf '%s\n' "$path" ;;
                esac
            fi
        done
    done
}

# python_interpreter_version PATH -- executes PATH (never trusts the
# filename -- a `python3` on PATH may be 3.9, a name is not a guarantee)
# and prints its real "X.Y.Z" version to stdout, or prints nothing and
# returns 1 if PATH can't be run or isn't actually a Python interpreter.
python_interpreter_version() {
    "$1" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null
}

# python_version_num "X.Y.Z" -- prints a single comparable integer
# (major*1000000 + minor*1000 + patch) so two versions can be compared with
# plain arithmetic `-ge`/`-gt` instead of a lexical string compare, which
# gets "3.9" vs "3.11" backwards (as strings, "3.11" < "3.9").
python_version_num() {
    local major minor patch
    IFS='.' read -r major minor patch <<EOF
$1
EOF
    major=${major:-0}; minor=${minor:-0}; patch=${patch:-0}
    printf '%d\n' $((major * 1000000 + minor * 1000 + patch))
}

# select_python -- scans find_python_candidates(), executes each one to
# confirm its real version, and selects the NEWEST candidate that meets
# the floor (3.11, see PY_FLOOR_NUM below) -- not merely the first name
# matched, since a versioned name found later in the probe order (e.g.
# python3.12) can be newer than an earlier one (e.g. a `python3` that
# turns out to be 3.9). Sets globals (never local -- callers read them
# after calling this): PYTHON_BIN / PYTHON_BIN_VERSION (the selection, or
# both empty if nothing met the floor), and PYTHON_BELOW_FLOOR_BIN /
# PYTHON_BELOW_FLOOR_VERSION (the newest below-floor candidate found, if
# any -- lets a caller say "found Python 3.9, need >= 3.11" instead of
# just "not found").
PY_FLOOR_NUM="$(python_version_num "3.11.0")"
select_python() {
    PYTHON_BIN=""
    PYTHON_BIN_VERSION=""
    PYTHON_BELOW_FLOOR_BIN=""
    PYTHON_BELOW_FLOOR_VERSION=""
    # --python bypasses the scan entirely -- validate_python_override()
    # (called right after arg parsing, before any of this runs) has already
    # confirmed PYTHON_OVERRIDE exists, is executable, and meets the floor,
    # so every caller of select_python() picks it up automatically.
    if [ -n "$PYTHON_OVERRIDE" ]; then
        PYTHON_BIN="$PYTHON_OVERRIDE"
        PYTHON_BIN_VERSION="$(python_interpreter_version "$PYTHON_BIN")"
        return 0
    fi
    local candidate version vnum best_num=0 below_num=0
    while IFS= read -r candidate; do
        [ -n "$candidate" ] || continue
        version="$(python_interpreter_version "$candidate")"
        [ -n "$version" ] || continue
        vnum="$(python_version_num "$version")"
        if [ "$vnum" -ge "$PY_FLOOR_NUM" ]; then
            if [ -z "$PYTHON_BIN" ] || [ "$vnum" -gt "$best_num" ]; then
                PYTHON_BIN="$candidate"
                PYTHON_BIN_VERSION="$version"
                best_num="$vnum"
            fi
        else
            if [ -z "$PYTHON_BELOW_FLOOR_BIN" ] || [ "$vnum" -gt "$below_num" ]; then
                PYTHON_BELOW_FLOOR_BIN="$candidate"
                PYTHON_BELOW_FLOOR_VERSION="$version"
                below_num="$vnum"
            fi
        fi
    done <<EOF
$(find_python_candidates)
EOF
}

# report_binary NAME -- same resolution as find_binary(), but also logs
# (to stderr -- stdout is this function's return value, same convention as
# find_binary(), and callers do `x="$(report_binary name)"`) when the
# binary was found ONLY outside PATH: the user's non-interactive PATH is
# incomplete, and they'll want to know that even though this install works
# around it.
report_binary() {
    local name="$1" on_path resolved
    on_path="$(command -v "$name" 2>/dev/null)"
    resolved="$(find_binary "$name")" || return 1
    if [ -z "$on_path" ]; then
        printf 'install.sh: found '\''%s'\'' outside PATH, at %s -- PATH is incomplete in this shell (common in a non-interactive/non-login shell on macOS). Add its directory to PATH (e.g. in ~/.zprofile) to stop relying on this fallback.\n' "$name" "$resolved" >&2
    fi
    printf '%s\n' "$resolved"
}

# Runs `python -m critdash.doctor` (see server/critdash/doctor.py) with
# whatever Python this install actually has -- the venv if it exists yet
# (normal case: step 2 below always runs before this is ever called), else
# whatever select_python() resolves (an explicit --python override, or the
# newest candidate meeting the floor, or -- deliberately -- the newest
# BELOW-floor candidate/bare `python3` if nothing meets it: doctor.py is
# stdlib-only, so it runs fine even under an old interpreter, and --probe
# needs to be able to *report* "nothing meets the floor" via that same
# under-floor interpreter instead of merely failing to run at all).
# Self-contained: also called for --doctor/--probe before the prerequisites
# section below has run, so it cannot rely on that section's resolved
# variables. Extra args ("$@") are passed straight through to
# `critdash.doctor` -- --probe uses this to pass --json. Propagates
# doctor's own exit code (0 = no mismatch / probe ready, 1 = mismatch found
# / probe not ready).
#
# Deliberately never uses `uv run` here: uv auto-syncs (creates/updates
# server/.venv, and can download a whole Python distribution) as a side
# effect of `uv run` in a project directory with no venv yet -- exactly
# the change --probe/--doctor must never make. `uv sync` only ever runs
# explicitly, in step 2 of a real (non-probe, non-doctor) install below.
run_doctor() {
    if [ -x "$SERVER_DIR/.venv/bin/python" ]; then
        (cd "$SERVER_DIR" && "$SERVER_DIR/.venv/bin/python" -m critdash.doctor "$@")
        return $?
    fi
    select_python
    if [ -z "${PYTHON_BIN:-}" ] && [ -z "${PYTHON_BELOW_FLOOR_BIN:-}" ] && ! command -v python3 >/dev/null 2>&1; then
        fail "no Python interpreter at all was found (not even one below the 3.11 floor) to run critdash.doctor itself -- install one (e.g. 'brew install python@3.12', or 'uv', which can provision one) or supply it explicitly: ./install.sh --python /path/to/python3"
    fi
    (cd "$SERVER_DIR" && "${PYTHON_BIN:-${PYTHON_BELOW_FLOOR_BIN:-python3}}" -m critdash.doctor "$@")
}

# validate_binary_override NAME PATH -- an override is used verbatim
# (detection is skipped for that tool entirely), but it is still checked:
# it must exist and be executable, or the install fails clearly right here
# instead of limping on with an unusable path. NAME is only used in the
# message (e.g. "git", "bd").
validate_binary_override() {
    local name="$1" path="$2"
    if [ ! -e "$path" ]; then
        fail "--$name path does not exist: $path"
    fi
    if [ ! -f "$path" ] || [ ! -x "$path" ]; then
        fail "--$name path is not an executable file: $path"
    fi
}

# validate_python_override PATH -- same idea as validate_binary_override,
# plus the version floor: an override below PY_FLOOR is exactly the
# "silent wrong interpreter" failure mode this whole flag exists to avoid,
# so it is a hard, distinct error rather than a generic "not executable".
validate_python_override() {
    local path="$1" version vnum
    if [ ! -e "$path" ]; then
        fail "--python path does not exist: $path"
    fi
    if [ ! -f "$path" ] || [ ! -x "$path" ]; then
        fail "--python path is not an executable file: $path"
    fi
    version="$(python_interpreter_version "$path")"
    if [ -z "$version" ]; then
        fail "--python path does not behave like a Python interpreter (running '$path -c \"import sys\"' failed): $path"
    fi
    vnum="$(python_version_num "$version")"
    if [ "$vnum" -lt "$PY_FLOOR_NUM" ]; then
        fail "--python path $path is version $version, below the required floor of 3.11. It cannot be used. Install a newer interpreter (e.g. 'brew install python@3.12') or install 'uv' (https://docs.astral.sh/uv/), then pass its --python path, or omit --python and let this script find/provision one itself."
    fi
}

[ -n "$PYTHON_OVERRIDE" ] && validate_python_override "$PYTHON_OVERRIDE"
[ -n "$GIT_OVERRIDE" ] && validate_binary_override git "$GIT_OVERRIDE"
[ -n "$UV_OVERRIDE" ] && validate_binary_override uv "$UV_OVERRIDE"
[ -n "$SSH_OVERRIDE" ] && validate_binary_override ssh "$SSH_OVERRIDE"
[ -n "$BD_OVERRIDE" ] && validate_binary_override bd "$BD_OVERRIDE"
[ -n "$HERDR_OVERRIDE" ] && validate_binary_override herdr "$HERDR_OVERRIDE"
[ -n "$CURL_OVERRIDE" ] && validate_binary_override curl "$CURL_OVERRIDE"

# Every override (already validated above, or empty) is exported as a
# CRITDASH_OVERRIDE_* env var unconditionally -- not just for --probe --
# so any `python -m critdash.doctor` this script runs (--doctor's table,
# --probe's JSON, or the doctor check step 3 runs mid-install) reports
# "configured value came from an explicit override, detection was
# skipped" consistently, regardless of which flag got it there.
if [ -n "$PYTHON_OVERRIDE" ]; then
    export CRITDASH_OVERRIDE_PYTHON_PATH="$PYTHON_OVERRIDE"
    export CRITDASH_OVERRIDE_PYTHON_VERSION="$(python_interpreter_version "$PYTHON_OVERRIDE")"
else
    export CRITDASH_OVERRIDE_PYTHON_PATH=""
    export CRITDASH_OVERRIDE_PYTHON_VERSION=""
fi
export CRITDASH_OVERRIDE_GIT="$GIT_OVERRIDE"
export CRITDASH_OVERRIDE_UV="$UV_OVERRIDE"
export CRITDASH_OVERRIDE_SSH="$SSH_OVERRIDE"
export CRITDASH_OVERRIDE_BD="$BD_OVERRIDE"
export CRITDASH_OVERRIDE_HERDR="$HERDR_OVERRIDE"
export CRITDASH_OVERRIDE_CURL="$CURL_OVERRIDE"

# run_probe -- the --probe implementation. Delegates to run_doctor (the
# CRITDASH_OVERRIDE_* env vars above are already set for it). Makes no
# changes: run_doctor only ever reads (see critdash.doctor's module
# docstring and config.load_config(create=False)).
run_probe() {
    if [ "$DO_JSON" -eq 1 ]; then
        run_doctor --json
        local code=$?
        if [ "$code" -ne 0 ]; then
            # JSON on stdout stays pure (see the file-header comment) --
            # this actionable hint goes to stderr. "ready": false only ever
            # comes from a REQUIRED key ("missing_required" in the JSON) --
            # today that's exactly {python, git} -- so naming both covers
            # every case, and the agent can tell which one from the JSON.
            printf 'install.sh: --probe reports NOT READY (see "missing_required" in the JSON above). If a required tool genuinely is not installed anywhere on this machine, install it (see INSTALL.md), then supply it explicitly and re-probe:\n  ./install.sh --python /path/to/python3.11-or-newer --probe --json\n  ./install.sh --git /path/to/git --probe --json\n' >&2
        fi
        return $code
    else
        run_doctor
    fi
}

if [ "$DO_PROBE" -eq 1 ]; then
    run_probe
    exit $?
fi

if [ "$DO_DOCTOR" -eq 1 ]; then
    run_doctor
    exit $?
fi

# -- 1. prerequisites ---------------------------------------------------------

if [ -n "$GIT_OVERRIDE" ]; then
    log "using git (--git override, detection skipped): $GIT_OVERRIDE"
else
    report_binary git >/dev/null \
        || fail "git is required and was not found on PATH or in: ${BINARY_CANDIDATE_DIRS[*]}. Install git, then either add its directory to PATH or supply it explicitly: ./install.sh --git /path/to/git"
fi

HAVE_UV=0
UV_BIN=""
if [ -n "$UV_OVERRIDE" ]; then
    UV_BIN="$UV_OVERRIDE"
    HAVE_UV=1
    log "using uv (--uv override, detection skipped): $UV_BIN"
else
    UV_BIN="$(report_binary uv)" && HAVE_UV=1
fi

PY_FLOOR_OK=0
PYTHON3_BIN=""
select_python
if [ -n "$PYTHON_BIN" ]; then
    PY_FLOOR_OK=1
    PYTHON3_BIN="$PYTHON_BIN"
    if [ -n "$PYTHON_OVERRIDE" ]; then
        log "using Python interpreter (--python override, detection skipped): $PYTHON_BIN (version $PYTHON_BIN_VERSION)."
    else
        if [ "$(command -v "$(basename "$PYTHON_BIN")" 2>/dev/null)" != "$PYTHON_BIN" ]; then
            log "found '$(basename "$PYTHON_BIN")' outside PATH, at $PYTHON_BIN -- PATH is incomplete in this shell (common in a non-interactive/non-login shell on macOS). Add its directory to PATH (e.g. in ~/.zprofile) to stop relying on this fallback."
        fi
        log "using Python interpreter: $PYTHON_BIN (version $PYTHON_BIN_VERSION, floor >= 3.11)."
    fi
fi

if [ "$HAVE_UV" -eq 0 ] && [ "$PY_FLOOR_OK" -eq 0 ]; then
    if [ -n "$PYTHON_BELOW_FLOOR_BIN" ]; then
        fail "found Python at $PYTHON_BELOW_FLOOR_BIN but it is version $PYTHON_BELOW_FLOOR_VERSION, below the required floor of 3.11 -- it cannot be used as-is. Install a newer Python (e.g. 'brew install python@3.12') or install 'uv' (https://docs.astral.sh/uv/), which can provision a matching Python itself. Next command once you have one: ./install.sh --python /path/to/python3.11-or-newer (run ./install.sh --probe --json first if you're not sure what's on this machine)."
    else
        fail "need Python >=3.11 on PATH or in: ${BINARY_CANDIDATE_DIRS[*]} (tried: ${PYTHON_INTERPRETER_NAMES[*]}); or 'uv' installed (https://docs.astral.sh/uv/) -- uv can provision a matching Python itself. Neither was found. Next command once you have one: ./install.sh --python /path/to/python3.11-or-newer (run ./install.sh --probe --json first if you're not sure what's on this machine)."
    fi
fi

for bin in ssh bd herdr; do
    bin_override=""
    case "$bin" in
        ssh) bin_override="$SSH_OVERRIDE" ;;
        bd) bin_override="$BD_OVERRIDE" ;;
        herdr) bin_override="$HERDR_OVERRIDE" ;;
    esac
    if [ -n "$bin_override" ]; then
        log "using '$bin' (--$bin override, detection skipped): $bin_override"
        continue
    fi
    if ! report_binary "$bin" >/dev/null; then
        # A `case` inside a `$( )` command substitution, with a `(` character
        # inside one of its quoted branches, is a real bash 3.2 parser bug
        # (confirmed: it is a hard syntax error, not just a warning) --
        # macOS's shipped bash. Assign the message first instead of nesting
        # the case inside the log call.
        optional_msg=""
        case "$bin" in
            ssh) optional_msg="multi-host fleet collection stays inactive (single-host mode still works fully)." ;;
            bd) optional_msg="the beads panel stays inactive." ;;
            herdr) optional_msg="pane-level agent detection stays inactive (session-based agent detection from ~/.claude/projects still works)." ;;
        esac
        log "optional: '$bin' not found on PATH or in: ${BINARY_CANDIDATE_DIRS[*]} -- $optional_msg If it's installed somewhere else, supply it explicitly: ./install.sh --$bin /path/to/$bin"
    fi
done

# -- 2. venv + deps ------------------------------------------------------------

if [ "$HAVE_UV" -eq 1 ]; then
    log "using uv to create the venv and install dependencies (this also provisions a compatible Python if needed)..."
    (cd "$SERVER_DIR" && "$UV_BIN" sync) || fail "uv sync failed."
else
    if [ ! -x "$SERVER_DIR/.venv/bin/python" ]; then
        log "creating venv with python3 (no uv found)..."
        "$PYTHON3_BIN" -m venv "$SERVER_DIR/.venv" || fail "python3 -m venv failed."
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
    mkdir -p "$CONFIG_DIR" || fail "could not create $CONFIG_DIR."
    # Run with whatever Python this install resolved in step 1 -- PYTHON3_BIN
    # if found, else `uv run python3` (HAVE_UV must be 1 in that case: step 1
    # already fails the whole install if neither is available).
    PY_RUNNER=("$PYTHON3_BIN")
    [ -n "$PYTHON3_BIN" ] || PY_RUNNER=("$UV_BIN" run python3)
    if ! PORT="$PORT" BIND="$BIND" SRC="$SOURCES_EXAMPLE" DST="$SOURCES_JSON" PYTHONPATH="$SERVER_DIR" \
        BD_OVERRIDE="$BD_OVERRIDE" HERDR_OVERRIDE="$HERDR_OVERRIDE" "${PY_RUNNER[@]}" - <<'PYEOF'
import json
import os
import subprocess
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
# --bd/--herdr (BD_OVERRIDE/HERDR_OVERRIDE, already validated by
# validate_binary_override before install.sh got this far) are used
# verbatim instead, skipping detect.resolve_binary entirely for that key.
found, not_found = [], []
for key, name, override_env in (
    ("bd_bin", "bd", "BD_OVERRIDE"), ("herdr_bin", "herdr", "HERDR_OVERRIDE"),
):
    override = os.environ.get(override_env, "").strip()
    if override:
        doc[key] = override
        found.append(f"{key}={override} (--{name} override)")
        continue
    resolved = detect.resolve_binary(doc.get(key), name)
    if resolved:
        doc[key] = resolved
        found.append(f"{key}={resolved}")
    else:
        not_found.append(key)

# beads_dir (briefing Task 1): if bd resolved above, ask it directly which
# workspace it would use right now via `bd where --json` (the documented
# way to find the active one) and write that as beads_dir -- this is what
# fixes the collector running `bd` from CritBoard's own directory instead
# of the user's shell, without the user ever having to figure out the
# BEADS_DIR value themselves. Left empty (the example's default) if bd
# isn't resolved, or if `bd where` fails/finds nothing -- both are normal
# "no workspace yet" states, not failures; `make doctor` surfaces beads_dir
# too, for a later fix.
bd_bin_resolved = doc.get("bd_bin")
if bd_bin_resolved and not (doc.get("beads_dir") or "").strip():
    try:
        bd_where = subprocess.run(
            [bd_bin_resolved, "where", "--json"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if bd_where.returncode == 0:
            workspace = json.loads(bd_where.stdout).get("path")
            if workspace:
                doc["beads_dir"] = workspace
                found.append(f"beads_dir={workspace} (from `bd where --json`)")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        pass

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
print(
    "install.sh: update settings from the example (edit config/sources.json to change): "
    f"update_repo={doc.get('update_repo')!r}, update_check_enabled={doc.get('update_check_enabled')!r}, "
    f"update_check_interval_s={doc.get('update_check_interval_s')!r}, "
    f"update_auto_apply={doc.get('update_auto_apply')!r}"
)
PYEOF
    then
        fail "failed to write config/sources.json -- see the Python error above."
    fi
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
        mkdir -p "$UNIT_DIR" || fail "could not create $UNIT_DIR."
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
    mkdir -p "$SERVER_DIR/data" || fail "could not create $SERVER_DIR/data."
    PIDFILE="$SERVER_DIR/data/critdash.pid"
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        log "already running (pid $(cat "$PIDFILE"))."
    else
        [ -x "$SERVER_DIR/.venv/bin/uvicorn" ] \
            || fail "uvicorn not found at $SERVER_DIR/.venv/bin/uvicorn -- step 2 (venv/dependency install) did not complete successfully. Re-run ./install.sh without --start and check its output before retrying --start."
        if [ -n "$CURL_OVERRIDE" ]; then
            CURL_BIN="$CURL_OVERRIDE"
            log "using curl (--curl override, detection skipped): $CURL_BIN"
        else
            CURL_BIN="$(report_binary curl)" \
                || fail "curl is required to verify the dashboard actually started, and was not found on PATH or in: ${BINARY_CANDIDATE_DIRS[*]}. Supply it explicitly: ./install.sh --curl /path/to/curl --start"
        fi

        # setsid (falling back to plain nohup if setsid isn't available) fully
        # detaches the new process into its own session -- plain `nohup ... &`
        # only ignores SIGHUP and redirects output, it does NOT detach from
        # this shell's process group/job table, and a long-running child left
        # attached there can make this script hang at exit waiting on its own
        # background job. If nohup itself is also missing, fall back further
        # to a plain backgrounded start rather than failing outright --
        # --start is a one-shot smoke test, not the durable path (--service
        # is, and needs neither setsid nor nohup).
        SETSID_BIN="$(report_binary setsid)" || SETSID_BIN=""
        NOHUP_BIN="$(report_binary nohup)" || NOHUP_BIN=""
        DETACH=()
        if [ -n "$SETSID_BIN" ] && [ -n "$NOHUP_BIN" ]; then
            DETACH=("$SETSID_BIN" "$NOHUP_BIN")
        elif [ -n "$NOHUP_BIN" ]; then
            DETACH=("$NOHUP_BIN")
        else
            log "'nohup' not found on PATH or in: ${BINARY_CANDIDATE_DIRS[*]} -- falling back to a plain backgrounded start (chosen fallback: no detach wrapper at all). It still outlives this script, but unlike the nohup/setsid path it is not immune to a SIGHUP delivered to this shell before install.sh exits (e.g. closing the terminal mid-run). Install nohup (coreutils) for a sturdier --start, or use --service, which needs neither."
        fi

        log "starting uvicorn in the background..."
        # No subshell/cd here on purpose: `--app-dir` points uvicorn at
        # server/ without changing this script's own cwd (nothing in the app
        # depends on cwd -- config.py resolves every path from __file__, not
        # cwd).
        # "${DETACH[@]+"${DETACH[@]}"}" (not bare "${DETACH[@]}"): under
        # `set -u`, bash 3.2 treats an empty array's [@] expansion as an
        # unset variable and aborts with "unbound variable" -- see
        # scripts/check-public.sh's comment on the same issue. DETACH is
        # empty exactly in the no-nohup fallback above.
        CRITDASH_CONFIG_DIR="$CONFIG_DIR" \
            "${DETACH[@]+"${DETACH[@]}"}" "$SERVER_DIR/.venv/bin/uvicorn" critdash.main:app --app-dir "$SERVER_DIR" \
            --host "$BIND" --port "$PORT" \
            > "$SERVER_DIR/data/critdash.log" 2>&1 < /dev/null &
        UVICORN_PID=$!
        echo "$UVICORN_PID" > "$PIDFILE"
        disown "$UVICORN_PID" 2>/dev/null || disown 2>/dev/null || true

        # POSIX counter instead of `for _ in $(seq 1 30)`: seq is not
        # guaranteed present (missing on some minimal/macOS setups), and
        # when it's absent `$(seq 1 30)` silently expands to nothing, so the
        # loop body never runs even once -- the health check is skipped
        # entirely and silently, which is the same bug class as never
        # checking its result at all.
        HEALTHZ_OK=0
        count=1
        while [ "$count" -le 30 ]; do
            if "$CURL_BIN" -fs "http://127.0.0.1:$PORT/api/healthz" >/dev/null 2>&1; then
                HEALTHZ_OK=1
                break
            fi
            sleep 0.5
            count=$((count + 1))
        done

        if [ "$HEALTHZ_OK" -eq 1 ]; then
            log "healthz OK."
        else
            log "ERROR: uvicorn did not answer http://127.0.0.1:$PORT/api/healthz within 15s -- the dashboard did NOT start."
            log "last 20 lines of $SERVER_DIR/data/critdash.log:"
            if [ -s "$SERVER_DIR/data/critdash.log" ]; then
                tail -n 20 "$SERVER_DIR/data/critdash.log" >&2
            else
                echo "install.sh: (log file is empty or missing: $SERVER_DIR/data/critdash.log)" >&2
            fi
            kill "$UVICORN_PID" 2>/dev/null || true
            rm -f "$PIDFILE"
            fail "dashboard failed to start -- see the log excerpt above for the real cause."
        fi
    fi
fi

# -- 6. done ----------------------------------------------------------------------

log "done. Dashboard URL: http://$([ "$BIND" = "0.0.0.0" ] && echo 127.0.0.1 || echo "$BIND"):$PORT/"
if [ "$DO_SERVICE" -eq 0 ] && [ "$DO_START" -eq 0 ]; then
    log "not started. Run 'make run' for the foreground process, or re-run with --service (systemd) or --start (background)."
fi
exit 0
