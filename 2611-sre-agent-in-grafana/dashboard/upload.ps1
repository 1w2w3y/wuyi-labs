# Uploads the Orders API dashboard to an Azure Managed Grafana workspace with the Grafana HTTP API.
# Needs the Grafana Editor or Grafana Admin role on the workspace, and Node.js to build the JSON.
param(
    [Parameter(Mandatory)] [string] $GrafanaEndpoint,   # https://<name>-<id>.<region>.grafana.azure.com
    [Parameter(Mandatory)] [string] $SubscriptionId,
    [string] $ResourceGroup = 'orders-api-demo-rg',
    [Parameter(Mandatory)] [string] $AppName,
    [string] $Location = 'westus3',
    [string] $Folder = 'demo'
)

$env:DEMO_SUBSCRIPTION = $SubscriptionId
$env:DEMO_RESOURCE_GROUP = $ResourceGroup
$env:DEMO_APP = $AppName
$env:DEMO_LOCATION = $Location
$dashboard = node "$PSScriptRoot\build-dashboard.js" | Out-String | ConvertFrom-Json -Depth 50

# ce34e7e5-485f-4d76-964f-b3d2b16d1e4f is the Azure Managed Grafana application ID.
$token = az account get-access-token --resource ce34e7e5-485f-4d76-964f-b3d2b16d1e4f --query accessToken -o tsv
$headers = @{ Authorization = "Bearer $token" }

$folderUid = (Invoke-RestMethod "$GrafanaEndpoint/api/folders" -Headers $headers | Where-Object title -eq $Folder).uid
if (-not $folderUid) {
    $folderUid = (Invoke-RestMethod "$GrafanaEndpoint/api/folders" -Method Post -Headers $headers -ContentType 'application/json' -Body (@{ title = $Folder } | ConvertTo-Json)).uid
}

$body = @{ dashboard = $dashboard; folderUid = $folderUid; overwrite = $true } | ConvertTo-Json -Depth 60 -Compress
$result = Invoke-RestMethod "$GrafanaEndpoint/api/dashboards/db" -Method Post -Headers $headers -ContentType 'application/json' -Body $body
"$GrafanaEndpoint$($result.url)"
