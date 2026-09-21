# Installing CritBoard

## For coding agents

One-liner, from a fresh clone of `critfusion/critboard`:

```sh
./install.sh --port 9999 --bind 127.0.0.1 --start
```

Success check:

```sh
curl -fs http://127.0.0.1:9999/api/healthz
```

returns `{"ok":true,...}`. If `--start` was omitted, run `make run` first
(foreground) or `./install.sh --service` (systemd --user, survives logout).

Nothing else is required before the check above passes. `git` and either
`uv` or `python3 >=3.11` must already be on `PATH` -- `install.sh` verifies
this itself and fails with a clear message naming what's missing, it does
not silently continue with an unusable interpreter.

**Required next step -- run the doctor and fix any mismatch it reports:**

```sh
make doctor
# or, if `make` isn't available:
./install.sh --doctor
```

This prints, for every tool/data path CritBoard uses, the value configured
in `config/sources.json`, whether that exact value exists, what was
actually detected on this machine, and the resulting state (`OK` /
`MISMATCH` / `MISSING`). It exits non-zero only on `MISMATCH`. Two outcomes
need different handling:

- **`MISSING`** (configured path doesn't exist, and nothing else was found
  either): fine, do nothing. That tool simply isn't installed on this
  machine, and its panel stays inactive -- this is a normal, expected state,
  not a bug.
- **`MISMATCH`** (configured path doesn't exist, but a working one WAS
  found elsewhere -- e.g. `bd_bin` says `~/.local/bin/bd` but `bd` is
  actually at `/opt/homebrew/bin/bd` on this Mac): **fix it.** Edit the
  matching key in `config/sources.json` to the value in doctor's
  `DETECTED` column, then re-run `make doctor` to confirm it now shows
  `OK`. This is exactly the bug that motivated this command: a tool IS
  installed, just not where the config says, and the dashboard would
  otherwise report it as "not configured" forever. See "Tool & data path
  detection" below for the full table of what's checked and where.

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
ships with -- see "Tool & data path detection" below.

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

## Running it day to day

| Command | What it does |
|---|---|
| `make run` | Foreground process on `$PORT`/`$BIND` (defaults 9999 / 127.0.0.1). Ctrl-C to stop. |
| `./install.sh --service` | systemd --user unit, survives logout/reboot. `systemctl --user status critdash.service` / `journalctl --user -u critdash.service -f`. |
| `make test` | Backend test suite (requires `uv`). |
| `make doctor` / `./install.sh --doctor` | Configured vs. detected tool/data paths, one table. Exits non-zero on a `MISMATCH`. See "Tool & data path detection" below. |
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
  on.
- `update_repo` -- empty by default. This dashboard's own upstream repo is
  private, so a third-party install's update check would 404 against a repo
  it can't read; leaving this empty makes `/api/update/check` report "no
  update repo configured" instead. If you forked this repo and want
  self-update, set `update_repo` to your own `"owner/repo"`.

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

| Key | What it's for | Powers | Binary search order / directories checked |
|---|---|---|---|
| `bd_bin` | The `bd` (beads) CLI | `beads` panel | PATH, then `~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin`, `/opt/local/bin`, `/usr/bin` (in that order) -- re-checked live, not just at install |
| `herdr_bin` | The `herdr` CLI (pane-level agent detection) | `agents` panel's pane/workspace fields (local host); `remote` panel per remote host | Same search order as `bd_bin`, resolved once at startup for the local host. A remote (`ssh`-mode) host resolves its own `herdr_bin` on ITS OWN filesystem, never against the local host's resolved path |
| `beads_env` | Env file `bd` needs sourced before every call (actor/DB config) | `beads` panel | `~/.config/beads/env` -- a dotfile this specific tool documents; no macOS-specific location is confirmed, so none is guessed at |
| `claude_projects_dir` | Claude Code's session logs | `agents`, `usage`, `analytics` panels | `~/.claude/projects` on every platform (Claude Code does not use a macOS Application Support directory) |
| `kimi_dir` | Kimi Code CLI's session/credentials directory | `kimi` panel, Kimi quota check | `~/.kimi-code` (dotfile, same on every platform this dashboard has verified). `~/Library/Application Support/Kimi` is probed as an unconfirmed, defensive fallback candidate on macOS only -- checked, never assumed |
| `overlord_dir` | Optional integration with a fleet dispatch tool | `dispatch` panel | `~/.overlord` (a homegrown dotfile, not a packaged/Homebrew tool -- no macOS-specific location exists to check) |
| `opencode_auth_path` | OpenCode CLI's stored keys (openrouter/opencode/google) | Quota check | `~/.local/share/opencode/auth.json`; no macOS-specific location confirmed |
| `grok_auth_path` | xAI/Grok CLI's OAuth session | Quota check (xai) | `~/.grok/auth.json`; no macOS-specific location confirmed |
| `codex_auth_path` | Codex CLI's stored key | Quota check (openai) | `~/.codex/auth.json`; no macOS-specific location confirmed |

Also checked (not `sources.json` keys -- always looked up by name, reported
by doctor for completeness): `ssh` (multi-host fleet collection), `git`
(worktrees/productivity), `uv` (dev tooling / self-update), using the same
PATH-then-candidate-directories search order as `bd_bin`.

None of the macOS Application Support candidates above are confirmed to be
used by any of these tools in practice -- this dashboard has not been run
on real macOS hardware. They're probed because a filesystem check costs
nothing, not because they're expected to be found; doctor's `DETECTED`
column shows definitively whether one ever was. If detection can't confirm
a macOS-specific location for a key, the dotfile/XDG-style default is kept
exactly as documented above, and this table says so rather than inventing
one.
