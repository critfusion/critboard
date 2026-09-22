# Installing CritBoard

## For coding agents

Run this loop. It needs no human, on any machine, including one you've
never seen before:

```
1. Probe:    ./install.sh --probe --json
2. Not ready, or an optional tool you want is "missing"?
   Discover or install the missing piece yourself -- see "Installing
   missing prerequisites" below. You are authorised to do this.
3. Supply it: --python/--git/--uv/--ssh/--bd/--herdr/--curl PATH on the
   command line (an override), or just re-probe if you installed it at
   a normal location -- detection usually finds it without one.
4. Re-run the probe (step 1). Repeat until "ready" is true.
5. Install for real, carrying over any override flags from step 3:
     ./install.sh --port 9999 --bind 127.0.0.1 --start
6. Verify:
     make doctor                                    # exits 0
     curl -fs http://127.0.0.1:9999/api/healthz      # {"ok":true,...}
```

Read **"MISSING vs MISMATCH"** below before step 2 -- it is the single
most common way an agent with install authority does the wrong thing
here (see the beads-server-on-a-laptop example).

### Probe schema (`./install.sh --probe --json`)

Prints one JSON object to stdout and nothing else. Makes **no changes** --
no venv, no `config/sources.json`, no server start; safe to run any number
of times, on a totally fresh clone. Pipe it through `python3 -m json.tool`
to confirm it parses. Exits `0` if `"ready"` is `true`, `1` otherwise (so
a script can check the exit code alone, without parsing JSON, if that's
all it needs).

Top level:

| Field | Type | Meaning |
|---|---|---|
| `ready` | bool | `true` unless a **required** item is `missing`. A `mismatch` never blocks readiness -- see "MISSING vs MISMATCH". |
| `platform` | string | `"Linux"`, `"Darwin"` (macOS), or `"Windows"`. |
| `python` | object | The Python selection (see below) -- not a `checks[]` row, since it's a >=3.11 floor over every candidate found, not one configured path. |
| `checks` | array | One object per tool/data path -- see below. |
| `missing_required` | array of string | Check keys (plus `"python"` if applicable) that are `missing` AND `required`. Empty exactly when `ready` is `true` -- this is the punch list. |

Each `checks[]` entry:

