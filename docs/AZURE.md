# Azure — infrastructure, deployment, and operations

Living document. **Update it in the same PR as any change it describes.** The
authoritative plan is `docs/PLAN.md` (§3.1 topology, §6 security, §8 CI/CD);
this doc is how that plan is actually wired in Azure. The exact command sequence
lives in `deploy/azure/README.md` — this file is the narrative + inventory.

Status: **all phases (P0–P6) shipped; a dev environment is live** (resource
group `rg-ndaassist-dev`: ACA apps `nda-api` + `nda-worker`, ACR, Key Vault,
Postgres Flexible Server, App Insights). Alembic migrations run `0001`–`0010`.
Phase tags on sections below record when each piece landed — history, not status.

---

## 1. Topology

Per environment (dev, prod) there is one resource group holding one Container
Apps environment running two always-on apps off one image:

- **nda-api** — external ingress on :8000 (FastAPI). Slack, form pages, the Word
  add-in, admin UI, and `/healthz` all land here.
- **nda-worker** — no ingress. Same image, command override `python -m
  app.worker`. IMAP poller, review-job claimer, scheduled sweeps, docx→PDF.

Both mount the same Azure Files share at `/data`, read secrets from Key Vault
through one user-assigned managed identity, and log to one Log Analytics
workspace. `minReplicas = 1` on both — Slack's 3s ack and the IMAP poller rule
out scale-to-zero (PLAN §2).

```
Internet ──▶ nda-api (:8000, ingress) ─┐
                                        ├─▶ Key Vault (secrets, via UAMI)
             nda-worker (no ingress) ───┘   ACR (image, via UAMI AcrPull)
                    │  │                     Postgres Flexible Server
                    │  └── /data ── Azure Files share
                    └────────────── Log Analytics ◀── App Insights
```

## 2. Resource inventory

Names derive from `namePrefix` + `environment` (e.g. `ndaassist-dev`). Globally
unique names (vault, registry, storage) append a deterministic
`uniqueString(resourceGroup().id)` suffix.

| Resource | Type | Name pattern | Why it exists |
|---|---|---|---|
| Log Analytics | `Microsoft.OperationalInsights/workspaces` | `log-<base>` | central log + telemetry sink |
| App Insights | `Microsoft.Insights/components` | `appi-<base>` | app telemetry (OTel export target) |
| Managed identity | `Microsoft.ManagedIdentity/userAssignedIdentities` | `id-<base>` | one identity for KV + ACR (bootstrap-safe) |
| Key Vault | `Microsoft.KeyVault/vaults` | `kv-<base><hash>` | secrets; RBAC mode; no secret in IaC |
| Container Registry | `Microsoft.ContainerRegistry/registries` | `acr<base><hash>` | app images; admin user disabled |
| Postgres | `Microsoft.DBforPostgreSQL/flexibleServers` | `psql-<base>` | app database (`nda`); fresh per PLAN §2 |
| Storage account | `Microsoft.Storage/storageAccounts` | `st<base><hash>` | backs the Azure Files share |
| File share | `.../fileServices/shares` | `data` | `/data` volume (uploads, exports, templates) |
| Container Apps env | `Microsoft.App/managedEnvironments` | `cae-<base>` | runtime; wired to Log Analytics |
| API app | `Microsoft.App/containerApps` | `nda-api` | public ingress :8000 |
| Worker app | `Microsoft.App/containerApps` | `nda-worker` | no ingress; command override |

**Role assignments** (created with the vault/registry, before the apps):

| Grant | Role id | Scope |
|---|---|---|
| Key Vault Secrets User | `4633458b-17de-408a-b874-0445c86b69e6` | the vault |
| AcrPull | `7f951dda-4ed3-4680-a7ca-43fe172d538d` | the registry |

## 3. Configuration model

### 3.1 Plain env vars (set directly on both apps)

