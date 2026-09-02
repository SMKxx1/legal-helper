// Log Analytics workspace + Application Insights (workspace-based).
//
// Contract: the workspace is the sink for both Container Apps console logs
// (wired by the managed environment) and App Insights telemetry. The
// App Insights connection string is surfaced as an output so the container
// apps can set APPLICATIONINSIGHTS_CONNECTION_STRING. Its absence in the app
// disables telemetry export (a capability, never a boot error) — here in
// Azure it is always present.

@description('Azure region for all resources in this module.')
param location string

@description('Base name used to derive resource names, e.g. "ndaassist-dev".')
param baseName string

@description('Retention in days for the Log Analytics workspace.')
param retentionInDays int = 30

@description('Tags applied to every resource.')
param tags object = {}

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${baseName}'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: retentionInDays
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${baseName}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
    IngestionMode: 'LogAnalytics'
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

@description('Resource id of the Log Analytics workspace.')
output workspaceId string = workspace.id

@description('Customer (workspace) id used by the Container Apps environment.')
output workspaceCustomerId string = workspace.properties.customerId

@description('App Insights connection string. Feed to APPLICATIONINSIGHTS_CONNECTION_STRING.')
output appInsightsConnectionString string = appInsights.properties.ConnectionString
