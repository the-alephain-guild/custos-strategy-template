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

# The checks `make verify` runs, in order. A private fork that keeps its own
# targets in local.mk can set VERIFY_CHECKS there to run its own list; the file
# is never part of the template, so an update never conflicts with it.
VERIFY_CHECKS ?= verify-pinned check-public-surface check-disclosure check-ownership check-dco check-publisher-pin lint test
-include local.mk

# The environment's own interpreter once there is one, and before that one with the
# standard library alone: help and next have to work before anything is installed.
VENV_DIR = $(if $(filter dev,$(TOOLCHAIN)),.venv-dev,.venv)
STDLIB_PY = $(if $(wildcard $(VENV_DIR)/bin/python),$(VENV_DIR)/bin/python,$(PY))

.PHONY: help next release arx-login arx-status arx-logout arx-evidence deploy-preview enroll-runner authorize-runner-transport runner-safety-policy verify verify-pinned check-public-surface check-disclosure check-ownership check-publisher-pin new-strategy add-venue setup setup-dev toolchain-banner lint test backtest check-dco

# Commands are listed under the `##@` heading above them, whichever file defines
# them, and the headings in HELP_SECTIONS order; any other heading follows.
HELP_SECTIONS = Getting started|Strategies|Running on a sandbox or testnet|Publishing|Deploying through ARX|Checks, as CI runs them
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

#> usage: make next [STRATEGY=<category>/<name>] [VENUE=<id>] [MODE=sandbox|testnet] [TOOLCHAIN=dev]
#> var: STRATEGY | the only one | which strategy to look at; with several and none named, they are listed
#> var: VENUE | unset | look at the strategy's run on this venue profile (venues/<id>.yaml)
#> var: MODE | sandbox | sandbox fills orders on this machine; testnet trades on the exchange's test environment
#> var: TOOLCHAIN | pinned | dev runs on the Custos build named in toolchain.local.toml (make setup-dev)
#> note: only reads: it changes nothing and starts nothing
#> example: make next STRATEGY=trend/supertrend MODE=testnet
next:  ## Say where this repository stands and what to run next
	@$(STDLIB_PY) tools/next.py --repo-name $(REPO_NAME) --mode $(MODE) --toolchain $(TOOLCHAIN) \
	    $(if $(STRATEGY),--strategy $(STRATEGY)) $(if $(VENUE),--venue $(VENUE))

##@ Strategies

#> usage: make new-strategy NAME=<name> [CATEGORY=<category>] [DOC_LANG=en|zh]
#> var: NAME | required | the strategy's directory and registry name
#> var: CATEGORY | trend | the directory it goes under in strategies/
#> var: DOC_LANG | asked | the language of its documents, en or zh; unset, copier asks and defaults to en
#> var: COPIER_FLAGS | none | extra copier options, such as --defaults to skip the questions
#> example: make new-strategy NAME=my_idea CATEGORY=trend
#> then: make backtest STRATEGY=trend/my_idea START=2025-01-01 END=2025-04-01|backtest it
new-strategy:  ## Create a strategy: make new-strategy NAME=my_idea [CATEGORY=trend]
	@test -n "$(NAME)" || { $(UI) error "usage: make new-strategy NAME=my_idea [CATEGORY=trend]" --tag new-strategy; exit 2; }
	@test ! -e "strategies/$(or $(CATEGORY),trend)/$(NAME)" || { $(UI) error "strategies/$(or $(CATEGORY),trend)/$(NAME) already exists" --tag new-strategy; exit 1; }
	uvx copier copy --vcs-ref=HEAD $(COPIER_FLAGS) --data name=$(NAME) --data category=$(or $(CATEGORY),trend) $(if $(DOC_LANG),--data language=$(DOC_LANG)) . strategies/$(or $(CATEGORY),trend)/$(NAME)
	@uv run python scripts/register-strategy.py $(or $(CATEGORY),trend) $(NAME)
	@$(UI) next \
	  "edit strategies/$(or $(CATEGORY),trend)/$(NAME)/config.yaml|its pairs and parameters" \
	  "make backtest STRATEGY=$(or $(CATEGORY),trend)/$(NAME) START=2025-01-01 END=2025-04-01$(TOOLCHAIN_SUFFIX)|backtest it"

