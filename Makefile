# Every target that runs Python goes through uv, so the interpreter is the one
# pinned in .python-version rather than whatever happens to be on PATH.
PY := uv run --no-project python

.PHONY: help verify check-public-surface check-disclosure

help:  ## List targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  %-24s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

check-public-surface:  ## Refuse tracked private working notes
	$(PY) scripts/check-public-surface.py --self-test
	$(PY) scripts/check-public-surface.py

check-disclosure:  ## Refuse names of systems outside this repository
	$(PY) scripts/check-disclosure.py --self-test
	$(PY) scripts/check-disclosure.py

verify: check-public-surface check-disclosure  ## Full gate; the two disclosure checks run first
	@echo "verify passed"
