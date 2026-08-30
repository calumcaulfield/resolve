.DEFAULT_GOAL := help
PY := PYTHONPATH=src python

# Commands that operate on a *running stack* execute inside the API container.
# That container is the one that has the database, the seeded data and the
# evals directory; running them on the host was the source of a class of bugs
# where a command "succeeded" against a different world than the one serving
# traffic.
DC     := docker compose
# The image sets PYTHONPATH=/app/src. The eval harness also needs /app on the
# path, because `evals` is a top-level package rather than part of `src`.
# `docker compose exec` takes environment through -e; an inline VAR=value would
# be interpreted as the command to run.
IN_API := $(DC) exec -T -e PYTHONPATH=/app/src:/app api
PROVIDER ?=

.PHONY: help install dev demo ticket eval eval-gate eval-local eval-gate-local \
        seed seed-local test lint fmt typecheck check up down restart logs ps \
        migrate clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Install the package and development dependencies
	python -m pip install --upgrade pip
	python -m pip install -e ".[dev]"

# ---------------------------------------------------------------------------
# Local, no infrastructure required
# ---------------------------------------------------------------------------

demo: ## Run the agent over the synthetic inbox (no database needed)
	$(PY) -m resolve.cli demo

ticket: ## Run one ticket with a full trace: make ticket N=8
	$(PY) -m resolve.cli ticket $(or $(N),0)

test: ## Run the test suite
	PYTHONPATH=src:. python -m pytest tests -q

lint: ## Lint
	ruff check src tests evals

fmt: ## Auto-format and fix lint
	ruff format src tests evals
	ruff check --fix src tests evals

typecheck: ## Static type checking
	mypy src

check: lint typecheck test eval-gate-local ## Everything CI runs (host, no Docker)

# ---------------------------------------------------------------------------
# Stack lifecycle
# ---------------------------------------------------------------------------

up: ## Start the full stack and wait until it is ready
	$(DC) up --build -d
	@# Wait for readiness rather than returning the moment the containers
	@# exist. `docker compose up -d` returns as soon as processes start, so
	@# `make up && make seed` raced the API's startup and failed confusingly.
	@printf "  waiting for the api"
	@for i in $$(seq 1 90); do \
	  if curl -sf http://localhost:8000/healthz >/dev/null 2>&1; then \
	    echo " ready"; break; \
	  fi; \
	  if [ $$i -eq 90 ]; then \
	    echo " timed out"; $(DC) logs --tail=40 api; exit 1; \
	  fi; \
	  printf "."; sleep 1; \
	done
	@echo "  console  http://localhost:3000"
	@echo "  api      http://localhost:8000/docs"

down: ## Stop the stack and remove volumes
	$(DC) down -v

restart: ## Rebuild and restart the stack
	$(DC) up --build -d --force-recreate

ps: ## Show stack status
	$(DC) ps

logs: ## Tail the stack logs
	$(DC) logs -f api agent-worker

dev: ## Run the API on the host with autoreload
	$(PY) -m uvicorn resolve.api.app:app --reload --port 8000

migrate: ## Apply database migrations inside the stack
	$(IN_API) python -m alembic upgrade head

# ---------------------------------------------------------------------------
# Stack operations — these run INSIDE the API container
# ---------------------------------------------------------------------------

seed: ## Load the synthetic inbox into the running stack
	@$(DC) ps --status running --services 2>/dev/null | grep -qx api || { \
	  echo "The stack is not running. Start it with: make up"; exit 1; }
	$(IN_API) python -m resolve.seed.load

eval: ## Run the evaluation suite in the stack; the console reads the result via the API
	@$(DC) ps --status running --services 2>/dev/null | grep -qx api || { \
	  echo "The stack is not running. Start it with: make up"; exit 1; }
	$(IN_API) python -m evals.runner $(if $(PROVIDER),--provider $(PROVIDER),)
	@echo
	@echo "  Report written inside the api container and served at"
	@echo "    GET /v1/evals/latest  ->  http://localhost:3000/evals"

eval-gate: ## Run the evaluation suite in the stack and fail on regression
	@$(DC) ps --status running --services 2>/dev/null | grep -qx api || { \
	  echo "The stack is not running. Start it with: make up"; exit 1; }
	$(IN_API) python -m evals.runner --gate $(if $(PROVIDER),--provider $(PROVIDER),)

# ---------------------------------------------------------------------------
# Host equivalents — used by CI, which has Python but no running stack
# ---------------------------------------------------------------------------

seed-local: ## Seed against an API running on the host
	PYTHONPATH=src python -m resolve.seed.load

eval-local: ## Run the evaluation suite on the host
	PYTHONPATH=src:. python -m evals.runner $(if $(PROVIDER),--provider $(PROVIDER),)

eval-gate-local: ## Run the evaluation suite on the host and fail on regression
	PYTHONPATH=src:. python -m evals.runner --gate $(if $(PROVIDER),--provider $(PROVIDER),)

clean: ## Remove caches and build artefacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache dist build *.egg-info
