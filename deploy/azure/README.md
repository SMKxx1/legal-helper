# Azure infrastructure (Bicep)

Infrastructure-as-code for one NDA Assistant environment. `main.bicep` is the
composition root; `modules/` holds the leaf resources; `parameters.dev.bicepparam`
and `parameters.prod.bicepparam` supply per-environment values.

This file is the **exact command sequence**. `docs/AZURE.md` is the narrative
version with the resource inventory and CI/CD wiring.

## What this deploys

| Module | Resource | Notes |
|---|---|---|
| `monitoring` | Log Analytics + Application Insights | telemetry sink |
| `identity` | user-assigned managed identity | created first (see ordering) |
| `keyvault` | Key Vault (RBAC) + `Secrets User` grant | secrets seeded out-of-band |
| `registry` | Azure Container Registry + `AcrPull` grant | admin user disabled |
| `postgres` | PostgreSQL Flexible Server + `nda` DB | burstable in dev |
| `storage` | Storage account + Azure Files share `data` | mounted `/data` |
| `containerapp-env` | Container Apps environment + storage def | Log-Analytics-wired |
| `containerapp` (x2) | `nda-api` (ingress :8000) + `nda-worker` | same image, min 1 |

## Ordering constraints (read before deploying)

1. **The user-assigned identity and its role assignments are created before the
   container apps.** A system-assigned identity would deadlock the first deploy
   (the app cannot read the Key Vault secret references it needs to boot before
   its identity exists). Bicep sequences this automatically because the app
   modules consume `keyvault`/`registry` outputs.

2. **The deploying principal needs permission to create role assignments.** The
   template grants `Key Vault Secrets User` and `AcrPull` to the identity, so
   you need **Owner**, or **Contributor + User Access Administrator**, on the
   resource group. A plain Contributor deploy fails at the role-assignment step.

3. **The first deploy uses a public placeholder image**
   (`mcr.microsoft.com/azuredocs/containerapps-helloworld:latest`, which listens
   on :80). `nda-api` ingress targets :8000, so the revision will **not** pass
   `/healthz` until CI pushes the real backend image (which listens on :8000)
   and runs `az containerapp update`. This is expected — the infra is up, the
   app is not yet.

4. **Key Vault is RBAC-mode.** Writing secrets needs the *data-plane*
   `Key Vault Secrets Officer` role on the vault, which is separate from the
   control-plane rights above. Grant it to yourself before `az keyvault secret
   set` (step 5).

5. **Secrets are seeded after deploy, not from Bicep.** `keyVaultSecretRefs` is
   empty in P0, so the first deploy needs no seeded secrets. P1 wires
   `database-url` (see step 6).

## 0. Prerequisites

```bash
az version                      # need the CLI
az bicep install                # need the Bicep compiler
az login
az account set --subscription "<SUBSCRIPTION_ID>"

# Register resource providers once per subscription.
for p in Microsoft.App Microsoft.ContainerRegistry Microsoft.KeyVault \
         Microsoft.DBforPostgreSQL Microsoft.OperationalInsights \
         Microsoft.Insights Microsoft.Storage Microsoft.ManagedIdentity; do
  az provider register --namespace "$p"
done
```

## 1. Resource group

```bash
az group create --name rg-ndaassist-dev --location southeastasia
```

## 2. Supply the Postgres password (never committed)

`parameters.*.bicepparam` reads it from the environment:

```bash
export PG_ADMIN_PASSWORD="$(openssl rand -base64 24)"
```

## 3. Validate, then deploy

```bash
# Compile-only sanity check.
az bicep build --file main.bicep

# Preview (what-if) — recommended before every deploy.
az deployment group what-if \
  --resource-group rg-ndaassist-dev \
  --template-file main.bicep \
  --parameters parameters.dev.bicepparam

# Deploy.
az deployment group create \
  --name ndaassist-dev-infra \
  --resource-group rg-ndaassist-dev \
  --template-file main.bicep \
  --parameters parameters.dev.bicepparam
```

## 4. Capture outputs

