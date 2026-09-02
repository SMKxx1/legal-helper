# Legal Helper

A Microsoft Word task-pane add-in plus a small FastAPI service that reviews a `.docx` against a legal playbook using a team of LLM agents over OpenRouter (Zero-Data-Retention routes only), and returns findings the add-in applies as tracked changes + comments.

Built as a teaching demo for the "Deployment 2" workshop: the codebase demonstrates **core deployment patterns** (persistent service, managed database, object storage, authentication, secrets, cost metering, schema migrations, CI/CD) in a single, explainable system.

---

## Quick start (local)

```bash
# Install backend deps and start the service
make install
make run

# In another terminal, verify the service
curl -s localhost:8000/healthz       # -> {"status":"ok"}
curl -s localhost:8000/              # -> landing page with capabilities

# Optional: start the add-in dev server (requires Node 18+)
cd word-addin
npm install
npm run dev-server               # serves taskpane.html on https://localhost:3000
# In Word: Insert Add-in → Upload Manifest → word-addin/manifest.dev.xml
```

From the add-in, sign in as:
- **Username:** `alice.tan`
- **Password:** (empty for dev; generated on first `make run`)

See [`word-addin/README.md`](word-addin/README.md) for sideloading on Mac, Windows, and web.

---

## Architecture

```
┌────────────────────────────┐        HTTPS (public)          ┌──────────────────────────────────────┐
│  Word (desktop / Mac / web)│ ─────────────────────────────▶ │ Railway service: legal-helper        │
│  task pane: taskpane.html  │  Authorization: Bearer <token> │ FastAPI + uvicorn, one container     │
│  Office.js reads/edits doc │ ◀───────────────────────────── │ /addin/*  /manifest.xml  /api/*  /   │
└────────────────────────────┘                                │                                      │
                                                              │  agents/  ─── OpenRouter (ZDR only)  │
                                                              │  (user's own key, decrypted per call)│
                                                              └───────┬───────────────────┬──────────┘
                                                        private net   │                   │  S3 API (public endpoint,
                                                 ${{Postgres.DATABASE_URL}}               │  bucket credentials)
                                                                      ▼                   ▼
                                                        ┌──────────────────┐   ┌──────────────────────┐
                                                        │ Railway Postgres │   │ Railway bucket       │
                                                        │ users, sessions, │   │ "documents"          │
                                                        │ reviews, llm_calls│  │ users/<id>/reviews/… │
                                                        └──────────────────┘   └──────────────────────┘
```

**Trust boundaries:**

1. **Word ↔ app** (public): bearer token per user; every `/api/*` route except `login` and `status` answers 401 without it.
2. **App ↔ Postgres** (private network): `DATABASE_URL` reference variable, never a public URL.
3. **App ↔ bucket** (S3 API over the public endpoint, credentials from bucket variable refs): objects are private; the browser only ever sees a short-lived presigned URL.
4. **App ↔ OpenRouter** (public): the **user's** key, stored Fernet-encrypted, decrypted in-memory for the duration of one review; request carries the ZDR policy; response `usage.cost` is written to `llm_calls`.

---

## Deployment

See [`docs/DEPLOY_RAILWAY.md`](docs/DEPLOY_RAILWAY.md) for the complete Railway click-path (15–20 minutes).

TL;DR: fork this repo, create a Railway project with Postgres + bucket, connect GitHub, set environment variables, generate a domain, and sideload `/manifest.xml` in Word.

---

## Requirements (testable proof)

| Quality | Requirement | Proof |
|---|---|---|
| Reliability | `/healthz` returns 200 on 10/10 checks after deploy; data survives a redeploy | Railway healthcheck; smoke test; redeploy demo |
| Security | Every `/api/*` route except login and status returns 401 without a valid bearer token | Integration tests; smoke test |
| Secrets | No key in git; user keys encrypted at rest; API never returns more than the last 4 characters | `test_me.py`; grep in CI |
| Privacy | Every OpenRouter request carries `provider.zdr=true` and `allow_fallbacks=false`; no route → error | `test_zdr_fail_closed.py` |
| Performance | p95 of `GET /api/me/usage` < 500 ms with 10,000 `llm_calls` rows | Smoke test; indexes on `user_id, created_at` |
| Cost | A user's reviews are refused once monthly spend exceeds `MAX_MONTHLY_COST_USD` (default $5); at most `MAX_DOCS_PER_USER` objects per user | `test_budget.py`; `test_retention.py` |
| Recovery | Reviews left `running` by a crash are marked failed within 15 minutes of restart | `test_stale_jobs.py` |
| Limits | Uploads over 10 MB or documents over 120,000 characters are rejected with 413 | `test_reviews_limits.py` |

---

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — answers the four architecture questions; request flow for quick vs deep; agent pipeline; what a worker service would change
- [`docs/API.md`](docs/API.md) — every endpoint: method, path, auth, request, success + failure examples
- [`docs/DEPLOY_RAILWAY.md`](docs/DEPLOY_RAILWAY.md) — deployment click-path with screenshots and troubleshooting
- [`docs/WORKSHOP_SLIDES.md`](docs/WORKSHOP_SLIDES.md) — slide mapping and concepts the code demonstrates
- [`word-addin/README.md`](word-addin/README.md) — dev server, sideloading, how the clause locator and tracked changes work, troubleshooting

---

