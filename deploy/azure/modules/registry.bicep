// Azure Container Registry + the "AcrPull" grant for the container-apps
// identity.
//
// As with the vault, the role assignment is scoped here and created with the
// registry, so the identity can pull images the moment the container apps are
// created (PLAN §3.1 bootstrap ordering). Admin user stays disabled — pulls
// use the managed identity, never a static credential.

@description('Azure region for the registry.')
param location string

@description('Base name used to derive the registry name.')
param baseName string

@description('Principal id of the user-assigned identity to grant AcrPull.')
param acrPullPrincipalId string

@description('Registry SKU. Basic is sufficient for dev; Standard/Premium for prod.')
@allowed([
  'Basic'
  'Standard'
  'Premium'
])
param sku string = 'Basic'

@description('Tags applied to the registry.')
param tags object = {}

// Registry names are globally unique, alphanumeric only, 5-50 chars.
var registryName = take('acr${replace(baseName, '-', '')}${uniqueString(resourceGroup().id)}', 50)

// Built-in role: AcrPull.
var acrPullRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '7f951dda-4ed3-4680-a7ca-43fe172d538d'
)

resource registry 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: registryName
  location: location
  tags: tags
  sku: {
    name: sku
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, acrPullPrincipalId, acrPullRoleId)
  scope: registry
  properties: {
    principalId: acrPullPrincipalId
    roleDefinitionId: acrPullRoleId
    principalType: 'ServicePrincipal'
  }
}

@description('Resource id of the registry.')
output id string = registry.id

@description('Registry name.')
output name string = registry.name

@description('Login server, e.g. acrxxx.azurecr.io — used as the image prefix.')
output loginServer string = registry.properties.loginServer
