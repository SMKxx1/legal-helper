# Credentials & secrets

Living document. One section per external service. Each states **where to create
the credential, the exact scopes/permissions, the identifier triple (Key Vault
secret ↔ env var ↔ local `.env` key), the capability it unlocks, and the
rotation procedure.** Remaining `TODO` markers are open decisions, not unwired phases.

## Conventions

- **Secret naming** (see `AZURE.md §3.2`): the Key Vault secret name is the
  **kebab-case of the env var**. `OPENROUTER_API_KEY` → `openrouter-api-key`.
- **Secret values never live in the repo.** In Azure they live in Key Vault
  (seeded with `az keyvault secret set`, RBAC data-plane). Locally they live in
  a git-ignored `backend/.env` (only `*.env.example` is committed).
- **Capability states** (`PLAN §6`): a missing credential leaves its capability
  **disabled** (feature politely off), never a boot error. A present-but-broken
  credential is **unhealthy** and shows in the admin capability report
  (`RUNBOOK.md §1`).
- To wire a secret into the apps, add it to `keyVaultSecretRefs` in the
  environment's `.bicepparam` (`{ envVarName, secretName }`) and roll a revision.

## Quick index

| Service | Capability | Phase | Key Vault secrets |
|---|---|---|---|
| OpenRouter (ZDR) | LLM inference (review, routing, classify, expiration) | P1 | `openrouter-api-key` |
| Anthropic (fallback) | direct-Anthropic adapter fallback | P1 | `anthropic-api-key` |
| Engine service keys | `/v1` machine auth (add-in, API callers) | P1 | `engine-api-key` |
| Postgres | app database | P0 seed / P1 use | `database-url` |
| Settings encryption | Fernet-at-rest for UI-editable secrets | P5 | `settings-encryption-key` |
| Slack app | `slack` — intake + replies + interactivity | P2 | `slack-signing-secret`, `slack-bot-token` |
| SMTP / IMAP | `email_in` + `email_out` — intake, replies, reset mail | P2 | `imap-password`, `smtp-password` |
| DocuSign | `docusign` — envelope send (JWT grant + consent) | P3 | `docusign-private-key` |
| Tally intake | `tally` — external NDA form + signed webhook | P3 | `tally-signing-secret` |
| Google Drive | `google_drive` — archive storage + watcher | P4 | `google-oauth-refresh-token`, `google-oauth-client-secret` |
| Airtable | `airtable` — expiration-date upsert | P4 | `airtable-pat` |
| Admin bootstrap | first admin login | P5 | `admin-bootstrap-password` |

---

## OpenRouter (ZDR) — _[wired in P1]_

Primary LLM provider, Zero-Data-Retention across all aliases (owner decision,
re-confirmed 2026-07-03; PLAN §2, §3.8, §6).

- **Where:** <https://openrouter.ai> → Keys. Create an org account (not a
  personal one) so the data policy is org-scoped.
- **ZDR account policy (required, do first):** in **Settings → Privacy**, set the
  account data policy to **deny** prompt logging/training and enable
  zero-retention routing. This is enforced again per-request (provider pinning,
  `data_collection: 'deny'`, `zdr: true`, `allow_fallbacks: false`) — the adapter
  **fails closed** if a model has no ZDR route (PLAN §3.8). The account policy is
  the outer guard; the per-request pin is the inner guard.
- **BYOK option:** attach your own Anthropic key to OpenRouter (Integrations →
  BYOK) so Anthropic-family tiers bill/route through your own Anthropic contract
  while still transiting OpenRouter. This is an **account-level dashboard
  setting — there is no env var and nothing to configure per request** (the
  adapter reads BYOK spend back via `usage.cost` +
  `cost_details.upstream_inference_cost`, so reported cost stays authoritative
  either way). Optional; documented as a hardening lever.
