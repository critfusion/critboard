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

**Platforms:** Linux and macOS. The system panel (load average, memory,
disk) uses `os.getloadavg()` and `/proc/meminfo` on Linux, `sysctl`/`vm_stat`
on macOS -- no platform-specific setup needed either way. Every collector
that shells out to an optional tool (`bd`, `herdr`, `ssh`) degrades to
"inactive, not configured" rather than failing when that tool is absent, on
either platform -- see "Config reference" below.

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
   `config/sources.example.json` and writes in the `--port`/`--bind` you
   passed. **If `config/sources.json` already exists, it is left completely
   untouched** -- `--port`/`--bind` are ignored on a re-run; edit that file
   directly to change them afterwards.
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
`config/sources.json`, and does not touch an already-installed systemd unit
that has different content than what it would write (it leaves that one
alone and tells you why, rather than guessing which version is "right").

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