| Var | Value | Meaning |
|---|---|---|
| `APP_ENV` | `dev` \| `prod` | environment selector (`test` used only in CI) |
| `LOG_LEVEL` | `DEBUG` (dev) / `INFO` (prod) | log verbosity |
| `LOG_FORMAT` | `console` (dev) / `json` (prod) | log rendering |
| `DATA_DIR` | `/data` | Azure Files mount path |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | from App Insights | telemetry export; **absent ⇒ export capability disabled, never an error** |

`LOG_LEVEL`/`LOG_FORMAT` are computed from `environment` when their params are
left empty; prod pins them explicitly in `parameters.prod.bicepparam`.

### 3.2 Key Vault secret naming convention

**A secret's name is the kebab-case of its env var.** This mapping is used
everywhere (Bicep `keyVaultSecretRefs`, the seeding commands, `CREDENTIALS.md`):

| Env var | Key Vault secret | `.env` key (local dev) |
|---|---|---|
| `DATABASE_URL` | `database-url` | `DATABASE_URL` |
| `OPENROUTER_API_KEY` | `openrouter-api-key` | `OPENROUTER_API_KEY` |
| `SLACK_SIGNING_SECRET` | `slack-signing-secret` | `SLACK_SIGNING_SECRET` |

Secret **values** never live in Bicep or the repo. They are seeded with
`az keyvault secret set` (Key Vault is RBAC-mode, so the operator needs the
data-plane `Key Vault Secrets Officer` role). The apps consume them through the
`keyVaultSecretRefs` parameter, which projects each `{ envVarName, secretName }`
as a Container Apps secret reference resolved by the user-assigned identity.

**`DATABASE_URL` is real in P1**: `app/config.py` reads it (normalizing a bare
`postgres://`/`postgresql://` to the pinned `postgresql+psycopg2://` driver) and
`app/db.py` builds the engine from it, so the secret ref now drives live
persistence — no longer "reserved". To activate it, seed the secret then set the
ref in the environment's `.bicepparam`:

```bicep
// deploy/azure/parameters.dev.bicepparam
param keyVaultSecretRefs = [
  { envVarName: 'DATABASE_URL', secretName: 'database-url' }
  // P1 also: { envVarName: 'OPENROUTER_API_KEY', secretName: 'openrouter-api-key' }
  //          { envVarName: 'ENGINE_API_KEY',     secretName: 'engine-api-key' }
]
```

`main.bicep` passes this array to both apps; `modules/containerapp.bicep`
projects each `{ envVarName, secretName }` as a Container Apps secret (name
lower-cased) plus the matching env var, resolved by the user-assigned identity.
The committed dev `.bicepparam` still defaults to `keyVaultSecretRefs = []`
(example commented) so the apps boot on SQLite with zero seeded secrets — flip it
once `database-url` exists in Key Vault (`AZURE.md §4` step 6).

### 3.3 Capability states (PLAN §6)

Every integration reports one of: **enabled** (config present, healthy) /
**disabled** (config missing — feature politely off) / **unhealthy** (config
present, runtime failing). Missing config **never crashes boot**. Gates
(signatures, allowlist, dedup, ZDR routing) fail *closed*; capabilities fail
*soft*. `APPLICATIONINSIGHTS_CONNECTION_STRING` is the canonical example: unset
locally ⇒ telemetry export **disabled**; set in Azure ⇒ **enabled**.

Registered capabilities today (`app/capabilities.py`) and the env keys that
enable each — see `RUNBOOK.md §1` for what each means and its fix-it steps:

