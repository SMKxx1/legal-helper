// Dev environment parameters.
//
// The Postgres admin password is read from the PG_ADMIN_PASSWORD environment
// variable at `az deployment` time — it is NEVER stored in this file. Export it
// in your shell (or the CI OIDC job) before deploying:
//   export PG_ADMIN_PASSWORD="$(openssl rand -base64 24)"
// then seed it into Key Vault as `database-url` afterwards (see README).

using './main.bicep'

param namePrefix = 'ndaassist'
param environment = 'dev'
param location = 'southeastasia'

// Cheapest always-on footprint: burstable Postgres, 0.25 vCPU apps, Basic ACR.
param postgresAdminLogin = 'ndaadmin'
param postgresAdminPassword = readEnvironmentVariable('PG_ADMIN_PASSWORD', '')
param postgresSkuName = 'Standard_B1ms'
param postgresSkuTier = 'Burstable'
param postgresStorageGB = 32

param acrSku = 'Basic'
param fileShareQuotaGB = 16
param keyVaultPurgeProtection = false

param cpuCores = '0.25'
param memory = '0.5Gi'

// P0: no Key Vault secret references wired yet (apps boot on safe defaults).
// P1 wires the database-url:
//   param keyVaultSecretRefs = [ { envVarName: 'DATABASE_URL', secretName: 'database-url' } ]
param keyVaultSecretRefs = []
