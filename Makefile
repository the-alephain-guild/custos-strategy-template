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

# The environment's own interpreter once there is one, and before that one with the
# standard library alone: help and next have to work before anything is installed.
VENV_DIR = $(if $(filter dev,$(TOOLCHAIN)),.venv-dev,.venv)
STDLIB_PY = $(if $(wildcard $(VENV_DIR)/bin/python),$(VENV_DIR)/bin/python,$(PY))

.PHONY: help next verify verify-pinned check-public-surface check-disclosure check-ownership new-strategy setup setup-dev toolchain-banner lint test backtest check-dco

# Commands are listed under the `##@` heading above them, whichever file defines
# them, and the headings in HELP_SECTIONS order; any other heading follows.
HELP_SECTIONS = Getting started|Strategies|Running on a sandbox or testnet|Checks, as CI runs them
ifdef CMD
help:
	@$(STDLIB_PY) tools/help.py $(CMD)
else
#> usage: make help [CMD=<command>]
#> var: CMD | none | a command to explain in full; without it every command is listed
#> example: make help CMD=start
#> then: make next|where this repository stands
help:  ## List the commands; make help CMD=<command> explains one
	@printf 'Usage: make <command> [STRATEGY=trend/my_idea] [MODE=sandbox|testnet] [TOOLCHAIN=dev]\n'
	@awk -v wanted='$(HELP_SECTIONS)' 'BEGIN {FS = ":.*## "; n = split(wanted, order, "|"); \
	    for (i = 1; i <= n; i++) seen[order[i]] = 1} \
	  /^##@ / {section = substr($$0, 5); if (!(section in seen)) {seen[section] = 1; order[++n] = section}; next} \
	  /^[a-zA-Z0-9-]+:.*## / {lines[section] = lines[section] sprintf("  %-22s %s\n", $$1, $$2)} \
	  END {for (i = 1; i <= n; i++) if (lines[order[i]] != "") printf "\n%s\n%s", order[i], lines[order[i]]}' \
	  $(MAKEFILE_LIST)
	@printf '\nOne command in detail: make help CMD=<command>\n'
	@printf 'Not sure where you are? make next\n'
endif

##@ Getting started

#> usage: make setup
#> note: downloads the strategy toolkit by the digests in toolchain.lock.toml and installs .venv; run it again after merging a template update
#> then: make new-strategy NAME=my_idea|create a strategy
setup:  ## Download the pinned strategy toolkit and install the environment
	$(PY) tools/toolchain/fetch.py
	uv sync
	@$(UI) next \
	  "make new-strategy NAME=my_idea|create a strategy" \
	  "make backtest STRATEGY=examples/trend/sma_cross START=2025-01-01 END=2025-02-01|or backtest the example first"

#> usage: make setup-dev
#> note: reads toolchain.local.toml (copy toolchain.local.toml.example); builds .venv-dev and the runner image from the named Custos commit
#> example: make setup-dev
#> then: make test TOOLCHAIN=dev|test on it
setup-dev:  ## Build .venv-dev from the local sources in toolchain.local.toml
	@$(DEV_TOOL) build
	@$(UI) next \
	  "make test TOOLCHAIN=dev|test on it" \
	  "make start STRATEGY=trend/my_idea MODE=sandbox TOOLCHAIN=dev|run a strategy on it"

#> usage: make next [STRATEGY=<category>/<name>] [MODE=sandbox|testnet] [TOOLCHAIN=dev]
#> var: STRATEGY | the only one | which strategy to look at; with several and none named, they are listed
#> var: MODE | sandbox | sandbox fills orders on this machine; testnet trades on the exchange's test environment
#> var: TOOLCHAIN | pinned | dev runs on the Custos build named in toolchain.local.toml (make setup-dev)
#> note: only reads: it changes nothing and starts nothing
#> example: make next STRATEGY=trend/supertrend MODE=testnet
next:  ## Say where this repository stands and what to run next
	@$(STDLIB_PY) tools/next.py --repo-name $(REPO_NAME) --mode $(MODE) --toolchain $(TOOLCHAIN) \
	    $(if $(STRATEGY),--strategy $(STRATEGY))

##@ Strategies