| Capability | Required env keys | Phase |
|---|---|---|
| `telemetry_export` | `APPLICATIONINSIGHTS_CONNECTION_STRING` | P0 |
| `llm_inference` | `OPENROUTER_API_KEY` | P1 |
| `slack` | `SLACK_BOT_TOKEN` + `SLACK_SIGNING_SECRET` | P2 |
| `email_in` | `IMAP_HOST` + `IMAP_USER` + `IMAP_PASSWORD` | P2 |
| `email_out` | `SMTP_HOST` + `SMTP_USER` + `SMTP_PASSWORD` | P2 |
| `tally` | `TALLY_SIGNING_SECRET` (webhook HMAC; the external Tally form replaced the in-house `/f` service) | P3 |
| `docusign` | `DOCUSIGN_ACCOUNT_ID` + `DOCUSIGN_INTEGRATION_KEY` + `DOCUSIGN_USER_ID` + `DOCUSIGN_PRIVATE_KEY` | P3 |
| `google_drive` | `GOOGLE_OAUTH_CLIENT_ID` + `GOOGLE_OAUTH_CLIENT_SECRET` + `GOOGLE_OAUTH_REFRESH_TOKEN` + `DRIVE_ARCHIVE_FOLDER_ID` | P4 |
| `airtable` | `AIRTABLE_PAT` + `AIRTABLE_BASE_ID` + `AIRTABLE_TABLE` | P4 |

### 3.4 Env-var additions by phase

Each phase adds config keys. The **secret-bearing** ones follow the kebab-case
naming (§3.2) and get a `keyVaultSecretRefs` entry; the plain ones (ids, hosts,
folder ids, booleans) are set directly on both apps. Verified against
`app/config.py` — see `CREDENTIALS.md` for where each value comes from.

| Phase | New env keys | Secret (→ Key Vault) vs plain |
|---|---|---|
| **P3** | `TALLY_SIGNING_SECRET` (+ optional `TALLY_FORM_ID`, `TALLY_BASE_URL`, `FORM_BASE_URL` for absolute outbound links); `DOCUSIGN_BASE_URI`, `DOCUSIGN_OAUTH_HOST`, `DOCUSIGN_ACCOUNT_ID`, `DOCUSIGN_INTEGRATION_KEY`, `DOCUSIGN_USER_ID`, `DOCUSIGN_PRIVATE_KEY` | secrets: `tally-signing-secret`, `docusign-private-key`; rest plain |
| **P4** | `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REFRESH_TOKEN`, `DRIVE_ARCHIVE_FOLDER_ID`, `DRIVE_CACHE_FOLDER_NAME`, `WATCHER_INTERVAL_MINUTES`; `AIRTABLE_PAT`, `AIRTABLE_BASE_ID`, `AIRTABLE_TABLE` | secrets: `google-oauth-client-secret`, `google-oauth-refresh-token`, `airtable-pat`; rest plain |
| **P5** | `SETTINGS_ENCRYPTION_KEY` (Fernet-at-rest for UI-editable secrets); `ADMIN_BOOTSTRAP_USER_ID`, `ADMIN_BOOTSTRAP_PASSWORD`; `NDA_ADMIN_SLACK_CHANNEL`, `NDA_ADMIN_EMAIL` (admin routing + the template-admin / approvals gate) | secrets: `settings-encryption-key`, `admin-bootstrap-password`; rest plain |

`WATCHER_INTERVAL_MINUTES` defaults to `5` (replacing the old n8n 1-minute
cadence) and `DRIVE_CACHE_FOLDER_NAME` to `"Signed Company NDAs Cache"`, so P4 boots
sensibly with only the OAuth trio + `DRIVE_ARCHIVE_FOLDER_ID` set. `NDA_ADMIN_*`
are only the **env fallback** for admin routing — since P6 the admin channel/email
(and the allowlist itself) are managed from the dashboard at `/admin/access`
(a `settings_store` override that wins over the env value).

## 4. First-time bootstrap (narrative)

Full commands: `deploy/azure/README.md`. The shape:

1. **Prereqs** — `az login`, select subscription, register resource providers,
   `az bicep install`.
2. **Resource group** — `az group create` in `southeastasia`.
3. **Rights check** — the deployer needs **Owner** (or **Contributor + User
   Access Administrator**) because the template creates role assignments. This
   is the most common first-deploy failure.
