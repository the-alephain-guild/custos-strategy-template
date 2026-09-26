# Every target that runs Python goes through uv, so the interpreter is the one
# pinned in .python-version rather than whatever happens to be on PATH.
PY := uv run --no-project python
# Messages and questions for the person running a command; see tools/ui.py.
UI = uv run python tools/ui.py
# The dev toolchain tool runs in the project environment, never in .venv-dev.
DEV_TOOL = env -u UV_PROJECT_ENVIRONMENT -u UV_NO_SYNC uv run python tools/toolchain/dev.py
MAKEFLAGS += --no-print-directory

# TOOLCHAIN=dev runs tests, backtests and the local runner on the dev toolchain
# built by `make setup-dev` from toolchain.local.toml; see docs/dev-toolchain.md.
# uv must not sync that environment: syncing would put the pinned packages back.
TOOLCHAIN ?= pinned
ifeq ($(TOOLCHAIN),dev)
export UV_PROJECT_ENVIRONMENT := $(CURDIR)/.venv-dev
export UV_NO_SYNC := 1
# Scripts run outside any project environment and need neither setting.
PY := env -u UV_PROJECT_ENVIRONMENT -u UV_NO_SYNC uv run --no-project python
TOOLCHAIN_BANNER := toolchain-banner
RUNNER_IMAGE ?= $(shell $(DEV_TOOL) runner-image)
else ifneq ($(TOOLCHAIN),pinned)
$(error TOOLCHAIN is pinned or dev, not $(TOOLCHAIN))
endif
# Added to the commands suggested as next steps, so they run on the same toolchain.
TOOLCHAIN_SUFFIX := $(if $(filter dev,$(TOOLCHAIN)), TOOLCHAIN=dev)

.DEFAULT_GOAL := help

.PHONY: help next verify verify-pinned check-public-surface check-disclosure check-ownership new-strategy setup setup-dev toolchain-banner lint test backtest check-dco

# Commands are listed under the `##@` heading above them, whichever file defines
# them, and the headings in HELP_SECTIONS order; any other heading follows.
HELP_SECTIONS = Getting started|Strategies|Running on a sandbox or testnet|Checks, as CI runs them
help:  ## List the commands
	@printf 'Usage: make <command> [STRATEGY=trend/my_idea] [MODE=sandbox|testnet] [TOOLCHAIN=dev]\n'
	@awk -v wanted='$(HELP_SECTIONS)' 'BEGIN {FS = ":.*## "; n = split(wanted, order, "|"); \
	    for (i = 1; i <= n; i++) seen[order[i]] = 1} \
	  /^##@ / {section = substr($$0, 5); if (!(section in seen)) {seen[section] = 1; order[++n] = section}; next} \
	  /^[a-zA-Z0-9-]+:.*## / {lines[section] = lines[section] sprintf("  %-22s %s\n", $$1, $$2)} \
	  END {for (i = 1; i <= n; i++) if (lines[order[i]] != "") printf "\n%s\n%s", order[i], lines[order[i]]}' \
	  $(MAKEFILE_LIST)
	@printf '\nNot sure where you are? make next\n'

##@ Getting started

setup:  ## Download the pinned strategy toolkit and install the environment
	$(PY) tools/toolchain/fetch.py
	uv sync
	@$(UI) next \
	  "make new-strategy NAME=my_idea|create a strategy" \
	  "make backtest STRATEGY=examples/trend/sma_cross START=2025-01-01 END=2025-02-01|or backtest the example first"

setup-dev:  ## Build .venv-dev from the local sources in toolchain.local.toml
	@$(DEV_TOOL) build
	@$(UI) next \
	  "make test TOOLCHAIN=dev|test on it" \
	  "make start STRATEGY=trend/my_idea MODE=sandbox TOOLCHAIN=dev|run a strategy on it"

# Runs on the environment's own interpreter once there is one; before that, on the
# standard library alone, since saying there is no environment yet is its first job.
next:  ## Say where this repository stands and what to run next
	@venv=$(if $(filter dev,$(TOOLCHAIN)),.venv-dev,.venv); \
	  if [ -x "$$venv/bin/python" ]; then python="$$venv/bin/python"; else python="$(PY)"; fi; \
	  $$python tools/next.py --repo-name $(REPO_NAME) --mode $(MODE) --toolchain $(TOOLCHAIN) \
	    $(if $(STRATEGY),--strategy $(STRATEGY))

