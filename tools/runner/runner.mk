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
# Docker names a run's containers after its compose project, and treats any
# project of the same name as the same run, whoever started it. The repository's
# name keeps runs from different repositories apart.
REPO_NAME := $(shell basename "$(CURDIR)" | tr 'A-Z_.' 'a-z--')
COMPOSE_PROJECT = custos-$(REPO_NAME)-$(subst _,-,$(STRATEGY_NAME))-$(MODE)
# Each run keeps its own runner state: what it last applied and whether it is
# ready. Runs of other strategies or modes share only the identity and the keys.
RUNNER_STATE = $(RUNNER_ROOT)/state/$(COMPOSE_PROJECT)

# Each start publishes a generation newer than anything before it, so the clock is used.
# It is computed once and exported, so every step of one start agrees on it.
GENERATION ?= $(shell $(PY) -c "import time; print(time.time_ns())")
GENERATION := $(GENERATION)
CLEAR_GENERATION = $(shell expr $(GENERATION) - 1)

SPEC_TOOL = uv run python tools/runner/spec.py
RUN_STAMP := $(shell date -u +%Y%m%dT%H%M%SZ)
TELEMETRY_READ = $(COMPOSE) exec -T custos-runner python /opt/repo/tools/runner/telemetry_read.py \
	--tenant-id $(TENANT_ID) --runner-label $(RUNNER_LABEL) --spec-id $(SPEC_ID)
IDENTITY_TOOL = uv run python tools/runner/identity.py
STRATEGY_CONTAINER_PATH = $(shell $(SPEC_TOOL) container-path --strategy $(STRATEGY))
COMPOSE = RUNNER_IMAGE=$(RUNNER_IMAGE) RUNNER_ROOT=$(RUNNER_ROOT) REPO_ROOT=$(CURDIR) \
	RUNNER_STATE=$(RUNNER_STATE) \
	TENANT_ID=$(TENANT_ID) SPEC_ID=$(SPEC_ID) RUNNER_LABEL=$(RUNNER_LABEL) \
	CUSTOS_ENGINE=$(CUSTOS_ENGINE) GENERATION=$(GENERATION) LIFECYCLE_STATE=$(LIFECYCLE_STATE) \
	WAIT_TIMEOUT=$(WAIT_TIMEOUT) STRATEGY_CONTAINER_PATH=$(STRATEGY_CONTAINER_PATH) \
	docker compose -p $(COMPOSE_PROJECT) -f tools/runner/docker-compose.yaml

export GENERATION

.PHONY: runner-init runner-vault run run-detached run-stop run-logs run-status run-smoke \
	runner-check-image runner-check runner-render runner-clear

define require_strategy
	@test -n "$(STRATEGY)" || { $(UI) error "set STRATEGY, for example STRATEGY=trend/my_idea"; exit 2; }
endef

# Steps whose output is the runner's own logging: shown only when they fail.
STEP_LOG = $(RUNNER_ROOT)/last-step.log
QUIETLY = > $(STEP_LOG) 2>&1 || { cat $(STEP_LOG); exit 1; }
# The same, then a sentence saying what failed and where to look.
fails_with = > $(STEP_LOG) 2>&1 || { cat $(STEP_LOG); $(UI) error "$(1)"; exit 1; }

# MODE is passed on only when given, so that a terminal can be asked which key it is.
MODE_GIVEN = $(filter command line environment override,$(origin MODE))

runner-init:  ## Create this machine's runner identity (once)
	@case "$(MODE)" in sandbox|testnet) ;; *) $(UI) error "a local identity is for sandbox and testnet only"; exit 2 ;; esac
	@$(MAKE) runner-check-image
	@$(IDENTITY_TOOL) init --tenant-id $(TENANT_ID) --image $(RUNNER_IMAGE)

runner-vault:  ## Seal a strategy's testnet key: make runner-vault STRATEGY=trend/my_idea MODE=testnet
	$(require_strategy)
	@uv run python tools/runner/vault.py --strategy $(STRATEGY) --arx-root $(RUNNER_ARX) \
		--image $(RUNNER_IMAGE) --tenant-id $(TENANT_ID) \
		$(if $(MODE_GIVEN),--mode $(MODE)) \
		$(if $(API_SECRET_ENV),--api-secret-env $(API_SECRET_ENV)) \
		$(if $(API_PASSPHRASE_ENV),--api-passphrase-env $(API_PASSPHRASE_ENV)) \
		$(if $(REPLACE),--replace)

run: run-detached  ## Run a strategy and follow its log: make run STRATEGY=trend/my_idea MODE=sandbox
	@$(UI) info "following the log; Ctrl-C stops following, not the strategy (make run-stop STRATEGY=$(STRATEGY) stops it)"
	@$(COMPOSE) logs -f custos-runner