- **DPA chain (PLAN §6):** OpenRouter is a broker. The data-processing chain is
  *you → OpenRouter → the pinned upstream provider*. Record OpenRouter's DPA and
  the upstream provider's DPA (e.g. Anthropic, Google Vertex for the `expiration`
  alias) so the chain is auditable. The ZDR residual is rated **Medium** and
  carried honestly.
- **Scopes:** a single API key with credit/limits set at the org level. Per-tier
  budgets + monthly caps are enforced in-app (`ENGINE_*` knobs), not by OpenRouter.
- **Model tiers wired today (P1):** the engine gateway pins three tiers, each an
  env var holding a vendor-namespaced OpenRouter model id
  (`routes_v1._build_gateways`, `app/config.py`):

  | Tier | Env var | Default model id |
  |---|---|---|
  | Deep reviewer | `OPENROUTER_MODEL_REVIEW_DEEP` | `anthropic/claude-opus-4-8` |
  | Quick reviewer | `OPENROUTER_MODEL_REVIEW_QUICK` | `anthropic/claude-sonnet-4-6` |
  | T0 doc-classifier router | `OPENROUTER_MODEL_ROUTER` | `anthropic/claude-haiku-4-5` |

  The `expiration` alias is also wired (`OPENROUTER_MODEL_EXPIRATION`, default
  `google/gemini-3.5-flash`, pinned to `google-vertex` via
  `EXPIRATION_PROVIDER_ONLY` — PLAN §3.8). The bot classifier reuses the router
  tier rather than carrying its own alias.

**Full env-var set (all optional; safe defaults in `config.py`):**

| Env var | Meaning | Default |
|---|---|---|
| `OPENROUTER_API_KEY` | the key; **present ⇒ `llm_inference` enabled**, all engine tiers route through OpenRouter | `""` (feature off / falls back to Anthropic) |
| `OPENROUTER_BASE_URL` | API base | `https://openrouter.ai/api/v1` |
| `OPENROUTER_ZDR_ONLY` | ZDR fail-closed routing (`data_collection:deny`, `zdr:true`, `allow_fallbacks:false`) | `true` |
| `OPENROUTER_PROVIDER_ONLY` | optional comma-separated provider pin sent as `provider.only` (also disables fallbacks) | `""` (any ZDR-qualifying route) |
| `OPENROUTER_PROVIDER_ONLY_DEEP` | deep-tier provider pin — opus-4-8's default ZDR route (Bedrock) rejects `json_schema` and 400s deep reviews; Vertex accepts it | `google-vertex` |
| `OPENROUTER_MODEL_REVIEW_DEEP` / `_QUICK` / `_ROUTER` | per-tier model ids (table above) | as shown |

| Identifier | Value |
|---|---|
| Key Vault secret | `openrouter-api-key` |
| Env var | `OPENROUTER_API_KEY` |
| `.env` key | `OPENROUTER_API_KEY` |
| Capability | `llm_inference` — all engine (and later bot) LLM calls |

- **Rotation:** create a new key in OpenRouter → `az keyvault secret set
  --name openrouter-api-key --value <new>` → roll a revision (`AZURE.md §6`) →
  delete the old key in OpenRouter. **ACTION carried from discovery (PLAN §11):**
  rotate the unused live `OPENROUTER_API_KEY` found in the old
  `nda-review-cloud/backend/.env`.

## Anthropic (direct fallback) — _[wired in P1]_

The ported direct-Anthropic adapter stays available as a per-alias configuration
fallback (PLAN §2, §3.8). Optional.

- **Where:** <https://console.anthropic.com> → API Keys.
- **Scopes:** standard API key.

| Identifier | Value |
|---|---|
| Key Vault secret | `anthropic-api-key` |
| Env var | `ANTHROPIC_API_KEY` |
| `.env` key | `ANTHROPIC_API_KEY` |
| Capability | direct-Anthropic fallback route |

- **Rotation:** rotate in the console, update the secret, roll a revision.

## Engine service keys — _[wired in P1]_

