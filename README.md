# NDA Assistant

NDA Assistant is a clean-slate rebuild of the NDA review + generation platform (replacing the
`nda-review-cloud` engine and the n8n workflow stack). It delivers NDA templates, generates and
reviews NDAs, drives DocuSign envelopes, archives signed documents, and extracts expiration dates —
reachable from Slack and email, with a Tally intake form and a Word add-in. It runs on Azure
Container Apps as two always-on roles built from one image: an `nda-api` FastAPI service and an
`nda-worker` background process.

This repository is a Python monorepo. The authoritative design is [`docs/PLAN.md`](docs/PLAN.md); the
backend was delivered in independently deployable phases (P0–P6), **all of which have shipped**:
the capability-registry foundation (every integration is `enabled` / `disabled` on missing config /
`unhealthy` on a runtime failure — missing config never crashes boot), the review engine with its
ZDR-pinned OpenRouter gateway, the Slack/email bot with an approval gate managed from the dashboard,
Tally-webhook NDA generation, DocuSign envelopes, Drive archive + expiration extraction, the admin
console (templates studio, token registry, access control), and the Word add-in. A dev environment
runs on Azure (see `docs/AZURE.md`); Alembic migrations `0001`–`0010` are current.

## Quickstart

Requires Python 3.13. From the repository root:

```bash
make install    # create backend/.venv and install pinned deps
make run        # serve the API on http://localhost:8000 (autoreload)
make test       # run the test suite
make check      # full gate: ruff lint + format check, mypy, pytest
```

Verify the app is up:

```bash
curl -s localhost:8000/healthz    # -> {"status":"ok"}  (+ an X-Correlation-Id response header)
```

The app boots with **zero** environment variables set. To configure it, copy
[`backend/.env.example`](backend/.env.example) to `backend/.env` and edit — each variable documents
the capability it unlocks.

Other targets: `make worker` (run the worker stub), `make lint`, `make type`, `make venv`, `make clean`.

## Repo layout

```
backend/
  app/
    config.py          # pydantic-settings Settings + "is this config group present?" helpers
    capabilities.py    # capability registry: enabled | disabled | unhealthy, report() + healthy()
    main.py            # create_app(): settings -> logging -> registry -> telemetry -> routes
    api/               # /v1 engine routes, admin UI/JSON routes, Tally webhook, add-in static
    engine/            # the review engine: findings, coverage, verify, cross-clause, redline
    ai/                # provider gateway: OpenRouter (ZDR-pinned) primary, direct Anthropic fallback
    bot/               # Slack/email bot: intents, approval gate (allowlist), delivery
    admin/             # dashboard templates (login, templates studio, tokens, forms, access)
    integrations/      # Tally webhook mapping, DocuSign, Google Drive, Airtable
    telemetry/         # structlog config, correlation-id middleware, Azure Monitor OTel (guarded)
    worker/            # `python -m app.worker` — review jobs, inbox sweep, archive watcher
    alembic/           # migrations 0001–0010 (create_all == alembic head is test-enforced)
  eval/                # offline/live eval corpus + release gates (docs/EVALUATION.md)
  tests/               # httpx/ASGI transport, no network
  Dockerfile           # python:3.13-slim, non-root; api default cmd, worker command documented
word-addin/            # Word taskpane add-in (review + exact-fidelity template tokenizer)
deploy/azure/          # Bicep: RG-scoped infra (ACA, ACR, KV, Postgres, App Insights)
Makefile               # venv / install / run / worker / test / lint / type / check / eval
docs/                  # PLAN.md (authoritative), plus the guides below
```

## Documentation

- [`docs/PLAN.md`](docs/PLAN.md) — the authoritative rebuild plan: architecture, decisions, security,
  and the P0–P6 phase breakdown.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — how the delivered system fits together.
- [`docs/AZURE.md`](docs/AZURE.md) — Azure resources, Bicep, environments, CI/CD wiring, and the scale path.
- [`docs/CREDENTIALS.md`](docs/CREDENTIALS.md) — per-service setup, scopes, and the Key Vault ↔ env var ↔
  capability mapping that `backend/.env.example` mirrors locally.
- [`docs/RUNBOOK.md`](docs/RUNBOOK.md) — operating the deployed system: capabilities, logs, recovery.
- [`docs/EVALUATION.md`](docs/EVALUATION.md) — the eval corpus + release gates for engine changes.
