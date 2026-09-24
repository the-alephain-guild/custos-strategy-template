# Every target that runs Python goes through uv, so the interpreter is the one
# pinned in .python-version rather than whatever happens to be on PATH.
PY := uv run --no-project python

# TOOLCHAIN=dev runs tests, backtests and the local runner on the dev toolchain
# built by `make toolkit-dev` from toolchain.local.toml; see docs/dev-toolchain.md.
# uv must not sync that environment: syncing would put the pinned packages back.
TOOLCHAIN ?= pinned
ifeq ($(TOOLCHAIN),dev)
export UV_PROJECT_ENVIRONMENT := $(CURDIR)/.venv-dev
export UV_NO_SYNC := 1
# Scripts run outside any project environment and need neither setting.
PY := env -u UV_PROJECT_ENVIRONMENT -u UV_NO_SYNC uv run --no-project python
TOOLCHAIN_BANNER := toolchain-banner
RUNNER_IMAGE ?= $(shell $(PY) tools/toolchain/dev.py runner-image)
else ifneq ($(TOOLCHAIN),pinned)
$(error TOOLCHAIN is pinned or dev, not $(TOOLCHAIN))
endif

.PHONY: help verify verify-pinned check-public-surface check-disclosure check-ownership new-strategy toolkit toolkit-dev toolchain-banner lint test backtest check-dco

help:  ## List targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  %-24s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

check-public-surface:  ## Refuse tracked private working notes
	$(PY) scripts/check-public-surface.py --self-test
	$(PY) scripts/check-public-surface.py

check-disclosure:  ## Refuse names of systems outside this repository
	$(PY) scripts/check-disclosure.py --self-test
	$(PY) scripts/check-disclosure.py

check-ownership:  ## Prove the fork/upstream boundary check bites (CI passes a range)
	$(PY) scripts/check-ownership.py --self-test

toolkit:  ## Download the pinned strategy toolkit and install the environment
	$(PY) tools/toolchain/fetch.py
	uv sync

toolkit-dev:  ## Build .venv-dev from the local sources in toolchain.local.toml
	$(PY) tools/toolchain/dev.py build

toolchain-banner:
	@$(PY) tools/toolchain/dev.py banner

lint:  ## Format check and lint
	uv run ruff format --check .
	uv run ruff check .

backtest: $(TOOLCHAIN_BANNER)  ## Backtest a strategy: make backtest STRATEGY=trend/my_idea START=2025-01-01 END=2025-04-01
	@test -n "$(STRATEGY)" -a -n "$(START)" -a -n "$(END)" || { echo "usage: make backtest STRATEGY=trend/my_idea START=2025-01-01 END=2025-04-01"; exit 2; }
	uv run python tools/backtest/run.py $(STRATEGY) --start $(START) --end $(END) $(if $(BALANCE),--balance $(BALANCE))

# Every strategy's code lives in a package named `refinement`, so each strategy's
# tests run in a process of their own.
test: $(TOOLCHAIN_BANNER)  ## Run the tool tests, then each strategy's tests
	uv run pytest tests
	@for dir in $$(find strategies examples -mindepth 3 -maxdepth 3 -name pyproject.toml -exec dirname {} \; 2>/dev/null | sort); do \
	  echo "== $$dir"; uv run pytest -q $$dir || exit 1; \
	done

new-strategy:  ## Create a strategy: make new-strategy NAME=my_idea [CATEGORY=trend]
	@test -n "$(NAME)" || { echo "usage: make new-strategy NAME=my_idea [CATEGORY=trend]"; exit 2; }
	@test ! -e "strategies/$(or $(CATEGORY),trend)/$(NAME)" || { echo "strategies/$(or $(CATEGORY),trend)/$(NAME) already exists"; exit 1; }
	uvx copier copy $(COPIER_FLAGS) --data name=$(NAME) --data category=$(or $(CATEGORY),trend) . strategies/$(or $(CATEGORY),trend)/$(NAME)
	$(PY) scripts/register-strategy.py $(or $(CATEGORY),trend) $(NAME)

include tools/runner/runner.mk

check-dco:  ## Prove the sign-off check bites (CI passes a pull request's range)
	$(PY) scripts/check-dco.py --self-test

# CI runs the pinned toolchain, so a result on the dev toolchain would not be one CI agrees with.
verify-pinned:
	@test "$(TOOLCHAIN)" = pinned || { echo "verify runs on the pinned toolchain only; run it without TOOLCHAIN=dev"; exit 2; }

verify: verify-pinned check-public-surface check-disclosure check-ownership check-dco lint test  ## Full gate (run make toolkit once first); disclosure checks run first
	@echo "verify passed"
