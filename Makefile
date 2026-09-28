.DEFAULT_GOAL := help
COMPOSE := docker compose
RUN := uv run
TEST_DATABASE_URL ?= postgresql+asyncpg://ods_delivery:ods_delivery_local@localhost:5451/ods_delivery_test

.PHONY: help up deps down logs run migrate revision seed test lint fmt shell-db check-heads import-base44

help: ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

up: ## whole stack in docker (db, minio, mailpit, api on :8110)
	$(COMPOSE) up -d --build

deps: ## only db + minio + mailpit (run the api with `make run`)
	$(COMPOSE) up -d db minio minio-init mailpit

down: ## stop the stack (volumes kept)
	$(COMPOSE) down

logs: ## follow the api logs
	$(COMPOSE) logs -f api

run: ## api on the host with reload (needs `make deps`)
	$(RUN) uvicorn app.main:app --host 127.0.0.1 --port 8110 --reload --reload-dir app

migrate: ## alembic upgrade head
	$(RUN) alembic upgrade head

revision: ## new migration: make revision m="add foo"
	$(RUN) alembic revision --autogenerate -m "$(m)"

seed: ## local admin + QA accounts + default settings (idempotent)
	$(RUN) python -m scripts.seed_local

test: ## pytest against the ods_delivery_test database
	DATABASE_URL=$(TEST_DATABASE_URL) $(RUN) pytest --cov=app --cov-report=term-missing:skip-covered $(ARGS)

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