#> usage: make add-venue STRATEGY=<category>/<name> CONNECTOR=<connector> PAIR=<pair>[,<pair>] [VENUE=<id>] [CREDENTIAL_ID=<name>]
#> var: STRATEGY | required | the strategy directory under strategies/, such as trend/my_idea
#> var: CONNECTOR | required | the exchange and market, as make new-strategy offers them: binance, binance_perpetual, okx, okx_perpetual, sodex, sodex_perpetual
#> var: PAIR | required | the pair as that exchange lists it; several with commas
#> var: VENUE | the connector | the profile's id, lowercase letters, digits and underscores; the file is venues/<id>.yaml
#> var: CREDENTIAL_ID | derived | the vault name its keys are sealed under; <exchange>-<name>-<id> unless given
#> note: the pair is checked as make new-strategy checks it, and every bar the strategy uses must be one the exchange serves
#> note: writes venues/<id>.yaml and the profile's block in run.yaml; the parameters stay in config.yaml, the same on every exchange (docs/exchanges.md)
#> example: make add-venue STRATEGY=trend/my_idea CONNECTOR=sodex PAIR=vBTC_vUSDC
#> then: make backtest STRATEGY=trend/my_idea VENUE=sodex START=2025-01-01 END=2025-04-01|backtest it on that exchange
#> then: make start STRATEGY=trend/my_idea VENUE=sodex MODE=sandbox|run it there
add-venue:  ## Add a venue profile to a strategy: make add-venue STRATEGY=trend/my_idea CONNECTOR=sodex PAIR=vBTC_vUSDC
	@test -n "$(STRATEGY)" -a -n "$(CONNECTOR)" -a -n "$(PAIR)" || { $(UI) error "usage: make add-venue STRATEGY=trend/my_idea CONNECTOR=sodex PAIR=vBTC_vUSDC" --tag add-venue; exit 2; }
	@uv run python scripts/add-venue.py $(STRATEGY) --connector $(CONNECTOR) --pair "$(PAIR)" $(if $(VENUE),--venue $(VENUE)) $(if $(CREDENTIAL_ID),--credential-id $(CREDENTIAL_ID))

