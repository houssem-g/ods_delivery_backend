.DEFAULT_GOAL := help
COMPOSE := docker compose
RUN := uv run
# parallel pytest workers, one database each (ods_delivery_test_gw<N>); TEST_WORKERS=0 = sequential
TEST_WORKERS ?= 4
TEST_DATABASE_URL ?= postgresql+asyncpg://ods_delivery:ods_delivery_local@localhost:5451/ods_delivery_test

.PHONY: help up up-full deps down logs logs-full run migrate revision seed test lint fmt shell-db check-heads \
	import-base44 backup restore db-counts reset-local import-local audit precommit

help: ## list targets
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

up: ## dev stack in docker (db, minio, mailpit, api with reload on :8110)
	$(COMPOSE) up -d --build

up-full: ## production-like stack: api-prod (no reload, JSON logs) on :8111 + front image on :5191
	$(COMPOSE) --profile full up -d --build db minio minio-init mailpit api-prod front

deps: ## only db + minio + mailpit (run the api with `make run`)
	$(COMPOSE) up -d db minio minio-init mailpit

down: ## stop the stack, full profile included (volumes kept)
	$(COMPOSE) --profile full down

logs: ## follow the api logs
	$(COMPOSE) logs -f api

logs-full: ## follow the api-prod and front logs
	$(COMPOSE) --profile full logs -f api-prod front

run: ## api on the host with reload (needs `make deps`)
	$(RUN) uvicorn app.main:app --host 127.0.0.1 --port 8110 --no-access-log --reload --reload-dir app

migrate: ## alembic upgrade head
	$(RUN) alembic upgrade head

revision: ## new migration: make revision m="add foo"
	$(RUN) alembic revision --autogenerate -m "$(m)"

seed: ## local admin + QA accounts + default settings (idempotent)
	$(RUN) python -m scripts.seed_local

test: ## pytest against the ods_delivery_test database
	DATABASE_URL=$(TEST_DATABASE_URL) $(RUN) pytest -n $(TEST_WORKERS) --cov=app --cov=migrate --cov-report=term-missing:skip-covered $(ARGS)

lint: ## ruff check + format check
	$(RUN) ruff check .
	$(RUN) ruff format --check .

fmt: ## ruff fix + format
	$(RUN) ruff check --fix .
	$(RUN) ruff format .

shell-db: ## psql on the local database
	$(COMPOSE) exec db psql -U ods_delivery -d ods_delivery

check-heads: ## fail unless alembic has a single head
	$(RUN) python scripts/check_single_head.py

import-base44: ## Base44 export -> db: make import-base44 EXPORT_DIR=… DATABASE_URL=… [ARGS="--files --dry-run"]
	@test -n "$(EXPORT_DIR)" || { echo "EXPORT_DIR=… is required"; exit 2; }
	@test "$(origin DATABASE_URL)" = "command line" || { echo "DATABASE_URL=… is required on the command line"; exit 2; }
	$(RUN) python -m migrate.pipeline "$(EXPORT_DIR)" --database-url "$(DATABASE_URL)" $(ARGS)

backup: ## pg_dump (custom format) into backups/: make backup [DB=ods_delivery]
	@scripts/db_backup.sh

restore: ## dump -> NEW database: make restore FILE=backups/x.dump DB=new_name [FORCE=1]
	@scripts/db_restore.sh

db-counts: ## row count per table: make db-counts DB=name
	@scripts/db_counts.sh

reset-local: ## drop + recreate + migrate + seed ods_delivery (asks; YES=1 to skip) [DB=name]
	@scripts/reset_local.sh

import-local: ## Base44 import of the latest ~/ODS-backups/migration/<date>/ [DB=… ARGS="--files"]
	@scripts/import_local.sh

audit: ## pip-audit of the locked dependencies [AUDIT_ARGS="--ignore-vuln ID"]
	@req=$$(mktemp) && trap 'rm -f "$$req"' EXIT && \
	  uv export --frozen --format requirements-txt --no-emit-project --quiet > "$$req" && \
	  $(RUN) pip-audit --strict --disable-pip --requirement "$$req" --progress-spinner off $(AUDIT_ARGS)

precommit: ## run the pre-commit hooks on every file (install the git hook: uv run pre-commit install)
	$(RUN) pre-commit run --all-files