Machine auth for the `/v1` engine plane (Word add-in + direct API callers).
`ENGINE_REQUIRE_KEY=true` in prod (fail closed if no usable key). The retired
SIGNED/HMAC plane and `/v1/support_task/bot/*` DAL are **not** carried over
(PLAN §3.1) — no `AUTH_PRINCIPAL_HMAC_KEY`.

- **Where:** self-issued. `engine-api-key` is the bootstrap default key (maps to
  `svc:default`). Named service keys are minted later from the admin plane
  (DB-backed, scoped, per-key rate/cost caps).

| Identifier | Value |
|---|---|
| Key Vault secret | `engine-api-key` |
| Env var | `ENGINE_API_KEY` |
| `.env` key | `ENGINE_API_KEY` |
| Capability | `/v1` machine auth |

- **Rotation:** mint a new key value, update the secret, roll a revision;
  update the add-in's configured key in lockstep. TODO(P6): add-in key scoped
  review-only with per-key caps + CORS pinned to the add-in origin (PLAN §6).

## Postgres — _[seeded P0, used P1]_

Connection string for the app database (`AZURE.md`, `deploy/azure/README.md`).

| Identifier | Value |
|---|---|
| Key Vault secret | `database-url` |
| Env var | `DATABASE_URL` |
| `.env` key | `DATABASE_URL` (local dev defaults to SQLite) |
| Capability | persistence (reserved in P0) |

- **Value shape:** `postgresql+psycopg2://ndaadmin:<pw>@<server>.postgres.database.azure.com:5432/nda?sslmode=require`
- **Rotation:** reset the server admin password (`az postgres flexible-server
  update --admin-password`), update `database-url`, roll a revision. TODO(P1):
  prefer a least-privilege application role over the admin login.

## Settings encryption key — _[wired in P5]_

Fernet key encrypting UI-editable secret settings at rest (`enc:v1:` prefix). It
must stay **stable** across revisions or previously-encrypted values become
unreadable (`AZURE.md §6`).

| Identifier | Value |
|---|---|
| Key Vault secret | `settings-encryption-key` |
| Env var | `SETTINGS_ENCRYPTION_KEY` |
| `.env` key | `SETTINGS_ENCRYPTION_KEY` |
| Capability | encrypt-at-rest for UI settings |

- **Generate:** `python -c "from cryptography.fernet import Fernet;
  print(Fernet.generate_key().decode())"`.
- **Rotation:** deliberate only — re-encrypt affected settings during the change.

## Slack app — _[wired in P2]_

- **Where:** <https://api.slack.com/apps> → Create New App (from manifest
  preferred). Separate **dev** and **prod** apps (PLAN §10) — prod cutover is a
  URL re-point.
- **Request URLs (HTTP, public ingress):** Event Subscriptions and
  Interactivity both point at `https://<nda-api-fqdn>/slack/events` and
  `/slack/interactivity` (paths confirmed against `app/bot/channels/slack.py`). Signature-verified
  (Slack v0 HMAC, 300s replay window, fail-closed — PLAN §6).
- **Bot token scopes (OAuth & Permissions):** `app_mentions:read`,
  `channels:history`, `groups:history`, `im:history`, `chat:write`,
  `files:read`, `files:write`, `users:read`, `users:read.email`, `commands`
  (for slash commands). This is the final set — the live app is configured with it.
- **Event subscriptions:** `app_mention`, `message.im`, `message.channels`.

| Purpose | Key Vault secret | Env var | Notes |
|---|---|---|---|
| Signing secret | `slack-signing-secret` | `SLACK_SIGNING_SECRET` | v0 HMAC verify (fail-closed) — **required** for the `slack` capability |
| Bot OAuth token | `slack-bot-token` | `SLACK_BOT_TOKEN` | `xoxb-…` Web API calls — **required** for the `slack` capability |
| Bot's own user id | — (non-secret) | `NDA_BOT_USER_ID` | the human-event guard drops the bot's own messages |
| Bot From address | — (non-secret) | `NDA_BOT_FROM_EMAIL` | outbound email From (default `nda-bot@example.com`) |

