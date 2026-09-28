// Jarvis on Azure App Service (Linux, UK South) + a storage account for the report archive.
// Everything gets its own plan and is tagged app=jarvis; nothing here touches other web apps (e.g. Salts FSM).
// Easiest route: `bash infra/deploy.sh` in Azure Cloud Shell, which checks names before creating anything.
targetScope = 'resourceGroup'

@minLength(3)
@maxLength(40)
@description('Globally unique web app name, e.g. salts-jarvis')
param appName string
param location string = 'uksouth'
@description('B1 is enough for one office; P0v3 for more headroom')
param skuName string = 'B1'

@secure()
@description('Password for the Jarvis display')
param ownerPassword string
@secure()
param secretKey string = newGuid()
@secure()
@description('Key staff use on the /report page')
param staffReportKey string = ''
@secure()
@description('Claude Max/Pro subscription token from `claude setup-token` (leave blank to use an API key)')
param claudeCodeOauthToken string = ''
@secure()
@description('Claude API key (only if not using the subscription)')
param anthropicApiKey string = ''

var storageName = toLower(take('${replace(appName, '-', '')}${uniqueString(resourceGroup().id)}', 24))
var tags = { app: 'jarvis' }

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
  }
}

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: '${appName}-plan'
  location: location
  tags: tags
  sku: { name: skuName }
  kind: 'linux'
  properties: { reserved: true }
}

resource app 'Microsoft.Web/sites@2023-12-01' = {
  name: appName
  location: location
  tags: tags
  kind: 'app,linux'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.12'
      appCommandLine: 'python -m jarvis'
      alwaysOn: true
      webSocketsEnabled: true
      healthCheckPath: '/healthz'
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      // Optional secrets are only added when given: an empty ANTHROPIC_API_KEY would still be seen by Claude Code.
      appSettings: concat([
        { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'true' }
        { name: 'WEBSITES_ENABLE_APP_SERVICE_STORAGE', value: 'true' }
        { name: 'WEBSITES_PORT', value: '8000' }
        { name: 'DATA_DIR', value: '/home/data' }
        { name: 'FORWARDED_ALLOW_IPS', value: '*' }
        { name: 'PUBLIC_BASE_URL', value: 'https://${appName}.azurewebsites.net' }
        { name: 'JARVIS_OWNER_PASSWORD', value: ownerPassword }
        { name: 'JARVIS_SECRET_KEY', value: secretKey }
        { name: 'AZURE_STORAGE_CONNECTION_STRING', value: 'DefaultEndpointsProtocol=https;AccountName=${storage.name};AccountKey=${storage.listKeys().keys[0].value};EndpointSuffix=${environment().suffixes.storage}' }
      ], empty(staffReportKey) ? [] : [
        { name: 'STAFF_REPORT_KEY', value: staffReportKey }
      ], empty(claudeCodeOauthToken) ? [] : [
        { name: 'CLAUDE_CODE_OAUTH_TOKEN', value: claudeCodeOauthToken }
      ], empty(anthropicApiKey) ? [] : [
        { name: 'ANTHROPIC_API_KEY', value: anthropicApiKey }
      ])
    }
  }
}

output url string = 'https://${app.properties.defaultHostName}'
output principalId string = app.identity.principalId
