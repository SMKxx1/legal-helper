# Workshop Slides Mapping

This document maps the "Deployment 2" workshop deck (41 slides) to the Legal Helper codebase. It replaces Section 5 of the deck (slides 33–41, the Word Agent workshop) with a Legal Helper walkthrough and describes small text edits to existing slides.

---

## 8.1 Concepts → Code locations

| Slide | Concept | Where it's demonstrated |
|---|---|---|
| 19 | Start with the user's job | The task pane lives in Word, in context with the document. Code: `word-addin/taskpane.html`, `taskpane.js` |
| 20–22 | Functional vs non-functional requirements; testable targets | `README.md` "Requirements" table (links to test names). Code: `backend/tests/` |
| 23–24 | Interface where the work happens: add-in = manifest + hosted web app + Office.js | `/manifest.xml` (dynamically generated), `taskpane.js` clause locator + tracked changes apply, `Office.js` from CDN |
| 25 | **The four questions:** Demand / runtime / data / integration | `docs/ARCHITECTURE.md` answers each explicitly. See below. |
| 26 | Serverless vs persistent service | One persistent uvicorn process on Railway. Deep reviews as in-process `asyncio` task. Extension: add a worker service. Code: `backend/app/main.py`, `routes_reviews.py` |
| 27 | Relational vs object storage | **Rows** in Postgres (`users`, `reviews`, `llm_calls`); **whole documents** in bucket (`users/<id>/reviews/...`). Presigned URLs for download. Code: `backend/app/storage/bucket.py`, `models.py` |
| 28 | Sync vs async APIs | Quick = `200 OK` synchronous (~30s); Deep = `202 Accepted` + `GET /api/reviews/{id}` polling (~2–3 min). Code: `routes_reviews.py`, `taskpane.js` polling logic |
| 30–32 | Platform choice: Railway as the middle ground | Three services: `legal-helper` (app), `Postgres`, `documents` (bucket). Code: `.railway/railway.ts`, `Dockerfile`, `.github/workflows/ci.yml` |
| 34 | Project / service / deployment | 1 project + 3 services (app, Postgres, bucket). Code: `.railway/railway.ts` |
| 35 | Railway runs the loop; you own the outcome | Deploy logs show migrate → seed → serve; Railway injects `PORT`, `DATABASE_URL`, `S3_*`. Code: `Dockerfile` start command, `db_migrate.py`, `seed_demo.py` |
| 36 | Trust boundaries | Four boundaries: Word ↔ app (bearer token), app ↔ Postgres (private network), app ↔ bucket (presigned URL), app ↔ OpenRouter (encrypted key). Code: `docs/ARCHITECTURE.md`, `auth/`, `crypto.py`, `storage/bucket.py`, `ai/openrouter.py` |
| 37 | The brief (project mandate) | Deploy a private repo → Python app + Postgres + bucket; protect with bearer tokens, encrypted keys, ZDR-only; prove: public URL, login, review, 401. Code: `README.md`, all of the above |
| 38 | Four (now five) responsibilities: Client · Compute · Data · Boundary · **Model provider** | **Client** (`word-addin/`), **Compute** (`backend/app/api/`, `agents/`), **Data** (Postgres + bucket), **Boundary** (auth + crypto + ZDR), **Model provider** (OpenRouter, user's key). Code: all modules |
| 39–41 | Prepare repo, deploy, connect, verify | See "Deployment walkthrough" below. Code: `docs/DEPLOY_RAILWAY.md`, `backend/scripts/smoke.py` |

---

## Four Architecture Questions (Slide 25)

### 1. Demand: What is the user's job?

The user's job: **review a legal document quickly and carefully, directly in Word.**

The task pane lives in Word. The user uploads a `.docx`, clicks "Review this document", and findings come back as tracked changes + comments. The user edits in Word's native UI, no copying to another app.

### 2. Runtime: How does the service execute?

**One persistent service** (no worker queue):
- Synchronous quick review: ~30–40 seconds, returns `200 OK`
- Asynchronous deep review: submit `202 Accepted`, poll until done (1–3 minutes)
- Crash recovery: stale `running` reviews are marked `failed` at startup

### 3. Data: What does the system remember?

**Postgres** (relational): users, sessions, reviews, llm_calls, billing metadata
**Bucket** (object store): original `.docx` files per review (optional; failure doesn't block review)

### 4. Integration: Where do external systems live?

**OpenRouter** only: user's own API key, encrypted at rest, decrypted per call, requests carry ZDR policy.

---

## 8.2 New slides to add (concepts the code teaches)

Suggested placement: after slide 28 (architecture) and inside the rewritten Section 5 (deployment).

### 1. **Authentication basics**
- Password hashing (argon2id): why not SHA-256?
- Sessions as opaque bearer tokens (stored hashed, 12-hour TTL)
- "Deny by default": `get_current_user` dependency injection in FastAPI
- Constant-time password comparison to prevent timing attacks
- Code: `backend/app/auth/`

### 2. **Secrets at rest and in transit**
- User's OpenRouter key: Fernet encryption with `APP_SECRET_KEY`
- Last-4 display (never full key in logs or responses)
- `.env.example` vs sealed Railway variables
- Why bearer tokens are better than API keys in cookies
- Code: `backend/app/crypto.py`, `routes_me.py`

### 3. **Metering and cost attribution**
- Every LLM call becomes a row in `llm_calls`
- OpenRouter's `usage.cost` is the source of truth
- Per-user monthly budget as a guardrail (`402 Payment Required`)
- SQL aggregates for the Usage tab
- Code: `backend/app/ai/ledger.py`, `api/routes_usage.py`, `models.py`

### 4. **Basic agent orchestration**
- An agent = model + prompt + JSON schema
- Pattern: classifier → specialists in parallel → deterministic merge (code, not LLM)
- Why the merge is code (cost, predictability, debugging)
- Fail-soft (classifier, coverage) vs fail-closed (reviewer)
- Verbatim-span verification as a safety gate before document editing
- Code: `backend/app/agents/`, `orchestrator.py`, `spans.py`

### 5. **Data-privacy routing (Zero Data Retention)**
- What ZDR means (the provider does not retain or use data for training)
- Per-request policy: `provider: {zdr: true, data_collection: "deny"}` — hard filters on which providers may serve it
- Fail-closed: no route → error, never a silent downgrade
- Model allowlist from `/api/v1/endpoints/zdr` (cached, auto-refreshed)
- Code: `backend/app/ai/zdr.py`, `openrouter.py`

### 6. **Capability registry / graceful degradation**
- Enabled / disabled / unhealthy status
- The app boots with zero env vars (all have sensible defaults)
- Bucket being optional is the worked example
- Code: `backend/app/capabilities.py`, `main.py`

### 7. **Schema migrations**
- Alembic baseline: one clean migration, not 10 legacy ones
- Migrate-then-serve in the start command vs Railway pre-deploy command
- The `create_all == alembic head` parity test
- Code: `backend/alembic/`, `db_migrate.py`, `tests/test_migrations.py`

### 8. **Observability basics**
- Structured JSON logs via `structlog`
- Correlation IDs (every request carries one)
- `/api/status` for health and capability state
- Railway logs and metrics as the platform layer
- "Observe" is the step students usually skip
- Code: `backend/app/telemetry/logging.py`, `main.py`

### 9. **Testing and CI**
- Pytest over an in-process ASGI client (no network, no LLM spend)
- Fake gateway for deterministic testing
- Smoke test against the live URL (`backend/scripts/smoke.py`)
- GitHub Actions gate → Railway auto-deploy
- Code: `backend/tests/`, `.github/workflows/ci.yml`

### 10. **Infrastructure as Code**
- `.railway.ts` mirrors the dashboard canvas (reference; manual steps are primary)
- Why `railway.json` is deprecated (hard cutoff 2026-12-01)
- Code: `.railway/railway.ts`

### 11. **Object storage patterns**
- Presigned URLs (S3 native) vs proxying (app serves the file)
- Free egress within a region; charged egress across regions
- Retention caps: delete oldest when cap is exceeded
- Per-environment buckets (dev ≠ prod)
- Code: `backend/app/storage/bucket.py`

### 12. **Seed data and demo readiness**
- Idempotent seeding (safe to run multiple times)
- Fixed RNG seed for reproducible synthetic history
- Synthetic users + reviews + `llm_calls` rows
- Why a demo needs believable history (Usage tab looks alive)
- Code: `backend/app/seed_demo.py`, `Makefile`

### 13. **API error contract**
- One `{"error": {"code", "message"}}` envelope
- Default-deny 404 (unmapped routes return 404, not 405)
- Upload limits: size + character count
- Never echo user input in error messages (422 never includes the bad input)
- Code: `backend/app/api/errors.py`, `main.py`

---

## 8.3 Text edits to existing slides

- **Slide 27 notes:** "Word Agent keeps prompts, activity and snapshots in Postgres" → "Legal Helper keeps users, reviews and per-call usage in Postgres and the reviewed documents in a bucket"

- **Slide 28 notes:** "Word Agent uses synchronous APIs today" → "Legal Helper uses both: quick reviews are synchronous, deep reviews are asynchronous with polling"

- **Slide 32:** "persistent Node web service" → "persistent Python web service"; "Two visible services" → "Three visible services"

- **Slides 37–41 notes:**
  - Replace `word-agent-railway` links with `legal-helper` repo and deployment
  - Replace live URL references with the Legal Helper Railway URL
  - Replace `API_TOKEN` / `MAX_SNAPSHOTS_PER_DOC` with `APP_SECRET_KEY` / `MAX_DOCS_PER_USER` / `MAX_MONTHLY_COST_USD`
  - Clarify: bucket contains full documents (use non-confidential files for demo)

---

## Deployment Walkthrough (Slides 39–41 replacement)

Replaces the Word Agent demo with Legal Helper deployment steps.

### Step 1: Prepare repo

```bash
git clone https://github.com/YOUR-GITHUB/legal-helper
cd legal-helper
make install && make run
curl localhost:8000/healthz  # -> {"status":"ok"}
```

### Step 2: Create a Railway project

1. Railway.app → New Project → Empty Project
2. Name it `legal-helper`

### Step 3: Add Postgres

`+ Create → Database → PostgreSQL` (keep default name)

### Step 4: Add a bucket

`+ Create → Bucket`, name `documents`, choose region (immutable)

### Step 5: Add the app service

`+ Create → GitHub Repo → legal-helper → main` (Railway auto-detects Dockerfile)

While building, set environment variables (see `docs/DEPLOY_RAILWAY.md` for full list):

| Variable | Value |
|---|---|
| `APP_ENV` | `prod` |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
| `APP_SECRET_KEY` | (generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`) |
| `S3_*` | (five references to the bucket) |
| `SEED_DEMO_DATA` | `true` |
| `DEMO_USER_PASSWORD` | (choose) |

### Step 6: Generate domain + healthcheck

Settings → Networking → Generate Domain (e.g., `legal-helper-prod.railway.app`)
Settings → Healthcheck path → `/healthz`

### Step 7: Verify

- Open `https://<domain>/` (landing page)
- Open `https://<domain>/healthz` (should be 200 OK)
- Watch deploy logs: migration → seed → uvicorn serving

### Step 8: Sideload add-in

Download manifest: `curl https://<domain>/manifest.xml > manifest.xml`

- **Word on the web:** Insert → Add-ins → Upload My Add-in → select `manifest.xml`
- **Mac:** Copy to `~/Library/Containers/com.microsoft.Word/Data/Documents/wef/`
- **Windows:** Copy to `%LOCALAPPDATA%\Microsoft\Office\16.0\Wef\`

### Step 9: Smoke test

```bash
python backend/scripts/smoke.py https://<domain> alice.tan <DEMO_USER_PASSWORD>
```

All checks should pass.

### Step 10: Live demo in Word

Sign in as `alice.tan`.
Paste your OpenRouter key in settings.
Upload `samples/nda_missing_governing_law.docx` and review.
Click **Apply all** to apply redlines.
Open **History** to see the original.

### Step 11: Redeploy

Push a commit to `main`:
```bash
git add . && git commit -m "Update playbook" && git push
```

Railway auto-builds and redeploys. Data persists.

### Step 12: Teardown

Delete the Railway project after the workshop.
Rotate `APP_SECRET_KEY` and `DEMO_USER_PASSWORD` if the deployment stays up.

---

## Requirements table (for slide replacement)

This replaces slide 22's "Word Agent examples" with Legal Helper's testable requirements:

| Quality | Requirement | Proof |
|---|---|---|
| **Reliability** | `/healthz` returns 200 on 10/10 checks after deploy; data survives a redeploy | Railway healthcheck; smoke; redeploy demo |
| **Security** | Every `/api/*` route except login and status returns 401 without a valid bearer token | Integration tests; smoke |
| **Secrets** | No key in git; user keys encrypted at rest; API never returns more than the last 4 characters | `test_me.py`; `grep` in CI |
| **Privacy** | Every OpenRouter request carries `provider.zdr=true` and `data_collection=deny`; no compliant route → error | `test_zdr_fail_closed` |
| **Performance** | p95 of `GET /api/me/usage` < 500 ms with 10,000 `llm_calls` rows | Smoke timing; indexes |
| **Cost** | A user's reviews are refused once monthly spend exceeds `MAX_MONTHLY_COST_USD` (default $5); at most `MAX_DOCS_PER_USER` objects per user | `test_budget.py`; `test_retention.py` |
| **Recovery** | Reviews left `running` by a crash are marked failed within 15 minutes of restart | `test_stale_jobs.py` |
| **Limits** | Uploads over 10 MB or documents over 120,000 characters are rejected with 413 | `test_reviews_limits.py` |

---

## Deck structure after updates

**Sections 1–4 (slides 1–32):** Conceptual (unchanged except for notes edits and one text change on slide 32).

**New slides after slide 28:** The 13 new concepts above (authentication, secrets, metering, agents, ZDR, capabilities, migrations, observability, testing, IaC, object storage, seed data, API errors).

**Section 5 (slides 39–41 replacement):** Legal Helper deployment walkthrough (the step-by-step above) + requirements table.

**Total new slides:** ~15 (concepts 1–13 + deployment walkthrough).

