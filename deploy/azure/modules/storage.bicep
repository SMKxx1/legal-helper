// Storage account + Azure Files share, mounted as /data on both container apps.
//
// The engine persists uploads, exports and template working files under
// DATA_DIR=/data (PLAN §3.1). Sharing one Azure Files share across api + worker
// is what lets both apps see the same files; it is also one of the three
// documented blockers to running replicas >= 2 (see AZURE.md scale path).

@description('Azure region for the storage account.')
param location string

@description('Base name used to derive the account name.')
param baseName string

@description('Name of the Azure Files share.')
param shareName string = 'data'

@description('Share quota in GB.')
param shareQuotaGB int = 16

@description('Redundancy SKU for the account.')
@allowed([
  'Standard_LRS'
  'Standard_ZRS'
])
param sku string = 'Standard_LRS'

@description('Tags applied to the account.')
param tags object = {}

// Account names are globally unique, lowercase alphanumeric, 3-24 chars.
var accountName = take('st${replace(baseName, '-', '')}${uniqueString(resourceGroup().id)}', 24)

resource account 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: accountName
  location: location
  tags: tags
  sku: {
    name: sku
  }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' = {
  parent: account
  name: 'default'
}

resource share 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileService
  name: shareName
  properties: {
    shareQuota: shareQuotaGB
    enabledProtocols: 'SMB'
  }
}

@description('Resource id of the storage account.')
output id string = account.id

@description('Storage account name — feeds the Container Apps environment storage.')
output accountName string = account.name

@description('Azure Files share name.')
output shareName string = share.name
