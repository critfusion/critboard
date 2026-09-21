# CritBoard

Mission control for AI coding agents. It shows what your agents are doing,
what they cost, and why work is or is not moving.

**It makes no LLM calls.** Every number comes from session logs, git, and
provider metadata already on your disk, so running it costs nothing.

## Install

```sh
git clone https://github.com/critfusion/critboard.git
cd critboard
./install.sh --port 9999 --bind 127.0.0.1 --start
```

Then open <http://127.0.0.1:9999/>.

Success check:

```sh
curl -fs http://127.0.0.1:9999/api/healthz    # -> {"ok":true,...}
```

Only `git` and either `uv` or `python3 >=3.11` are required. `install.sh`
checks both and fails with a message naming what is missing. Full options,
systemd setup and uninstall: [INSTALL.md](INSTALL.md).

## What it tracks

| | |
|---|---|
| **Agents** | Live sessions per host, with repo, branch, model and status. Found from session logs, so an agent started outside a pane manager is still seen. |
| **Spend and tokens** | Per model, per provider, per host, per project. Today / 7d / 30d / 90d with daily history. |
| **Work queue** | Optional [beads](https://github.com/steveyegge/beads) integration, including whether an item can actually reach an agent. |
| **Worktrees** | Branch, dirty state, ahead/behind and staleness across every repo it finds. |
| **Errors** | Tool failure patterns and per-tool error rates from the transcripts. |
| **Quota** | Manually-triggered remaining-balance check per provider. Never polled. |

Supports Claude Code and Kimi Code today, on one host or many over SSH.
A provider whose files are absent is simply inactive — nothing to configure.

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

No build step, no bundler, no framework. Plain ES modules and CSS, with
uPlot vendored locally so the page works offline.

## Updating

The dashboard can check this repo for a newer commit and apply it.
Self-update is **disabled by default**: it refuses a dirty working tree,
refuses anything that is not a fast-forward, and only pulls from the
configured origin. Enable with `"allow_self_update": true` in
`config/sources.json`.

## Privacy

`config/sources.json` holds machine-local paths and hosts and is
gitignored; it is generated from `config/sources.example.json` on first
run. `scripts/check-public.sh` scans the tree for personal data,private IPs and
key material before you push. Add your own machine's literals to
`scripts/check-public.local` (see the `.example`).
