// User-assigned managed identity for the container apps.
//
// Bootstrap-ordering note (PLAN §3.1): a *user-assigned* identity is created
// first and granted Key Vault + ACR access BEFORE the container apps depend on
// it. A system-assigned identity would create a first-deploy chicken-and-egg
// (the app cannot read the KV secret references it needs to boot because its
// identity does not exist until after the app is created). The role
// assignments themselves live in the keyvault and registry modules, scoped to
// those resources, so the grants are correct-scope and land before the apps.

@description('Azure region for the identity.')
param location string

@description('Base name used to derive the identity name, e.g. "ndaassist-dev".')
param baseName string

@description('Tags applied to the identity.')
param tags object = {}

resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${baseName}'
  location: location
  tags: tags
}

@description('Resource id of the user-assigned managed identity.')
output id string = uami.id

@description('Principal (object) id — used for role assignments.')
output principalId string = uami.properties.principalId

@description('Client id — used for AAD-based KV/registry auth from the container.')
output clientId string = uami.properties.clientId
