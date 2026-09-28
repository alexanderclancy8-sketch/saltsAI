// Jarvis on Azure App Service (Linux, UK South) + a storage account for the report archive.
// Deploy:  az group create -n rg-jarvis -l uksouth
//          az deployment group create -g rg-jarvis -f infra/main.bicep -p appName=salts-jarvis ownerPassword=... ...
targetScope = 'resourceGroup'

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

var storageName = toLower(take(replace('${appName}store', '-', ''), 24))

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
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
  sku: { name: skuName }
  kind: 'linux'
  properties: { reserved: true }
}

resource app 'Microsoft.Web/sites@2023-12-01' = {
  name: appName
  location: location
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
      appSettings: [
        { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'true' }
        { name: 'WEBSITES_ENABLE_APP_SERVICE_STORAGE', value: 'true' }
        { name: 'WEBSITES_PORT', value: '8000' }
        { name: 'DATA_DIR', value: '/home/data' }
        { name: 'FORWARDED_ALLOW_IPS', value: '*' }
        { name: 'PUBLIC_BASE_URL', value: 'https://${appName}.azurewebsites.net' }
        { name: 'JARVIS_OWNER_PASSWORD', value: ownerPassword }
        { name: 'JARVIS_SECRET_KEY', value: secretKey }
        { name: 'STAFF_REPORT_KEY', value: staffReportKey }
        { name: 'CLAUDE_CODE_OAUTH_TOKEN', value: claudeCodeOauthToken }
        { name: 'ANTHROPIC_API_KEY', value: anthropicApiKey }
        { name: 'AZURE_STORAGE_CONNECTION_STRING', value: 'DefaultEndpointsProtocol=https;AccountName=${storage.name};AccountKey=${storage.listKeys().keys[0].value};EndpointSuffix=${environment().suffixes.storage}' }
      ]
    }
  }
}

output url string = 'https://${app.properties.defaultHostName}'
output principalId string = app.identity.principalId