## Repo structure

**Backend:**
- `backend/app/main.py` — FastAPI app factory, health check, 404 default-deny
- `backend/app/config.py` — settings from env (pydantic-settings)
- `backend/app/capabilities.py` — database, bucket, and ZDR model list health
- `backend/app/db.py`, `db_migrate.py` — SQLAlchemy, Alembic migration helpers
- `backend/app/auth/` — argon2 password hashing, bearer sessions, `get_current_user`
- `backend/app/crypto.py` — Fernet encryption/decryption for user OpenRouter keys
- `backend/app/api/` — routers: auth, user profile, reviews, usage stats, landing page, add-in manifest
- `backend/app/agents/` — LLM agent orchestration: classifier, reviewer, coverage checker, span verifier, deterministic merge
- `backend/app/ai/` — OpenRouter adapter (ZDR-pinned), gateway, usage ledger, model list cache
- `backend/app/ingestion/docx.py` — `.docx` parsing
- `backend/app/storage/bucket.py` — S3-compatible object store (presigned URLs, retention)
- `backend/app/seed_demo.py` — idempotent seeding of synthetic users + history
- `backend/app/telemetry/` — structured logging, correlation IDs
- `backend/app/models.py` — SQLAlchemy table definitions
- `backend/alembic/` — database migration scripts
- `backend/tests/` — pytest suite over an in-process ASGI client

**Add-in:**
- `word-addin/taskpane.html` — task pane UI (sign in, key settings, review controls, history, usage)
- `word-addin/taskpane.js` — Office.js integration, clause locator, tracked changes, polling
- `word-addin/taskpane.css` — styles
- `word-addin/manifest.dev.xml` — dev manifest (hardcoded localhost URLs)
- `word-addin/dev-server.mjs` — local dev server (HTTPS, CORS, `/api` proxy to backend)
- `word-addin/tests/` — unit tests (redline diffing, async transport helpers, no Office.js)

**Data & config:**
- `playbook/legal_helper_playbook.json` — legal playbook positions (presence, context)
- `samples/` — synthetic `.docx` files for testing and demos
- `Dockerfile` — Python 3.13, single-stage production image
- `.railway/railway.ts` — Railway IaC (reference; manual dashboard steps are primary)
- `.github/workflows/ci.yml` — CI: `make check` + `npm test` on every push
- `backend/.env.example` — environment variable template

---

## Development

**Backend:**
```bash
cd backend
source .venv/bin/activate          # or just `make install`
make check                         # ruff + mypy + pytest
python -m pytest -k test_name -v   # run specific test
```

**Add-in:**
```bash
cd word-addin
npm install
npm test                           # node --test tests/**/*.test.js
npm run dev-server                 # HTTPS dev server on localhost:3000
```

**Playbook:**
Edit `playbook/legal_helper_playbook.json` and restart the backend.

**Database migrations:**
```bash
cd backend
alembic upgrade head               # apply pending migrations
alembic downgrade -1               # roll back one migration
```

---

## Testing

**Unit + integration (backend):**
```bash
cd backend
make check                         # runs pytest over an in-process ASGI client
```

**Smoke test (deployed):**
```bash
python backend/scripts/smoke.py https://<your-domain> alice.tan <password>
```

**Seed demo data:**
```bash
cd backend
python -m app.seed_demo            # idempotent; safe to run multiple times
make seed                          # alias
```

---

## Deployment checklist

Before deploying, ensure:

- [ ] `.env` and `.env.example` are correct (`.env` is in `.gitignore`)
- [ ] `make check` passes on `main`
- [ ] `Dockerfile` builds: `docker build -t legal-helper .`
- [ ] A Railway account is ready and linked to your GitHub fork
- [ ] You have an OpenRouter API key for live reviews

See [`docs/DEPLOY_RAILWAY.md`](docs/DEPLOY_RAILWAY.md) for the step-by-step.

---

## Concepts taught

This codebase is a working example of:

- **Authentication:** password hashing (argon2id), bearer tokens, session expiry, `get_current_user` dependency injection
- **Secrets at rest:** Fernet encryption, `APP_SECRET_KEY`, last-4 display
- **API design:** one error envelope, default-deny 404, request/response schemas, status codes
- **Schema migrations:** Alembic baseline, migrate-then-serve in the start command
- **Async work:** in-process `asyncio` task, semaphore, polling from the client
- **Metering:** every LLM call is a row; OpenRouter's `usage.cost`; per-user monthly budget
- **Agent orchestration:** pattern classifier → specialists in parallel → deterministic merge
- **Data privacy:** Zero Data Retention (ZDR) per-request policy, model allowlist, fail-closed
- **Capability registry:** enabled/disabled/unhealthy; graceful degradation (bucket is optional)
- **Object storage:** presigned URLs, retention caps, per-environment buckets
- **Observability:** structured JSON logs, correlation IDs, `/api/status`
- **CI/CD:** GitHub Actions, auto-deploy to Railway
- **Infrastructure as code:** `.railway.ts` mirrors the dashboard canvas

---

## Spec

The complete plan — architecture, decisions, data model, and the phase-by-phase build — lives in [`LEGAL_HELPER_PLAN.md`](LEGAL_HELPER_PLAN.md). Start there for rationale and dependencies between phases.

---

## License

Teaching demo, no license specified. Use freely for educational purposes.