#> usage: make new-strategy NAME=<name> [CATEGORY=<category>]
#> var: NAME | required | the strategy's directory and registry name
#> var: CATEGORY | trend | the directory it goes under in strategies/
#> var: COPIER_FLAGS | none | extra copier options, such as --defaults to skip the questions
#> example: make new-strategy NAME=my_idea CATEGORY=trend
#> then: make backtest STRATEGY=trend/my_idea START=2025-01-01 END=2025-04-01|backtest it
new-strategy:  ## Create a strategy: make new-strategy NAME=my_idea [CATEGORY=trend]
	@test -n "$(NAME)" || { $(UI) error "usage: make new-strategy NAME=my_idea [CATEGORY=trend]" --tag new-strategy; exit 2; }
	@test ! -e "strategies/$(or $(CATEGORY),trend)/$(NAME)" || { $(UI) error "strategies/$(or $(CATEGORY),trend)/$(NAME) already exists" --tag new-strategy; exit 1; }
	uvx copier copy $(COPIER_FLAGS) --data name=$(NAME) --data category=$(or $(CATEGORY),trend) . strategies/$(or $(CATEGORY),trend)/$(NAME)
	@uv run python scripts/register-strategy.py $(or $(CATEGORY),trend) $(NAME)
	@$(UI) next \
	  "edit strategies/$(or $(CATEGORY),trend)/$(NAME)/config.yaml|its pairs and parameters" \
	  "make backtest STRATEGY=$(or $(CATEGORY),trend)/$(NAME) START=2025-01-01 END=2025-04-01$(TOOLCHAIN_SUFFIX)|backtest it"

#> usage: make backtest STRATEGY=<category>/<name> START=<date> END=<date> [BALANCE=<amount>] [JSON=1] [TOOLCHAIN=dev]
#> var: STRATEGY | required | the strategy directory under strategies/, or examples/trend/sma_cross
#> var: START | required | first day, an ISO date or time, UTC unless it has an offset
#> var: END | required | last day, in the same form
#> var: BALANCE | 10000 | the starting balance in the quote currency
#> var: JSON | unset | 1 prints the summary as JSON
#> var: TOOLCHAIN | pinned | dev runs on the Custos build named in toolchain.local.toml (make setup-dev)
#> note: market data comes from the exchange's public API into .data/ and is reused; the summary is also written to the strategy's backtests/output/
#> example: make backtest STRATEGY=trend/my_idea START=2025-01-01 END=2025-04-01
#> then: make start STRATEGY=trend/my_idea MODE=sandbox|run it on live market data
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
#> usage: make test [TOOLCHAIN=dev]
#> var: TOOLCHAIN | pinned | dev runs on the Custos build named in toolchain.local.toml (make setup-dev)
#> note: the repository's tool tests first, then each strategy's own tests in a process of its own
test: $(TOOLCHAIN_BANNER)  ## Run the tool tests, then each strategy's tests
	uv run pytest tests
	@for dir in $$(find strategies examples -mindepth 3 -maxdepth 3 -name pyproject.toml -exec dirname {} \; 2>/dev/null | sort); do \
	  $(UI) info "$$dir" --tag test; uv run pytest -q $$dir || exit 1; \
	done

toolchain-banner:
	@$(DEV_TOOL) banner

include tools/runner/runner.mk

##@ Checks, as CI runs them

#> usage: make lint
#> note: ruff format check and ruff lint, as CI runs them
lint:  ## Format check and lint
	uv run ruff format --check .
	uv run ruff check .

# CI runs the pinned toolchain, so a result on the dev toolchain would not be one CI agrees with.
verify-pinned:
	@test "$(TOOLCHAIN)" = pinned || { $(UI) error "verify runs on the pinned toolchain only; run it without TOOLCHAIN=dev" --tag verify; exit 2; }

#> usage: make verify
#> note: every check CI runs, disclosure checks first; refuses TOOLCHAIN=dev, since CI runs the pinned toolchain
#> note: run make setup once first
verify: verify-pinned check-public-surface check-disclosure check-ownership check-dco lint test  ## Full gate (run make setup once first); disclosure checks run first
	@$(UI) ok "all checks passed" --tag verify

#> usage: make check-public-surface
#> note: refuses tracked private working notes
check-public-surface:  ## Refuse tracked private working notes
	$(PY) scripts/check-public-surface.py --self-test
	$(PY) scripts/check-public-surface.py

#> usage: make check-disclosure
#> note: refuses names of systems outside this repository, anywhere in it
check-disclosure:  ## Refuse names of systems outside this repository
	$(PY) scripts/check-disclosure.py --self-test
	$(PY) scripts/check-disclosure.py

#> usage: make check-ownership
#> note: proves the fork and upstream boundary check refuses what it should; CI passes it a range
check-ownership:  ## Prove the fork/upstream boundary check bites (CI passes a range)
	$(PY) scripts/check-ownership.py --self-test

#> usage: make check-dco
#> note: proves the sign-off check refuses what it should; CI passes it a pull request's range
check-dco:  ## Prove the sign-off check bites (CI passes a pull request's range)
	$(PY) scripts/check-dco.py --self-test
