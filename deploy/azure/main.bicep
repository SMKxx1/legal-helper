// NDA Assistant — Azure infrastructure (one deployment per environment).
//
// Composition root. Deployed at resource-group scope. Provisions monitoring,
// the container-apps identity + its Key Vault/ACR grants, the registry, Key
// Vault, Postgres, Azure Files storage, the Container Apps environment, and the
// nda-api + nda-worker container apps.
//
// Bootstrap ordering (PLAN §3.1): the user-assigned identity and its role
// assignments are created before the container apps. Because the app modules
// consume keyvault/registry outputs, Bicep sequences those modules — and the
// role assignments they contain — ahead of the apps automatically.
//
// First deploy uses a public placeholder image (containerImage default); CI
// then pushes the real backend image and runs `az containerapp update`. See
// deploy/azure/README.md for the full bootstrap sequence.

targetScope = 'resourceGroup'

// --- naming & environment -------------------------------------------------
@description('Short prefix for resource names, e.g. "ndaassist".')
param namePrefix string = 'ndaassist'

@description('Deployment environment.')
@allowed([
  'dev'
  'prod'
])
param environment string = 'dev'

@description('Azure region. Defaults to the region of the partial prior attempt.')
param location string = 'southeastasia'

// --- app config -----------------------------------------------------------
@description('LOG_LEVEL override. Empty => DEBUG in dev, INFO otherwise.')
param logLevel string = ''

@description('LOG_FORMAT override. Empty => console in dev, json otherwise.')
param logFormat string = ''

@description('Container image for both apps. Default is a public placeholder for the first infra deploy; CI replaces it with the ACR image.')
param containerImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@description('Port the api listens on (external ingress target).')
param containerTargetPort int = 8000

@description('CPU cores per replica.')
param cpuCores string = '0.25'

@description('Memory per replica.')
param memory string = '0.5Gi'

@description('Key Vault secret references projected as env vars on both apps. Empty in P0; P1+ adds e.g. { envVarName: "DATABASE_URL", secretName: "database-url" }.')
param keyVaultSecretRefs array = []

// --- Postgres -------------------------------------------------------------
@description('Postgres administrator login.')
param postgresAdminLogin string = 'ndaadmin'

@description('Postgres administrator password. Supplied at deploy time; never committed. In .bicepparam use readEnvironmentVariable().')
@secure()
param postgresAdminPassword string

@description('Postgres compute SKU name.')
param postgresSkuName string = 'Standard_B1ms'

@description('Postgres compute tier.')
param postgresSkuTier string = 'Burstable'

@description('Postgres provisioned storage (GB).')
param postgresStorageGB int = 32

// --- registry / storage ---------------------------------------------------
@description('ACR SKU.')
param acrSku string = 'Basic'

@description('Azure Files share quota (GB).')
param fileShareQuotaGB int = 16

@description('Enable Key Vault purge protection (recommended on for prod).')
param keyVaultPurgeProtection bool = false

@description('Extra tags merged onto every resource.')
param tags object = {}

// --- derived values -------------------------------------------------------
var baseName = '${namePrefix}-${environment}'
var effectiveLogLevel = empty(logLevel) ? (environment == 'dev' ? 'DEBUG' : 'INFO') : logLevel
var effectiveLogFormat = empty(logFormat) ? (environment == 'dev' ? 'console' : 'json') : logFormat
var commonTags = union(
  {
    application: 'nda-assistant'
    environment: environment
    managedBy: 'bicep'
  },
  tags
)

var storageDefinitionName = 'data'

// --- modules --------------------------------------------------------------
module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  params: {
    location: location
    baseName: baseName
    tags: commonTags
  }
}

module identity 'modules/identity.bicep' = {
  name: 'identity'
  params: {
    location: location
    baseName: baseName
    tags: commonTags
  }
}

module keyvault 'modules/keyvault.bicep' = {
  name: 'keyvault'
  params: {
    location: location
    baseName: baseName
    secretsUserPrincipalId: identity.outputs.principalId
    enablePurgeProtection: keyVaultPurgeProtection
    tags: commonTags
  }
}