run-detached: $(TOOLCHAIN_BANNER)  ## Same as run, returning once the strategy reports running
	$(require_strategy)
	@$(UI) info "checking the runner image"
	@$(MAKE) runner-check-image
	@$(UI) info "checking this machine's identity and the $(MODE) key"
	@$(MAKE) runner-check
	@$(MAKE) runner-render
	@$(UI) info "validating the deployment for $(STRATEGY) in $(MODE) mode"
	@docker run --rm -v "$(CURDIR):/opt/repo:ro" -v "$(RUNNER_ROOT):/runtime:ro" $(RUNNER_IMAGE) \
		deployment validate --spec-file /runtime/deployment.json \
		--strategy-dir $(STRATEGY_CONTAINER_PATH)/refinement/nautilus \
		$(call fails_with,the runner refused this deployment of $(STRATEGY); the reason is above)
	@$(UI) info "starting the runner"
	@mkdir -p $(RUNNER_STATE)
	@$(COMPOSE) up -d --wait --wait-timeout $(WAIT_TIMEOUT) custos-runner $(QUIETLY)
	@$(MAKE) runner-clear
	@$(MAKE) runner-render
	@$(UI) info "publishing the deployment and waiting for it to run"
	@$(COMPOSE) run --rm --no-deps spec-publisher $(QUIETLY)
	@$(COMPOSE) run --rm --no-deps status-probe \
		$(call fails_with,$(STRATEGY) did not report running; its log: make run-logs STRATEGY=$(STRATEGY))
	@$(UI) ok "$(STRATEGY) is running in $(MODE) mode"

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
	  '' | 0 | *[!0-9]*) $(UI) warn "generation $(GENERATION) leaves nothing older to clear with; skipping" ;; \
	  *) $(UI) info "clearing what the runner remembers from its last start" && \
	     $(MAKE) runner-render GENERATION=$(CLEAR_GENERATION) LIFECYCLE_STATE=stopped && \
	     $(COMPOSE) run --rm --no-deps spec-publisher > $(STEP_LOG) 2>&1 && \
	     $(MAKE) run-wait GENERATION=$(CLEAR_GENERATION) LIFECYCLE_STATE=stopped WAIT_TIMEOUT=30 >> $(STEP_LOG) 2>&1 || { \
	       cat $(STEP_LOG); \
	       $(UI) error "the previous deployment was not cleared within 30s; retry, or unset a pinned GENERATION"; \
	       exit 1; } ;; \
	esac

run-wait:
	@$(COMPOSE) run --rm --no-deps status-probe

runner-render:
	@mkdir -p $(RUNNER_ROOT)
	@$(SPEC_TOOL) render --strategy $(STRATEGY) --mode $(MODE) --generation $(GENERATION) \
		--lifecycle-state $(LIFECYCLE_STATE) --output $(RUNNER_SPEC)

# Sandbox needs no real key, so its placeholder is sealed here when missing rather
# than asked of the user. Testnet's key has to come from them.
runner-check:
	@$(IDENTITY_TOOL) check --tenant-id $(TENANT_ID)
	@CREDENTIAL_ID=$$($(SPEC_TOOL) credential-id --strategy $(STRATEGY) --mode $(MODE)); \
	if [ "$(MODE)" = sandbox ] && [ ! -e "$(RUNNER_ARX)/vault/$$CREDENTIAL_ID.enc" ]; then \
	  $(MAKE) runner-vault STRATEGY=$(STRATEGY) MODE=sandbox || exit 1; \
	fi; \
	$(IDENTITY_TOOL) check-vault --credential-id $$CREDENTIAL_ID && \
	docker run --rm -v "$(RUNNER_ARX):/home/custos/.arx" -e SOPS_AGE_KEY_FILE=/home/custos/.arx/age.key \
		$(RUNNER_IMAGE) vault verify --tenant-id $(TENANT_ID) --key-id $$CREDENTIAL_ID \
		--vault-dir /home/custos/.arx/vault >/dev/null || { \
	  $(UI) error "no $(MODE) key is sealed for $(STRATEGY); run: make runner-vault STRATEGY=$(STRATEGY) MODE=$(MODE)"; \
	  exit 1; }

ifeq ($(TOOLCHAIN),dev)
# A dev image reports the same package version as the release it precedes, so it
# is checked by the source revision it was built from instead.
runner-check-image:
	@$(DEV_TOOL) check-image $(RUNNER_IMAGE)
	@$(SPEC_TOOL) check-runner --image $(RUNNER_IMAGE)
