# Filling in credentials (dev on Azure)

Each disabled capability turns **on** the moment its required config is present (a blank/whitespace
value counts as unset, so a capability never half-enables). This is the exact list of what to set and
where. Nothing here is set yet — fill a group and its capability flips to `enabled` on the next
revision restart.

## Where things live

| Kind | Where | How you set it |
|---|---|---|
| **Secret** (tokens, passwords, keys) | Key Vault `kv-ndaassistdeva7uz4n6kd` (kebab-case name) → exposed to the app as an UPPER_SNAKE env var via a secret-ref | 2 commands (below) |
| **Config** (hostnames, folder/base IDs, channels, ports, emails) | Plain env var on the container app | 1 command (below) |

- Resource group: `rg-ndaassist-dev` · Apps: `nda-api` (web), `nda-worker` (jobs).
- The user-assigned managed identity is already attached to both apps and already has Key Vault read.
- Key Vault URI: `https://kv-ndaassistdeva7uz4n6kd.vault.azure.net/`

### Set a SECRET (Key Vault → env var)

```bash
RG=rg-ndaassist-dev; KV=kv-ndaassistdeva7uz4n6kd
KVURI=https://$KV.vault.azure.net/
IDID=$(az identity show -g $RG -n id-ndaassist-dev --query id -o tsv)

# 1. put the value in Key Vault (kebab-case secret name)
az keyvault secret set --vault-name $KV --name slack-bot-token --value "xoxb-REAL-VALUE"

# 2. wire it onto the app as a secret-ref + env var (repeat --name nda-worker where the table says both)
az containerapp secret set --name nda-api -g $RG \
  --secrets "slack-bot-token=keyvaultref:${KVURI}secrets/slack-bot-token,identityref:${IDID}"
az containerapp update --name nda-api -g $RG --set-env-vars SLACK_BOT_TOKEN=secretref:slack-bot-token
```

### Set a CONFIG value (plain env var)

```bash
az containerapp update --name nda-api -g $RG --set-env-vars NDA_ADMIN_SLACK_CHANNEL=C0XXXXXXX
```

> Tip: batch a whole capability's `--set-env-vars` into one `az containerapp update` call to spin only
> one new revision. Update **both** `nda-api` and `nda-worker` for rows marked *both*.

---

## Slack  → capability `slack` (+ bot replies)

| Env var | Kind | Key Vault secret | App | What to put |
|---|---|---|---|---|
| `SLACK_BOT_TOKEN` | secret | `slack-bot-token` | api | Slack app **Bot User OAuth Token** (`xoxb-…`) |
| `SLACK_SIGNING_SECRET` | secret | `slack-signing-secret` | api | Slack app **Signing Secret** |
| `NDA_BOT_USER_ID` | config | — | api | the bot's Slack **user id** (`U…`) — used by the loop/thread guards |
| `NDA_ADMIN_SLACK_CHANNEL` | config | — | api | channel id (`C…`) where approval requests are posted |

Slack app event URL → `https://<api-fqdn>/slack/events`; interactivity URL → `/slack/interactivity`.

## Email — inbound  → capability `email_in` (IMAP poller, worker)

| Env var | Kind | Key Vault secret | App | What to put |
|---|---|---|---|---|
| `IMAP_HOST` | config | — | worker | IMAP server host |
| `IMAP_USER` | config | — | worker | mailbox login |
| `IMAP_PASSWORD` | secret | `imap-password` | worker | mailbox password / app password |
| `IMAP_PORT` | config | — | worker | default `993` (only set to override) |
| `IMAP_FOLDER` | config | — | worker | default `INBOX` (only set to override) |

## Email — outbound  → capability `email_out` (SMTP replies)

| Env var | Kind | Key Vault secret | App | What to put |
|---|---|---|---|---|
| `SMTP_HOST` | config | — | **both** | SMTP server host |
| `SMTP_USER` | config | — | **both** | SMTP login |
| `SMTP_PASSWORD` | secret | `smtp-password` | **both** | SMTP password |
| `SMTP_PORT` | config | — | both | default `587` (STARTTLS); `465` for implicit TLS |
| `SMTP_SECURE` | config | — | both | default `false` (STARTTLS); `true` for implicit TLS on 465 |
| `NDA_BOT_FROM_EMAIL` | config | — | both | from-address (default `nda-bot@example.com`) |
| `NDA_ADMIN_EMAIL` | config | — | both | admin address for approval notices (email fallback) |

## DocuSign  → capability `docusign` (envelope send)

| Env var | Kind | Key Vault secret | App | What to put |
|---|---|---|---|---|
| `DOCUSIGN_ACCOUNT_ID` | config | — | api | DocuSign **API Account ID** (GUID) |
| `DOCUSIGN_INTEGRATION_KEY` | config | — | api | app **Integration Key** (client id) |
| `DOCUSIGN_USER_ID` | config | — | api | the **impersonated user's** id (GUID) |
| `DOCUSIGN_PRIVATE_KEY` | secret | `docusign-private-key` | api | the RSA **private key PEM** (JWT grant) |
| `DOCUSIGN_BASE_URI` | config | — | api | default `https://demo.docusign.net`; prod `https://na*.docusign.net` |
| `DOCUSIGN_OAUTH_HOST` | config | — | api | default `account-d.docusign.com`; prod `account.docusign.com` |

One-time: grant JWT **consent** for the integration key + impersonated user in DocuSign.

## Google Drive  → capability `google_drive` (archive + watcher)

| Env var | Kind | Key Vault secret | App | What to put |
|---|---|---|---|---|
| `GOOGLE_OAUTH_CLIENT_ID` | config | — | **both** | OAuth client id |
| `GOOGLE_OAUTH_CLIENT_SECRET` | secret | `google-oauth-client-secret` | **both** | OAuth client secret |
| `GOOGLE_OAUTH_REFRESH_TOKEN` | secret | `google-oauth-refresh-token` | **both** | offline refresh token (Drive scope) |
| `DRIVE_ARCHIVE_FOLDER_ID` | config | — | both | Drive folder id — the "Signed Company NDAs" destination |
| `DRIVE_CACHE_FOLDER_NAME` | config | — | both | default `Signed Company NDAs Cache` (only set to override) |

## Airtable  → capability `airtable` (expiration tracker, worker)

| Env var | Kind | Key Vault secret | App | What to put |
|---|---|---|---|---|
| `AIRTABLE_PAT` | secret | `airtable-pat` | worker | Airtable Personal Access Token |
| `AIRTABLE_BASE_ID` | config | — | worker | base id (`app…`) |
| `AIRTABLE_TABLE` | config | — | worker | table name (or id) |

Field names the writer targets: **`File Id`** (merge key), **`Name`**, **`Expiration Date`** (Date type).
If your base uses other names, change the `FIELD_*` constants in `app/integrations/airtable.py`.

---

## Already set (for reference — don't re-do)

`DATABASE_URL`, `OPENROUTER_API_KEY`, `SETTINGS_ENCRYPTION_KEY`, `ENGINE_API_KEY`,
`ADMIN_BOOTSTRAP_USER_ID`, `ADMIN_BOOTSTRAP_PASSWORD`, `FORM_LINK_SECRET`, `FORM_BASE_URL`,
`APPLICATIONINSIGHTS_CONNECTION_STRING` — so `llm_inference`, `forms`, and `telemetry_export` are
already **enabled**.

## After filling a group

The container app auto-restarts on `az containerapp update`. Confirm on the admin **Capabilities**
page (or `GET /api/admin/capabilities`) that the capability flipped to `enabled`. If it still shows
`disabled`, the reason text names the exact env var still missing.
