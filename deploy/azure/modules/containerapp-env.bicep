// Container Apps managed environment, wired to Log Analytics, with an Azure
// Files storage definition that the api + worker apps mount at /data.
//
// The environment-level storage definition is a named handle ("data") that
// each container app references from its volumes; the SMB account key is read
// at deploy time via listKeys and never leaves the control plane.

@description('Azure region for the environment.')
param location string

@description('Base name used to derive the environment name.')
param baseName string

@description('Log Analytics customer (workspace) id.')
param logAnalyticsCustomerId string

@description('Resource id of the Log Analytics workspace (for the shared key lookup).')
param logAnalyticsWorkspaceId string

@description('Storage account name backing the Azure Files share.')
param storageAccountName string

@description('Azure Files share name to mount.')
param fileShareName string

@description('Name of the environment storage definition apps reference in their volumes.')
param storageDefinitionName string = 'data'

@description('Tags applied to the environment.')
param tags object = {}

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource logWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: last(split(logAnalyticsWorkspaceId, '/'))
}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-${baseName}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalyticsCustomerId
        sharedKey: logWorkspace.listKeys().primarySharedKey
      }
    }
  }
}

resource dataStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: environment
  name: storageDefinitionName
  properties: {
    azureFile: {
      accountName: storageAccountName
      accountKey: storageAccount.listKeys().keys[0].value
      shareName: fileShareName
      accessMode: 'ReadWrite'
    }
  }
}

@description('Resource id of the managed environment.')
output id string = environment.id

@description('Default domain of the environment (used to build the api ingress FQDN).')
output defaultDomain string = environment.properties.defaultDomain

@description('Name of the storage definition apps mount as /data.')
output storageDefinitionName string = dataStorage.name
