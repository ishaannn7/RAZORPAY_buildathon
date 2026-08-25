.DEFAULT_GOAL := help
SHELL := /bin/bash

API_DIR := apps/api
WEB_DIR := apps/web
PY := $(API_DIR)/.venv/bin/python
API_PORT ?= 8817
WEB_PORT ?= 43917

.PHONY: help
help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

.PHONY: setup
setup: ## Create the virtualenv and install both apps
	@command -v uv >/dev/null 2>&1 || curl -fsSL https://astral.sh/uv/install.sh | sh
	cd $(API_DIR) && uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ".[dev]"
	cd $(WEB_DIR) && npm install

.PHONY: demo
demo: ## Generate data, fit, calibrate, reconcile and analyze (about a minute)
	cd $(API_DIR) && .venv/bin/python -m reconproof.cli demo

.PHONY: demo-quick
demo-quick: ## Smaller demo; calibration may not prove the target, which correctly disables automation
	cd $(API_DIR) && .venv/bin/python -m reconproof.cli demo --scale 0.25

.PHONY: api
api: ## Run the API on $(API_PORT)
	cd $(API_DIR) && .venv/bin/python -m reconproof.cli serve --port $(API_PORT)

.PHONY: web
web: ## Run the web app on $(WEB_PORT)
	cd $(WEB_DIR) && npm run dev -- --port $(WEB_PORT) --hostname 127.0.0.1

.PHONY: status
status: ## Show database, scorer and provider status
	cd $(API_DIR) && .venv/bin/python -m reconproof.cli status

.PHONY: report
report: ## Print the stored evaluation report
	cd $(API_DIR) && .venv/bin/python -m reconproof.cli evaluate

.PHONY: test
test: ## Run the Python test suite
	cd $(API_DIR) && .venv/bin/python -m pytest -q

.PHONY: lint
lint: ## Lint and typecheck both apps
	cd $(API_DIR) && .venv/bin/ruff check reconproof tests
	cd $(API_DIR) && .venv/bin/ruff format --check reconproof tests
	cd $(WEB_DIR) && npx tsc --noEmit && npx eslint src --max-warnings 0

.PHONY: format
format: ## Auto-format the Python app
	cd $(API_DIR) && .venv/bin/ruff format reconproof tests
	cd $(API_DIR) && .venv/bin/ruff check --fix reconproof tests

.PHONY: build
build: ## Production build of the web app
	cd $(WEB_DIR) && npm run build

.PHONY: clean
clean: ## Remove generated state (database, uploads, model artifacts)
	rm -rf .reconproof
