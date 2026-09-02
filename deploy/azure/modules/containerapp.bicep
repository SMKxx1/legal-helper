// Reusable Container App definition, used for both nda-api and nda-worker.
//
// api  = external ingress on :8000, no command override.
// worker = no ingress, command override ["python","-m","app.worker"].
// Both: same image, minReplicas 1 (always-on per PLAN §2 — Slack's 3s ack and
// the IMAP poller rule out scale-to-zero), user-assigned identity, /data mount,
// and optional Key Vault secret references resolved through that identity.
//
// Missing config never crashes boot: APPLICATIONINSIGHTS_CONNECTION_STRING is
// only injected when non-empty, and keyVaultSecretRefs defaults to [] so the
// first deploy needs no seeded secrets (PLAN "capabilities fail soft").

@description('Container app name, e.g. nda-api or nda-worker.')
param name string

@description('Azure region.')
param location string

@description('Resource id of the Container Apps managed environment.')
param environmentId string

@description('Resource id of the user-assigned managed identity.')
param identityId string

@description('ACR login server used as the image registry (for identity-based pulls).')
param registryServer string

@description('Container image reference. Defaults to a public placeholder so the first infra deploy succeeds before CI has pushed the real image.')
param image string

@description('APP_ENV value: dev | prod | test.')
param appEnv string

@description('LOG_LEVEL value, e.g. DEBUG or INFO.')
param logLevel string

@description('LOG_FORMAT value: console | json.')
param logFormat string

@description('App Insights connection string. Empty string => telemetry export disabled (capability off, not an error).')
param appInsightsConnectionString string = ''

@description('Key Vault data-plane URI, e.g. https://kv-xxx.vault.azure.net/.')
param keyVaultUri string

@description('Secrets to project from Key Vault as env vars. Each item: { envVarName, secretName }. Empty in P0; P1+ adds e.g. { envVarName: "DATABASE_URL", secretName: "database-url" }.')
param keyVaultSecretRefs array = []

@description('Name of the environment storage definition to mount at /data.')
param storageDefinitionName string

@description('Mount path for the shared Azure Files volume.')
param dataMountPath string = '/data'

@description('Enable external ingress (api). False for worker.')
param enableExternalIngress bool = false

@description('Ingress target port (the port the app listens on).')
param targetPort int = 8000

@description('Command override. Empty => use the image default (api). Worker passes ["python","-m","app.worker"].')
param command array = []

@description('CPU cores per replica.')
param cpuCores string = '0.25'

@description('Memory per replica.')
param memory string = '0.5Gi'

@description('Minimum replicas. 1 keeps both apps always-on.')
param minReplicas int = 1

@description('Maximum replicas.')
param maxReplicas int = 1

@description('Tags applied to the app.')
param tags object = {}

// --- env + secret projection ---------------------------------------------
var baseEnv = [
  {
    name: 'APP_ENV'
    value: appEnv
  }
  {
    name: 'LOG_LEVEL'
    value: logLevel
  }
  {
    name: 'LOG_FORMAT'
    value: logFormat
  }
  {
    name: 'DATA_DIR'
    value: dataMountPath
  }
]

var telemetryEnv = empty(appInsightsConnectionString)
  ? []
  : [
      {
        name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
        value: appInsightsConnectionString
      }
    ]

var secretEnv = [
  for ref in keyVaultSecretRefs: {
    name: ref.envVarName
    secretRef: toLower(ref.secretName)
  }
]

var kvSecrets = [
  for ref in keyVaultSecretRefs: {
    name: toLower(ref.secretName)
    keyVaultUrl: '${keyVaultUri}secrets/${ref.secretName}'
    identity: identityId
  }
]

var ingressConfig = enableExternalIngress
  ? {
      external: true
      targetPort: targetPort
      transport: 'auto'
      allowInsecure: false
      traffic: [
        {
          latestRevision: true
          weight: 100
        }
      ]
    }
  : null

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: name
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  properties: {
    managedEnvironmentId: environmentId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: ingressConfig
      secrets: kvSecrets
      registries: [
        {
          server: registryServer
          identity: identityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: name
          image: image
          command: empty(command) ? null : command
          env: concat(baseEnv, telemetryEnv, secretEnv)
          resources: {
            cpu: json(cpuCores)
            memory: memory
          }
          volumeMounts: [
            {
              volumeName: 'data'
              mountPath: dataMountPath
            }
          ]
        }
      ]
      volumes: [
        {
          name: 'data'
          storageType: 'AzureFile'
          storageName: storageDefinitionName
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
      }
    }
  }
}

@description('Resource id of the container app.')
output id string = app.id

@description('Container app name.')
output name string = app.name

@description('Public FQDN when ingress is enabled, else empty.')
output fqdn string = enableExternalIngress ? app.properties.configuration.ingress.fqdn : ''
