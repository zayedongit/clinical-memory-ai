# Clinical Memory AI — the commands you actually need.
#
# Everything here runs without production credentials. `make check` is exactly
# what CI runs, so a green local run means a green build.

SHELL := /bin/bash
BACKEND := backend
FRONTEND := frontend

# A scratch PostgreSQL for the database tests. Override to point at your own.
export CMA_TEST_DATABASE_URL ?= postgresql://postgres:postgres@127.0.0.1:5432/postgres

.PHONY: help
help:                     ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# --------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------- #
.PHONY: setup
setup: setup-backend setup-frontend  ## Install everything

.PHONY: setup-backend
setup-backend:            ## Install backend dependencies (uv)
	cd $(BACKEND) && uv sync --frozen --group dev --group ml
	@test -f $(BACKEND)/.env || (cp $(BACKEND)/.env.example $(BACKEND)/.env && \
	  echo "Created backend/.env from the template — fill in your Supabase keys.")

.PHONY: setup-frontend
setup-frontend:           ## Install frontend dependencies (pnpm)
	cd $(FRONTEND) && pnpm install --frozen-lockfile
	@test -f $(FRONTEND)/.env.local || (cp $(FRONTEND)/.env.local.example $(FRONTEND)/.env.local && \
	  echo "Created frontend/.env.local from the template — fill it in.")

# --------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------- #
.PHONY: dev-backend
dev-backend:              ## Run the API on :8000
	cd $(BACKEND) && uv run fastapi dev app/main.py

.PHONY: dev-frontend
dev-frontend:             ## Run the web app on :3000
	cd $(FRONTEND) && pnpm dev

# --------------------------------------------------------------------- #
# Checks — `make check` is the CI gate
# --------------------------------------------------------------------- #
.PHONY: check
check: lint test eval frontend-check  ## Everything CI runs

.PHONY: lint
lint:                     ## Lint the backend
	cd $(BACKEND) && uv run ruff check .

.PHONY: test
test:                     ## Backend tests (database tests skip without a database)
	cd $(BACKEND) && uv run pytest -q

.PHONY: test-db
test-db:                  ## Database tests only (needs CMA_TEST_DATABASE_URL)
	cd $(BACKEND) && uv run pytest tests/db -q -rs

.PHONY: frontend-check
frontend-check:           ## Frontend typecheck, lint, tests, build
	cd $(FRONTEND) && pnpm exec tsc --noEmit && pnpm run lint && pnpm test && pnpm run build

# --------------------------------------------------------------------- #
# Evaluation and models
# --------------------------------------------------------------------- #
.PHONY: eval
eval: eval-red-flags eval-extraction  ## Run every evaluation harness

.PHONY: eval-red-flags
eval-red-flags:           ## Red-flag escalation: sensitivity + false-alarm rate
	cd $(BACKEND) && uv run python scripts/eval_red_flags.py

.PHONY: eval-extraction
eval-extraction:          ## Extraction + citation grounding (deterministic)
	cd $(BACKEND) && uv run python scripts/eval_extraction.py

.PHONY: eval-extraction-live
eval-extraction-live:     ## Same cases, through the real providers (needs a key)
	cd $(BACKEND) && uv run python scripts/eval_extraction.py --live

.PHONY: train
train:                    ## Retrain the escalation-risk model and its metrics
	cd $(BACKEND) && uv run --group ml python scripts/train_risk_model.py

# --------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------- #
.PHONY: db-local
db-local:                 ## Build a local database from the migrations (needs psql)
	./scripts/local_db.sh

.PHONY: db-push
db-push:                  ## Apply migrations to the linked Supabase project
	supabase db push

.PHONY: demo-data
demo-data:                ## Load synthetic demo patients into a local database
	cd $(BACKEND) && uv run python scripts/seed_demo.py

.PHONY: docs-pdf
docs-pdf:                 ## Render the docs to PDF (needs pandoc + Chrome)
	./scripts/build_docs_pdf.sh

.PHONY: clean
clean:                    ## Remove build and cache artefacts
	rm -rf $(FRONTEND)/.next $(FRONTEND)/node_modules/.cache
	find $(BACKEND) -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf $(BACKEND)/.pytest_cache $(BACKEND)/.ruff_cache
