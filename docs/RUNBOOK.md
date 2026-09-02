# Runbook

Operational playbook for the NDA Assistant on Azure Container Apps. Living
document — add a row to the failures table every time production teaches you
something. Companion docs: `AZURE.md` (infra), `CREDENTIALS.md` (secrets).

Status: **all phases (P0–P6) shipped; dev is live on Azure.** Every capability
below is registered in `app/capabilities.py`; whether each is enabled in an
environment depends only on which secrets are seeded there. Phase tags record
when a piece landed — history, not status.

---

## 1. Health: `/healthz` vs the admin capability report

Two distinct surfaces, deliberately split (PLAN §2 decision 4, §6):

- **`GET /healthz`** — **public, shallow liveness only.** Returns `200` when the
  process is up (and, once wired, the DB is reachable), `503` otherwise. It
  leaks **no** configuration state. This is what the Container Apps ingress
  probe and the deploy smoke-test hit. If it is `503`, the app is down or the DB
  is unreachable — not "a capability is misconfigured".

- **Admin capability report** — **behind admin auth.** Enumerates each
  integration as **enabled** / **disabled (missing config)** / **unhealthy
  (runtime failure)**. This is where you diagnose "why is DocuSign not working"
  — never `/healthz`. _[The admin report surfaces in P5 with the admin plane; in
  P0 capabilities are inspectable via structured logs.]_

Rule of thumb: **`/healthz` red ⇒ platform/rollback problem** (this runbook §3);
**capability disabled/unhealthy ⇒ credential/config problem** (`CREDENTIALS.md`).

### Reading it

```bash
# Public liveness (no auth).
FQDN=$(az containerapp show -n nda-api -g <rg> \
  --query properties.configuration.ingress.fqdn -o tsv)
curl -i "https://${FQDN}/healthz"

# Live logs (both apps stream to Log Analytics; console tail for quick looks).
az containerapp logs show -n nda-api    -g <rg> --follow
az containerapp logs show -n nda-worker -g <rg> --follow
```

### 1.1 Capability catalog (what each means + fix-it)

The registry evaluates once at boot from `Settings` and is mutated at runtime by
health probes. **disabled** = required config missing (feature politely off, a
normal state); **unhealthy** = configured but failing at runtime. None of these
pull `/healthz` to 503 — only a *critical* capability would, and none are marked
critical today (a provider outage must not restart a healthy container).

