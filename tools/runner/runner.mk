# Run a strategy on a local Custos runner, against the exchange's sandbox or testnet.
# Included by the root Makefile; see `make help` and docs/local-run.md.

RUNNER_ROOT := $(CURDIR)/.runner
RUNNER_ARX := $(RUNNER_ROOT)/.arx
RUNNER_SPEC := $(RUNNER_ROOT)/deployment.json
RUNNER_LOGS := $(RUNNER_ROOT)/logs
RUNNER_IMAGE ?= $(shell $(PY) -c "import tomllib; print(tomllib.load(open('toolchain.lock.toml','rb'))['runner']['image'])")
RUNNER_PACKAGE_VERSION ?= $(shell $(PY) -c "import tomllib; print(tomllib.load(open('toolchain.lock.toml','rb'))['runner']['package_version'])")

MODE ?= sandbox
TENANT_ID ?= local
CUSTOS_ENGINE ?= nautilus
WAIT_TIMEOUT ?= 60
LIFECYCLE_STATE ?= running
STRATEGY_NAME = $(notdir $(STRATEGY))
SPEC_ID = $(STRATEGY_NAME)-$(MODE)
RUNNER_LABEL = local-$(STRATEGY_NAME)
COMPOSE_PROJECT = custos-$(subst _,-,$(STRATEGY_NAME))-$(MODE)

# Each start publishes a generation newer than anything before it, so the clock is used.
# It is computed once and exported, so every step of one start agrees on it.
GENERATION ?= $(shell $(PY) -c "import time; print(time.time_ns())")
GENERATION := $(GENERATION)
CLEAR_GENERATION = $(shell expr $(GENERATION) - 1)

SPEC_TOOL = uv run python tools/runner/spec.py
IDENTITY_TOOL = uv run python tools/runner/identity.py
STRATEGY_CONTAINER_PATH = $(shell $(SPEC_TOOL) container-path --strategy $(STRATEGY))
COMPOSE = RUNNER_IMAGE=$(RUNNER_IMAGE) RUNNER_ROOT=$(RUNNER_ROOT) REPO_ROOT=$(CURDIR) \
	TENANT_ID=$(TENANT_ID) SPEC_ID=$(SPEC_ID) RUNNER_LABEL=$(RUNNER_LABEL) \
	CUSTOS_ENGINE=$(CUSTOS_ENGINE) GENERATION=$(GENERATION) LIFECYCLE_STATE=$(LIFECYCLE_STATE) \
	WAIT_TIMEOUT=$(WAIT_TIMEOUT) STRATEGY_CONTAINER_PATH=$(STRATEGY_CONTAINER_PATH) \
	docker compose -p $(COMPOSE_PROJECT) -f tools/runner/docker-compose.yaml

export GENERATION

.PHONY: runner-init runner-vault run run-detached run-stop run-logs run-status run-smoke \
	runner-check-image runner-check runner-render runner-clear

define require_strategy
	@test -n "$(STRATEGY)" || { echo "set STRATEGY, for example STRATEGY=trend/my_idea"; exit 2; }
endef

runner-init:  ## Create this machine's runner identity (once)
	@case "$(MODE)" in sandbox|testnet) ;; *) echo "a local identity is for sandbox and testnet only"; exit 2 ;; esac
	$(MAKE) runner-check-image
	$(IDENTITY_TOOL) init --tenant-id $(TENANT_ID) --image $(RUNNER_IMAGE)

runner-vault:  ## Seal a strategy's exchange key: make runner-vault STRATEGY=trend/my_idea
	$(require_strategy)
	@bash tools/runner/vault.sh --arx-root $(RUNNER_ARX) --image $(RUNNER_IMAGE) \
		--tenant-id $(TENANT_ID) --strategy $(STRATEGY) \
		--credential-id $$($(SPEC_TOOL) credential-id --strategy $(STRATEGY)) \
		--connector $$($(SPEC_TOOL) connector --strategy $(STRATEGY)) \
		$(if $(API_SECRET_ENV),--api-secret-env $(API_SECRET_ENV)) \
		$(if $(API_PASSPHRASE_ENV),--api-passphrase-env $(API_PASSPHRASE_ENV)) \
		$(if $(REPLACE),--replace)

run: run-detached  ## Run a strategy and follow its log: make run STRATEGY=trend/my_idea MODE=sandbox
	$(COMPOSE) logs -f custos-runner

run-detached: $(TOOLCHAIN_BANNER)  ## Same as run, returning once the strategy reports running
	$(require_strategy)
	$(MAKE) runner-check-image
	$(MAKE) runner-check
	$(MAKE) runner-render
	docker run --rm -v "$(CURDIR):/opt/repo:ro" -v "$(RUNNER_ROOT):/runtime:ro" $(RUNNER_IMAGE) \
		deployment validate --spec-file /runtime/deployment.json \
		--strategy-dir $(STRATEGY_CONTAINER_PATH)/refinement/nautilus
	$(COMPOSE) up -d --wait --wait-timeout $(WAIT_TIMEOUT) custos-runner
	$(MAKE) runner-clear
	$(MAKE) runner-render
	$(COMPOSE) run --rm --no-deps spec-publisher
	$(COMPOSE) run --rm --no-deps status-probe

