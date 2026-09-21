# CritBoard -- developer/operator convenience targets. See INSTALL.md for
# the full agent-drivable install walkthrough; this Makefile is a thin
# wrapper around install.sh, uv, and scripts/check-public.sh.

PORT ?= 9999
BIND ?= 127.0.0.1
SHELL := /bin/bash

.PHONY: install run test check doctor update

install: ## One-command setup: venv, deps, config/sources.json. See install.sh --help.
	./install.sh --port $(PORT) --bind $(BIND)

run: ## Run the dashboard in the foreground (Ctrl-C to stop). Needs `make install` first.
	@if [ ! -x server/.venv/bin/uvicorn ]; then \
		echo "Makefile: ERROR: venv not set up (server/.venv/bin/uvicorn missing). Run 'make install' first." >&2; \
		exit 1; \
	fi
	cd server && .venv/bin/uvicorn critdash.main:app --host $(BIND) --port $(PORT)

test: ## Run the backend test suite. Requires uv (dev dependency group).
	@command -v uv >/dev/null 2>&1 || { \
		echo "Makefile: ERROR: 'uv' not found on PATH. Install uv (https://docs.astral.sh/uv/) to run tests." >&2; \
		exit 1; \
	}
	cd server && uv run pytest -q

check: ## Run the public-repo sanitization guard (scripts/check-public.sh).
	bash scripts/check-public.sh

doctor: ## Report configured vs. detected tool/data paths; exits non-zero on a mismatch. See install.sh --doctor.
	./install.sh --doctor

update: ## Pull the latest commit and reinstall dependencies if the lockfile changed.
	git pull --ff-only
	@if [ -x server/.venv/bin/pip ]; then \
		cd server && .venv/bin/pip install -e .; \
	elif command -v uv >/dev/null 2>&1; then \
		cd server && uv sync; \
	else \
		echo "Makefile: ERROR: Neither venv (.venv/bin/pip) nor 'uv' found. Run 'make install' first." >&2; \
		exit 1; \
	fi