| Capability | What it unlocks | Required config (env → Key Vault secret) | disabled ⇒ effect | Fix-it |
|---|---|---|---|---|
| `telemetry_export` | Export traces/logs/metrics to Azure App Insights (OTel) | `APPLICATIONINSIGHTS_CONNECTION_STRING` (plain env, not a secret) | telemetry export is a clean no-op; logs still stream to Log Analytics | set the App Insights connection string env var on both apps, roll a revision. **unhealthy** ⇒ the Azure Monitor exporter refused to start (bad/blocked connection string or missing `azure-monitor-opentelemetry`) — check the string + egress, roll a revision |
| `llm_inference` | All engine review/router LLM calls via the ZDR-pinned OpenRouter gateway | `OPENROUTER_API_KEY` → `openrouter-api-key` | `/v1/reviews` answers `503 no_provider` **unless** `ANTHROPIC_API_KEY` is set (the direct-Anthropic fallback) | seed `openrouter-api-key`, add its `keyVaultSecretRefs` entry, roll a revision. **ZDR fail-closed:** if a model has no ZDR route the *call* errors (terminal, `404 no providers`) rather than downgrading — verify the OpenRouter account data-policy + `OPENROUTER_PROVIDER_ONLY` pin (`CREDENTIALS.md`). Deeper LLM health (breaker open / elevated fallback) shows in the `/healthz` **body**, not its status code |
| `slack` | Slack intake + threaded replies + button/modal interactivity (Bolt) | `SLACK_BOT_TOKEN` → `slack-bot-token` **and** `SLACK_SIGNING_SECRET` → `slack-signing-secret` (both required) | the Slack channel is off; email can still work | seed both secrets, add their refs, roll a revision. `401`/`invalid signature` on inbound = signing-secret drift → rotate `slack-signing-secret`. The v0-HMAC gate, dedup, and allowlist fail *closed* in the bot code — a disabled capability is not the same as a failing gate |
| `email_in` | Worker IMAP UNSEEN poller → normalized into the same envelope | `IMAP_HOST` + `IMAP_USER` + `IMAP_PASSWORD` (→ `imap-password`) | no email intake; Slack + `/v1` unaffected | seed `imap-password` + set the IMAP host/user env vars, roll a revision. Deliverability/login issues: check the mailbox app-password + `IMAP_PORT`/`IMAP_FOLDER`. **DMARC:** with `EMAIL_REQUIRE_DMARC=true` (default) unaligned mail stays read-only-helpful — that is by design, not a fault |
| `email_out` | Threaded SMTP replies + attachments; also admin password-reset mail | `SMTP_HOST` + `SMTP_USER` + `SMTP_PASSWORD` (→ `smtp-password`) | replies can't send by email; password-reset mail won't deliver | seed `smtp-password` + set SMTP host/user env vars, roll a revision. `587` = STARTTLS (`SMTP_SECURE=false`), `465` = implicit TLS (`SMTP_SECURE=true`). From address is `NDA_BOT_FROM_EMAIL`. If Azure egress blocks SMTP, fall back to a transactional provider (ACS/SendGrid) behind the same reply interface (PLAN §10) |
| `tally` _[P3]_ | Tally intake: verified webhook → NDA generation + reply (the external form replaced the in-house `/f` service) | `TALLY_SIGNING_SECRET` (→ `tally-signing-secret`) | the webhook answers 503 (stub); the bot still hands out the form link | seed `tally-signing-secret` (same value as in Tally's webhook settings), roll a revision. Signature failures (401) = secret drift between Tally and the app; also confirm the webhook URL is `https://<api-fqdn>/integrations/tally/webhook` (`CREDENTIALS.md` Tally) |
| `docusign` _[P3]_ | Envelope send for signature (JWT-grant; we fill the docx, DocuSign signs) | `DOCUSIGN_ACCOUNT_ID` + `DOCUSIGN_INTEGRATION_KEY` + `DOCUSIGN_USER_ID` + `DOCUSIGN_PRIVATE_KEY` (→ `docusign-private-key`) | the envelope flow replies "e-signature isn't set up"; everything else works | seed the four fields, roll a revision. **`consent_required`** on first send = the impersonated user hasn't granted JWT consent → open the one-time consent URL (`CREDENTIALS.md`). Demo vs prod is `DOCUSIGN_BASE_URI` + `DOCUSIGN_OAUTH_HOST` |
| `google_drive` _[P4]_ | Archive upload + the cache-folder watcher (auto-naming filed NDAs) | the OAuth trio `GOOGLE_OAUTH_CLIENT_ID` + `GOOGLE_OAUTH_CLIENT_SECRET` (→ `google-oauth-client-secret`) + `GOOGLE_OAUTH_REFRESH_TOKEN` (→ `google-oauth-refresh-token`) **and** `DRIVE_ARCHIVE_FOLDER_ID` | archive + watcher politely turn off (no boot error) | seed the two secrets + set the client id + archive folder id, roll a revision. `invalid_grant` = the offline refresh token was revoked/expired → re-mint it (`CREDENTIALS.md`). The watcher polls the folder named by `DRIVE_CACHE_FOLDER_NAME` (default "Signed Company NDAs Cache") |
| `airtable` _[P4]_ | Expiration-date upsert for signed NDAs (minimal fields) | `AIRTABLE_PAT` (→ `airtable-pat`) + `AIRTABLE_BASE_ID` + `AIRTABLE_TABLE` | expiration tracking is a clean no-op; archive still files | seed `airtable-pat` + set base id + table, roll a revision. The three field names are fixed constants in `app/integrations/airtable.py` (`FIELD_FILE_REF="File Id"`, `FIELD_DISPLAY_NAME="Name"`, `FIELD_EXPIRATION_DATE="Expiration Date"`) — the Airtable table columns MUST match these exactly or upserts 422 (`CREDENTIALS.md`) |

## 1.2 Capability report vs `/healthz`

`/healthz` is the **shallow public liveness** probe (200/503 only, no detail).
The **detailed** per-capability report is admin-gated:
`GET /api/admin/capabilities` (`require_admin`) returns
`{ "app": {version, env}, "capabilities": [{name, state, reason, summary, critical}] }`
— the same rows as the table above, states only (never secret values). The admin
home page renders one card per capability from it. A 401 = not signed in; 403 =
signed in but not `admin`. Use this (not `/healthz`) when triaging "is integration
X wired?" without shelling into the container.

## 2. Common failures

Placeholder rows — expand as real incidents occur.

| Symptom | Likely cause | First check | Fix |
|---|---|---|---|
| `/healthz` 503 right after deploy | new revision unhealthy | `az containerapp revision list` | roll back (§3) |
| `/healthz` 503, revision healthy | DB unreachable | Postgres firewall / `database-url` secret | fix firewall / secret, roll revision |
| `/v1/reviews` → `503 no_provider` | no LLM key configured | `openrouter-api-key` (and Anthropic fallback) seeded + ref'd | seed `openrouter-api-key`, add its `keyVaultSecretRefs` entry, roll revision |
| Deploy aborts at the migrate step | `alembic upgrade` failed | migrate-job logs; is the DB reachable / the migration valid | fix forward; the old revision keeps serving (pre-deploy gate, `AZURE.md §5.3`) |
| First deploy: api never healthy | placeholder image on :80, ingress :8000 | expected pre-CI (`AZURE.md §4`) | push real image via CI |
| Capability shows **disabled** | secret not seeded / not wired | Key Vault + `keyVaultSecretRefs` | seed secret, add ref, roll revision |
| Capability shows **unhealthy** | credential present but rejected | provider-side status + logs | rotate/repair credential (`CREDENTIALS.md`) |
| Image pull fails | AcrPull grant missing / wrong registry | identity role assignment | re-run infra deploy |
| Secret ref fails at revision create | secret absent in Key Vault | `az keyvault secret list` | seed the secret, roll revision |
| Slack requests 401/`invalid signature` _[P2]_ | signing secret drift | `slack-signing-secret` | rotate + roll revision |
| Deploy job skipped (green) | `AZURE_CONFIGURED` not `true` | repo/env variables | set the flag when ready (`AZURE.md §5.2`) |

## 3. Revision rollback (ACA)

Container Apps keeps revisions; rollback is a traffic shift, not a redeploy.

```bash
RG=<rg>; APP=nda-api

# List revisions, newest first, with health + traffic weight.
az containerapp revision list -n "$APP" -g "$RG" \
  --query "reverse(sort_by([].{name:name, active:properties.active, healthy:properties.healthState, created:properties.createdTime, weight:properties.trafficWeight}, &created))" \
  -o table

# Pin 100% traffic to a known-good revision (single-revision mode: activate it).
az containerapp revision activate   -n "$APP" -g "$RG" --revision <good-revision>
az containerapp ingress traffic set -n "$APP" -g "$RG" --revision-weight <good-revision>=100

# Deactivate the bad revision.
az containerapp revision deactivate -n "$APP" -g "$RG" --revision <bad-revision>
```

Do the same for `nda-worker` (no ingress — just activate the good revision and
deactivate the bad one). **Migrations are expand/contract compatible one release
back** (PLAN §3.1), so rolling the app back one revision is safe without a DB
rollback. The migration entrypoint is `python -m app.db_migrate` (chain
`0001_baseline` … `0010_approval_access`); it runs pre-deploy, never at boot
(`AZURE.md §5.3`).

Roll-forward alternative: re-run `deploy-dev.yml` on a fixed commit.

## 4. Routine operations

- **Force a new revision after a secret rotation** (secrets resolve at
  revision-create time — `AZURE.md §6`):
  ```bash
  az containerapp update -n nda-api    -g <rg> --set-env-vars _rotated=$(date +%s)
  az containerapp update -n nda-worker -g <rg> --set-env-vars _rotated=$(date +%s)
  ```
- **Scale note:** both apps are pinned `minReplicas = maxReplicas = 1` (PLAN §2).
  Do **not** raise replicas before clearing the scale-path blockers
  (`AZURE.md §7`). Vertical scale (cpu/memory) is the only safe lever today.
- **Worker one-off / inspect:** `az containerapp exec -n nda-worker -g <rg>
  --command /bin/sh`.

## 4.1 Cache-folder watcher (`nda_cache_processed` lifecycle) — _[P4]_

The worker's watcher polls the Drive cache folder every
`WATCHER_INTERVAL_MINUTES` (default **5** — the old n8n Schedule had `field=minutes`
with no interval, so it effectively hammered every minute; the config default of 5
is the deliberate fix). Each processed file gets exactly one durable row in
`nda_cache_processed` (the dedup + status ledger), so a file is never re-filed:

| Status | Meaning | Operator action |
|---|---|---|
| `processing` | claimed, mid-classification (the initial insert) | none — transient; a row stuck here means the worker crashed mid-run, re-runs recover it |
| `renamed` | classified + filed as `<yyyyMMdd>_<issuer>_<mNDA\|uNDA>_<recipient>.pdf` | none — the happy path |
| `saved_default_name` | classification failed/incomplete → filed under its original name | optional: rename by hand in Drive; the file IS filed, just not auto-named |
| `duplicate_skipped` | an identically-named file already exists in the destination | none — deliberate skip + notify; delete the dupe upstream if it was an error |
| `failed` | the drop couldn't be filed at all | check worker logs + the `google_drive` capability; re-drop the file to retry |

Skip filters ("certificate of completion", `summary.pdf`) never create a row.
"Watcher does nothing" → confirm `google_drive` is **enabled** (§1.1) and the folder
`DRIVE_CACHE_FOLDER_NAME` resolves (it's looked up by name, not id).

## 4.2 Template studio troubleshooting — _[P5]_

- **`studio_stale_view` (409)** on a tokenize/undo/redo call — the client's document
  view is stale (the draft moved since it was extracted; the request's
  `expected_hash` ≠ the live `actual_hash`). This is expected optimistic-concurrency
  behaviour, not corruption: the page must **re-extract the view and retry** the
  operation. It never mutates the stored docx on a stale hash.
- **Oplog undo/redo refuses** (`OpIntegrityError`) — the operations-log record no
  longer matches the document bytes (a tampered record, or undo run against the
  wrong base). Recovery: re-extract the current draft; the undo/redo stack is
  per-draft-version and truncates its redo tail on a new op (standard editor
  semantics). Nothing is published until the admin explicitly publishes.
- **Publish blocked** — a variant publishes only when its scope's required tokens
  are all present (the live checklist). The Slack template-admin flow surfaces the
  same gate in plain English ("missing required token(s): …"); fix the `.docx` in
  Word, re-upload, re-validate.

## 5. n8n / Railway retirement checklist — _[executed in P6]_

The old stack (Railway: Caddy + api + worker + n8n + Postgres) and its 14 n8n
workflows are retired **after** a side-by-side parity week (PLAN §9 P6, §10). Do
not start until P4 has read `Main_Project` and the requester-DM mapping is
verified (PLAN §3.10 gate).

**The two ordering rules that keep this reversible and lossless:**

1. **Export the template bytes FIRST** — while Railway (and its Postgres) is
   still alive. Those `.docx` are the only irreplaceable state; everything else
   is config you can re-point. Do this before touching anything else.
2. **Decommission Railway LAST** — only after the rollback window closes. As long
   as Railway lives you can re-point Slack back and re-enable the n8n workflows in
   minutes. Once it's gone, rollback means a full redeploy.

Work the phases in order. Each box is a gate for the next.

### Phase 0 — Export templates while Railway lives (do this FIRST)

- [ ] Export the 8 logical templates' current `.docx` bytes from the old
      DB/Drive (both variants each — `empty`, `tokenised`). Keep the raw files;
      do **not** rely on Railway staying up.
- [ ] Re-upload each into the new studio at **`/admin/templates`** (admin-authed):
      pick the slot → upload `.docx` → tokenise → **Publish**. The uploader is
      now recorded (`template_version.created_by`, P6) and shown on the list.
- [ ] Verify the generate flow renders a filled NDA from a re-uploaded template
      (one per jurisdiction/counterparty) before proceeding.

### Phase 1 — Parity week (both stacks live, side by side)

- [ ] `Main_Project` envelope-landing behaviour confirmed against the new
      watcher (PLAN §3.10) — the requester-DM keying is real, not assumed.
- [ ] Slack flows verified new-vs-old: mention → review, generate → form link,
      archive → cache → auto-name/file, expiration upsert.
- [ ] Email flows verified new-vs-old: intake (IMAP), threaded reply (SMTP),
      DMARC gate behaviour (unaligned mail stays read-only-helpful).
- [ ] Capability report (`GET /api/admin/capabilities`, §1.2) shows every
      integration **enabled** on the Azure apps.

### Phase 2 — Seed allowlists via the admin UI (before cutover, no day-one lockout)

- [ ] Sender allowlist seeded with the current active users (PLAN §3.4) so the
      first real message isn't bounced to admin approval.
- [ ] **Admin IP allowlist** set if the admin plane should be network-scoped:
      set `ADMIN_IP_ALLOWLIST` (comma/space/semicolon IPs+CIDRs; empty = allow
      all — P6, §config) and roll a revision, **or** leave empty to allow all.
      Confirm your own egress IP is on the list *before* rolling, or you lock
      yourself out of `/admin` (recover by blanking the var + rolling again).

### Phase 3 — Cutover (re-point inbound traffic to Azure)

- [ ] Slack app re-pointed (api.slack.com → your app → **Event Subscriptions**
      Request URL = `https://<nda-api-fqdn>/slack/events`; **Interactivity &
      Shortcuts** Request URL = `https://<nda-api-fqdn>/slack/interactivity`).
      Both must return 200 on Slack's URL-verification challenge (the signing
      secret must already be seeded — §1.1 `slack`).
- [ ] Bot mailbox IMAP/SMTP cut over to the new worker (creds seeded as
      `imap-*`/`smtp-*`; §1.1 `email_in`/`email_out`).
- [ ] DocuSign Connect webhook + any DNS records confirmed pointing at the Azure
      ingress (not Railway/Caddy).

### Phase 4 — Disable n8n workflows, in dependency order (not deleted)

Disable **inbound-first, schedulers-last** so nothing new enters the old pipe
while in-flight work drains, and disable — never delete — so re-enable is instant:

- [ ] 1. **Ingress/trigger** workflows first (Slack webhook, email trigger,
         interactivity) — stops new work entering n8n.
- [ ] 2. **Mid-pipeline** processors (review, generate, envelope, archive) — let
         any in-flight item started before step 1 finish first.
- [ ] 3. **Schedulers/watchers** last (cache-folder watcher, expiration sweep) —
         the new worker now owns these (§4.1).
- [ ] Leave every workflow **disabled, not deleted**, for the whole rollback
      window.

### Phase 5 — Rollback window (keep the monolith restorable)

- [ ] Legacy Railway monolith kept **running and restorable** for the agreed
      window (e.g. one to two weeks). Rollback = re-point the Slack Request URLs
      back to Railway + re-enable the n8n workflows in reverse order (schedulers
      last). No DB rollback needed — the planes are independent.
- [ ] Watch the new stack's logs + capability report daily through the window.

### Phase 6 — Teardown (only after the window closes — Railway LAST)

- [ ] Old `OPENROUTER_API_KEY` in `nda-review-cloud/backend/.env` rotated
      (PLAN §11 ACTION) — it lived in the old repo.
- [ ] n8n Postgres DB retired (the SIGNED plane + `/v1/support_task/bot/*` DAL
      are already dropped by design — PLAN §3.1). **Confirm Phase 0 templates
      are safely in the new studio first** — this is the point of no return for
      the old data.
- [ ] Decommission the Railway services (Caddy + api + worker + n8n + Postgres)
      — **the last action**, once nothing above still depends on them.
