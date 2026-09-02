# NDA Assistant — Rebuild Plan

**Status:** v1.1 — final for approval · 2026-07-03 (v1.0 draft revised after 3-critic adversarial review: parity, engine/Azure, security)
**Replaces:** `nda-review-cloud` (proj-o61q4ptw, Railway) + 14 n8n workflows + n8n itself.

---

## 1. Goal and non-goals

**Goal.** One clean, understandable, robust codebase that does everything the old engine + n8n stack does — NDA template delivery, generation, review, DocuSign envelopes, archival — plus the agreed new features, deployed on Azure with continuous deployment, aligned with the company's In-house AI Infra direction.

**New features (agreed):**
- Expiration-date extraction from signed NDAs → Airtable (on archive + nightly sweep + manual Slack override).
- Archive works symmetrically via Slack **and** email (today's no-file recovery path is Slack-only).
- Self-serve template updating for non-technical admins (Slack guided flow + admin web page; validation, test-drive, versioning, rollback).
- In-house form service replacing Tally — v1 single-party, architected for two-party (employee form + counterparty link) with partial fill.

**Non-goals (v1):** multi-org tenancy activation (schema stays ready), SharePoint archival (provider stub only — company creds pending), Entra ID SSO (seam only), migrating historical review data, fine-tuning, DocuSign Doc Gen, MCP server (deliberate thin-adapter seam kept — see below).

**Future MCP surface (kept cheap by design):** intent handlers stay channel-agnostic with typed Pydantic inputs/outputs (they become MCP tool schemas nearly verbatim); SERVICE-key entitlements enumerate exactly which actions a machine caller may invoke; and the human-confirm invariant lives in the action layer, so a future MCP client (e.g. the In-house ERP agent) gets read/review/generate tools directly while outward actions (DocuSign, archive, publish) return confirmation handoffs. Adding MCP later = mounting the SDK's streamable-HTTP router + an auth shim, not a re-architecture.

---

## 2. Decisions

| Decision | Choice | Reason |
|---|---|---|
| Language/shape | **Python monorepo** — port + refactor the engine, build the bot orchestrator beside it | Engine is mature (271 tests, playbook, span checks); Python owns doc-processing (python-docx, PyMuPDF, OCR); In-house doc's TS pick was UI-driven (doesn't apply); its Python runner-up (FastAPI) fits |
| Compute | **Azure Container Apps** | Revisions for safe deploy/rollback, Key Vault integration, managed TLS, matches the prior partial ACA attempt. **api and worker run minReplicas=1 always-on** — Slack's 3s ack and the IMAP poller/scheduler rule out scale-to-zero for these apps (dev cost stays small at 0.25 vCPU) |
| LLM provider | **OpenRouter with Zero Data Retention — all aliases** | Owner decision, **re-confirmed 2026-07-03 as a deliberate override** of the internal memo (`nda-review-openrouter` docs) that recommended direct Anthropic. Verified: no OpenRouter code exists in any prior repo — the adapter is built from scratch (§3.8); the ported direct-Anthropic adapter stays available as a config fallback. ZDR enforced as *routing policy* — see §6 |
| Data | **Fresh Azure Postgres** | Template bytes must be re-uploaded anyway; old history stays queryable in the old system until retirement |
| Intake form | **In-house form service** | Future two-party/partial-fill flows impossible with Tally; party data stays in our tenant |
| DocuSign | **Keep current shape** (we fill docx; DocuSign only signs) | Generate-only and envelope paths stay uniform; template updates stay self-serve |
| Archive | **Google Drive now, SharePoint later** behind a storage-provider interface | SharePoint is the stated destination; creds pending; Drive replicates it meanwhile |
| Admin auth | **Port password/session plane; pluggable seam for Entra ID** | Works with zero IT dependency now; SSO later without rework. Hardened per §6 |
| Word add-in | **Port in v1** | No n8n dependency; one endpoint; low cost, keeps lawyer workflow |
| Environments | **Dev + Prod** | Safe integration testing; marginal ACA cost |
| docx→PDF | **Bundled LibreOffice (`soffice`), no Gotenberg container** *(revised)* | The engine image already bundles soffice + tesseract for ingestion; one conversion path (subprocess + timeout in worker jobs) beats running a second container for the same library |