| Field | Type | Meaning |
|---|---|---|
| `key` | string | `bd_bin`, `herdr_bin`, `git`, `uv`, `ssh`, `curl`, `claude_projects_dir`, `kimi_dir`, `overlord_dir`, `beads_env`, `beads_dir`, `opencode_auth_path`, `grok_auth_path`, `codex_auth_path`. |
| `kind` | string | `"binary"` \| `"dir"` \| `"file"`. |
| `collector` | string | Which panel/feature this powers, e.g. `"beads"`, `"agents (pane detection) / remote"`. |
| `configured` | string or null | The value in `config/sources.json`, or the `--<key>` override if one was supplied, or `null` if unset. |
| `configured_exists` | bool | Whether `configured` itself resolves on this machine. |
| `detected` | string or null | What detection found instead. Always `null` when `override` is `true` -- detection is skipped entirely, not merely preferred. |
| `state` | string | `"ok"` \| `"missing"` \| `"mismatch"` \| `"not_workspace"` (`beads_dir` only) -- see "MISSING vs MISMATCH". |
| `required` | bool | `true` only for `git` (`python`, at the top level, is also always required). Every other key is optional -- its collector/panel just stays inactive without it. |
| `override` | bool | `true` if this key's value came from an explicit `--<key>` flag. |
| `note` | string or null | Extra human-readable detail a state above doesn't already carry. Only ever set for `beads_dir`'s `"not_workspace"` state (bd's own reason, from `bd where --json`, for rejecting the directory); `null` for every other key/state. |

`python` (top level -- same idea, different shape, since it's a floor over
several candidates rather than one configured path):

| Field | Meaning |
|---|---|
| `configured` / `selected` | Both the same interpreter path once one is chosen, or both `null` if nothing satisfies the floor. |
| `version` | `"X.Y.Z"`, or `null`. |
| `floor` | `"3.11"` -- this dashboard's minimum (`datetime.UTC`, used throughout the backend). |
| `state` | `"ok"` or `"missing"`. |
| `required` | Always `true`. |
| `override` | `true` if `--python PATH` was supplied (verbatim, detection skipped). |
| `detected_below_floor` | `{"path": ..., "version": ...}` if a too-old interpreter was found -- so you know exactly what's on the machine even though it can't be used -- else `null`. |

Trimmed example:

```json
{
  "ready": true,
  "platform": "Linux",
  "python": {
    "configured": null, "selected": "/usr/bin/python3", "version": "3.13.5",
    "floor": "3.11", "state": "ok", "required": true, "override": false,
    "detected_below_floor": null
  },
  "checks": [
    {
      "key": "bd_bin", "kind": "binary", "collector": "beads",
      "configured": "~/.local/bin/bd", "configured_exists": true,
      "detected": "/home/user/.local/bin/bd", "state": "ok",
      "required": false, "override": false
    }
  ],
  "missing_required": []
}
```

`./install.sh --doctor` is the same data as a human-readable table instead
of JSON (`--probe` alone, without `--json`, prints the same table). `make
doctor` runs `--doctor`.

### MISSING vs MISMATCH vs NOT_WORKSPACE -- read this before installing anything

This distinction has already confused both a human and an agent working
on this project. Get it wrong and you'll install services nobody asked
for, or "fix" configuration that was never broken.

- **`MISSING`** -- the configured value (or nothing, if unset) doesn't
  exist, **and nothing else was found either.** This is a **valid end
  state**, not a problem: that tool genuinely isn't installed on this
  machine, and its panel/collector simply stays inactive. For every
  *optional* key (everything except `git`/`python`), `missing` is fine
  and `ready` stays `true`. **Do not install something just because its
  check says `missing`** -- an agent with install authority that does
  this will install a beads server (`bd`) on a laptop that never wanted
  one, because the `beads` panel happened to be `missing`. Only install a
  missing *optional* tool if the person/task actually wants that feature
  active. `git` and `python` are the exception: they're `required`, so
  their `missing` state does block `ready` and does mean "go get one" --
  see "Installing missing prerequisites" below.
- **`MISMATCH`** -- the configured value doesn't exist, but detection
  found a **working** one somewhere else (e.g. `bd_bin` says
  `~/.local/bin/bd` but `bd` is actually at `/opt/homebrew/bin/bd` on this
  Mac). The tool **is** installed and **is** usable right now -- detection
  already resolves it live, every time, independent of `config/sources.json`
  -- but the configured path is stale and worth fixing so the doctor table
  (and a human reading `config/sources.json`) isn't misleading. Fix it by
  editing that key in `config/sources.json` to the `detected` value, or by
  passing the matching `--<key>` override on your next `./install.sh` run.
  **Never** treat `mismatch` as a reason to install anything -- the tool
  is already there.
- **`NOT_WORKSPACE`** (`beads_dir` only) -- the configured directory
  **exists**, but `bd where --json`, run with `BEADS_DIR` pointed at it,
  does not resolve it to a workspace (see check_beads_workspace in
  `server/critdash/collectors/beads.py`). This is the validation gap that
  motivated this whole section: an existing directory is **not** evidence
  it is the right one -- `~/.beads` is the textbook example (it exists on
  most machines, but only holds `bd`'s own global event/lock state, never
  a workspace). Never treat `not_workspace` as "close enough" or edit
  around it -- go back to "Obtaining `beads_dir` safely" above, get the
  real path from `bd where --json`'s `"path"` field, and verify via
  `/api/healthz` before considering it fixed. `not_workspace` never blocks
  `ready` (beads stays optional, like `missing`/`mismatch`), but `make
  doctor`/`./install.sh --doctor` exit non-zero when it's present, since
  -- unlike a merely-unconfigured optional tool -- it means something IS
  configured and IS wrong.

### Installing missing prerequisites

The owner has explicitly authorised an installing agent to **install
missing prerequisites** -- you do not need to stop and ask a human for
permission to install `git`, a newer Python, `uv`, `bd`, or `herdr`. You
do need to follow these rules:

1. **Prefer user-local installs that need no `sudo`.** [`uv`](https://docs.astral.sh/uv/)
   installs into `~/.local/bin` with a single unprivileged command and can
   provision its own Python (`uv python install 3.12`) even when the
   system Python is too old or missing -- this resolves the common case
   without touching a system package manager at all:
   ```sh
   curl -LsSf https://astral.sh/uv/install.sh | sh   # installs to ~/.local/bin
   ```
   Reach for Homebrew (macOS) or `apt`/`dnf`/etc. (Linux) only when `uv`
   genuinely cannot provide what's missing (e.g. `git` itself, or `bd`/
   `herdr`, which `uv` doesn't package).
2. **Never run `sudo` silently.** If a step genuinely requires it (e.g. no
   Homebrew and no user-local package manager, `apt install git` on a
   fresh Debian box), **stop and report** the exact command a human needs
   to run -- do not escalate privileges on your own.
3. **Never modify or replace the system interpreter.** `/usr/bin/python3`
   on macOS belongs to Apple; the equivalent on most Linux distros belongs
   to the package manager. Install a new interpreter alongside it (via
   Homebrew, `uv python install`, or the distro's versioned package, e.g.
   `python3.12`) and point `--python` at that -- never overwrite, symlink
   over, or `pip install --upgrade` into the system one.
4. **Report every package installed.** State plainly what you ran and
   what it added (e.g. "ran `brew install python@3.12`, installed
   `/opt/homebrew/bin/python3.12`") so the machine's owner can see what
   changed on their machine.
5. **Be idempotent.** Check first (see "Discovery commands" below) and
   skip the install step entirely if the tool is already present --
   re-running this loop must never reinstall what's already there.

**Discovery commands** (run these instead of guessing where something
might be -- they're what `install.sh` itself checks, see "Tool & data path
detection" below):

| Looking for | Command |
|---|---|
| Any tool on `PATH` | `command -v <name>` |
| Homebrew's prefix (macOS) | `brew --prefix` (then look in `<prefix>/bin`) |
| Every versioned Python in a prefix | `ls <prefix>/bin/python3.*` (e.g. `ls /opt/homebrew/bin/python3.*` or `ls /usr/local/bin/python3.*`) |
| A Python's real version (never trust the filename) | `<path> -c 'import sys; print(sys.version_info[:3])'` |
| Whether `uv` is already here | `command -v uv` or `ls ~/.local/bin/uv` |
| `uv`-provisioned Pythons | `uv python list` |
| Whether Homebrew itself is installed | `command -v brew` |

**Platforms:** Linux and macOS. The system panel (load average, memory,
disk) uses `os.getloadavg()` and `/proc/meminfo` on Linux, `sysctl`/`vm_stat`
on macOS -- no platform-specific setup needed either way. Every collector
that shells out to an optional tool (`bd`, `herdr`, `ssh`) degrades to
"inactive, not configured" rather than failing when that tool is absent, on
either platform -- see "Config reference" below. `install.sh` and the
running dashboard both resolve `bd_bin`/`herdr_bin` against a list of real
install locations (PATH, then `~/.local/bin`, `/opt/homebrew/bin`,
`/usr/local/bin`, `/opt/local/bin`, `/usr/bin`) instead of trusting
whichever single machine's absolute path `config/sources.example.json`
ships with -- see "Tool & data path detection" below. `install.sh` searches
its own required/optional prerequisites (`git`, `python3`, `uv`, `ssh`,
`bd`, `herdr`) the same way, for the same reason: a perfectly-installed
tool that isn't on a non-interactive shell's `PATH` must not make the
install fail or silently disable a panel.

**bash:** `install.sh` and `scripts/check-public.sh` are written to run
under bash 3.2, the version macOS ships (Apple does not ship a newer,
GPLv3-licensed bash) -- no bash 4+ syntax (`mapfile`, associative arrays,
`${var^^}` case conversion, etc.) is used. If you see a syntax error
running either script, `bash --version` first; something other than the
system bash may be shadowing it on `PATH`.

---

## Prerequisites

| Requirement | Version | Required? |
|---|---|---|
| `git` | any recent | **Required** -- installs a checkout and (if `--service` is used) checks the origin remote. |
| `python3` | >=3.11 | Required *unless* `uv` is installed (see below). `datetime.UTC`, used throughout the backend, needs 3.11+. |
| `uv` (https://docs.astral.sh/uv/) | any recent | Recommended, not required. If present, `install.sh` uses it to create the venv and install dependencies, and it will provision a compatible Python itself even if the system `python3` is too old or missing. Without it, `install.sh` falls back to `python3 -m venv` + `pip`. |
| `ssh` | any | Optional. Needed only for multi-host fleet collection (`hosts: [...]` with `mode: "ssh"` in `config/sources.json`). Single-host mode works fully without it. |
| `bd` (beads CLI) | any | Optional. Needed only for the beads/work-queue panel. Without it, that panel stays inactive (auto-detected, not scheduled); nothing else is affected. |
| `herdr` | any | Optional. Needed only for pane-level agent detection (which terminal pane an agent is running in). Without it, agent detection still works from `~/.claude/projects/*/*.jsonl` session files -- you lose the pane/workspace fields, not the agent list itself. |

`install.sh` checks `git` and the Python floor and **fails with a specific
error** if neither is satisfiable. It checks `ssh`/`bd`/`herdr` too, but only
**warns** for each missing one and says exactly which feature stays
inactive -- it never blocks the install over them.

**Explicit overrides:** every tool above (plus `curl`, used only by
`--start`'s healthz check) can be pinned with `--<name> PATH` --
`--python`, `--git`, `--uv`, `--ssh`, `--bd`, `--herdr`, `--curl`. An
override is used verbatim, skipping detection entirely for that tool, but
is still validated: it must exist and be executable (and, for `--python`,
meet the 3.11 floor) or `install.sh` fails immediately with a specific
message -- never a silent fall-through to an unusable path. Use these when
detection guessed wrong, or to hand a just-installed tool straight to
`install.sh` without relying on it being found again. See `./install.sh
--help` and "Probe schema" above.

## Steps (what `install.sh` does)

1. Verifies `git` is on `PATH`.
2. Verifies a Python >=3.11 is reachable, either directly or via `uv`.
3. Warns (does not fail) about any of `ssh`/`bd`/`herdr` missing.
4. Creates `server/.venv` and installs `fastapi`, `uvicorn[standard]`,
   `httpx` into it (`uv sync` if `uv` is present, else `python3 -m venv` +
   `pip install -e server`).
5. If `config/sources.json` does not already exist, copies it from
   `config/sources.example.json`, writes in the `--port`/`--bind` you
   passed, and **resolves `bd_bin`/`herdr_bin` for this machine** (PATH,
   then the install-location candidates listed above) instead of leaving
   whatever path `config/sources.example.json` happened to ship with --
   printing what it found and what it couldn't. **If `config/sources.json`
   already exists, it is left completely untouched** -- `--port`/`--bind`
   are ignored on a re-run, and no path is rewritten -- but `install.sh`
   still runs the doctor check against it and prints a warning naming any
   `MISMATCH` it finds, so a stale path doesn't go unnoticed. The dashboard
   process does the same never-overwrite treatment for `config/layout.json`
   from `config/layout.example.json` on its first start (not `install.sh`
   itself) -- **an existing `config/layout.json` is likewise never
   touched**, so your panel arrangement, title and `human_labels` survive
   every `git pull`/reinstall.
6. With `--service`: writes and enables a systemd **user** unit at
   `~/.config/systemd/user/critdash.service` (skipped with a message if
   `systemctl --user` isn't available -- e.g. no systemd, or no lingering
   user session). The unit's filename stays `critdash.service` even though
   the product is CritBoard -- see "Legacy unit name" below.
7. With `--start`: launches `uvicorn` directly in the background (no
   systemd), writes its pid to `server/data/critdash.pid`, and polls
   `/api/healthz` until it answers or 15 seconds pass.
8. Prints the dashboard URL.

Every step above is **idempotent** -- re-running `install.sh` with the same
flags does not recreate the venv from scratch, does not overwrite
`config/sources.json` or `config/layout.json`, and does not touch an
already-installed systemd unit that has different content than what it
would write (it leaves that one alone and tells you why, rather than
guessing which version is "right").

## Verifying success

```sh
curl -fs http://127.0.0.1:9999/api/healthz | python3 -m json.tool
```

`"ok": true` at the top level means the process is up and answering.
Per-collector `"ok"` fields inside `"collectors"` reflect what's actually
configured -- `beads`/`dispatch`/`remote` staying `false` with
`"optional": true` and a `reason_code` is expected if you don't have `bd`
installed, no `~/.overlord`, or no remote `hosts` configured; it is not a
failed install. A collector in that state is auto-detected as inactive and
is not scheduled at all (no repeated failing subprocess every cycle) -- see
"Config reference" below.

```sh
curl -s http://127.0.0.1:9999/api/version
```

Reports the running build hash and, if this is a git checkout, the current
commit/branch (used by the self-update check -- see `SPEC.md`).

## Setting up beads (optional work-queue integration)

Beads (`bd`) is the work-queue CLI CritBoard's `beads` panel talks to.
It is **optional** -- skip this whole section if you don't use beads.
Without `bd`, the panel auto-detects as inactive (see "MISSING vs
MISMATCH" above) and every other panel works fine; nothing else in the
dashboard depends on it. Canonical project:
[gastownhall/beads](https://github.com/gastownhall/beads) -- a previous,
incorrect repo reference elsewhere in this project's docs has been fixed
to point here.

### 1. Detect

```sh
command -v bd
```

Found -- skip to step 3. Not found -- only install it if beads is
actually wanted here (a `missing` `bd_bin` check by itself is not a
reason to install anything -- see "MISSING vs MISMATCH" above).

### 2. Install `bd` (only if wanted and missing)

Pick one, in this order of preference (user-local first, no `sudo`):

```sh
brew install beads                     # macOS/Linux, if Homebrew is present
npm install -g @beads/bd               # if npm is present
curl -fsSL https://raw.githubusercontent.com/gastownhall/beads/main/scripts/install.sh | bash
```

Same install-authority rules as "Installing missing prerequisites" above:
prefer the user-local path, never a silent `sudo`, report exactly what you
ran and what it added, and check `command -v bd` first so re-running this
step never reinstalls what's already there.

### 3. Choose embedded vs. server

Two storage modes:

- **Embedded (default recommendation).** `bd init` creates a local
  [Dolt](https://www.dolthub.com/) database under `.beads/embeddeddolt/`.
  Single writer, nothing to run, no credentials. This is what a new user
  wants: their own local tracker, not someone else's server.
- **Server.** `bd init --server` connects to an external `dolt
  sql-server` for concurrent writers across machines. This is "joining an
  existing fleet," not a fresh setup -- it needs a host, port, and
  credentials that live outside this repo and that an installing agent
  cannot invent. **Stop and ask the human for the connection details
  instead of guessing; do not run `bd init --server` speculatively.**

Default to embedded unless you were explicitly told to join a server.

### 4. Initialise safely -- protect this repo's `AGENTS.md`

**Trap:** `bd init` creates or updates an `AGENTS.md` in the current
directory and installs Claude/Codex integrations there by default.
CritBoard already has its own `AGENTS.md` at the repo root (an unrelated
agent pointer) -- running plain `bd init` inside this checkout overwrites
it.

```sh
bd init --skip-agents          # embedded, run from inside this checkout
```

If you'd rather keep CritBoard's own beads workspace (tracking work on
this dashboard itself) fully separate from the repo, initialise it in a
directory outside the checkout instead (that directory's own `.beads/`
subdirectory is the workspace -- point `BEADS_DIR` at `<that
directory>/.beads`, e.g. from a `beads_env` file, or run `bd where` from
inside it to confirm the resolved path). `--skip-agents` is required
either way if you run `bd init` from inside this repo -- never let it
touch this repo's `AGENTS.md`.

### 5. Verify

```sh
bd list --json --all --limit 0    # -> "[]" or a JSON array, exit 0
```

### 6. Point CritBoard at it

In `config/sources.json`:

- `bd_bin` -- the path `command -v bd` resolved. Leave the shipped
  default (`~/.local/bin/bd`) if that's where it landed; `install.sh` and
  the running dashboard re-resolve it live against PATH and the usual
  install locations regardless (see "Tool & data path detection" below),
  so an exact match needs no edit.
- `beads_env` -- leave empty unless your `bd` setup specifically needs a
  sourced env file for credentials (typical of server mode). An embedded,
  `bd init`-created workspace needs none.
- `beads_dir` -- leave empty if `bd` from your own shell already finds the
  right workspace with no `BEADS_DIR` set (the common case). Set it
  explicitly if the `beads` panel reports "no beads database found" even
  though `bd` works fine for you interactively -- the collector runs `bd`
  from CritBoard's own working directory, not your shell, so it doesn't
  inherit anything ambient.

  **Obtaining `beads_dir` safely -- a path that merely exists is not
  evidence it is correct.** Do not guess a plausible-looking directory
  (`~/.beads` is the single most common wrong guess -- on most machines it
  holds only `bd`'s own global event/lock state, not a workspace, and it
  will pass a naive "does this directory exist" check while still being
  wrong). Never assume `~/.beads` is the answer. Instead:

  1. **Obtain the path, don't invent one.** From a directory where `bd`
     already works for you (interactively, or the directory you'd `cd`
     into to run `bd list`), run:
     ```sh
     bd where --json
     ```
     Use its `"path"` field verbatim. **That is the `.beads` directory
     itself** (e.g. `/path/to/project/.beads`), never its parent -- the
     easy mistake -- and never a directory you merely suspect. If `bd
     where --json` fails or finds nothing from anywhere you try, there is
     no workspace yet to point at: leave `beads_dir` unset (see below),
     don't write a guess.
  2. **Write it** to `beads_dir` in `config/sources.json`.
  3. **Verify end to end, not just that the file was written.** Writing
     the key proves nothing on its own -- `bd` itself has the final word.
     Restart the dashboard, then check the `beads` entry directly:
     ```sh
     systemctl --user restart critdash.service   # or your non-systemd equivalent
     curl -s http://127.0.0.1:9999/api/healthz | python3 -c \
       'import json,sys; d=json.load(sys.stdin)["collectors"]["beads"]; print(d["ok"], d["reason_code"])'
     # -> True None
     ```
     `"ok": true` (with `"reason_code": null`) is the only thing that
     counts as success.
  4. **If it is not `ok`, don't leave it looking configured but broken.**
     Read that same entry's full `"detail"` and `"remedy"` fields (not
     just `ok`/`reason_code`) -- the collector runs the exact same `bd
     where --json` check against the configured `beads_dir` and reports
     bd's own reason it rejected the directory (e.g. "beads_dir exists but
     is not a beads workspace: No active beads workspace found."). Fix
     `beads_dir` using that reason (usually: re-run `bd where --json` from
     the right place, or realize no workspace exists yet) and go back to
     step 3. Do not stop at "the key has a value" -- a `beads_dir` that
     exists on disk but that `bd` itself doesn't recognize is exactly the
     failure this whole section exists to prevent (see "NOT_WORKSPACE" in
     "MISSING vs MISMATCH vs NOT_WORKSPACE" above), and it produces a
     *worse* outcome than leaving it unset: a panel that looks configured
     but never works.

  Takes precedence over `beads_env`, which takes precedence over `bd`'s
  own resolution.

Beads stays optional end to end: an agent that cannot find a workspace by
following step 1 above (nothing `bd where --json` resolves anywhere)
leaves `beads_dir` unset and the `beads` panel inactive, and every other
panel is unaffected. That is a normal, valid outcome -- not a failure to
fix by guessing.

## Running it day to day

| Command | What it does |
|---|---|
| `make run` | Foreground process on `$PORT`/`$BIND` (defaults 9999 / 127.0.0.1). Ctrl-C to stop. |
| `./install.sh --service` | systemd --user unit, survives logout/reboot. `systemctl --user status critdash.service` / `journalctl --user -u critdash.service -f`. |
| `make test` | Backend test suite (requires `uv`). |
| `make doctor` / `./install.sh --doctor` | Configured vs. detected tool/data paths, one table. Exits non-zero on a `MISMATCH`. See "Tool & data path detection" below. |
| `./install.sh --probe --json` | Same data as `--doctor`, as one JSON object for a script/agent -- no side effects. See "Probe schema" above. |
| `make check` | `scripts/check-public.sh` -- the sanitization regression guard (only meaningful if you're working in a clone of this repo, not a downstream deploy). |
| `make update` | `git pull --ff-only` + reinstall deps if the lockfile changed. Or use the in-app self-update API -- see `SPEC.md`'s `/api/update/*` contract; it's off by default (`allow_self_update: false`). |

## Legacy unit name

The systemd unit installed by `--service` is named `critdash.service`, not
`critboard.service`. This is deliberate: the product was renamed
to CritBoard, but the unit filename is treated as a
stable interface, not cosmetic -- an existing install's `systemctl --user
enable`/`status`/logs all key off that exact filename, and renaming it would
silently orphan anyone who already has the old unit enabled. New installs
get the same name for consistency with existing documentation and muscle
memory (`systemctl --user restart critdash.service`).

## Uninstalling

```sh
systemctl --user disable --now critdash.service   # if installed with --service
rm -f ~/.config/systemd/user/critdash.service
systemctl --user daemon-reload

rm -rf server/.venv server/data server/uv.lock
rm -f config/sources.json   # only if you want to discard your local config too
rm -f config/layout.json    # only if you want to discard your panel arrangement/title/human_labels too
```

The repo checkout itself (`git`, `config/*.example.json`, etc.) is left for
you to `rm -rf` the whole directory, or keep, as you prefer -- nothing above
touches files outside this checkout and `~/.config/systemd/user/`.

## Config reference

`config/sources.json` is gitignored and machine-local by design -- see
`config/sources.example.json` for every key, with comments, and
`server/critdash/config.py`'s `DEFAULT_SOURCES` for the code-level fallback
if a key is missing entirely. Notable ones for a fresh install:

- `bind_host` / `bind_port` -- set by `install.sh --bind`/`--port` on first
  run only (see step 5 above).
- `repo_roots` -- directories to scan for git worktrees/agents. Defaults to
  `~/repos`, `~/src`, `~/work`; edit to match where your projects actually
  live.
- `hosts` -- for multi-host fleet collection over `ssh`. A single-host
  install needs no changes here.
- `disk_mounts` -- mounts the system panel reports on, local and remote.
  Defaults to `["/"]`; add more (e.g. `"/srv"`, `"/mnt/data"`) if you want
  them shown. A configured mount that doesn't exist on a given host is
  skipped, not a failure.
- `collectors` -- per-collector `{"enabled": true|false}` overrides. Defaults
  to `{}` (auto-detect everything): `beads`/`dispatch`/`remote` start
  inactive on their own when their dependency (`bd`, `~/.overlord`, an
  `ssh`-mode host) is absent, and are re-checked every
  `collector_redetect_interval_s` (default 60s) -- installing `bd` later
  brings that panel alive with no restart. Force one on or off regardless of
  detection with e.g. `{"beads": {"enabled": false}}`.
- `allow_self_update` -- `false` by default. See `SPEC.md` for the
  `/api/update/check` and `/api/update/apply` contract before turning this
  on. `update_auto_apply` (below) still requires this to be `true` -- it's
  an additional opt-in, not a replacement.
- `update_repo` -- `"critfusion/critboard"` by default (that repo is
  public, so this works unauthenticated with zero setup). If you forked
  this repo, set `update_repo` to your own `"owner/repo"`, or `""` to
  disable the check entirely (an explicit empty string always means
  "disabled", even though the default is no longer empty).
- `update_check_enabled` -- `true` by default. Turns on a background check
  every `update_check_interval_s` seconds; it's read-only (a conditional
  GET, ETag-cached across restarts) and safe on its own. `GET
  /api/settings/updates` / `POST /api/settings/updates` is the
  settings-panel-facing way to change this plus `check_interval_s` and
  `auto_apply` without touching `sources.json` by hand -- see `SPEC.md`.
- `update_check_interval_s` -- `900` (15 minutes) by default. GitHub's
  unauthenticated rate limit is 60 requests/hour; a 304 (unchanged) response
  doesn't count against it, which is what makes this interval free. The
  settings endpoint clamps any value below 300s.
- `update_auto_apply` -- `false` by default. `true` lets the periodic check
  pull a fast-forward update on its own, no click -- but only when
  `allow_self_update` is also `true`, and every existing apply safety check
  (clean tree, fast-forward only, configured origin only) still applies.

`config/layout.json` is gitignored and personal by the same design -- see
`config/layout.example.json` for the shipped panel layout. It is created on
the dashboard's first start (not by `install.sh`), and an existing one is
never overwritten. Two keys in it are settings-panel state, not grid
geometry:

- `title` -- the dashboard's header text. Shipped default: `"CritBoard"`.
- `human_labels` -- bead labels that mean "a person owns this, not the
  fleet" (e.g. dispatch never wakes an agent for one). Shipped default:
  `[]` -- a fresh install classifies nothing as human-owned until you set
  one, either by editing this key directly or via the settings panel's
  detected-label suggestion (gear icon → Human work labels → "Use
  detected").

## Tool & data path detection

`config/sources.example.json` ships one machine's resolved absolute paths
(the repo's own dev host). Every path below is checked against the real
filesystem -- by `install.sh` when it first writes `config/sources.json`,
by `make doctor`/`./install.sh --doctor` on demand, and by the running
dashboard on every periodic redetect tick for `bd_bin` -- instead of ever
being assumed correct just because it's what the example file says.

**A missing OPTIONAL tool is fine.** If `MISSING` in doctor's output means
nothing was found configured OR anywhere else -- that tool simply isn't
installed on this machine, and its panel stays inactive. **A `MISMATCH` is
the thing to fix**: the configured path doesn't exist, but detection found
the tool working at a different path. That means the tool IS installed --
just not where `config/sources.json` says -- and it must be corrected
there, or the panel stays inactive even though the tool works. This exact
distinction is what confused the dashboard's owner: they had `bd` (beads)
installed on a Mac, but `config/sources.json` still had the Debian path
`~/.local/bin/bd` baked in from the example file, so the dashboard reported
"not configured" for a tool that was right there.

**`beads_dir` alone can also be `NOT_WORKSPACE`**: the configured directory
exists (so it isn't `MISSING`), but `bd where --json` run against it
doesn't resolve to a workspace -- an installing agent guessing `~/.beads`
is the case that motivated this. A directory existing is never, by itself,
proof it is the right one; see "MISSING vs MISMATCH vs NOT_WORKSPACE" above
and "Obtaining `beads_dir` safely" under "Setting up beads" for the fix.

| Key | What it's for | Powers | Binary search order / directories checked |
|---|---|---|---|
| `bd_bin` | The `bd` (beads) CLI | `beads` panel | PATH, then `~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin`, `/opt/local/bin`, `/usr/bin` (in that order) -- re-checked live, not just at install |
| `herdr_bin` | The `herdr` CLI (pane-level agent detection) | `agents` panel's pane/workspace fields (local host); `remote` panel per remote host | Same search order as `bd_bin`, resolved once at startup for the local host. A remote (`ssh`-mode) host resolves its own `herdr_bin` on ITS OWN filesystem, never against the local host's resolved path |
| `beads_env` | **Optional.** Env file some `bd` setups source before every call (actor/DB config) -- a site-specific convention, not something `bd` itself requires. Empty by default; a `bd` with its own local workspace (`bd init`, or `BEADS_DIR` set) needs none. Sourced only when it exists | `beads` panel | Not searched for -- set it yourself only if your `bd` setup actually uses one; no default path is guessed |
| `beads_dir` | **Optional.** The beads workspace's `.beads` directory itself -- exported as `BEADS_DIR` before every `bd` call, taking precedence over `beads_env`. Empty by default | `beads` panel | Not searched for -- `install.sh` tries `bd where --json` once, on first config write, uses its `"path"` field only if that same path then passes the workspace check below too, and otherwise leaves it empty, a normal state. Validated as a real workspace (not just an existing directory) by running `bd where --json` with `BEADS_DIR` set to the candidate -- the same check `make doctor`/`--probe` and the live collector both run; see `check_beads_workspace` in `server/critdash/collectors/beads.py` and "NOT_WORKSPACE" above |
| `claude_projects_dir` | Claude Code's session logs | `agents`, `usage`, `analytics` panels | `~/.claude/projects` on every platform (Claude Code does not use a macOS Application Support directory) |
| `kimi_dir` | Kimi Code CLI's session/credentials directory | `kimi` panel, Kimi quota check | `~/.kimi-code` (dotfile, same on every platform this dashboard has verified). `~/Library/Application Support/Kimi` is probed as an unconfirmed, defensive fallback candidate on macOS only -- checked, never assumed |
| `overlord_dir` | Optional integration with a fleet dispatch tool | `dispatch` panel | `~/.overlord` (a homegrown dotfile, not a packaged/Homebrew tool -- no macOS-specific location exists to check) |
| `opencode_auth_path` | OpenCode CLI's stored keys (openrouter/opencode/google) | Quota check | `~/.local/share/opencode/auth.json`; no macOS-specific location confirmed |
| `grok_auth_path` | xAI/Grok CLI's OAuth session | Quota check (xai) | `~/.grok/auth.json`; no macOS-specific location confirmed |
| `codex_auth_path` | Codex CLI's stored key | Quota check (openai) | `~/.codex/auth.json`; no macOS-specific location confirmed |

Also checked (not `sources.json` keys -- always looked up by name, reported
by doctor for completeness): `ssh` (multi-host fleet collection), `git`
(worktrees/productivity), `uv` (dev tooling / self-update), `curl`
(`--start`'s healthz check), using the same PATH-then-candidate-directories
search order as `bd_bin`. Any of the ten keys above -- and `git`/`uv`/`ssh`/
`curl` -- can be pinned with an explicit `--<name> PATH` override, which
skips this whole search for that one key. See "Explicit overrides" under
"Prerequisites" above.

None of the macOS Application Support candidates above are confirmed to be
used by any of these tools in practice -- this dashboard has not been run
on real macOS hardware. They're probed because a filesystem check costs
nothing, not because they're expected to be found; doctor's `DETECTED`
column shows definitively whether one ever was. If detection can't confirm
a macOS-specific location for a key, the dotfile/XDG-style default is kept
exactly as documented above, and this table says so rather than inventing
one.
