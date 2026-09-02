# NDA Assistant — developer tasks. All targets operate on the backend/ package.
# Quickstart:  make install && make check

BACKEND := backend
VENV := $(BACKEND)/.venv
PY := $(VENV)/bin/python
PYTHON ?= python3.13

.PHONY: venv install run worker test lint type check verify eval eval-live clean

# Create the venv only when it is missing (the python binary is the sentinel).
$(PY):
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install --upgrade pip

venv: $(PY)  ## Create the virtualenv (idempotent)

install: $(PY)  ## Install pinned dependencies into the venv
	$(PY) -m pip install -r $(BACKEND)/requirements.txt

run: $(PY)  ## Run the API locally with autoreload on :8000
	cd $(BACKEND) && .venv/bin/uvicorn app.main:create_app --factory --reload --host 0.0.0.0 --port 8000

worker: $(PY)  ## Run the worker stub
	cd $(BACKEND) && .venv/bin/python -m app.worker

test: $(PY)  ## Run the test suite
	cd $(BACKEND) && .venv/bin/pytest

lint: $(PY)  ## Ruff lint + format check
	cd $(BACKEND) && .venv/bin/ruff check app tests && .venv/bin/ruff format --check app tests

type: $(PY)  ## Static type check
	cd $(BACKEND) && .venv/bin/mypy app

check: lint type test  ## Full gate: lint + type + test

verify: $(PY)  ## Drive the REAL generation pipeline end-to-end (no mocks/network/LLM) and inspect the output
	cd $(BACKEND) && .venv/bin/python -m scripts.verify_generation_e2e

eval: $(PY)  ## Offline eval: validate the corpus manifest + gold files + docs (no network)
	cd $(BACKEND) && .venv/bin/python -m eval.run_eval

eval-live: $(PY)  ## Live eval: run the corpus through the engine and enforce the release gates (needs a provider key)
	cd $(BACKEND) && .venv/bin/python -m eval.run_eval --live

clean:  ## Remove caches (keeps the venv)
	rm -rf $(BACKEND)/.pytest_cache $(BACKEND)/.mypy_cache $(BACKEND)/.ruff_cache
	find $(BACKEND)/app $(BACKEND)/tests -name __pycache__ -type d -prune -exec rm -rf {} +