- **Capability:** `slack` — enabled only when **both** `SLACK_BOT_TOKEN` and
  `SLACK_SIGNING_SECRET` are set; absent ⇒ the Slack channel is politely off
  (`capabilities.py`). Gates layered on top (v0 HMAC signature, dedup, allowlist)
  fail *closed* in the bot's transactional code.
- **Rotation:** regenerate the signing secret / reinstall for a new bot token in
  the Slack app config; update secrets; roll a revision. Signing-secret rotation
  is momentarily fail-closed for in-flight requests (acceptable).

## SMTP / IMAP (bot mailbox) — _[wired in P2]_

Email is a first-class channel (intake + replies) and carries password-reset
mail. DMARC/DKIM alignment is required for any allowlisted or action-triggering
sender (PLAN §3.3, §6) — unauthenticated mail is read-only-helpful.

- **Where:** the bot mailbox provider (TODO(P2): provider + address). Deliverability
  from Azure is validated in P2; a transactional-provider fallback (ACS/SendGrid)
  sits behind the same reply-service interface (PLAN §10).

Env var names match `app/config.py` exactly (they are `IMAP_USER`/`SMTP_USER`,
**not** `…_USERNAME`; there is no `SMTP_FROM` — the outbound From address is
`NDA_BOT_FROM_EMAIL` in the Slack section above).

| Purpose | Key Vault secret | Env var | Default |
|---|---|---|---|
| IMAP host | — (non-secret) | `IMAP_HOST` | `""` |
| IMAP port | — | `IMAP_PORT` | `993` (IMAPS) |
| IMAP user | — | `IMAP_USER` | `""` |
| IMAP password | `imap-password` | `IMAP_PASSWORD` | `""` |
| IMAP folder | — | `IMAP_FOLDER` | `INBOX` |
| SMTP host | — | `SMTP_HOST` | `""` |
| SMTP port | — | `SMTP_PORT` | `587` (STARTTLS) |
| SMTP implicit TLS | — | `SMTP_SECURE` | `false` (set `true` for 465) |
| SMTP user | — | `SMTP_USER` | `""` |
| SMTP password | `smtp-password` | `SMTP_PASSWORD` | `""` |

- **Capabilities:** `email_in` — enabled when `IMAP_HOST` + `IMAP_USER` +
  `IMAP_PASSWORD` are set (worker UNSEEN poller). `email_out` — enabled when
  `SMTP_HOST` + `SMTP_USER` + `SMTP_PASSWORD` are set (threaded replies +
  attachments; also carries admin password-reset mail, a stub in the old engine,
  wired real here). Either absent ⇒ that channel is politely off.
- **Scopes:** an app password or dedicated mailbox credential; least-privilege
  (single mailbox). **Rotation:** rotate the mailbox app password, update the two
  secrets, roll a revision.

### Admin routing & email-sender policy (same P2 config group)

Non-secret env keys that govern approvals routing and the email-trust gate
(`config.py`; PLAN §3.3, §3.4, §6):

| Env var | Meaning | Default |
|---|---|---|
| `NDA_ADMIN_SLACK_CHANNEL` | channel id where allowlist-miss approvals are announced | `""` |
| `NDA_ADMIN_EMAIL` | admin email for approval notifications | `""` |
| `EMAIL_REQUIRE_DMARC` | require SPF/DKIM/DMARC alignment before an email sender is **verified** (can match the allowlist / trigger envelope+archive) | `true` |
| `BOT_INBOX_SWEEP_SECONDS` | worker sweep cadence for stuck/failed `bot_inbox` rows | `30` |

## DocuSign — _[wired in P3]_

JWT-grant integration (we fill the docx; DocuSign only signs — PLAN §2). The
≥2-signer path gains an explicit human confirm before send (PLAN §3.9).

- **Where:** DocuSign Admin → Apps and Keys. Create an integration key, an RSA
  keypair (JWT grant), and grant consent. Demo (`demo.docusign.net`) first, then
  prod (PLAN §11 timeline TODO).