else
runner-check-image:
	@docker image inspect $(RUNNER_IMAGE) >/dev/null 2>&1 || { \
	  $(UI) info "pulling the runner image $(RUNNER_IMAGE)"; docker pull -q $(RUNNER_IMAGE) >/dev/null; } || { \
	  $(UI) error "runner image $(RUNNER_IMAGE) is neither here nor pullable; see docs/local-run.md"; exit 1; }
	@version=$$(docker run --rm --entrypoint python $(RUNNER_IMAGE) -c \
	  "from importlib.metadata import version; print(version('custos-runner'))"); \
	test "$$version" = "$(RUNNER_PACKAGE_VERSION)" || { \
	  $(UI) error "runner image reports custos-runner $$version; toolchain.lock.toml expects $(RUNNER_PACKAGE_VERSION)"; \
	  exit 1; }
	@$(SPEC_TOOL) check-runner --image $(RUNNER_IMAGE)
endif

# The last report is saved before the stop: what the runner reported lives only as
# long as this run's NATS server. Then stop, then save the logs, then remove the
# containers: the shutdown happens while the containers stop, so saving before would
# miss it and removing first would lose it. Saving never blocks the stop.
#
# The runner's exit code says whether every strategy confirmed it stopped; 137 means
# docker killed it before it could say.
#
# Runners that stop each strategy cleanly on SIGTERM arrived in the same Custos
# change as the reports, so the reports' module marks them. An older runner
# ignores SIGTERM and would only be killed at the end of the grace period, so it
# is given the 30 seconds it always had rather than 90 it cannot use.
CLEAN_STOP_MARKER = custos.offline.telemetry
run-stop:  ## Stop a running strategy and keep its logs in .runner/logs/
	$(require_strategy)
	@mkdir -p $(RUNNER_LOGS)
	@out="$(RUNNER_LOGS)/$(COMPOSE_PROJECT)-$(RUN_STAMP).report.json"; \
	  if $(TELEMETRY_READ) > "$$out" 2> $(STEP_LOG) && [ -s "$$out" ]; then \
	    $(UI) ok "last report saved to $${out#$(CURDIR)/}"; \
	  else rm -f "$$out"; fi
	@if $(COMPOSE) exec -T custos-runner python -c "import $(CLEAN_STOP_MARKER)" > /dev/null 2>&1; then \
	  grace=90; $(UI) info "stopping $(STRATEGY); it may take up to 90 seconds to finish cleanly"; \
	else \
	  grace=30; $(UI) warn "this runner release stops without letting $(STRATEGY) cancel its orders"; \
	fi; \
	  $(COMPOSE) stop --timeout $$grace > $(STEP_LOG) 2>&1 || true
	@out="$(RUNNER_LOGS)/$(COMPOSE_PROJECT)-$(RUN_STAMP).log"; \
	  if $(COMPOSE) logs --no-color --timestamps > "$$out" 2>&1 && [ -s "$$out" ]; then \
	    $(UI) ok "logs saved to $${out#$(CURDIR)/}"; \
	  else rm -f "$$out"; $(UI) warn "no logs to save for $(COMPOSE_PROJECT)"; fi
	@runner=$$($(COMPOSE) ps -aq custos-runner 2>/dev/null); \
	  code=$$( [ -n "$$runner" ] && docker inspect -f '{{.State.ExitCode}}' $$runner 2>/dev/null ); \
	  case "$$code" in \
	    0) $(UI) ok "every strategy confirmed it stopped" ;; \
	    "") ;; \
	    137) $(UI) warn "the runner was killed before it finished stopping; check the exchange for orders it left" ;; \
	    *) $(UI) warn "the runner could not confirm every strategy stopped (exit $$code); check the exchange for orders it left" ;; \
	  esac
	@$(COMPOSE) down $(QUIETLY)
	@$(UI) ok "$(STRATEGY) stopped"

# What the runner has reported about this run: its account, positions, open orders
# and fills. Read inside the runner container, where its NATS server is reachable.
run-report:  ## Show a running strategy's positions, orders and fills: add JSON=1 for JSON
	$(require_strategy)
	@if [ -z "$$($(COMPOSE) ps -q --status running custos-runner 2>/dev/null)" ]; then \
	  $(UI) error "$(STRATEGY) is not running in $(MODE) mode (make run STRATEGY=$(STRATEGY) MODE=$(MODE) starts it)"; \
	  exit 1; fi
	@out="$(RUNNER_ROOT)/last-report.json"; \
	  if ! $(TELEMETRY_READ) > "$$out" 2> $(STEP_LOG); then \
	    $(UI) error "could not read what the runner reported; its output is below"; \
	    cat $(STEP_LOG) >&2; exit 1; fi; \
	  uv run python tools/runner/report.py --strategy $(STRATEGY) --mode $(MODE) \
	    $(if $(JSON),--json) < "$$out"

run-logs:  ## Follow a running strategy's log
	$(require_strategy)
	@$(COMPOSE) logs -f custos-runner

run-status:  ## Show the local runner's containers
	$(require_strategy)
	@$(COMPOSE) ps

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
