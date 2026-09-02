// Key Vault in RBAC mode + the "Key Vault Secrets User" grant for the
// container-apps identity.
//
// The grant lives here (scoped to this vault) so it is created together with
// the vault and therefore BEFORE any container app that depends on this
// module — closing the bootstrap-ordering gap (PLAN §3.1). No secret values
// are set from Bicep: secrets are seeded out-of-band via `az keyvault secret
// set` (see deploy/azure/README.md), so nothing sensitive lives in IaC.

@description('Azure region for the vault.')
param location string

@description('Base name used to derive the vault name.')
param baseName string

@description('Principal id of the user-assigned identity to grant Secrets User.')
#disable-next-line secure-secrets-in-params // an object id, not a secret
param secretsUserPrincipalId string

@description('Tenant id that owns the vault.')
param tenantId string = subscription().tenantId

@description('Soft-delete retention window in days.')
param softDeleteRetentionInDays int = 7

@description('Enable purge protection. Off in dev to allow clean teardown; on in prod.')
param enablePurgeProtection bool = false

@description('Tags applied to the vault.')
param tags object = {}

// Vault names are globally unique and capped at 24 chars. uniqueString keeps
// it deterministic per resource group without leaking the subscription id.
var vaultName = take('kv-${replace(baseName, '-', '')}${uniqueString(resourceGroup().id)}', 24)

// Built-in role: Key Vault Secrets User (read secret values at data plane).
var secretsUserRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '4633458b-17de-408a-b874-0445c86b69e6'
)

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: vaultName
  location: location
  tags: tags
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: softDeleteRetentionInDays
    enablePurgeProtection: enablePurgeProtection ? true : null
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
  }
}

resource secretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, secretsUserPrincipalId, secretsUserRoleId)
  scope: vault
  properties: {
    principalId: secretsUserPrincipalId
    roleDefinitionId: secretsUserRoleId
    principalType: 'ServicePrincipal'
  }
}

@description('Resource id of the vault.')
output id string = vault.id

@description('Vault name.')
output name string = vault.name

@description('Vault data-plane URI, e.g. https://kv-xxx.vault.azure.net/.')
output vaultUri string = vault.properties.vaultUri