- **Scopes:** `signature`, `impersonation` (JWT). Connect webhook for envelope
  status (HMAC-signed).

The four **required** fields for the `docusign` capability (verified against
`app/config.py`) are the account id, integration key, impersonated user id, and the
RSA private key; the base URI + OAuth host select demo vs prod.

| Purpose | Key Vault secret | Env var | Default / note |
|---|---|---|---|
| Base URI | — | `DOCUSIGN_BASE_URI` | `https://demo.docusign.net` (prod: `https://www.docusign.net`) |
| OAuth host | — | `DOCUSIGN_OAUTH_HOST` | `account-d.docusign.com` (prod: `account.docusign.com`) |
| Account id | — | `DOCUSIGN_ACCOUNT_ID` | required |
| Integration key | — | `DOCUSIGN_INTEGRATION_KEY` | required (the JWT client id) |
| Impersonated user id | — | `DOCUSIGN_USER_ID` | required — the JWT `sub` (the API user we impersonate) |
| RSA private key (PEM, RS256) | `docusign-private-key` | `DOCUSIGN_PRIVATE_KEY` | required |

- **Consent (one-time, easy to miss):** JWT `impersonation` grant needs the
  impersonated user to have granted consent to the integration key **once**. The
  first send after setup fails with `consent_required` until you open the consent
  URL and approve — e.g.
  `https://<oauth-host>/oauth/auth?response_type=code&scope=signature%20impersonation&client_id=<integration-key>&redirect_uri=<any-registered-uri>`
  in a browser signed in as `DOCUSIGN_USER_ID`. This is a person-in-the-loop step,
  not a secret to seed.
- **Capability:** envelope create/send. (A signed Connect webhook for status intake
  is planned — no `Settings` field exists for it yet; when added it will carry an
  HMAC secret.)
- **Rotation:** generate a new RSA keypair in DocuSign, update
  `docusign-private-key`, roll a revision.

## Tally intake (external form + signed webhook) — _[wired in P3, replaced the in-house `/f` form]_