```bash
az deployment group show \
  --name ndaassist-dev-infra \
  --resource-group rg-ndaassist-dev \
  --query properties.outputs
```

You need `acrName`, `acrLoginServer`, `keyVaultName`, `apiFqdn`, `postgresFqdn`.

## 5. Seed Key Vault secrets

Grant yourself data-plane write, then set secrets. **Secret names are
kebab-case of the env var** (env `DATABASE_URL` ↔ secret `database-url`).

```bash
KV=$(az deployment group show -n ndaassist-dev-infra -g rg-ndaassist-dev \
  --query properties.outputs.keyVaultName.value -o tsv)
PG=$(az deployment group show -n ndaassist-dev-infra -g rg-ndaassist-dev \
  --query properties.outputs.postgresFqdn.value -o tsv)
ME=$(az ad signed-in-user show --query id -o tsv)
KVID=$(az keyvault show --name "$KV" --query id -o tsv)

az role assignment create \
  --role "Key Vault Secrets Officer" \
  --assignee-object-id "$ME" --assignee-principal-type User \
  --scope "$KVID"

# database-url (reserved in P0; used by P1). Note the +psycopg2 driver.
az keyvault secret set --vault-name "$KV" --name database-url \
  --value "postgresql+psycopg2://ndaadmin:${PG_ADMIN_PASSWORD}@${PG}:5432/nda?sslmode=require"
```

Later phases add their own secrets under the same convention (see
`docs/CREDENTIALS.md`).

## 6. Wire the secret reference (P1)

Once `database-url` exists, set `keyVaultSecretRefs` in the `.bicepparam` and
re-deploy so the apps get `DATABASE_URL` from Key Vault:

```bicep
param keyVaultSecretRefs = [ { envVarName: 'DATABASE_URL', secretName: 'database-url' } ]
```

Rotating a secret does **not** restart the app — Container Apps resolves secret
references at revision-create time. After rotating, roll a new revision:
`az containerapp update --name nda-api -g rg-ndaassist-dev --set-env-vars _rotated=$(date +%s)`.

## 7. GitHub OIDC federation (no stored cloud secrets)

Create an app registration, federate the GitHub repo, and grant it deploy
rights. Replace `ORG/REPO`.

```bash
APP_ID=$(az ad app create --display-name "gh-ndaassist" --query appId -o tsv)
az ad sp create --id "$APP_ID"
SP_ID=$(az ad sp show --id "$APP_ID" --query id -o tsv)
TENANT_ID=$(az account show --query tenantId -o tsv)
SUB_ID=$(az account show --query id -o tsv)

# Federated credentials: one per trigger the workflows use.
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

# Deploy rights on the resource group (Contributor covers acr build + containerapp update).
az role assignment create --assignee-object-id "$SP_ID" --assignee-principal-type ServicePrincipal \
  --role "Contributor" --scope "/subscriptions/${SUB_ID}/resourceGroups/rg-ndaassist-dev"
```

Then set GitHub repo **variables** (Settings → Secrets and variables → Actions →
Variables):

| Variable | Value |
|---|---|
| `AZURE_CLIENT_ID` | `$APP_ID` |
| `AZURE_TENANT_ID` | `$TENANT_ID` |
| `AZURE_SUBSCRIPTION_ID` | `$SUB_ID` |
| `AZURE_RESOURCE_GROUP` | `rg-ndaassist-dev` |
| `ACR_NAME` | from `acrName` output |
| `ACR_LOGIN_SERVER` | from `acrLoginServer` output |
| `AZURE_CONFIGURED` | `true` (flip last — this arms `deploy-dev.yml`) |

For prod, repeat under a GitHub **Environment** named `production` (its own
variable scope, plus a required-reviewers approval gate) with the prod resource
group and registry. See `docs/AZURE.md` for the full CI/CD walkthrough.

## Teardown

```bash
az group delete --name rg-ndaassist-dev --yes --no-wait
```

Purge protection is **off** in dev (clean teardown) and **on** in prod. A
purge-protected vault name cannot be reused until the retention window elapses.