**Deliberate behavior changes vs. the old system** (not regressions — documented so nobody "fixes" them back):
1. The direct ≥2-signer envelope path gains an explicit human **confirm button before DocuSign send** (today it sends immediately). Closes the spoofed-email → outbound envelope hole (§6).
2. Dedup **fails closed** (today an engine outage lets duplicates through).
3. The allowlist becomes **real** (today the stub allows everyone) — with a proper pending-approval flow and a seeded initial allowlist so nobody is locked out on day one (§3.4).
4. `/healthz` becomes a shallow public liveness probe; the detailed capability report moves behind admin auth (today's plan draft would have leaked config state anonymously).

---

## 3. Target architecture

### 3.1 Runtime topology (Azure Container Apps, per environment)

```
                        ┌────────────────────────────────────────────┐
 Slack events/actions ─▶│  api (FastAPI, public ingress, min 1)      │──▶ OpenRouter (ZDR-pinned)
 Form pages (/f) ──────▶│  ├─ /slack/*   events+interactivity (Bolt) │──▶ DocuSign
 Word add-in ──────────▶│  ├─ /v1/*      engine API                  │──▶ Google Drive / SharePoint (later)
 Admin browser ────────▶│  ├─ /f/*       form service                │──▶ Airtable
                        │  ├─ /admin/*   admin UI + API (sessions)   │──▶ SMTP
                        │  ├─ /addin/*   Word add-in static          │
                        │  ├─ /healthz   shallow liveness (public)   │
                        │  └─ 404 default-deny fallback              │
                        ├────────────────────────────────────────────┤
                        │  worker (same image, min 1, always-on)     │──▶ IMAP (poll)
                        │  ├─ IMAP intake poller                     │──▶ Drive watcher
                        │  ├─ review job claimer (DB queue, 10s)     │──▶ nightly sweeps
                        │  ├─ docx→PDF conversions (soffice)         │
                        │  └─ APScheduler under pg advisory locks    │
                        └────────────────────────────────────────────┘
  Azure Database for PostgreSQL · Azure Files (/data volume) · Key Vault (user-assigned MI)
  ACR · Log Analytics + App Insights (redacting sampler)
```

Infra notes that will bite if skipped (from adversarial review):
- **Azure Files share mounted as `DATA_DIR=/data`** on api + worker — the engine persists uploads/exports/template working files there; it is one of the three documented replicas≥2 blockers (Redis + /data + in-process caches). In the Bicep inventory from day one.
- **User-assigned managed identity, created and granted Key Vault `get` *before* the container apps** in the Bicep dependency graph (system-assigned identities create a first-deploy chicken-and-egg with KV secret references). Secret rotation requires a new revision — documented in AZURE.md.
- **ACA ingress request timeout (~240s) is shorter than a worst-case deep review (up to 10 min)**. The Word add-in's current synchronous POST /v1/reviews call therefore moves to the async pattern for deep mode: submit → 202 + job id → poll `GET /v1/reviews/jobs/{id}` (both endpoints already exist in the engine; small add-in JS change). Sync stays for quick reviews.
- **Migrations run as a pre-deploy step** (Actions job), never at boot — ported CONTRACT discipline (expand/contract, compatible one release back). Mandatory on ACA where old+new revisions overlap during rollout.
- The **n8n Postgres database, the SIGNED-principal (HMAC/Ed25519) plane, and the `/v1/support_task/bot/*` DAL endpoints are retired** — they existed only as n8n's safe doorway. The bot is in-process; guards, dedup, allowlist become ordinary transactional code. `/v1/reviews` and `/v1/support_task/generate-nda` are preserved (add-in + API-caller contract intact).

### 3.2 Monorepo layout

```
backend/
  app/
    main.py             # app assembly; capability registry boots here
    capabilities.py     # each integration: enabled | disabled(missing config) | unhealthy
    engine/             # ported review pipeline (router, wholedoc, coverage, synthesis, spans, simcache)
    ai/                 # gateway (ported) + OpenRouter adapter (REWRITE — see §3.8) + pricing + trace sink
    ingestion/          # ported: docx/pdf/txt parsing, OCR, redline extraction, segmentation
    generation/         # ported: tokenised docx fill, strip_unfilled, template resolution
    bot/
      channels/         # slack.py (Bolt), email_in.py (IMAP + auth checks + text cleaning), email_out.py, replies.py
      router.py         # guards → dedup → deterministic route → LLM classifier → allowlist hardening
      intents/          # template.py generate.py review.py envelope.py archive.py help.py expiration.py template_admin.py
      interactivity.py  # typed, versioned button/modal payload contracts (schema-validated)
      approvals.py      # pending-approval flow (persist, notify admin, resume)
      correlation.py    # request/confirmation state in Postgres
    forms/              # schemas from token registry, instances, links, public pages, submissions, token derivation
    integrations/       # docusign.py airtable.py convert.py(soffice) storage/ (base.py drive.py sharepoint.py)
    archive/            # archive intent + cache-folder watcher (skip filters, duplicate check) + naming
    auth/               # ported WEB sessions + SERVICE keys; provider seam (password now, entra later); real allowlist
    admin/              # template mgmt + allowlist/approvals + server-rendered pages (sessions, CSRF, CSP)
    telemetry/          # structlog, correlation ids, OTel → App Insights, llm_traces (metadata-only default)
    db/                 # SQLAlchemy models + alembic (squashed clean baseline)
  tests/                # ported 271 + bot/forms/integration tests (FakeAdapter, no network) + evals/
word-addin/             # ported (async deep-review polling added)
deploy/azure/           # Bicep + parameter files
docs/                   # PLAN.md ARCHITECTURE.md AZURE.md CREDENTIALS.md RUNBOOK.md
.github/workflows/      # ci.yml · deploy-dev.yml (master) · deploy-prod.yml (approval/tag)
```

### 3.3 Message flow (replacing the n8n Router)

1. **Intake**: Slack event (Bolt, signature-verified, 3s ack) or IMAP message (worker poll) → normalized envelope `{channel, sender, verified_sender, thread, text, attachments, event_key}`.
   - **Email hardening (new)**: SPF/DKIM/DMARC alignment is checked on intake (`Authentication-Results` header parsing + our own DKIM verification); failing mail is treated as **untrusted** — it can ask for help/templates but can never match the allowlist or trigger envelope/archive actions.
   - **Email text cleaning (ported)**: quoted-history stripping ("On … wrote:", forwarded-header blocks, `>` quotes, "Sent from my …"), re/fwd subject detection — *before* the has-content guard and the classifier.
2. **Guards** (ported, bugs fixed): human-event filter, bot-thread continuity gate, has-content check (both channels now), **fail-closed dedup** (unique insert on `event_key`).
3. **Routing**: deterministic keyword router first (ported regex set); ambiguous → LLM classifier (cheap tier via OpenRouter, structured output, few-shot) → **allowlist hardening** of every field (ported rules verbatim: intent set, US/SG, counterparty set, mutuality only from literal directionality keywords, cc_timing default `after`).
4. **Dispatch**: intent handlers behind a registry; each checks its capability health first and degrades to a friendly reply if disabled.
5. **Reply delivery**: one channel-aware reply service (text/file) — Slack thread or threaded email, ported subject-threading and HTML escaping.
6. **Interactivity**: Slack button/modal callbacks carry typed, versioned payloads (schema-validated) — fixing the old lost-context bug.

### 3.4 Allowlist & approvals (was a stub; now real — without locking anyone out)

- `review` and `envelope` intents check the allowlist (fail-closed) keyed on **verified** identity (Slack user id; email only when DMARC-aligned).
- On miss: persist a `pending_approval` request, notify the admin channel/email ("X wants to run Y — request id"), reply "pending approval" to the user — the ported UX, now actually functional.
- Approval: admin adds the user on the admin allowlist page (or a one-click approve button in the admin notification); the user retries or the pending request auto-resumes.
- **Cutover seeding**: the current active users are seeded into the allowlist before the flip, so day-one behavior is unchanged for existing users.

### 3.5 Long-running work

Ported DB job queue (visibility timeout, worker claimer): intent handlers enqueue + reply "working on it"; worker completes and posts through the reply service. Retries with backoff; dead letters visible in admin. Deep reviews from the add-in ride the same queue (§3.1).

### 3.6 Form service (Tally replacement → full form builder)

Design informed by source-level review of HeyForm and OpnForm (research notes in session scratchpad). OpnForm contributes the data model; HeyForm contributes the edit-safety model.

> **License boundary (checked 2026-07-03): both are AGPL-3.0** (OpnForm additionally has a commercial license on its `Enterprise` directory). AGPL is viral over network use — copying their code into this proprietary service would obligate source disclosure to everyone who interacts with it, including external counterparties filling forms. Therefore: **patterns and data-model ideas only, zero code copying.** In practice this costs nothing — they're TypeScript/PHP SPAs and we're building Python server-rendered pages, so everything is reimplemented from scratch anyway.

We also deliberately do **not** copy either one's SPA build-chain approach — the JSON model is the valuable part; our builder stays server-rendered + light JS (Alpine/htmx + SortableJS) over the same model, and the builder's preview reuses the exact public renderer so WYSIWYG is honest.

- **Form model (OpnForm pattern)**: one JSONB `blocks` array per form; each block `{id: uuid, type, label, help, required, placeholder, party: internal|counterparty, token_binding: token_id|null, type-specific props}`. A data-driven **block registry** (Python dict) powers palette + defaults + validation from one source, with the virtual→concrete pattern (radio *is* select+flag) keeping renderers/validators few. v1 block set: text, long-text, email, phone, date, select/radio, checkbox, number, plus layout text and page-break. Presentation/behavior settings live in one `settings` JSONB (not 60 columns — OpnForm's own regret).
- **Edit safety (HeyForm pattern)**: `draft_blocks` vs published `blocks` with an integer version and **optimistic concurrency** (stale writes rejected) + autosave; publishing promotes the draft. Removed fields are soft-retained (`removed_blocks`) so old submissions always render.
- **Undo/redo (required, all three surfaces)**: the **form builder** keeps a client-side command history over the draft blocks (Ctrl+Z / Ctrl+Shift+Z + visible buttons), backed by server-side draft snapshots so recovery survives a reload; the **template studio** keeps a server-side operations log per draft — every tokenize drop records the replaced text, so undo restores the original span in the docx and redo re-applies, arbitrarily deep until publish; the **public form** keeps a lightweight per-page answer-history stack so a respondent can step back through value changes (including restoring a cleared field) before submitting.
- **Unrestricted creation & sending**: users create any form (NDA-bound or standalone), then send it — copy a share link, or have the bot deliver it into a Slack thread/email. Submissions viewable/exportable from the same page.
- **Submissions**: rows keyed `{field_uuid: value}` with `status: partial|completed`, a `public_id` UUID for save/resume links, debounced autosave, server-side validation derived from block props (required skipped for partials), and the **completed-is-terminal** race guard — all straight from OpnForm's proven mechanics.
- **Token binding & derivation**: a bound field writes to its token at generation time, **resolved server-side** from submission data (never client-side). **Computed variables** (`{id, name, formula}`, topologically evaluated) express the ported derivation rules declaratively where possible — `effective_date` → *"the date of the last signature"* sentinel, `purpose` + *" relating to "* composition, SG/US inference, empty-token dropping (then `strip_unfilled`); the remaining rules port as code. Every token carries a fallback text (mention-fallback pattern).
- **NDA instance flow (v1)**: generate intent → instantiate the (auto-maintained) NDA form bound to the correlation record → link into thread/email → fill → validate → resolve tokens → generate docx → deliver + DocuSign offer.
- **Link security (per adversarial review)**: URL carries only a random instance id; the signed token travels in the **fragment** (`#t=…`), exchanged client-side for a short-TTL session cookie — never in server/App Insights/referrer logs. `Referrer-Policy: no-referrer`; expiring links; single active session per link; partial-fill reads bound to the session. Resume links use `public_id` + the same fragment exchange.
- **Two-party (v1.1, designed-in now)**: neither reference product has this — it's net-new, built on the `party` attribute + **two scoped links writing into one submission row** (each renders/validates only its party's fields), instance states `draft → partially_filled → complete`, notify-on-complete. v1 ships single-party on this exact schema.
- **Anti-abuse (unauthenticated surface)**: per-instance session gating, per-IP and global submission rate limits, and a **global generation budget circuit-breaker** — a public endpoint that triggers LLM + docx work is a cost surface.

### 3.7 Template studio, token registry, and drift management

**Templates are .docx only** — hard-enforced at upload with a plain-English rejection. The template/token/form triangle is managed as one system:

- **Token registry (user-managed)**: tokens are first-class rows — `{id, name (validated snake_case), label, help, data_type (text|date|email|choice), scope (template variants), required_per_scope, party, fallback}`. Admins create tokens freely; **deletion shows every usage** (template versions + bound form fields) and requires confirming the consequences. Every create/delete emits a **drift event**.
- **Template studio (the "user adds tokens, page assists" flow)**: a new upload lands as a **draft** version — "no tokens yet" is an expected state, not an error. The core interaction is a **highlight → click tokenizer**:
  - The server extracts the docx into a faithful read-only **document view** (paragraphs, tables, headers/footers — everywhere the filler recurses) with stable addressing (paragraph locator + character offsets over normalized run text).
  - The user **highlights** the text to be replaced (e.g. the hardcoded company name in the source document), then simply **clicks the token** in the palette that should replace it; the server performs the replacement in the real docx — locating the span across formatting runs and swapping it for `{{token}}` while preserving the first run's formatting (the same run-aware machinery the filler already uses, in reverse). While text is highlighted the palette doubles as the action bar (tokens light up as clickable); with nothing highlighted, clicking a token just shows its details.
  - The view re-renders instantly with the token highlighted in place, and the **live checklist** updates (required-for-this-scope tokens found/missing; unknown/typo'd tokens flagged with closest matches). Full **undo/redo** per §3.6: every drop is a logged, reversible operation (the replaced text is retained), so the user can step backward and forward through the entire tokenizing session; nothing touches the stored version until publish.
  - Complementary paths remain for users who prefer Word: click-to-copy `{{…}}` chips, an **upload-revalidate loop** (edit in Word, re-upload, see the token diff), and the **find-and-map assistant** (typed placeholders like `[COMPANY NAME]` detected and offered as one-click replacements).

  Publish is gated: a tokenised variant publishes only when its scope's required tokens are present; the **sample-NDA test drive** (dummy values) remains the final human check. Versioning, audit, and one-click rollback as before; the Slack guided flow remains for simple file replacements — token work happens in the studio.
- **Drift → notify → one-click sync**: any drift event (token created/deleted, template version published with a changed token set) flags every affected form **needs update**, notifies the form owner (Slack DM or email) and banners the admin UI. The form editor then offers a **prepared sync**: "add a field bound to `{{new_token}}`" / "this field's token was deleted — unbind (keep as standalone field) or remove". The user approves a diff instead of reconstructing a form. Forms stay usable while flagged, but **generation-bound sends are blocked while a required binding is missing** — a silently unfilled NDA is worse than a blocked send.
- **Storage**: reuses `document_blob` (sha256) + `template_version` (is_current) + audit rows; the old filename-convention seeder is retired.

### 3.8 AI gateway: the OpenRouter adapter is a rewrite, not a swap

The engine's request builder is Anthropic-native (`output_config.format=json_schema` strict decoding, `effort` knob, system-as-block-list with `cache_control`, `stop_reason=refusal` taxonomy). OpenRouter's OpenAI-compatible surface differs. **Verified 2026-07-03: no prior OpenRouter implementation exists to port** (the `nda-review-openrouter` fork is docs-only and *behind* main); the adapter is new code. The port baseline for `ai/` is `nda-review-cloud`'s newer version (circuit breaker, `provider_health()`, 5m/1h `cache_ttl` support — the fork lacks all three). The internal eval memo's hardening ideas carry over: BYOK (Anthropic key attached to OpenRouter) is a supported option, and the direct-Anthropic adapter remains a configuration fallback per alias. The adapter work is scoped explicitly:

- **Structured output**: `response_format json_schema` where the routed model supports strictness; **client-side schema re-validation + one repair round-trip** as a fallback (the portable-schema validator exists); D1 reasoning-before-verdict ordering kept.
- **Effort → `reasoning` translation** per model family; never temperature.
- **Exception taxonomy remap** (Retryable vs Terminal) for OpenRouter/OpenAI-style errors, incl. content-filter → Terminal.
- **Usage/cost mapping**: OpenRouter usage (incl. its reported cost and cache discounts) → the engine's `Usage` dataclass; model-id namespace (`anthropic/claude-…`) reconciled with the pricing table; **one authoritative cost source chosen (OpenRouter's reported cost)** so monthly caps don't under/over-count. P1 includes a test asserting correct non-zero `cost_usd` through OpenRouter.
- **Prompt caching**: `cache_control` passthrough for Anthropic-family models via OpenRouter; cache-hit accounting verified in P1, not assumed.
- **ZDR enforcement**: account-level data policy + per-request provider pinning; **fail closed** — no silent fallback to a non-ZDR route; startup health check that each configured model alias resolves under the ZDR policy.
- **Model aliases**: `router`, `review-quick`, `review-deep`, `classifier`, `expiration` — each independently pinned and budgeted.
- **The `expiration` alias is not an Anthropic path**: the benchmark's winning contract is `google/gemini-3.5-flash` with `provider:{only:['google-vertex'],allow_fallbacks:false,data_collection:'deny',zdr:true}`, the `file-parser` plugin (`pdf.engine=native`), file-part data-URI content, filename withheld, strict `YYYY-MM-DD|ERROR` output validated by `/^\d{4}-\d{2}-\d{2}$/`. The adapter supports file parts + plugins for this alias; the benchmark lives on as its eval.

### 3.9 Envelope & DocuSign flow (three entry points — the modal is a collector, not a guard)

a. **≥2 signers + clean attached doc**: unfilled-`{{token}}` guard → build envelope (routing order from `sequential`, CC placement from `cc_timing`) → **NEW: explicit confirm card** → create+send → persist attempt with ported idempotency key (`sha1(doc|recipients)`), requester mapping recorded for later DM.
b. **<2 signers**: doc confirm + **"Enter signing details" button → modal** collecting: Amperesand signer email, counterparty signer email, signing order (`all_at_once | amp_first | cp_first`), CC emails, CC timing (`before | after`) → confirm → send. Same modal is reachable from the post-generation "Send via DocuSign? Yes" button.
c. **No file**: thread-doc recovery (Slack replies scan; email correlated attachments) → use-this-doc confirm → (a) or (b).

The modal schema and every button's typed value contract are named deliverables in `interactivity.py`.

### 3.10 Archive, watcher, expiration

- **Archive intent**: both channels; attachment → PDF-normalize (soffice) → naming convention → storage provider upload → record. No attachment → recovery + confirm (buttons / email reply). Email-initiated actions require DMARC-aligned senders (§3.3).
- **Watcher** (worker schedule, correct interval, parameterized SQL): detects completed-envelope drops → **skip filters ported** ("certificate of completion", `summary.pdf`) → destination-folder **duplicate check** (skip + notify) → LLM classification (issuer/recipient/mutuality) → rename `<yyyyMMdd>_<issuer>_<mNDA|uNDA>_<recipient>.pdf` → record with ported status lifecycle (`processing/renamed/saved_default_name/duplicate_skipped/failed`) → requester DM.
- **Requester-DM verification gate (revised)**: the envelope_id ↔ Drive-folder-name correspondence is currently produced by the unread "Main_Project" workflow. **P4 starts by reading Main_Project and observing how completed envelopes actually land in the folder** (Main_Project vs DocuSign Connect); only then is the requester mapping keyed and the DM feature claimed.
- **Expiration**: extraction per §3.8; triggers = archive-time, nightly sweep (doubles as backfill), manual Slack commands (`set expiration …` / `re-extract …`); Airtable upsert capability-gated, **minimal fields** (§6).

---

## 4. Alignment with the In-house AI Infra doc

| In-house pattern | This project |
|---|---|
| ZDR inference provider (their pick: Together AI) | OpenRouter with ZDR **routing policy + provider pinning, fail-closed** (§3.8, §6 residual documented) |
| LiteLLM gateway: aliases, budgets, fallback | Gateway seam in-code: per-capability aliases + budgets + monthly cap; LiteLLM insertable later unchanged (OpenAI-compatible both sides) |
| Cheap-model routing | Deterministic router first; cheap-tier classifier only for ambiguous turns |
| Prompt caching, stable-prefix discipline | Ported for Anthropic-family aliases; verified through OpenRouter in P1 |
| Postgres memory + traces | Correlation state + `llm_traces` (model, tokens, cost, latency, cache-hit, purpose, correlation id) — **metadata-only by default** (§6) |
| Langfuse observability (they deferred it too) | OTel GenAI semconv → App Insights now; trace schema Langfuse-compatible for later |
| Eval harness | Ported engine eval passes + expiration benchmark as pytest evals (FakeAdapter in CI; real-provider eval mode on demand) |
| Threat model, architectural read-only, egress discipline | §6; the model can never fire an outward action without a human confirm |
| Cost guardrails | Per-capability budgets, rate limits, global breaker on public surfaces, cost per trace, monthly cap alarm |

## 5. Feature parity matrix (old → new)

| Current behavior | Where it lands |
|---|---|
| Router guards, dedup, deterministic+LLM routing, field hardening | `bot/router.py` (dedup fail-closed; email has-content + text cleaning + DMARC) |
| Template intent + picker + email ask | `bot/intents/template.py` (0-row guard added) |
| Generate → form link → callback → engine fill → deliver + DocuSign offer | `bot/intents/generate.py` + `forms/` (in-house form; token derivation rules ported — §3.6) |
| Review → engine quick review → severity-grouped summary | `bot/intents/review.py` (real allowlist + approvals — §3.4) |
| Envelope: all three interactive entry points + modal | §3.9 (`envelope.py`, `interactivity.py`, `docusign.py`) |
| Archive + confirm chains | `archive/` + storage provider (email path added) |
| Help / unknown fallback | `bot/intents/help.py` |
| Reply / Reply File delivery | `bot/channels/replies.py` |
| Slack interactivity state machine | `interactivity.py` (typed payloads; context bug fixed) |
| Cache Folder Watcher | `archive/watcher.py` (skips, duplicate check, statuses, schedule, SQL — §3.10) |
| Seed templates | retired → template admin flow |
| Expiration benchmark | `tests/evals/expiration/` + production extractor (§3.8 contract) |
| Engine `/v1` (reviews sync+async, redline, generate-nda, simcache, playbook v4, spans) | ported; add-in deep mode goes async (§3.1) |
| Engine auth WEB + SERVICE | ported; SIGNED plane + `/bot/*` retired |
| Admin plane | ported slim + template mgmt + allowlist/approvals + hardened login (§6); reset email wired to SMTP |
| Word add-in | ported (config.js injection via FastAPI; deep-mode polling) |

**Old bugs fixed by design:** stub allowlist → real + approvals; fail-open dedup → fail-closed; interactivity context loss → typed payloads; watcher 1-min schedule → config; string-built SQL → parameterized; email has-content gap → guarded; template 0-row edge → guarded.

## 6. Security architecture

- **Identity & secrets**: Key Vault + user-assigned managed identity (§3.1 ordering); no secrets in repo/CI logs; `SETTINGS_ENCRYPTION_KEY` stability + rotation-requires-revision documented; `ENGINE_REQUIRE_KEY=true`.
- **Webhook & sender trust**: Slack v0 HMAC + 300s replay window, fail-closed. **Email: DMARC/DKIM alignment required for any allowlisted or action-triggering identity** — unauthenticated mail is read-only-helpful. Form links: fragment-carried token → short-TTL session (§3.6).
- **Human-in-the-loop invariant (now true on every path)**: DocuSign send, archive write, and template publish each require an explicit human confirmation. LLM output is re-validated against allowlists before any action; the bot has no delete tools at all.
- **Prompt injection**: untrusted document/form/email text fenced (ported `<document>` convention + zero-width neutralization), "treat as data" system contracts, span-faithfulness secondary check.
- **PII & traces**: `llm_traces` and OTel spans store **metadata only** by default — no prompt/completion bodies; a raw-content debug mode exists off-by-default for dev. NDA text is inherently un-maskable to the model — recorded as an accepted residual, mitigated by ZDR pinning. App Insights telemetry runs a redacting sampler (no tokens, no form values in URLs by design). Airtable receives **minimal fields** (dates + file reference, not full party payloads); its DPA noted in CREDENTIALS.md.
- **ZDR residual (documented)**: OpenRouter is a broker — ZDR must hold end-to-end, so routing is pinned to ZDR-qualifying providers with fallbacks disabled; if a model has no ZDR route, the capability degrades rather than silently downgrading. The In-house doc's "residual: Medium" rating for provider exfil carries over honestly.
- **Public-surface hardening**: strict CSP (`default-src 'self'`, no inline script) + context-aware autoescaping on all `/f` and `/admin` pages; admin login rate-limited with lockout, strict cookies (`Secure/HttpOnly/SameSite=strict`), single-use time-boxed reset tokens, optional IP allowlist on `/admin`; add-in SERVICE key scoped to review-only with per-key rate + cost caps and CORS pinned to the add-in origin (client-visible key = accepted, bounded residual, killed when Entra arrives).
- **Gates fail closed** (signatures, allowlist, dedup, ZDR routing); **capabilities fail soft** (missing Airtable token = feature politely off). The distinction is explicit in code.
- **`/healthz` split**: public = liveness only (200/503); detailed capability states require admin auth.
- **Cost**: per-capability budgets, per-principal rate limits, per-IP + global breakers on unauthenticated surfaces, monthly cap alarm, cost per trace.
- **Egress allowlist** documented (OpenRouter, Slack, DocuSign, Google, Airtable, SMTP/IMAP); VNet + NSG enforcement as a hardening step in AZURE.md.

## 7. Living documentation

- **docs/AZURE.md** — resources, Bicep usage, environment setup, CI/CD wiring, scale path (Redis + /data + replicas), rotation notes. Updated in the same PR as any change it describes.
- **docs/CREDENTIALS.md** — per service (Slack app, DocuSign, Google OAuth, OpenRouter incl. ZDR account policy + DPA chain, Airtable, SMTP/IMAP, admin bootstrap): where to create, exact scopes, Key Vault name ↔ env var ↔ `.env`, capability mapping, rotation.
- **docs/ARCHITECTURE.md** — the new system end-to-end.
- **docs/RUNBOOK.md** — health interpretation, common failures, revision rollback, n8n-retirement checklist.

## 8. CI/CD

- **ci.yml**: ruff format+check, mypy (blocking), alembic upgrade on SQLite, pytest + coverage gate, addin prettier/node:test.
- **deploy-dev.yml**: push to master → build → ACR → migrate (job) → new ACA revision (dev) → smoke `/healthz`.
- **deploy-prod.yml**: manual approval/tag → same against prod. GitHub↔Azure via OIDC federation (no stored cloud secrets).

## 9. Build phases (each independently deployable + verified)

| Phase | Delivers | Verified by |
|---|---|---|
| **P0 Foundations** | Scaffold, capability registry, telemetry, Bicep (incl. Azure Files, user-assigned MI), dev env live, CI/CD, AZURE.md + CREDENTIALS.md skeletons | "hello /healthz" deployed to dev via Actions |
| **P1 Engine port** | engine/ ai/ (OpenRouter adapter per §3.8) ingestion/ generation/ auth(slim) db(squashed); `/v1`; template files exported from old DB **early**; 271 tests adapted | pytest green; **eval gate: old-vs-new review parity on sample NDAs through OpenRouter incl. schema-valid structured output, cache accounting, non-zero cost_usd**; generate-nda round-trip |
| **P2 Bot core** | Slack + IMAP intake (DMARC checks, text cleaning), guards/dedup, router+classifier, template/review/help intents, replies, interactivity base, approvals flow | scripted E2E dev-Slack + mailbox tests; classifier eval; allowlist deny→approve→retry walkthrough |
| **P3 Generate + forms + DocuSign** | Form **runtime**: block model + registry, public renderer, submissions (autosave/resume, partial|completed), token binding + computed variables, link/session security per §3.6; generate intent; envelope intent all three entry points + modal + confirm; requester mapping write | full generate→form→docx→confirm→DocuSign(demo) loop in dev; form-link security checks (no token in logs/referrer) |
| **P4 Archive + expiration** | **Read Main_Project first (§3.10 gate)**; archive both channels, watcher (skips/dupes/statuses), soffice conversion, expiration extractor + Airtable + manual commands + sweep | archive E2E; watcher against seeded cache folder incl. certificate-skip; expiration eval ≥ benchmark accuracy; Airtable rows verified |
| **P5 Template studio + form builder + auth** | Template studio (highlight→click tokenizer, checklist, find-and-map, test-drive), token registry CRUD + drift events + one-click form sync, form **builder** UI (draft/publish, optimistic concurrency, undo/redo, send via bot/link), Slack template flow, admin pages (CSP, hardened login), password auth + reset email, audit | non-technical dry-run: upload bare .docx→highlight-click tokenize→undo/redo→publish→generate; token delete→drift notification→one-click sync; login throttle test |
| **P6 Add-in + cutover** | Add-in from new origin (async deep polling), hardening pass, prod env, allowlist seeding, Slack app re-point, n8n disabled | side-by-side parity week; retirement checklist executed |

## 10. Risks & mitigations

- **OpenRouter structured-output/caching parity** is the #1 technical risk → P1 eval gate before anything user-facing; direct-Anthropic adapter kept as a configuration fallback since the gateway seam survives.
- **Slack single-endpoint constraint** → separate dev Slack app + mailbox; prod cutover is a URL re-point with the legacy monolith restorable.
- **Main_Project unknowns** (envelope landing + requester mapping) → P4 opens with reading it; watcher DM feature is gated on that verification.
- **Form-service scope creep** → v1 strictly single-party; two-party only after parity is stable.
- **Template bytes only in old DB** → exported in P1 while Railway is up.
- **IMAP/SMTP deliverability from Azure** → validated in P2; transactional-provider fallback (ACS/SendGrid) behind the same reply-service interface.

## 11. Open configuration details (asked, non-blocking)

Airtable base/table/fields + PAT · Azure subscription/region (+ reuse of the partial `southeastasia` attempt or fresh RG) · Slack app strategy (reuse prod + new dev app) · bot mailbox details · DocuSign account (demo→prod timeline) · **"Main_Project" n8n workflow contents (needed by P4)** · admin roster + allowlist seed list · public domain for form links · monthly LLM budget · n8n/Railway retirement timing · **ACTION: rotate the unused live `OPENROUTER_API_KEY` sitting in `nda-review-cloud/backend/.env`** (found during discovery; no code references it).
