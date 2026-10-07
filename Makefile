# radreport — see README.md "Operations" for what each target is for and the
# order to run them in.
#
# Two connection roles matter and are easy to confuse:
#   migrations  run as the DATABASE OWNER  (migration 0002 creates roles and
#               reassigns view ownership, which the app role may not do)
#   the app     runs as radreport_app_login, a NON-owner, NON-superuser role
#               (superusers bypass RLS unconditionally, owners bypass it
#               without FORCE — either would make the isolation tests pass
#               vacuously)

.PHONY: help install up down migrate migrate-owner revision seed seed-local admin admin-password pg-observe \
        run dev stop restart status logs worker test test-unit lint fmt check clean

PORT ?= 8000
HOST ?= 127.0.0.1
WORKERS ?= 2
PIDFILE := .uvicorn.pid
LOGFILE := .uvicorn.log
OWNER_URL ?= postgresql+psycopg://$(shell whoami)@localhost:5432/radreport

help:  ## Show this help
	@grep -hE '^[a-z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk -F':.*?## ' '{printf "  \033[1m%-16s\033[0m %s\n", $$1, $$2}'

# --------------------------------------------------------------- setup -----
install:  ## Create the venv and install the project with dev extras
	python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

up:  ## Start Postgres 16 + pgvector and MinIO via docker compose
	docker compose up -d

down:  ## Stop the docker compose services
	docker compose down

migrate:  ## Apply migrations as the app role (use migrate-owner if this fails)
	.venv/bin/alembic upgrade head

migrate-owner:  ## Apply migrations as the database owner (needed for 0002 and 0005)
	RADREPORT_DATABASE_URL="$(OWNER_URL)" .venv/bin/alembic upgrade head

revision:  ## Autogenerate a migration: make revision M="what changed"
	RADREPORT_DATABASE_URL="$(OWNER_URL)" \
	  .venv/bin/alembic revision --autogenerate -m "$(M)"

seed:  ## Seed the model catalog and the first product admin (password from RADREPORT_SEED_ADMIN_PASSWORD)
	.venv/bin/python -m radreport.devtools.seed

seed-local:  ## Developer machines: an account for every role, written to local-credentials.md
	.venv/bin/python -m radreport.devtools.seed --local-accounts

admin:  ## Create the first product admin interactively: make admin EMAIL=you@example.com
	.venv/bin/python -m radreport.admin.cli create --email $(EMAIL)

admin-password:  ## Set or reset an admin panel password: make admin-password EMAIL=you@example.com
	.venv/bin/python -m radreport.admin.cli set-password --email $(EMAIL)

pg-observe:  ## Turn on the slow-query log and pg_stat_statements (superuser; needs a Postgres restart)
	psql "$(subst +psycopg,,$(OWNER_URL))" -f ops/postgres/observability.sql
	psql "$(subst +psycopg,,$(OWNER_URL))" -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements"
	@echo "now restart Postgres, e.g. 'brew services restart postgresql@16' or 'docker compose restart db'"

# ---------------------------------------------------------------- server ----
dev:  ## Run the API in the foreground with auto-reload (Ctrl-C to stop)
	.venv/bin/uvicorn radreport.api.app:app --reload --host $(HOST) --port $(PORT)

run:  ## Start the API in the background (writes .uvicorn.pid)
	@if [ -f $(PIDFILE) ] && kill -0 `cat $(PIDFILE)` 2>/dev/null; then \
	  echo "already running (pid `cat $(PIDFILE)`) — use 'make restart'"; exit 1; fi
	@.venv/bin/uvicorn radreport.api.app:app --host $(HOST) --port $(PORT) \
	  --workers $(WORKERS) > $(LOGFILE) 2>&1 & echo $$! > $(PIDFILE)
	@# Wait for the port to answer rather than guessing with sleep: with
	@# multiple workers uvicorn takes a moment to bind, and a target that
	@# prints "started" before then sends you to a connection error.
	@for i in $$(seq 1 40); do \
	  if curl -fsS -o /dev/null http://$(HOST):$(PORT)/ready 2>/dev/null; then \
	    echo "started on http://$(HOST):$(PORT) (pid `cat $(PIDFILE)`)"; exit 0; fi; \
	  if ! kill -0 `cat $(PIDFILE)` 2>/dev/null; then \
	    echo "failed to start — last lines of $(LOGFILE):"; tail -20 $(LOGFILE); \
	    rm -f $(PIDFILE); exit 1; fi; \
	  sleep 0.25; \
	done; \
	echo "did not become healthy in 10s; see $(LOGFILE)"; exit 1

stop:  ## Stop the background server
	@if [ -f $(PIDFILE) ]; then kill `cat $(PIDFILE)` 2>/dev/null || true; \
	  rm -f $(PIDFILE); echo "stopped"; else echo "not running"; fi

restart: stop  ## Restart the background server
	@sleep 1
	@$(MAKE) --no-print-directory run

status:  ## Is the server up, and are its dependencies reachable?
	@if [ -f $(PIDFILE) ] && kill -0 `cat $(PIDFILE)` 2>/dev/null; then \
	  echo "running (pid `cat $(PIDFILE)`)"; \
	  curl -fsS http://$(HOST):$(PORT)/health && echo; \
	  curl -sS http://$(HOST):$(PORT)/ready && echo; \
	else echo "not running"; fi

worker:  ## Run a job worker in the foreground: make worker CONCURRENCY=2
	.venv/bin/python -m radreport.workers --concurrency $(or $(CONCURRENCY),1)

logs:  ## Follow the background server's log
	@tail -f $(LOGFILE)

# ----------------------------------------------------------------- checks ---
test:  ## Run every test (DB-backed ones skip without RADREPORT_TEST_DATABASE_URL)
	.venv/bin/pytest -q

test-unit:  ## Run only the tests that need no database
	.venv/bin/pytest -q tests/unit

lint:  ## Check formatting and lint rules
	.venv/bin/ruff check radreport tests

fmt:  ## Apply formatting
	.venv/bin/ruff format radreport tests

check: lint test  ## Lint and test — what CI runs

clean:  ## Remove caches and the server pid/log
	rm -rf .pytest_cache .ruff_cache $(PIDFILE) $(LOGFILE)
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
