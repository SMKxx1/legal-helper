// Azure Database for PostgreSQL Flexible Server + one application database.
//
// Fresh Postgres per PLAN §2 (old history stays in the retired system). In P0
// the app does not connect (DATABASE_URL is reserved) — the server is stood up
// now so P1's migrations have a target. Dev runs the cheapest burstable tier;
// prod scales via parameters. The admin password is a @secure() param supplied
// at deploy time (never stored in the repo) and seeded into Key Vault as
// `database-url` afterwards (see README).

@description('Azure region for the server.')
param location string

@description('Base name used to derive the server name.')
param baseName string

@description('Administrator login name.')
param administratorLogin string

@description('Administrator password. Supplied at deploy time; never committed.')
@secure()
param administratorPassword string

@description('Application database name.')
param databaseName string = 'nda'

@description('Compute SKU name, e.g. Standard_B1ms (dev burstable) or Standard_D2ds_v5 (prod).')
param skuName string = 'Standard_B1ms'

@description('Compute tier for the SKU.')
@allowed([
  'Burstable'
  'GeneralPurpose'
  'MemoryOptimized'
])
param skuTier string = 'Burstable'

@description('Provisioned storage in GB.')
param storageSizeGB int = 32

@description('PostgreSQL major version.')
param postgresVersion string = '16'

@description('Allow other Azure services (e.g. the Container Apps environment) to reach the server. VNet integration is the documented hardening step (AZURE.md).')
param allowAzureServices bool = true

@description('Tags applied to the server.')
param tags object = {}

var serverName = 'psql-${baseName}'

resource server 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: serverName
  location: location
  tags: tags
  sku: {
    name: skuName
    tier: skuTier
  }
  properties: {
    version: postgresVersion
    administratorLogin: administratorLogin
    administratorLoginPassword: administratorPassword
    storage: {
      storageSizeGB: storageSizeGB
      autoGrow: 'Enabled'
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    authConfig: {
      activeDirectoryAuth: 'Disabled'
      passwordAuth: 'Enabled'
    }
    network: {
      publicNetworkAccess: 'Enabled'
    }
  }
}

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: server
  name: databaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

// 0.0.0.0 is Azure's sentinel firewall rule meaning "allow all Azure-internal
// services" (NOT the public internet). Container Apps reaches the server via
// this until VNet integration lands.
resource allowAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = if (allowAzureServices) {
  parent: server
  name: 'AllowAllAzureServicesAndResourcesWithinAzureIps'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

@description('Resource id of the flexible server.')
output id string = server.id

@description('Fully-qualified domain name of the server.')
output fqdn string = server.properties.fullyQualifiedDomainName

@description('Application database name.')
output databaseName string = database.name
