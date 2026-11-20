# Creates the Orders API demo in one resource group:
#   - a Linux web app (B1) running demo/app, with Application Insights and console logs in Log Analytics
#   - an Azure Monitor workspace that stores the app's Prometheus metrics
#   - a container group that runs Prometheus to scrape the app and remote-write to that workspace
# Then it grants two identities access: your Grafana workspace (to read) and your SRE agent (to restart).
#
# Needs: Azure CLI, Owner or User Access Administrator on the subscription, and a region with B1 quota.
param(
    [Parameter(Mandatory)] [string] $SubscriptionId,
    [Parameter(Mandatory)] [string] $AppName,                  # globally unique, becomes <AppName>.azurewebsites.net
    [string] $ResourceGroup = 'orders-api-demo-rg',
    [string] $Location = 'westus3',
    [string] $GrafanaPrincipalId,                              # managed identity of the Azure Managed Grafana workspace
    [string] $SreAgentPrincipalId                              # managed identity the SRE agent acts with
)

$ErrorActionPreference = 'Stop'
$work = Join-Path ([IO.Path]::GetTempPath()) "orders-api-demo"
New-Item -ItemType Directory -Force $work | Out-Null
$rgScope = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup"

az group create -n $ResourceGroup -l $Location --subscription $SubscriptionId -o none

# --- Logs and Application Insights ------------------------------------------
$workspaceId = az monitor log-analytics workspace create -g $ResourceGroup -n orders-api-logs -l $Location --subscription $SubscriptionId --query id -o tsv
az monitor app-insights component create -g $ResourceGroup -a orders-api-insights -l $Location --workspace $workspaceId --kind web --application-type web --subscription $SubscriptionId -o none
$connectionString = az monitor app-insights component show -g $ResourceGroup -a orders-api-insights --subscription $SubscriptionId --query connectionString -o tsv

# --- Web app ------------------------------------------------------------------
az appservice plan create -g $ResourceGroup -n orders-api-plan --is-linux --sku B1 -l $Location --subscription $SubscriptionId -o none
az webapp create -g $ResourceGroup -p orders-api-plan -n $AppName --runtime 'NODE:22-lts' --https-only true --subscription $SubscriptionId -o none
az webapp config set -g $ResourceGroup -n $AppName --always-on true --startup-file 'node server.js' --subscription $SubscriptionId -o none

# App settings go through a file: the connection string contains semicolons.
@{
    APPLICATIONINSIGHTS_CONNECTION_STRING      = $connectionString
    ApplicationInsightsAgent_EXTENSION_VERSION = '~3'      # Application Insights auto-instrumentation
    XDT_MicrosoftApplicationInsights_NodeJS    = '1'
    SCM_DO_BUILD_DURING_DEPLOYMENT             = 'false'   # the app has no dependencies to install
} | ConvertTo-Json | Set-Content "$work\settings.json"
az webapp config appsettings set -g $ResourceGroup -n $AppName --settings "@$work\settings.json" --subscription $SubscriptionId -o none

$siteId = az webapp show -g $ResourceGroup -n $AppName --subscription $SubscriptionId --query id -o tsv
'[{"category":"AppServiceConsoleLogs","enabled":true},{"category":"AppServiceHTTPLogs","enabled":true},{"category":"AppServicePlatformLogs","enabled":true}]' | Set-Content "$work\diag.json"
az monitor diagnostic-settings create --name to-log-analytics --resource $siteId --workspace $workspaceId --logs "@$work\diag.json" -o none

Compress-Archive -Path "$PSScriptRoot\app\*" -DestinationPath "$work\app.zip" -Force
az webapp deploy -g $ResourceGroup -n $AppName --src-path "$work\app.zip" --type zip --async true --subscription $SubscriptionId -o none

# --- Prometheus: Azure Monitor workspace + scraper ----------------------------
$amw = az monitor account create -g $ResourceGroup -n orders-api-prometheus -l $Location --subscription $SubscriptionId -o json | ConvertFrom-Json
$dcrId = $amw.defaultIngestionSettings.dataCollectionRuleResourceId
$dceId = $amw.defaultIngestionSettings.dataCollectionEndpointResourceId
$dcrImmutableId = az resource show --ids $dcrId --query properties.immutableId -o tsv
$ingestion = az resource show --ids $dceId --query properties.metricsIngestion.endpoint -o tsv

$scraper = az identity create -g $ResourceGroup -n orders-api-scraper-id -l $Location --subscription $SubscriptionId -o json | ConvertFrom-Json
Start-Sleep 20   # let the new identity replicate before assigning a role to it
az role assignment create --assignee-object-id $scraper.principalId --assignee-principal-type ServicePrincipal --role 'Monitoring Metrics Publisher' --scope $dcrId -o none

$prometheusYml = (Get-Content "$PSScriptRoot\prometheus\prometheus.yml" -Raw).
    Replace('__APP_HOST__', "$AppName.azurewebsites.net").
    Replace('__INGESTION_ENDPOINT__', $ingestion).
    Replace('__DCR_IMMUTABLE_ID__', $dcrImmutableId).
    Replace('__SCRAPER_CLIENT_ID__', $scraper.clientId)
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($prometheusYml))
(Get-Content "$PSScriptRoot\prometheus\container-group.yaml" -Raw).
    Replace('__LOCATION__', $Location).
    Replace('__SCRAPER_IDENTITY_ID__', $scraper.id).
    Replace('__PROMETHEUS_YML_BASE64__', $encoded) | Set-Content "$work\container-group.yaml"
az container create -g $ResourceGroup --file "$work\container-group.yaml" --subscription $SubscriptionId -o none

# --- Access -------------------------------------------------------------------
if ($GrafanaPrincipalId) {
    # Platform metrics, Application Insights and Log Analytics, then the Prometheus workspace.
    az role assignment create --assignee-object-id $GrafanaPrincipalId --assignee-principal-type ServicePrincipal --role 'Monitoring Reader' --scope $rgScope -o none
    az role assignment create --assignee-object-id $GrafanaPrincipalId --assignee-principal-type ServicePrincipal --role 'Monitoring Data Reader' --scope $amw.id -o none
}
if ($SreAgentPrincipalId) {
    # Scoped to this one resource group: read it, and manage (so, restart) its web apps.
    az role assignment create --assignee-object-id $SreAgentPrincipalId --assignee-principal-type ServicePrincipal --role 'Reader' --scope $rgScope -o none
    az role assignment create --assignee-object-id $SreAgentPrincipalId --assignee-principal-type ServicePrincipal --role 'Website Contributor' --scope $rgScope -o none
}

"Web app:              https://$AppName.azurewebsites.net/stats"
"Prometheus endpoint:  $($amw.metrics.prometheusQueryEndpoint)   (add as a Prometheus data source in Grafana, managed identity auth)"
"Next: node load/load.js https://$AppName.azurewebsites.net   and   dashboard/upload.ps1"
"Clean up: az group delete -n $ResourceGroup --subscription $SubscriptionId"
