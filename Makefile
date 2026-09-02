# Legal Helper — developer tasks. Most targets operate on the backend/ package.
# Quickstart:  make install && make check

BACKEND := backend
VENV := $(BACKEND)/.venv
PY := $(VENV)/bin/python
PYTHON ?= python3.13

.PHONY: venv install run test lint type check seed smoke addin-test clean

# Create the venv only when it is missing (the python binary is the sentinel).
$(PY):
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install --upgrade pip

venv: $(PY)  ## Create the virtualenv (idempotent)

install: $(PY)  ## Install pinned dependencies into the venv
	$(PY) -m pip install -r $(BACKEND)/requirements.txt

run: $(PY)  ## Run the API locally with autoreload on :8000
	cd $(BACKEND) && .venv/bin/uvicorn app.main:create_app --factory --reload --host 0.0.0.0 --port 8000

test: $(PY)  ## Run the backend test suite
	cd $(BACKEND) && .venv/bin/pytest

lint: $(PY)  ## Ruff lint + format check
	cd $(BACKEND) && .venv/bin/ruff check app tests && .venv/bin/ruff format --check app tests

type: $(PY)  ## Static type check
	cd $(BACKEND) && .venv/bin/mypy app

check: lint type test  ## Full gate: lint + type + test

seed: $(PY)  ## Seed demo history onto an existing account: make seed USER_ACCOUNT=jane.tan
	@test -n "$(USER_ACCOUNT)" || (echo 'usage: make seed USER_ACCOUNT=<username>  (register the account from the add-in first)'; exit 1)
	cd $(BACKEND) && .venv/bin/python -m app.seed_demo --for $(USER_ACCOUNT)

smoke: $(PY)  ## Hit a running deployment's public endpoints and report pass/fail
	cd $(BACKEND) && .venv/bin/python scripts/smoke.py $(ARGS)

addin-test:  ## Run the Word add-in's Node test suite
	cd word-addin && npm test

clean:  ## Remove caches (keeps the venv)
	rm -rf $(BACKEND)/.pytest_cache $(BACKEND)/.mypy_cache $(BACKEND)/.ruff_cache
	find $(BACKEND)/app $(BACKEND)/tests -name __pycache__ -type d -prune -exec rm -rf {} +