#> usage: make backtest STRATEGY=<category>/<name> START=<date> END=<date> [VENUE=<id>] [BALANCE=<amount>] [JSON=1] [TOOLCHAIN=dev]
#> var: STRATEGY | required | the strategy directory under strategies/, or examples/trend/sma_cross
#> var: VENUE | unset | a venue profile of the strategy (venues/<id>.yaml): backtest it on that exchange's data instead of config.yaml's
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
	@uv run python tools/backtest/run.py $(STRATEGY) --start $(START) --end $(END) $(if $(VENUE),--venue $(VENUE)) $(if $(BALANCE),--balance $(BALANCE)) $(if $(JSON),--json)
	@$(if $(JSON),true,if [ -f .runner/.arx/runner.toml ]; then \
	  $(UI) next "make start STRATEGY=$(STRATEGY)$(if $(VENUE), VENUE=$(VENUE)) MODE=sandbox$(TOOLCHAIN_SUFFIX)|run it on the exchange's live market data, filling orders on this machine"; \
	else \
	  $(UI) next "make setup-runner$(TOOLCHAIN_SUFFIX)|create this machine's runner identity, once" \
	    "make start STRATEGY=$(STRATEGY)$(if $(VENUE), VENUE=$(VENUE)) MODE=sandbox$(TOOLCHAIN_SUFFIX)|then run it on the exchange's live market data"; \
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

##@ Publishing

#> usage: make release STRATEGY=<category>/<name>
#> var: STRATEGY | required | the strategy directory under strategies/, such as trend/my_idea
#> note: builds, signs and publishes the strategy's version from its pyproject.toml, in GitHub Actions, from the commit you have pushed; it pushes nothing itself
#> note: needs gh logged in with the workflow scope; the release's signature is recorded in Sigstore's public log, the release stays in your private package (docs/releasing.md)
#> example: make release STRATEGY=trend/my_idea
#> then: cat .releases/<category>/<name>/<version>/…|the receipt a deployment refers to
release:  ## Publish a signed release of a strategy through GitHub Actions
	@test -n "$(STRATEGY)" || { $(UI) error "name the strategy: make release STRATEGY=trend/my_idea" --tag release; exit 2; }
	@$(STDLIB_PY) tools/release.py $(STRATEGY)

##@ Deploying through ARX

#> usage: make arx-login ARX_URL=<address> [ARX_EMAIL=<email>] [ARX_TENANT=<organisation id>]
#> var: ARX_URL | required | the ARX API address, https (plain http only to localhost, 127.0.0.1 or ::1)
#> var: ARX_EMAIL | asked | the email you sign in to ARX with
#> var: ARX_TENANT | your default | the organisation to act in, when you are a member of several
#> note: asks for your password and an authenticator code without showing them, and keeps neither
#> note: keeps the session in ~/.config/custos-strategy/arx/hosts.json (or under XDG_CONFIG_HOME), readable by you only; it ends once seven days pass without a command using it (docs/deploying.md)
#> example: make arx-login ARX_URL=https://arx.example.com
#> then: make arx-status ARX_URL=https://arx.example.com|check the session
arx-login:  ## Sign in to ARX and keep the session on this machine
	@test -n "$(ARX_URL)" || { $(UI) error "name the ARX: make arx-login ARX_URL=https://arx.example.com" --tag arx; exit 2; }
	@$(STDLIB_PY) tools/arx/session.py login --url "$(ARX_URL)" $(if $(ARX_EMAIL),--email "$(ARX_EMAIL)") $(if $(ARX_TENANT),--tenant "$(ARX_TENANT)")

#> usage: make arx-status [ARX_URL=<address>]
#> var: ARX_URL | the only one | which ARX session to show, when this machine has several
#> note: refreshes the session if its access token has expired, and asks ARX whether it still holds
#> then: make arx-login ARX_URL=https://arx.example.com|sign in again if it has ended
arx-status:  ## Show the ARX session kept on this machine
	@$(STDLIB_PY) tools/arx/session.py status $(if $(ARX_URL),--url "$(ARX_URL)")

#> usage: make arx-logout [ARX_URL=<address>]
#> var: ARX_URL | the only one | which ARX session to end, when this machine has several
#> note: ends the session on ARX as well as here; neither its access token nor its refresh cookie works again
#> then: make arx-login ARX_URL=https://arx.example.com|sign in again
arx-logout:  ## End the ARX session and remove it from this machine
	@$(STDLIB_PY) tools/arx/session.py logout $(if $(ARX_URL),--url "$(ARX_URL)")

#> usage: make arx-evidence STRATEGY=<category>/<name> [VERSION=<version>]
#> var: STRATEGY | required | the strategy directory under strategies/, such as trend/my_idea
#> var: VERSION | its pyproject.toml | the released version, whose receipt make release kept in .releases/
#> note: reads the release back from its package by digest and checks every manifest and layer against the receipt; it sends nothing to ARX
#> note: needs gh logged in with the read:packages scope (gh auth refresh -s read:packages); the token is used for pulling from this one package only
#> example: make arx-evidence STRATEGY=trend/my_idea
#> then: make arx-status|the ARX session a deployment will use
arx-evidence:  ## Read a release back from its package and check it for ARX
	@test -n "$(STRATEGY)" || { $(UI) error "name the strategy: make arx-evidence STRATEGY=trend/my_idea" --tag arx; exit 2; }
	@$(STDLIB_PY) tools/arx/evidence.py $(STRATEGY) $(if $(VERSION),--version $(VERSION))

#> usage: make deploy-preview STRATEGY=<category>/<name> [MODE=sandbox|testnet] RUNNER=<runner id> PRODUCT=<product id> [VERSION=<version>] [RELEASE=<release id>]
#> var: STRATEGY | required | the strategy directory under strategies/, with a deploy.yaml next to its config.yaml
#> var: MODE | sandbox | sandbox or testnet; a live deployment comes only from an approved promotion
#> var: RUNNER | required | the id of the runner the first instance starts on
#> var: PRODUCT | required | the id of the product the deployment trades for
#> var: VERSION | its pyproject.toml | the released version, whose receipt make release kept in .releases/
#> var: RELEASE | not chosen | the release's id in ARX, once it has been drafted there
#> note: builds the DeploymentSpec from the release's trading scope, config.yaml and deploy.yaml, and shows what it says with its policy digests and the request digest; it sends nothing to ARX
#> note: reads the release back from its package first, as make arx-evidence does, so it needs gh with the read:packages scope
#> example: make deploy-preview STRATEGY=trend/my_idea MODE=sandbox RUNNER=5e3c1b7a-9d2f-4a6e-b180-3c5d7e9f1a2b PRODUCT=4d1f6a0e-2b7c-4c1e-9a53-0e8f2d6b7c10
#> then: make arx-status|the ARX session a deployment will use
deploy-preview:  ## Show the DeploymentSpec a release would be deployed with, sending nothing
	@test -n "$(STRATEGY)" -a -n "$(RUNNER)" -a -n "$(PRODUCT)" || { $(UI) error "usage: make deploy-preview STRATEGY=trend/my_idea MODE=sandbox RUNNER=<runner id> PRODUCT=<product id>" --tag arx; exit 2; }
	@uv run python tools/arx/spec.py $(STRATEGY) --mode $(MODE) --runner "$(RUNNER)" --product "$(PRODUCT)" $(if $(VERSION),--version $(VERSION)) $(if $(RELEASE),--release "$(RELEASE)")

#> usage: make enroll-runner RUNNER=<runner id> NAME="<display name>" [SCOPE=<1-63>] [PAPER_ONLY=0] [ARX_URL=<address>]
#> var: RUNNER | required | the runner's id
#> var: NAME | required | the display name people see in the ARX console, 1 to 120 characters
#> var: SCOPE | none | from 1 to 63, recorded with the runner's credential; needed only when a token is issued
#> var: PAPER_ONLY | 1 | 0 issues a token whose runner may obtain live material; 1 never can
#> var: ARX_URL | the only one | which ARX session to use, when this machine has several
#> note: an ADMIN's command; lists the runners first and does the one step due: a runner not enrolled gets an enrollment token, an enrolled one without a name is named NAME
#> note: the token is written to .runner-enroll/<runner id>.token, readable by you only and ignored by git, and shown nowhere else; each write asks for a fresh authenticator code
#> example: make enroll-runner RUNNER=5e3c1b7a-9d2f-4a6e-b180-3c5d7e9f1a2b NAME="Trading box 1" SCOPE=3
#> then: make enroll-runner RUNNER=<runner id> NAME="Trading box 1"|once the runner has enrolled with the token, name it
enroll-runner:  ## Issue a runner's enrollment token, or name it once enrolled
	@test -n "$(RUNNER)" -a -n "$(NAME)" || { $(UI) error 'usage: make enroll-runner RUNNER=<runner id> NAME="Trading box 1" [SCOPE=3]' --tag arx; exit 2; }
	@$(STDLIB_PY) tools/arx/runner_admin.py $(if $(ARX_URL),--url "$(ARX_URL)") enroll --runner "$(RUNNER)" --name "$(NAME)" $(if $(SCOPE),--scope "$(SCOPE)") $(if $(filter 0,$(PAPER_ONLY)),--live)

#> usage: make authorize-runner-transport RUNNER=<runner id> [MODE=sandbox|testnet|live] [OPERATION=issue|rotate|revoke] [GENERATION=<n>] [ARX_URL=<address>]
#> var: RUNNER | required | the enrolled runner's id
#> var: MODE | sandbox | the mode the credential is for; each mode has its own
#> var: OPERATION | issue | issue a credential, or rotate or revoke the active one
#> var: GENERATION | none | the generation active on the runner, for rotate and revoke
#> var: ARX_URL | the only one | which ARX session to use, when this machine has several
#> note: an ADMIN's command, with a fresh authenticator code; prints the authorization intent id the runner's transport command takes, and when it expires
#> example: make authorize-runner-transport RUNNER=5e3c1b7a-9d2f-4a6e-b180-3c5d7e9f1a2b MODE=sandbox
#> then: make runner-safety-policy ACTION=submit RUNNER=<runner id> MODE=sandbox FILE=<request.json>|cap what the runner may hold
authorize-runner-transport:  ## Authorise a runner's message-transport credential for one mode
	@test -n "$(RUNNER)" || { $(UI) error "usage: make authorize-runner-transport RUNNER=<runner id> MODE=sandbox" --tag arx; exit 2; }
	@$(STDLIB_PY) tools/arx/runner_admin.py $(if $(ARX_URL),--url "$(ARX_URL)") authorize-transport --runner "$(RUNNER)" --mode $(MODE) --operation $(or $(OPERATION),issue) $(if $(GENERATION),--generation "$(GENERATION)")

#> usage: make runner-safety-policy ACTION=submit|approve|activate RUNNER=<runner id> [MODE=sandbox|testnet|live] [REQUEST=<request id>] [FILE=<request.json>] [REASON="…"] [ARX_URL=<address>]
#> var: ACTION | required | submit asks for a policy, approve and activate take a request further
#> var: RUNNER | required | the runner's id
#> var: MODE | sandbox | the mode the policy caps
#> var: REQUEST | none | the request id, for approve and activate
#> var: FILE | none | submit: a JSON object with settlement_currency, max_order_notional, max_total_notional, effective_at, expires_at, capability and reason
#> var: CURRENCY, MAX_ORDER, MAX_TOTAL, EFFECTIVE_AT, EXPIRES_AT, REVISION | from FILE | submit: give or override one field; the notional values are decimal strings
#> var: REASON | none | submit: the request's reason; approve: required, the approval's reason
#> var: ARX_URL | the only one | which ARX session to use, when this machine has several
#> note: a two-person control: an ADMIN or OPERATOR submits, a FINANCE holder who is someone else approves and activates; approving your own request is refused before a code is asked for
#> example: make runner-safety-policy ACTION=submit RUNNER=5e3c1b7a-9d2f-4a6e-b180-3c5d7e9f1a2b MODE=sandbox FILE=policy.json
#> then: make runner-safety-policy ACTION=approve RUNNER=<runner id> MODE=sandbox REQUEST=<request id> REASON="…"|run by the second person
runner-safety-policy:  ## Ask for, approve or activate a runner safety policy
	@test -n "$(ACTION)" -a -n "$(RUNNER)" || { $(UI) error "usage: make runner-safety-policy ACTION=submit|approve|activate RUNNER=<runner id> MODE=sandbox ..." --tag arx; exit 2; }
	@$(STDLIB_PY) tools/arx/runner_admin.py $(if $(ARX_URL),--url "$(ARX_URL)") safety-policy $(ACTION) --runner "$(RUNNER)" --mode $(MODE) $(if $(REQUEST),--request "$(REQUEST)") $(if $(FILE),--file "$(FILE)") $(if $(CURRENCY),--currency "$(CURRENCY)") $(if $(MAX_ORDER),--max-order "$(MAX_ORDER)") $(if $(MAX_TOTAL),--max-total "$(MAX_TOTAL)") $(if $(EFFECTIVE_AT),--effective-at "$(EFFECTIVE_AT)") $(if $(EXPIRES_AT),--expires-at "$(EXPIRES_AT)") $(if $(REASON),--reason "$(REASON)") $(if $(REVISION),--revision "$(REVISION)")

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
#> note: run make setup once first; a fork sets VERIFY_CHECKS in local.mk to run its own list
verify: $(VERIFY_CHECKS)  ## Full gate (run make setup once first); disclosure checks run first
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

#> usage: make check-publisher-pin
#> note: refuses a release workflow that calls another publisher version than toolchain.lock.toml pins
check-publisher-pin:  ## Refuse a release workflow that disagrees with the pinned publisher
	$(PY) scripts/check-publisher-pin.py --self-test
	$(PY) scripts/check-publisher-pin.py

#> usage: make check-dco
#> note: proves the sign-off check refuses what it should; CI passes it a pull request's range
check-dco:  ## Prove the sign-off check bites (CI passes a pull request's range)
	$(PY) scripts/check-dco.py --self-test