The NDA intake form lives on **Tally** (<https://tally.so>, form "NDA Generator");
the in-house `/f` form service was retired. The bot's generate intent hands out a
channel-prefilled link; on submit Tally POSTs a signed webhook to
`/integrations/tally/webhook`, which verifies the HMAC signature, maps the fields
and generates the NDA. `TALLY_SIGNING_SECRET` gates the `tally` capability;
absent, the webhook is a 503 stub (the generate intent still hands out the link).

- **Where:** Tally → the form → Integrations → Webhooks. Point the webhook at
  `https://<api-host>/integrations/tally/webhook` and set a signing secret; store
  the same value as the env var / KV secret below.

| Purpose | Key Vault secret | Env var | Default |
|---|---|---|---|
| Webhook HMAC signing secret | `tally-signing-secret` | `TALLY_SIGNING_SECRET` | `""` (capability off) |
| Form id (public link path) | — | `TALLY_FORM_ID` | `jagDPJ` |
| Form host | — | `TALLY_BASE_URL` | `https://tally.so` |

- **Rotation:** set a new secret in Tally's webhook settings and update the KV
  secret in the same change; in-flight submissions signed with the old secret are
  rejected (re-submit the form).
- `FORM_BASE_URL` survives as an optional, capability-free base URL for absolute
  outbound links (password-reset emails); unset, those degrade to relative paths.

## Google Drive (storage provider) — _[wired in P4]_

Archive destination now; SharePoint later behind the same interface (PLAN §2).

- **Where:** Google Cloud Console → APIs & Services → Credentials. Enable the
  Drive API; create an OAuth client and obtain a refresh token for the archive
  account (the deployed choice — a service account with domain-wide delegation
  remains a possible later swap behind the same interface).
- **Scopes:** `https://www.googleapis.com/auth/drive.file` (files the app
  creates/opens) — widen to `drive` only if the watcher must read pre-existing
  folders.

The OAuth **trio** authenticates; the folder ids/name tell the archive + watcher
where to write and poll (verified against `app/config.py`).

| Purpose | Key Vault secret | Env var | Note |
|---|---|---|---|
| OAuth client id | — | `GOOGLE_OAUTH_CLIENT_ID` | required |
| OAuth client secret | `google-oauth-client-secret` | `GOOGLE_OAUTH_CLIENT_SECRET` | required |
| OAuth refresh token (offline grant) | `google-oauth-refresh-token` | `GOOGLE_OAUTH_REFRESH_TOKEN` | required — mints access tokens |
| Archive (destination) folder id | — | `DRIVE_ARCHIVE_FOLDER_ID` | required — where filed, auto-named signed NDAs land (the old n8n hard-coded "Signed Company NDAs", now configurable) |
| Cache folder name (watcher source) | — | `DRIVE_CACHE_FOLDER_NAME` | default `"Signed Company NDAs Cache"` — resolved **by name** (Drive folder query), not id |

- **Capability:** `google_drive` (archive upload + cache-folder watcher). The trio
  **and** `DRIVE_ARCHIVE_FOLDER_ID` are all required; absent any, archive + watcher
  turn politely off (never a boot error).
- **Rotation:** revoke + re-consent for a new refresh token (`invalid_grant` on use
  = it was revoked/expired); rotate the client secret in the console; update secrets;
  roll a revision.

## Airtable — _[wired in P4]_

Expiration-date upsert target. **Minimal fields only** (dates + file reference,
not full party payloads — PLAN §6); note Airtable's DPA.

- **Where:** <https://airtable.com/create/tokens> → personal access token, scoped
  to the one base.
- **Scopes:** `data.records:read`, `data.records:write`, `schema.bases:read`,
  restricted to the target base only.

| Purpose | Key Vault secret | Env var |
|---|---|---|
| Personal Access Token | `airtable-pat` | `AIRTABLE_PAT` |
| Base id (`appXXXXXXXXXXXXXX`) | — | `AIRTABLE_BASE_ID` |
| Table id/name | — | `AIRTABLE_TABLE` |

- **`FIELD_*` constants — the table columns MUST match exactly.** The upsert writes
  three fixed field names, hard-coded in `app/integrations/airtable.py` (they are
  NOT configurable — the Airtable table has to use these exact column labels or the
  upsert 422s):
  - `FIELD_FILE_REF = "File Id"` — the Drive file reference (the record key).
  - `FIELD_DISPLAY_NAME = "Name"` — the display name.
  - `FIELD_EXPIRATION_DATE = "Expiration Date"` — the extracted date.
- **Capability:** `airtable` expiration upsert (capability-gated; off ⇒ feature
  politely off). Minimal fields only — no full party payloads (PLAN §6).
- **Config TODO (PLAN §11):** exact base/table ids pending; the field labels above
  are fixed by the code.
- **Rotation:** regenerate the PAT, update `airtable-pat`, roll a revision.

## Admin bootstrap — _[wired in P5]_

The first admin account for the `/admin` plane (hardened login: rate limit +
lockout, strict cookies, single-use reset tokens — PLAN §6).

| Purpose | Key Vault secret | Env var |
|---|---|---|
| Bootstrap user id | `admin-bootstrap-user-id` | `ADMIN_BOOTSTRAP_USER_ID` |
| Bootstrap temp password | `admin-bootstrap-password` | `ADMIN_BOOTSTRAP_PASSWORD` |

- **Capability:** first admin login (must-change-password on first use).
- **Allowlist + roles (PLAN §3.4):** managed from the dashboard at `/admin/access`
  (allowlist CRUD, pending approval requests, admin channel/email routing) — not
  by env vars. Approving a request auto-adds the requester as a member.
- **Rotation:** normal password-change flow once logged in; the bootstrap secret
  is only for first login and can be cleared afterward.
