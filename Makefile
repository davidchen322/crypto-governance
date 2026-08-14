.DEFAULT_GOAL := help
SHELL := /bin/bash
VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest
RUFF := $(VENV)/bin/ruff

.PHONY: help install lint fmt test up down nuke logs ps verify verify-fast verify-live harvest check-openai clean

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

clean: ## Remove venv and caches
	rm -rf $(VENV) .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
