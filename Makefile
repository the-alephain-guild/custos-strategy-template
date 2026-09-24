# Every target that runs Python goes through uv, so the interpreter is the one
# pinned in .python-version rather than whatever happens to be on PATH.
PY := uv run --no-project python

.PHONY: help verify check-public-surface check-disclosure check-ownership new-strategy

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

new-strategy:  ## Create a strategy: make new-strategy NAME=my_idea [CATEGORY=trend]
	@test -n "$(NAME)" || { echo "usage: make new-strategy NAME=my_idea [CATEGORY=trend]"; exit 2; }
	@test ! -e "strategies/$(or $(CATEGORY),trend)/$(NAME)" || { echo "strategies/$(or $(CATEGORY),trend)/$(NAME) already exists"; exit 1; }
	uvx copier copy $(COPIER_FLAGS) --data name=$(NAME) --data category=$(or $(CATEGORY),trend) . strategies/$(or $(CATEGORY),trend)/$(NAME)
	$(PY) scripts/register-strategy.py $(or $(CATEGORY),trend) $(NAME)

verify: check-public-surface check-disclosure check-ownership  ## Full gate; the two disclosure checks run first
	@echo "verify passed"
