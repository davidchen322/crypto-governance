.DEFAULT_GOAL := help
SHELL := /bin/bash
VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest
RUFF := $(VENV)/bin/ruff

.PHONY: help install lint fmt test up down nuke logs ps verify verify-fast verify-live harvest check-openai silver silver-selftest sql spark-sql embed eval eval-sources clean api airflow-up verify-airflow airflow-check airflow-cli

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

$(VENV)/bin/activate: pyproject.toml
	python3 -m venv $(VENV)
	$(PIP) install --quiet --upgrade pip
	$(PIP) install --quiet -e ".[dev]"
	@touch $(VENV)/bin/activate

install: $(VENV)/bin/activate ## Create venv and install the package with dev extras

lint: install ## Run ruff
	$(RUFF) check .
	$(RUFF) format --check .

fmt: install ## Autoformat with ruff
	$(RUFF) format .
	$(RUFF) check --fix .

test: install ## Unit tests only (no docker needed)
	$(PYTEST)

up: ## Start the stack and block until every healthcheck passes
	docker compose up -d --wait

down: ## Stop containers, KEEP volumes (used by the persistence check)
	docker compose down

nuke: ## Stop containers and DESTROY volumes
	docker compose down -v --remove-orphans

ps: ## Show service status
	docker compose ps

logs: ## Tail logs for all services
	docker compose logs -f --tail=100

verify: install ## Full Phase 0+1 acceptance: rebuild from zero, probe, restart, re-probe
	./scripts/verify.sh

verify-fast: install ## Probes only, against an already-running stack
	$(PYTEST) -m "integration and not persistence and not live" -v

verify-live: install ## Contract tests against the real Snapshot and Discourse APIs
	$(PYTEST) -m live -v

harvest: install ## Harvest governance data into bronze (make harvest ARGS="--protocol aave")
	$(PY) -m data_pipeline.harvest $(ARGS)

check-openai: install ## Verify OPENAI_API_KEY works and returns the expected vector width
	$(PY) scripts/check_openai.py

embed: install ## Embed silver into pgvector (ARGS=--dry-run for cost only)
	$(PY) -m ai_agent.chains.embeddings $(ARGS)

eval: install ## Score retrieval against the eval set (ARGS="--verbose --save")
	$(PY) tests/eval/score.py $(ARGS)

eval-sources: install ## Print eval questions with links to their source documents
	$(PY) scripts/eval_sources.py $(ARGS)

search: install ## Semantic search from the shell (make search Q="oracle deprecation")
	$(VENV)/bin/gov search "$(Q)" $(ARGS)

backfill-metadata: install ## Fill title/document_date on embeddings from silver (no re-embed)
	$(PY) scripts/backfill_citation_metadata.py $(ARGS)

ask: install ## Answer a governance question with citations (make ask Q="...")
	$(VENV)/bin/gov ask "$(Q)" $(ARGS)

eval-routing: install ## Score the intent router against the routing eval set
	$(PY) tests/eval/score_routing.py $(ARGS)

API_PORT ?= 8000
api: install ## Run the FastAPI dev server (API_PORT=8001 make api for a different port)
	$(VENV)/bin/uvicorn backend_api.main:app --reload --port $(API_PORT)

sql: ## Interactive Trino shell — fast, use this for exploring
	docker compose exec -it trino trino

spark-sql: ## Interactive Spark SQL shell (slower; matches what the jobs run)
	docker compose exec -it spark spark-sql

silver: ## Build the SCD2 silver tables from bronze (Spark)
	docker compose exec -T spark spark-submit --master "local[*]" \
		/opt/app/data_pipeline/transformation/build_silver.py

silver-selftest: ## Deterministic Spark fixture checks (SCD2 windows, optional-column safety)
	docker compose exec -T spark spark-submit --master "local[*]" /opt/app/tests/spark/scd2_selftest.py
	docker compose exec -T spark spark-submit --master "local[*]" \
		/opt/app/tests/spark/optional_column_selftest.py

airflow-up: ## Start Airflow (Phase 7): migrate + create admin user, then webserver + scheduler
	docker compose up -d airflow-init
	docker compose up -d --wait airflow-webserver airflow-scheduler
	@echo "Airflow UI: http://localhost:$${AIRFLOW_WEB_PORT:-8082}  (see .env for admin credentials)"

verify-airflow: ## Phase 7 DAG tests, against the real Airflow install in the scheduler container
	docker compose exec -T -w /opt/app airflow-scheduler python -m pytest tests/test_phase7_dags.py -v -p no:cacheprovider

airflow-check: ## Quick DAG import-error check against the real Airflow install
	docker compose exec -T airflow-scheduler airflow dags list-import-errors

airflow-cli: ## Interactive shell in the scheduler container (airflow dags test, airflow variables, ...)
	docker compose exec -it airflow-scheduler bash

clean: ## Remove venv and caches
	rm -rf $(VENV) .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
