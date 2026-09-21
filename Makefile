# CritBoard -- developer/operator convenience targets. See INSTALL.md for
# the full agent-drivable install walkthrough; this Makefile is a thin
# wrapper around install.sh, uv, and scripts/check-public.sh.

PORT ?= 9999
BIND ?= 127.0.0.1

.PHONY: install run test check doctor update

install: ## One-command setup: venv, deps, config/sources.json. See install.sh --help.
	./install.sh --port $(PORT) --bind $(BIND)

run: ## Run the dashboard in the foreground (Ctrl-C to stop). Needs `make install` first.
	cd server && .venv/bin/uvicorn critdash.main:app --host $(BIND) --port $(PORT)

test: ## Run the backend test suite. Requires uv (dev dependency group).
	cd server && uv run pytest -q

check: ## Run the public-repo sanitization guard (scripts/check-public.sh).
	bash scripts/check-public.sh

doctor: ## Report configured vs. detected tool/data paths; exits non-zero on a mismatch. See install.sh --doctor.
	cd server && uv run python -m critdash.doctor

update: ## Pull the latest commit and reinstall dependencies if the lockfile changed.
	git pull --ff-only
	cd server && uv sync