# The runner remembers the last deployment it applied for this spec, in .runner/.arx,
# which outlives the containers. With that record in place, a start that changes the
# strategy, pair or venue is treated as a reconfiguration, which the engine refuses.
# A stop published first clears the record, so the start takes the deploy path.
#
# The stop has to be older than the deployment that follows it: the runner applies
# each generation once and ignores anything older than what it applied. And publishing
# is not landing -- the stream keeps only the newest message per subject -- so the stop
# is waited for. A stop that never reports means either a newer spec replaced it before
# the runner read it, or GENERATION was pinned at or below one already applied.
runner-clear:
	@case "$(CLEAR_GENERATION)" in \
	  '' | 0 | *[!0-9]*) echo "generation $(GENERATION) leaves nothing older to clear with; skipping" ;; \
	  *) $(MAKE) runner-render GENERATION=$(CLEAR_GENERATION) LIFECYCLE_STATE=stopped && \
	     $(COMPOSE) run --rm --no-deps spec-publisher && \
	     $(MAKE) run-wait GENERATION=$(CLEAR_GENERATION) LIFECYCLE_STATE=stopped WAIT_TIMEOUT=30 || { \
	       echo "the previous deployment was not cleared within 30s; retry, or unset a pinned GENERATION" >&2; \
	       exit 1; } ;; \
	esac

run-wait:
	$(COMPOSE) run --rm --no-deps status-probe

runner-render:
	@mkdir -p $(RUNNER_ROOT)
	$(SPEC_TOOL) render --strategy $(STRATEGY) --mode $(MODE) --generation $(GENERATION) \
		--lifecycle-state $(LIFECYCLE_STATE) --output $(RUNNER_SPEC)

runner-check:
	$(IDENTITY_TOOL) check --tenant-id $(TENANT_ID)
	@CREDENTIAL_ID=$$($(SPEC_TOOL) credential-id --strategy $(STRATEGY)); \
	$(IDENTITY_TOOL) check-vault --credential-id $$CREDENTIAL_ID && \
	docker run --rm -v "$(RUNNER_ARX):/home/custos/.arx" -e SOPS_AGE_KEY_FILE=/home/custos/.arx/age.key \
		$(RUNNER_IMAGE) vault verify --tenant-id $(TENANT_ID) --key-id $$CREDENTIAL_ID \
		--vault-dir /home/custos/.arx/vault >/dev/null || { \
	  echo "the exchange key for $(STRATEGY) is not sealed; run: make runner-vault STRATEGY=$(STRATEGY)" >&2; \
	  exit 1; }

ifeq ($(TOOLCHAIN),dev)
# A dev image reports the same package version as the release it precedes, so it
# is checked by the source revision it was built from instead.
runner-check-image:
	@$(PY) tools/toolchain/dev.py check-image $(RUNNER_IMAGE)
	@$(SPEC_TOOL) check-runner --image $(RUNNER_IMAGE)
else
runner-check-image:
	@docker image inspect $(RUNNER_IMAGE) >/dev/null 2>&1 || docker pull $(RUNNER_IMAGE) || { \
	  echo "runner image $(RUNNER_IMAGE) is neither here nor pullable; see docs/local-run.md" >&2; exit 1; }
	@version=$$(docker run --rm --entrypoint python $(RUNNER_IMAGE) -c \
	  "from importlib.metadata import version; print(version('custos-runner'))"); \
	test "$$version" = "$(RUNNER_PACKAGE_VERSION)" || { \
	  echo "runner image reports custos-runner $$version; toolchain.lock.toml expects $(RUNNER_PACKAGE_VERSION)" >&2; \
	  exit 1; }
	@$(SPEC_TOOL) check-runner --image $(RUNNER_IMAGE)
endif

# Stop first, then save the logs, then remove the containers: the shutdown happens
# while the containers stop, so saving before would miss it and removing first would
# lose it. Saving never blocks the stop.
run-stop:  ## Stop a running strategy and keep its logs in .runner/logs/
	$(require_strategy)
	-$(COMPOSE) stop
	@mkdir -p $(RUNNER_LOGS)
	@out="$(RUNNER_LOGS)/$(COMPOSE_PROJECT)-$$(date -u +%Y%m%dT%H%M%SZ).log"; \
	  if $(COMPOSE) logs --no-color --timestamps > "$$out" 2>&1 && [ -s "$$out" ]; then \
	    echo "logs saved: $$out"; \
	  else rm -f "$$out"; echo "no logs to save for $(COMPOSE_PROJECT)"; fi
	$(COMPOSE) down

run-logs:  ## Follow a running strategy's log
	$(require_strategy)
	$(COMPOSE) logs -f custos-runner

run-status:  ## Show the local runner's containers
	$(require_strategy)
	$(COMPOSE) ps

# Starts and stops the strategy against a simulated engine that never contacts an
# exchange, so it needs no exchange key and checks only that the lane works end to end.
run-smoke:  ## Start and stop a strategy on the simulated engine
	$(require_strategy)
	@set -eu; trap '$(MAKE) run-stop' 0; \
	  $(MAKE) run-detached CUSTOS_ENGINE=sandbox-sim MODE=sandbox; \
	  stop=$$(expr $(GENERATION) + 1); \
	  $(MAKE) runner-render MODE=sandbox GENERATION=$$stop LIFECYCLE_STATE=stopped; \
	  $(COMPOSE) run --rm --no-deps spec-publisher; \
	  $(MAKE) run-wait MODE=sandbox GENERATION=$$stop LIFECYCLE_STATE=stopped; \
	  trap - 0; $(MAKE) run-stop MODE=sandbox
