# CritBoard -- agent notes

CritBoard is a dashboard for a fleet of coding agents: live sessions, spend,
worktrees, errors, and an optional beads work queue. It is a FastAPI backend
(`server/critdash`) with a static frontend (`web/`). Panels are declared in
`config/layout.json`; settings live in `config/sources.json`.

Read first: `README.md` (overview), `INSTALL.md` (install runbook), `SPEC.md`
(build contract and hard constraints).

## Rules from SPEC.md

- No LLM API calls at runtime. Collectors only run subprocesses and parse files.
- Read-only by default. Write actions need an explicit opt-in flag.
- Layout is data: change `config/layout.json`, not JS.
- A failing collector marks itself `stale`; the rest of the page keeps working.

## Commands

- `make install` sets up the venv and `config/sources.json`.
- `make run` serves on `127.0.0.1:9999` (override with `PORT` and `BIND`).
- `make test` runs the backend tests; `make lint` runs ruff over `server/`.
- `make doctor` checks that optional tools resolve on this machine.
- `make check` runs `scripts/check-public.sh`, which guards against
  machine-specific details in tracked files.

## Public repo

This repository is public. Do not commit hostnames, IP addresses, home-directory
paths, credentials, account names, or webhook URLs. Keep machine-specific
settings in `config/sources.json`, which git ignores.
