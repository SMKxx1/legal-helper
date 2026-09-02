# Architecture

This document answers four fundamental questions about Legal Helper and describes how the system behaves under different workloads.

---

## The four architecture questions (from Deployment 2, Slide 25)

### 1. Demand: What is the user's job?

The user's job: **review a legal document quickly and carefully, directly in Word.**

The user opens a `.docx`, clicks "Review this document" in the task pane, and the service analyzes the contract against a legal playbook, returning findings as tracked changes + comments in Word. The user can then accept, reject, or edit each redline, all within Word's native tracked changes UI.

No copying to another app. No PDF upload. No email loop. Just: read in Word, review in Word, edit in Word.

### 2. Runtime: How does the service execute?

Legal Helper is a **single persistent service**, not a distributed system:

- **One container** running a FastAPI app + uvicorn on one or more replicas
- **Synchronous reviews** (Quick mode) complete in ~30–40 seconds and return immediately (`200 OK`)
- **Asynchronous reviews** (Deep mode) accept the request immediately (`202 Accepted`), execute in an in-process `asyncio.create_task()` under a semaphore, and the client polls `/api/reviews/{id}` for status
- **Crash recovery:** any `running` reviews older than 15 minutes are marked `failed` at startup (5 lines of code)

There is **no separate worker queue, message broker, or job system**. The async task runs in the same process as the HTTP server. This works for a teaching demo and a small deployment. The plan flags adding a worker service as an extension point.

### 3. Data: What does the system remember?

**Postgres** stores relational data:

- `users` — username, password hash, encrypted OpenRouter key (last 4 chars visible), preferred model choices, role
- `sessions` — bearer token (hashed), user ID, TTL, last-seen time
- `reviews` — one per document reviewed: metadata, status, findings (JSON), cost, duration
- `llm_calls` — one per LLM call: which agent, model, tokens, cost, latency (for billing and observability)

**Bucket** (S3-compatible) stores objects:

- `users/<user-id>/reviews/<review-id>.docx` — the original document (for History → "Original .docx" download)
- Retention: at most `MAX_DOCS_PER_USER` (default 20) per user; oldest are deleted when the cap is exceeded

**Session storage** is in-memory with Postgres persistence. A bearer token lives in the `sessions` table; it expires after 12 hours.

### 4. Integration: Where do external systems live?

**OpenRouter** (LLM provider):

- User supplies their own API key (entered in the add-in, encrypted server-side)
- Server calls OpenRouter with the user's key per review
- Requests carry `provider: {zdr: true, data_collection: "deny", allow_fallbacks: false}` — Zero Data Retention policy
- Model choices are validated against OpenRouter's `/api/v1/endpoints/zdr` endpoint (cached, updated on startup)
- If no ZDR route exists for a model, the review fails with `no_zdr_route` (fail-closed, never silent downgrade)

**No integrations with Slack, email, DocuSign, Drive, Airtable, or Tally.** This is intentional: the codebase is a teaching demo and should be explainable in under an hour.

---

## Request flow: Quick review (synchronous)

```
1. Client uploads .docx + mode="quick"
   POST /api/reviews
   Content-Type: multipart/form-data
   Authorization: Bearer <token>

2. Server validates:
   - Auth token valid?
   - User has OpenRouter key set?
   - Monthly spend < MAX_MONTHLY_COST_USD?
   - Document size < MAX_DOC_CHARS?
   - Concurrency semaphore has capacity?

3. Server runs the review pipeline:
   a. Parse .docx (python-docx)
   b. Classifier agent: document type, parties, governing law, confidence
   c. Reviewer agent: findings with suggested edits (spans + text)
   d. Coverage agent: missing required clauses from the playbook
   e. Synthesize: prune findings, compute risk tier + adherence score
   f. Verify each finding span against original document (safety gate)
   g. Record LLM calls in llm_calls table (agent, model, tokens, cost)

4. Server stores the original .docx in the bucket (optional; failure doesn't block review)

5. Server returns:
   {
     "id": "...",
     "status": "done",
     "risk_tier": "high",
     "adherence_score": 0.75,
     "findings": [
       {
         "id": "...",
         "agent": "reviewer",
         "clause": "Indemnification",
         "risk": "high",
         "finding": "Indemnification is one-sided...",
         "span": "indemnification",
         "suggested": "mutual indemnification of both parties"
       },
       ...
     ],
     "coverage": [
       {
         "position": "Governing Law",
         "required": true,
         "found": false
       },
       ...
     ],
     "tokens": {
       "input": 2500,
       "output": 800
     },
     "cost_usd": 0.15,
     "duration_ms": 34000
   }

6. Client applies redlines via Office.js:
   For each finding: locate the span in the document → Turn on Track Changes →
   Replace with suggested text → Attach a margin comment → Turn off Track Changes
```

