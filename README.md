# CritBoard

Mission control for AI coding agents. It shows what your agents are doing,
what they cost, and why work is or is not moving.

**It makes no LLM calls.** Every number comes from session logs, git, and
provider metadata already on your disk, so running it costs nothing.

## What this repo is

CritBoard is a **template plus a runbook**, not a hosted app you pull and
open. `git clone` gets you the code and nothing configured for your
machine yet. The entry point is [INSTALL.md](INSTALL.md): an executable
runbook written for a coding agent to run on your behalf, including
installing anything missing and configuring the optional integrations
(beads, `herdr`, multi-host fleets) that need choices only you can make.

That said, the gap isn't total. On a machine that already has `git` and a
suitable Python (`>=3.11`, or `uv` to provision one), this genuinely works
with no agent involved:

```sh
git clone https://github.com/critfusion/critboard.git
cd critboard
./install.sh --port 9999 --bind 127.0.0.1 --start
curl -fs http://127.0.0.1:9999/api/healthz    # -> {"ok":true,...}
```

That brings up a running dashboard at <http://127.0.0.1:9999/> with a venv,
dependencies, and a machine-detected `config/sources.json` -- core panels
(agents, worktrees, usage, system) work immediately. What it will **not**
do for you: point `repo_roots` at your actual projects, pick a title, or
install and wire up optional integrations (`bd`/beads, `herdr`, remote
hosts, quota auth) -- those are exactly what an agent following
[INSTALL.md](INSTALL.md) does next.

After either path, run `make doctor` (or `./install.sh --doctor`) and fix
any `MISMATCH` it reports -- it resolves `bd`/`herdr`/etc. against where
they actually are on this machine (Homebrew, MacPorts, `~/.local/bin`,
...) instead of trusting one machine's baked-in path, and tells you
exactly what to change in `config/sources.json` if a tool you have
installed still shows up as "not configured". Full options, systemd setup,
beads setup and uninstall: [INSTALL.md](INSTALL.md).

## What it tracks

| | |
|---|---|
| **Agents** | Live sessions per host, with repo, branch, model and status. Found from session logs, so an agent started outside a pane manager is still seen. |
| **Spend and tokens** | Per model, per provider, per host, per project. Today / 7d / 30d / 90d with daily history. |
| **Work queue** | Optional [beads](https://github.com/gastownhall/beads) integration, including whether an item can actually reach an agent. Local by default (embedded, no server) -- see [INSTALL.md](INSTALL.md#setting-up-beads-optional-work-queue-integration). |
| **Worktrees** | Branch, dirty state, ahead/behind and staleness across every repo it finds. |
| **Errors** | Tool failure patterns and per-tool error rates from the transcripts. |
| **Quota** | Manually-triggered remaining-balance check per provider. Never polled. |

Supports Claude Code and Kimi Code today, on one host or many over SSH, on
Linux or macOS. A provider or optional dependency (`bd`, `~/.overlord`, an
SSH host) whose files are absent is simply inactive — nothing to configure,
and nothing gets scheduled to fail on a loop until it's installed.

## How the numbers are kept honest

Cost reporting is the easiest thing to get quietly wrong, so:

- Rates match by longest model-id prefix, and the two cache-write TTL tiers
  are billed separately. Collapsing them understates spend on any fleet
  using long-lived prompt caching.
- A provider billed by subscription reports a **null** cost, never `0.00`.
  Null means *not applicable*; zero would claim it is free.
- Any mixed total states which providers its dollar figure covers, so no
  panel can show a "fleet cost" that silently omits one.
- Where one host's history starts later than another's, the chart marks the
  incomplete region instead of letting missing data look like a drop.

## Changing the layout

`config/layout.json` declares the grid. Each panel `type` maps to one file
in `web/js/widgets/`. Move, resize, reorder or hide a panel — including
per-breakpoint on mobile — by editing JSON and refreshing.

`config/layout.json` also holds personal settings (`title`, `human_labels`,
`timezone`) and your own panel arrangement, so like `config/sources.json` it
is gitignored and never shipped: it is generated from
`config/layout.example.json` on first run and, once it exists, is never
overwritten by an upstream `git pull` — see "Privacy" below.

No build step, no bundler, no framework. Plain ES modules and CSS, with
uPlot vendored locally so the page works offline.

## Updating

The dashboard checks this repo for a newer commit every 15 minutes
(`update_check_interval_s`, default 900s) using a conditional request --
GitHub's unauthenticated rate limit is 60/hour, and an unchanged (304)
response doesn't count against it, so this costs nothing. `update_repo`
defaults to `"critfusion/critboard"` (public, so this works out of the box);
if you forked this repo, point it at your own `"owner/repo"`, or set it to
`""` to disable checking entirely. The settings panel (gear icon) can toggle
checking on/off and the interval without editing `config/sources.json`
directly (`GET`/`POST /api/settings/updates`).

Applying an update is separate and **disabled by default**: it refuses a
dirty working tree, refuses anything that is not a fast-forward, and only
pulls from the configured origin. Enable manual apply (a button in the
update banner) with `"allow_self_update": true` in `config/sources.json`.
Enable fully automatic apply -- the periodic check pulls a fast-forward
update on its own, no click -- with `"update_auto_apply": true` as well
(still requires `allow_self_update: true`; every safety check above still
applies).

## Privacy

`config/sources.json` holds machine-local paths and hosts, and
`config/layout.json` holds your title, `human_labels` and panel arrangement
— both are gitignored; each is generated from its own `*.example.json` on
first run. `scripts/check-public.sh` scans the tree for personal data,
private IPs and key material before you push — including checking any
tracked layout config's `human_labels`/`title` directly. Add your own
machine's literals to `scripts/check-public.local` (see the `.example`).
