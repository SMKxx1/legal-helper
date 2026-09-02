// Prod environment parameters.
//
// As in dev, PG_ADMIN_PASSWORD is read from the environment at deploy time and
// never committed. Prod scales the database and registry up, enables Key Vault
// purge protection, and switches logging to structured JSON at INFO.

using './main.bicep'

param namePrefix = 'ndaassist'
param environment = 'prod'
param location = 'southeastasia'

// Prod logging: JSON at INFO (the empty-default logic would already pick this,
// but pin it explicitly so prod behaviour is not a function of a default).
param logLevel = 'INFO'
param logFormat = 'json'

// Scaled Postgres for prod. General Purpose, larger disk.
param postgresAdminLogin = 'ndaadmin'
param postgresAdminPassword = readEnvironmentVariable('PG_ADMIN_PASSWORD', '')
param postgresSkuName = 'Standard_D2ds_v5'
param postgresSkuTier = 'GeneralPurpose'
param postgresStorageGB = 64

param acrSku = 'Standard'
param fileShareQuotaGB = 32
param keyVaultPurgeProtection = true

param cpuCores = '0.5'
param memory = '1.0Gi'

// P1 wires the database-url here as well:
//   param keyVaultSecretRefs = [ { envVarName: 'DATABASE_URL', secretName: 'database-url' } ]
param keyVaultSecretRefs = []