**Latency:** 20–40 seconds (mostly waiting for LLM calls, parallel where possible).

---

## Request flow: Deep review (asynchronous)

```
1. Client uploads .docx + mode="deep"
   POST /api/reviews
   (same validation as Quick)

2. Server returns 202 Accepted:
   {
     "id": "review-abc123",
     "status": "queued"
   }

3. Server creates an asyncio.create_task() that runs the review in the background,
   under a semaphore (max REVIEW_CONCURRENCY tasks at once).

4. Client polls:
   GET /api/reviews/review-abc123
   Authorization: Bearer <token>

   Response (while running):
   {
     "id": "...",
     "status": "running",
     "progress_message": "Running coverage check..."
   }

   Response (when done):
   {
     "id": "...",
     "status": "done",
     "risk_tier": "...",
     "findings": [...],
     ...
   }

5. Client continues polling every 2–5 seconds until status == "done" or "failed".

6. On success, client applies redlines as in Quick mode.
   On failure, client shows the error message to the user.
```

**Latency:** 1–3 minutes (Opus is slower; includes coverage check). The polling interval is configurable in the add-in.

**Crash recovery:** if the service crashes, any `review.status` that is `running` and `created_at` is older than 15 minutes is set to `failed` with error "service restarted" at startup.

---

## Agent pipeline

The orchestrator runs agents in sequence and parallel:

```
1. Classifier agent (always runs)
   Input: document text
   Output: {doc_type, parties, governing_law, our_side_guess, one_line_summary, confidence}
   Model: haiku (fast, cheap)
   Failure: fail-soft (review continues with empty doc_type)

2. Specialists in parallel (if mode == "quick" OR after classifier succeeds)
   - Reviewer agent: findings with suggested edits
   - Coverage agent: required clauses checklist
   Model: sonnet (quick) or opus (deep)
   Failure: reviewer failure → fail-closed (review fails); coverage failure → soft (skipped)

3. Deterministic merge (code, not LLM)
   - Prune findings below a confidence threshold
   - Deduplicate overlapping spans
   - Sort by risk
   - Compute risk_tier (high, medium, low) from the distribution
   - Compute adherence_score (0–1) from coverage

4. Span verification (safety gate)
   - For each finding: re-find the exact span in the original document
   - If the span doesn't exist, discard the finding (add-in can't apply a non-existent redline)
   - Log the discarded findings for debugging
```

**Key design:**
- Agents are **stateless functions** — each call is independent, no memory between reviews
- The **merge is deterministic code**, not a fourth LLM call — this keeps costs predictable and failure modes clear
- **Fail-soft** (classifier, coverage) vs **fail-closed** (reviewer): if the reviewer fails, the user sees an error and can retry, rather than an incomplete review

---

## Trust boundaries

### 1. Word ↔ App (public HTTPS)

**Threat:** attacker intercepts or forges a token.

**Mitigation:**
- Bearer tokens are opaque, 256-bit random, hashed with SHA-256 before storage
- Tokens expire after 12 hours
- Every `/api/*` route except `login` and `status` requires a valid token
- Tokens are transmitted only in the `Authorization` header, not in query strings or cookies
- No CSRF token needed (bearer auth is not vulnerable to CSRF)

### 2. App ↔ Postgres (private network)

**Threat:** attacker gains access to the `DATABASE_URL` or eavesdrops on the connection.