##@ Strategies

new-strategy:  ## Create a strategy: make new-strategy NAME=my_idea [CATEGORY=trend]
	@test -n "$(NAME)" || { $(UI) error "usage: make new-strategy NAME=my_idea [CATEGORY=trend]" --tag new-strategy; exit 2; }
	@test ! -e "strategies/$(or $(CATEGORY),trend)/$(NAME)" || { $(UI) error "strategies/$(or $(CATEGORY),trend)/$(NAME) already exists" --tag new-strategy; exit 1; }
	uvx copier copy $(COPIER_FLAGS) --data name=$(NAME) --data category=$(or $(CATEGORY),trend) . strategies/$(or $(CATEGORY),trend)/$(NAME)
	@uv run python scripts/register-strategy.py $(or $(CATEGORY),trend) $(NAME)
	@$(UI) next \
	  "edit strategies/$(or $(CATEGORY),trend)/$(NAME)/config.yaml|its pairs and parameters" \
	  "make backtest STRATEGY=$(or $(CATEGORY),trend)/$(NAME) START=2025-01-01 END=2025-04-01$(TOOLCHAIN_SUFFIX)|backtest it"

backtest: $(TOOLCHAIN_BANNER)  ## Backtest a strategy: make backtest STRATEGY=trend/my_idea START=2025-01-01 END=2025-04-01
	@test -n "$(STRATEGY)" -a -n "$(START)" -a -n "$(END)" || { $(UI) error "usage: make backtest STRATEGY=trend/my_idea START=2025-01-01 END=2025-04-01" --tag backtest; exit 2; }
	@uv run python tools/backtest/run.py $(STRATEGY) --start $(START) --end $(END) $(if $(BALANCE),--balance $(BALANCE)) $(if $(JSON),--json)
	@$(if $(JSON),true,if [ -f .runner/.arx/runner.toml ]; then \
	  $(UI) next "make start STRATEGY=$(STRATEGY) MODE=sandbox$(TOOLCHAIN_SUFFIX)|run it on the exchange's live market data, filling orders on this machine"; \
	else \
	  $(UI) next "make setup-runner$(TOOLCHAIN_SUFFIX)|create this machine's runner identity, once" \
	    "make start STRATEGY=$(STRATEGY) MODE=sandbox$(TOOLCHAIN_SUFFIX)|then run it on the exchange's live market data"; \
	fi)

# Every strategy's code lives in a package named `refinement`, so each strategy's
# tests run in a process of their own.
test: $(TOOLCHAIN_BANNER)  ## Run the tool tests, then each strategy's tests
	uv run pytest tests
	@for dir in $$(find strategies examples -mindepth 3 -maxdepth 3 -name pyproject.toml -exec dirname {} \; 2>/dev/null | sort); do \
	  $(UI) info "$$dir" --tag test; uv run pytest -q $$dir || exit 1; \
	done

toolchain-banner:
	@$(DEV_TOOL) banner

include tools/runner/runner.mk

##@ Checks, as CI runs them

lint:  ## Format check and lint
	uv run ruff format --check .
	uv run ruff check .

# CI runs the pinned toolchain, so a result on the dev toolchain would not be one CI agrees with.
verify-pinned:
	@test "$(TOOLCHAIN)" = pinned || { $(UI) error "verify runs on the pinned toolchain only; run it without TOOLCHAIN=dev" --tag verify; exit 2; }

verify: verify-pinned check-public-surface check-disclosure check-ownership check-dco lint test  ## Full gate (run make setup once first); disclosure checks run first
	@$(UI) ok "all checks passed" --tag verify

check-public-surface:  ## Refuse tracked private working notes
	$(PY) scripts/check-public-surface.py --self-test
	$(PY) scripts/check-public-surface.py

check-disclosure:  ## Refuse names of systems outside this repository
	$(PY) scripts/check-disclosure.py --self-test
	$(PY) scripts/check-disclosure.py

check-ownership:  ## Prove the fork/upstream boundary check bites (CI passes a range)
	$(PY) scripts/check-ownership.py --self-test

check-dco:  ## Prove the sign-off check bites (CI passes a pull request's range)
	$(PY) scripts/check-dco.py --self-test