4. **Password** — `export PG_ADMIN_PASSWORD=…`; the `.bicepparam` reads it from
   the environment so it never touches the repo.
5. **Deploy** — `az deployment group create` with the env's `.bicepparam`. The
   first deploy stands up everything on a **placeholder public image**; `nda-api`
   will not pass `/healthz` on :8000 until CI pushes the real image. Expected.
6. **Seed secrets** — grant yourself `Key Vault Secrets Officer`, then
   `az keyvault secret set database-url …`.
7. **First real deploy** — CI (below) builds the image into ACR and rolls both
   apps onto it; `/healthz` goes green. This is the P0 acceptance test:
   *"hello /healthz deployed to dev via Actions."*

## 5. CI/CD

Three workflows in `.github/workflows/`:

- **`ci.yml`** — push + PR. Python 3.13, pip cache, `ruff format --check`,
  `ruff check`, `mypy app` (blocking), `pytest`, all under `backend/`. Green CI
  on master is the gate the dev deploy waits for. A second `addin` job checks
  `word-addin/` (prettier `format:check` + `node --test`).
  - **Exit-code propagation (`set -o pipefail`).** The full local gate is
    `make check` (lint + type + test) run from the repo root. Any CI step that
    pipes the gate's output — e.g. `make check | tee build.log`, or a `make check
    2>&1 | <formatter>` — MUST run under `set -o pipefail` (or capture
    `${PIPESTATUS[0]}`), otherwise the pipeline reports the exit code of the LAST
    command in the pipe (usually `tee`/the formatter, always 0) and a red
    `make`/`pytest` is silently swallowed → a broken build deploys green. GitHub's
    `run:` blocks default to `bash -e` but **not** `pipefail`; add
    `shell: bash` + `set -o pipefail` (or a top-level `defaults.run.shell: bash`
    with an explicit `set -o pipefail` in any piped step). The same rule applies to
    the pre-deploy migration + smoke steps: never `| tee` a gate without pipefail.
- **`deploy-dev.yml`** — triggers via `workflow_run` after CI succeeds on
  master. Builds the image in ACR (`az acr build`, tagged with the commit sha),
  runs a placeholder migration step _[real in P1]_, then `az containerapp
  update` for `nda-api` + `nda-worker`, then smoke-tests `/healthz`. The whole
  job is guarded on `vars.AZURE_CONFIGURED == 'true'` — until Azure is wired,
  pushes are **green-but-skipped**.
- **`deploy-prod.yml`** — `workflow_dispatch` or a `v*` tag. Runs in the GitHub
  **`production`** environment (manual-approval gate + prod-scoped variables).
  Same pipeline against the prod resource group.

### 5.1 OIDC federation (no stored cloud secrets)

GitHub authenticates to Azure via OIDC — there is **no** client secret stored in
GitHub. Create an app registration, add a federated credential per trigger, and
grant it Contributor on the resource group:

```bash
APP_ID=$(az ad app create --display-name "gh-ndaassist" --query appId -o tsv)
az ad sp create --id "$APP_ID"
SP_ID=$(az ad sp show --id "$APP_ID" --query id -o tsv)

for sub in \
  "repo:ORG/REPO:ref:refs/heads/master" \
  "repo:ORG/REPO:environment:production" \
  "repo:ORG/REPO:ref:refs/tags/v*" \
  "repo:ORG/REPO:pull_request"; do
  name=$(echo "$sub" | tr ':/*' '-')
  az ad app federated-credential create --id "$APP_ID" --parameters "{
    \"name\": \"$name\",
    \"issuer\": \"https://token.actions.githubusercontent.com\",
    \"subject\": \"$sub\",
    \"audiences\": [\"api://AzureADTokenExchange\"]
  }"
done

az role assignment create --assignee-object-id "$SP_ID" \
  --assignee-principal-type ServicePrincipal --role "Contributor" \
  --scope "/subscriptions/<SUB>/resourceGroups/rg-ndaassist-dev"