**Mitigation:**
- `DATABASE_URL` is a Railway reference variable, never stored in code or `.env`
- On Railway, Postgres is on a private network — no public IP
- Connection is encrypted (Railway's default)
- Passwords are hashed with argon2id before storage; the database never contains plaintext passwords
- Session tokens are hashed with SHA-256 before storage

### 3. App ↔ Bucket (public S3 endpoint)

**Threat:** attacker gains access to bucket credentials or downloads someone else's document.

**Mitigation:**
- Bucket credentials are Railway reference variables, never in code
- All documents are private (S3 object ACLs)
- The client never gets direct S3 access; it only gets a short-lived (15 min) presigned URL from the server
- The server validates that the requesting user owns the review before issuing a presigned URL
- Bucket objects are deleted after `MAX_DOCS_PER_USER` are exceeded (retention cap)

### 4. App ↔ OpenRouter (public API)

**Threat:** attacker steals the user's OpenRouter key or routes the request to a non-ZDR endpoint.

**Mitigation:**
- User's OpenRouter key is encrypted with Fernet (a symmetric cipher) using `APP_SECRET_KEY`
- Encrypted key is stored in `users.openrouter_key_enc` in Postgres
- Decryption happens in-memory, per review, and the plaintext key is never logged or persisted
- Every OpenRouter request carries `provider: {zdr: true, data_collection: "deny", allow_fallbacks: false}`
- Before each review, the app calls OpenRouter's `/api/v1/endpoints/zdr` to fetch the current list of ZDR-capable models
- If a model is not in the list, the review fails with `no_zdr_route` (fail-closed)
- OpenRouter's response includes `usage.cost`; the app records this in `llm_calls` and checks against `MAX_MONTHLY_COST_USD`

---

## Extension: Worker service (what would change)

The plan flagges adding a worker service as an optional next step. Here's what would change:

**Today (in-process async task):**
- Deep review = `202 Accepted` + `asyncio.create_task(run_review(...))`
- All I/O and LLM calls happen in the same process as the HTTP server
- Semaphore limits concurrency to `REVIEW_CONCURRENCY` (default 2)
- A crash loses in-flight reviews (crash recovery marks them failed after 15 min)

**With a worker:**
1. **Web service** (no change to HTTP API):
   - `POST /api/reviews` returns `202 Accepted` immediately
   - `reviews.status` starts as `queued`
   - No blocking I/O; returns immediately

2. **Worker service** (new):
   - Polls or consumes from a message queue (e.g., Railway's Bull Queue or a Redis queue)
   - Runs the full review pipeline (orchestrator, agents, storage)
   - Updates `reviews.status` and `llm_calls` when done
   - Retry logic: if a task fails, re-queue it (up to N retries)

3. **Benefits:**
   - The web service stays responsive even under heavy load
   - Worker can scale independently from the web service
   - Better crash recovery: the queue persists pending reviews
   - Easier to add priority (e.g., quick reviews before deep reviews)

4. **Trade-offs:**
   - More complex: you now manage a queue + workers + their health
   - More to deploy: web service + worker service + queue storage
   - Debugging is harder (tasks run in a different process)

For a teaching demo or small deployment, in-process async is simpler and sufficient. The worker is a clear next step once scale or reliability demands it.

---

## Data model

See `LEGAL_HELPER_PLAN.md` §5 for the full schema. Key tables:

- `users` (id, username, display_name, password_hash, role, openrouter_key_enc, openrouter_key_last4, created_at, last_login_at)
- `sessions` (id, user_id, token_sha256, created_at, expires_at, last_seen_at)
- `reviews` (id, user_id, created_at, finished_at, filename, doc_sha256, doc_type, our_side, mode, status, risk_tier, adherence_score, findings_count, input_tokens, output_tokens, cost_usd, duration_ms, doc_object_key, doc_bytes, result_json, error)
- `llm_calls` (id, review_id, user_id, agent, model, provider, prompt_tokens, completion_tokens, cached_tokens, cost_usd, latency_ms, ok, error, created_at)

All rows have indexed `created_at` for sorting and retention. `llm_calls` has a compound index on `(review_id, created_at)` for billing queries.

---

## Observability

**Logging:**
- Structured JSON logs via `structlog`
- Each request carries a `correlation_id` (UUID, passed in `X-Correlation-ID` header or generated)
- All LLM calls, storage operations, and API responses are logged
- Log level is configurable via `LOG_LEVEL` env var (default `INFO`)

**Health check:**
- `GET /api/status` returns `{"ok": true}` + capability states (database, bucket, ZDR list) + current time
- `GET /healthz` returns `{"status": "ok"}` (Railway healthcheck target)

**Metrics:**
- Prometheus-style metrics would go here in a production system; for a demo, the `llm_calls` table is the metrics store
- Query `SELECT COUNT(*), SUM(cost_usd) FROM llm_calls WHERE created_at > now() - interval '1 day'` for daily cost

---

## Testing strategy

**Unit tests** (pytest over in-process ASGI client):
- Auth: login, wrong password, unknown user, throttle, 401 without token
- Reviews: upload validation, semaphore limits, status transitions
- Agents: classifier, reviewer, coverage, span verification (mocked gateway)
- Storage: presigned URL auth, retention deletion
- Integration: full happy path (mocked OpenRouter)

**Smoke test** (deployed):
- `/healthz` x10, `/api/status`, login, `/api/me`, `/api/me/usage` timing, optional sample review

**Manual test** (in Word):
- Sign in, set key, review a sample, apply redlines, check History, download original

**CI/CD:**
- `make check` (ruff + mypy + pytest) on every push
- `npm test` (add-in tests) on every push
- GitHub Actions gates the deploy

---

## Performance considerations

**Database queries:**
- All queries should complete in <50ms
- Indexes on `users.username`, `sessions.user_id`, `reviews.(user_id, created_at)`, `llm_calls.(review_id, user_id)`
- Usage aggregates (`SELECT COUNT(*), SUM(cost_usd)`) are slow over 100k rows; cache them or use materialized views in production

**LLM calls:**
- Orchestrator runs agents in parallel where possible (reviewer + coverage side-by-side)
- Caching: OpenRouter's prompt caching is enabled; subsequent reviews of the same document may be cheaper
- Timeouts: `PROVIDER_TIMEOUT_S` (default 150s) protects against hanging calls

**Storage:**
- Presigned URLs are generated on-demand (no caching)
- Bucket retention is enforced by a cleanup job that runs at review completion
- Documents are stored with a sha256 key for deduplication (optional future optimization)

---

## Deployment topology

**Local (SQLite, no bucket):**
```bash
make install && make run
```
Single process, in-memory async, SQLite on disk. No S3. No seed data by default. Good for development.

**Railway (Postgres + bucket):**
```bash
# Railway project with 3 services:
# 1. legal-helper (app, auto-built from Dockerfile)
# 2. Postgres (managed)
# 3. documents (bucket, managed)
#
# All services on a private network; app has public domain + HTTPS
# Postgres connection via reference variable ${{Postgres.DATABASE_URL}}
# Bucket credentials via reference variables ${{documents.*}}
```

**Docker Compose (local multi-container):**
A `docker-compose.yml` could add Postgres + LocalStack (S3 emulator) for local full-stack testing (not shown; students can build this as a stretch exercise).

---

## FAQ

**Q: Why not a separate worker queue?**
A: For a teaching demo, in-process async is simpler to explain. The worker is a clear next step.

**Q: Why bearer tokens instead of cookies?**
A: Cookies + CSRF are a pain in an Office webview. Bearer tokens are stateless and easier to teach in an API context.

**Q: Why Postgres instead of SQLite for demo?**
A: SQLite works locally; Railway's managed Postgres demonstrates the "same code, different storage" pattern.

**Q: Why store the user's OpenRouter key encrypted, not in plaintext?**
A: Keys at rest must be encrypted; decryption at call time allows per-review key rotation (future) and safer auditing.

**Q: What if OpenRouter is down?**
A: The review fails with `provider_error`. The client shows the error; the user can retry.

**Q: Can I use a different LLM provider?**
A: The orchestrator is generic; you'd swap `ai/openrouter.py` for `ai/anthropic.py` or `ai/vertex.py`. The ZDR-pinning logic is OpenRouter-specific.