module registry 'modules/registry.bicep' = {
  name: 'registry'
  params: {
    location: location
    baseName: baseName
    acrPullPrincipalId: identity.outputs.principalId
    sku: acrSku
    tags: commonTags
  }
}

module postgres 'modules/postgres.bicep' = {
  name: 'postgres'
  params: {
    location: location
    baseName: baseName
    administratorLogin: postgresAdminLogin
    administratorPassword: postgresAdminPassword
    skuName: postgresSkuName
    skuTier: postgresSkuTier
    storageSizeGB: postgresStorageGB
    tags: commonTags
  }
}

module storage 'modules/storage.bicep' = {
  name: 'storage'
  params: {
    location: location
    baseName: baseName
    shareName: storageDefinitionName
    shareQuotaGB: fileShareQuotaGB
    tags: commonTags
  }
}

module containerEnv 'modules/containerapp-env.bicep' = {
  name: 'containerEnv'
  params: {
    location: location
    baseName: baseName
    logAnalyticsCustomerId: monitoring.outputs.workspaceCustomerId
    logAnalyticsWorkspaceId: monitoring.outputs.workspaceId
    storageAccountName: storage.outputs.accountName
    fileShareName: storage.outputs.shareName
    storageDefinitionName: storageDefinitionName
    tags: commonTags
  }
}

// nda-api — external ingress, image default command.
// Depends (implicitly, via keyvault/registry outputs) on the role assignments
// that let the identity read secrets and pull images.
module apiApp 'modules/containerapp.bicep' = {
  name: 'apiApp'
  params: {
    name: 'nda-api'
    location: location
    environmentId: containerEnv.outputs.id
    identityId: identity.outputs.id
    registryServer: registry.outputs.loginServer
    image: containerImage
    appEnv: environment
    logLevel: effectiveLogLevel
    logFormat: effectiveLogFormat
    appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
    keyVaultUri: keyvault.outputs.vaultUri
    keyVaultSecretRefs: keyVaultSecretRefs
    storageDefinitionName: containerEnv.outputs.storageDefinitionName
    enableExternalIngress: true
    targetPort: containerTargetPort
    cpuCores: cpuCores
    memory: memory
    minReplicas: 1
    maxReplicas: 1
    tags: commonTags
  }
}

// nda-worker — no ingress, same image, command override.
module workerApp 'modules/containerapp.bicep' = {
  name: 'workerApp'
  params: {
    name: 'nda-worker'
    location: location
    environmentId: containerEnv.outputs.id
    identityId: identity.outputs.id
    registryServer: registry.outputs.loginServer
    image: containerImage
    appEnv: environment
    logLevel: effectiveLogLevel
    logFormat: effectiveLogFormat
    appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
    keyVaultUri: keyvault.outputs.vaultUri
    keyVaultSecretRefs: keyVaultSecretRefs
    storageDefinitionName: containerEnv.outputs.storageDefinitionName
    enableExternalIngress: false
    command: [
      'python'
      '-m'
      'app.worker'
    ]
    cpuCores: cpuCores
    memory: memory
    minReplicas: 1
    maxReplicas: 1
    tags: commonTags
  }
}

// --- outputs (consumed by CI and the bootstrap runbook) -------------------
@description('ACR login server — image prefix for CI push/update.')
output acrLoginServer string = registry.outputs.loginServer

@description('ACR resource name — used by `az acr build`.')
output acrName string = registry.outputs.name

@description('Key Vault name — target for `az keyvault secret set`.')
output keyVaultName string = keyvault.outputs.name

@description('Public FQDN of nda-api.')
output apiFqdn string = apiApp.outputs.fqdn

@description('Managed identity client id (for AAD-scoped diagnostics).')
output identityClientId string = identity.outputs.clientId

@description('Postgres server FQDN (for the database-url secret).')
output postgresFqdn string = postgres.outputs.fqdn

@description('Container Apps environment name.')
output environmentName string = 'cae-${baseName}'