```

### 5.2 GitHub repo variables and secrets

All three IDs are **variables** (not secrets — they are not sensitive and OIDC
means no client secret exists). `AZURE_CONFIGURED` is the arming flag.

| Name | Kind | Scope | Value |
|---|---|---|---|
| `AZURE_CLIENT_ID` | var | repo (+ `production` env) | app registration appId |
| `AZURE_TENANT_ID` | var | repo (+ `production` env) | directory tenant id |
| `AZURE_SUBSCRIPTION_ID` | var | repo (+ `production` env) | subscription id |
| `AZURE_RESOURCE_GROUP` | var | repo = dev RG; `production` env = prod RG | e.g. `rg-ndaassist-dev` |
| `ACR_NAME` | var | per env | `acrName` bicep output |
| `ACR_LOGIN_SERVER` | var | per env | `acrLoginServer` bicep output |
| `AZURE_CONFIGURED` | var | per env | `true` to arm deploys (flip last) |

No cloud **secrets** are stored in GitHub in P0. Application secrets live in Key
Vault, not GitHub. _[Phases that need build-time secrets, if any, document them
here.]_

### 5.3 Image + migration model

- One image (`nda-assistant:<sha>`) serves both apps; the worker overrides the
  command with `python -m app.worker`. CI tags with the commit sha (immutable)
  and `latest`.
- **Migrations run as a pre-deploy step, never at container boot** (PLAN §3.1,
  CONTRACT expand/contract discipline). A failed migration aborts the deploy
  while the old revision keeps serving. P1 shipped the entrypoint
  `app/db_migrate.py` (`python -m app.db_migrate`), which handles a fresh,
  pre-Alembic, or already-migrated DB safely (stamp-then-`upgrade head`, so it
  never crash-loops). The chain runs `0001_baseline` … `0010_approval_access`
  (bot core, forms→tally drop, envelopes, archive, token registry, studio ops,
  attribution, approval/access); `create_all == alembic head` is test-enforced.
- **Exact pre-deploy step** (replaces the placeholder `echo` still in
  `.github/workflows/deploy-dev.yml`): run the migration from the newly-built
  image, against the target DB, *before* `az containerapp update`, e.g. as a
  one-shot Container Apps job on the same image + secret ref:

  ```bash
  az containerapp job create \
    --name nda-migrate -g "$RG" \
    --environment "$CAE" \
    --image "$ACR_LOGIN_SERVER/nda-assistant:$SHA" \
    --trigger-type Manual --replica-timeout 600 \
    --secrets database-url=keyvaultref:... \
    --env-vars DATABASE_URL=secretref:database-url \
    --command "python" "-m" "app.db_migrate"
  az containerapp job start --name nda-migrate -g "$RG"   # gate the deploy on this succeeding
  ```

  (Or, more simply, an `az acr run`/one-off container executing the same
  `python -m app.db_migrate` against `DATABASE_URL`.) Only after it exits 0 do the
  `nda-api` / `nda-worker` revisions roll forward.
- **Runtime-image contents.** `backend/Dockerfile` builds from the **repo root**
  (`az acr build --file backend/Dockerfile .`; a repo-root `.dockerignore` keeps the
  context small). It installs the **`tesseract-ocr`** binary + `tessdata_best` (OCR:
  `pytesseract`, `OCR_ENABLED=true`) and **LibreOffice** (`SOFFICE_BIN=soffice`, the
  `.doc`→`.docx` + docx→PDF path), and ships the repo-root engine data at the absolute
  paths the app resolves (`_REPO == "/"`): `/playbook` (the v3 json + v4 tree — a missing
  playbook is a fail-closed `503 playbook_unavailable`), `/samples` (the standard-NDA
  baseline), and `/word-addin` (the add-in static bundle; a missing bundle fails soft).
  Build-time `RUN` assertions verify all of these exist, so an incomplete image fails the
  build rather than a live review. Both were discovered missing during the first Azure dev
  deploy and fixed.

## 6. Secret rotation ⇒ new revision

Container Apps resolves Key Vault secret references **at revision-create time**,
not per request. Rotating a secret in Key Vault does **not** hot-reload a running
app. After rotating, force a new revision:

```bash
az containerapp update --name nda-api -g <rg> --set-env-vars _rotated=$(date +%s)
az containerapp update --name nda-worker -g <rg> --set-env-vars _rotated=$(date +%s)
```

`SETTINGS_ENCRYPTION_KEY` (Fernet key for UI-editable secret settings) must stay
**stable** across revisions or previously-encrypted values become unreadable —
rotate it deliberately, re-encrypting affected settings. _[Wired in P5 with the
admin settings plane.]_

## 7. Scale path (replicas = 1 today)

Both apps run **one** replica. Three blockers must clear, in order, before
`minReplicas`/`maxReplicas` can rise (PLAN §3.1, CONTRACT):

1. **In-process state → Redis.** Sessions, the per-principal rate limiter, and
   the auth IP throttle are in-memory today. Add `REDIS_URL` (the rate store is
   already Redis-ready) and move session + rate state there. _[P5 for sessions;
   rate state seam exists.]_
2. **`/data` externalization.** Redline exports and template working files live
   on the shared Azure Files volume. A second replica already *shares* the mount,
   but any in-process file cache/lock assumptions must be audited before scaling.
3. **Raise replicas.** Only after 1–2: bump `maxReplicas` on `nda-api`. The
   worker stays effectively single-active via Postgres advisory locks
   (APScheduler), so horizontal worker scale never double-fires — but review-job
   claiming already uses a lease, so it can scale once 1–2 are done.

Until then, vertical scale (cpu/memory params) is the lever.

## 8. Network & egress hardening _[tracked; applied incrementally]_

P0 uses public endpoints (Postgres "allow Azure services", public ACR/KV network
access) for a working dev loop. The hardening path (PLAN §6):

- **VNet-integrate** the Container Apps environment; put Postgres, Key Vault,
  ACR, and Storage behind private endpoints; drop public network access.
- **Egress allowlist** (NSG / firewall): OpenRouter, Slack, DocuSign, Google,
  Airtable, SMTP/IMAP only.
- Keep purge protection **on** in prod (already set); consider it in dev once
  teardown cadence settles.

## 9. Phase placeholders

- **[P1] ✅ shipped** — `DATABASE_URL` consumed by the app + `keyVaultSecretRefs`
  wiring real (§3.2); `python -m app.db_migrate` pre-deploy entrypoint (§5.3).
  Seed `openrouter-api-key` + `engine-api-key` and add their refs to arm the LLM
  and `/v1` machine-auth capabilities. **Still open:** the `deploy-dev.yml`
  migration step is a placeholder `echo` — swap it for the §5.3 job; and the dev
  `.bicepparam` `keyVaultSecretRefs` is still `[]`.
- **[P2]** Slack + IMAP/SMTP secrets (`slack-signing-secret`, `slack-bot-token`,
  `imap-password`, `smtp-password`); bot mailbox. The config groups + capabilities
  (`slack`, `email_in`, `email_out`) and the bot-core tables (`0002_bot_core`)
  already exist; P2 seeds the secrets and wires the channels.
- **[P3]** DocuSign secrets; Tally webhook signing secret (`tally-signing-secret`
  — the external Tally form replaced the in-house form service and its
  form-link key).
- **[P4]** Airtable PAT + base/table config; storage-provider (Drive) creds.
- **[P5]** admin bootstrap; `SETTINGS_ENCRYPTION_KEY`; CSP/allowlist config.
- **[P6]** Word add-in origin; prod cutover; n8n retirement (see `RUNBOOK.md`).
