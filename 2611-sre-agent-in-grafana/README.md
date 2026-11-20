# Azure SRE Agent inside Azure Managed Grafana: the Orders API demo

Sample code for the blog post [*Introducing Azure SRE Agent in Azure Managed Grafana: The Dashboard as Command Center*](https://techcommunity.microsoft.com/blog/appsonazureblog/introducing-azure-sre-agent-in-azure-managed-grafana-the-dashboard-as-command-ce/4575039).

It is a small web app with a deliberate bug, a Grafana dashboard that watches it through four sources, and
the scripts that create both. With the [Azure SRE Agent integration](https://learn.microsoft.com/azure/managed-grafana/how-to-integrate-with-sre-agent)
enabled on the Grafana workspace, you can open the agent beside the dashboard, ask what is wrong, and
approve a restart without leaving the page:

```text
load/load.js  ->  Orders API (Azure App Service, Node.js)
                    |  requests            -> Application Insights (auto-instrumentation)
                    |  console log         -> Log Analytics workspace
                    |  platform metrics    -> Azure Monitor
                    |  /metrics            <- Prometheus (Azure Container Instances)
                    |                           -> remote write -> Azure Monitor workspace
                    v
        Azure Managed Grafana: dashboard/orders-api.json
          Azure Monitor data source   (metrics, Application Insights, Log Analytics)
          Prometheus data source      (the Azure Monitor workspace)
                    ^
        Azure SRE Agent pane: reads the dashboard and its data through the Grafana MCP endpoint,
                              restarts the web app with its own managed identity, after you approve
```

## What's here

| Path | What it is |
|---|---|
| `app/` | The Orders API: a dependency-free Node.js server with an order cache that is never evicted |
| `load/load.js` | A load generator that looks up a new order ID about twice a second |
| `prometheus/` | The Prometheus scrape config and the container group that runs it |
| `dashboard/build-dashboard.js` | Generates the Grafana dashboard JSON |
| `dashboard/orders-api.json` | The generated dashboard, ready to import |
| `dashboard/upload.ps1` | Uploads the dashboard through the Grafana HTTP API |
| `deploy.ps1` | Creates every Azure resource in one resource group |

## The bug

Every order that is looked up goes into an in-memory cache, and nothing is ever evicted. Each lookup
scans the whole cache. With the load generator running, memory and lookup time climb together for about
15 minutes on a B1 plan, until lookups pass their 1.5-second deadline and fail with HTTP 503
(`OrderCacheLookupTimeout`). Restarting the process empties the cache, so a restart brings the service
back for another 15 minutes. The fix is to bound the cache.

Three settings change the pace: `ORDER_ENTRY_KB` (size of one cached order, default 384),
`CACHE_SCAN_STEP` (smaller is slower, default 2) and `LOOKUP_DEADLINE_MS` (default 1500).

## The four sources

| Source | Reaches Grafana through | What it shows |
|---|---|---|
| Application Insights | Azure Monitor data source (logs) | Order lookups by result code, lookup time percentiles |
| App Service platform metrics | Azure Monitor data source (metrics) | Memory working set, plan CPU and memory, HTTP 5xx at the front end |
| The app's `/metrics` endpoint | Prometheus data source on an Azure Monitor workspace | Cached orders, cache size, lookup duration, process uptime |
| App Service console logs | Azure Monitor data source (Log Analytics) | The app's own log lines and error kinds |

The dashboard marks each process start with an annotation, so a restart shows as a vertical line on
every time series.

## Run it

You need the Azure CLI, PowerShell 7, Node.js 20 or later, an Azure Managed Grafana workspace in the
Standard tier, and Owner or User Access Administrator on the subscription for the role assignments.

```powershell
# 1. Create the resources (about 5 minutes). The two principal IDs are optional.
./deploy.ps1 -SubscriptionId <subscription-id> -AppName <unique-app-name> `
    -GrafanaPrincipalId <grafana-managed-identity-object-id> `
    -SreAgentPrincipalId <sre-agent-managed-identity-object-id>

# 2. In Grafana, add a Prometheus data source named "Orders API Prometheus" (UID orders-api-prometheus)
#    that points at the Prometheus endpoint the script prints, with managed identity authentication.

# 3. Upload the dashboard.
./dashboard/upload.ps1 -GrafanaEndpoint https://<your-grafana-endpoint> `
    -SubscriptionId <subscription-id> -AppName <unique-app-name>

# 4. Start traffic and watch the dashboard.
node load/load.js https://<unique-app-name>.azurewebsites.net
```

`deploy.ps1` defaults to West US 3 and the resource group `orders-api-demo-rg`. Pass `-Location` and
`-ResourceGroup` to change them, and pass the same values to `upload.ps1`.

## Let the agent act

Reading works as soon as the agent is connected to the Grafana workspace and the integration is enabled.
To let the agent restart the web app:

- `deploy.ps1 -SreAgentPrincipalId` grants the agent's managed identity Reader and Website Contributor on
  the demo resource group and nothing else.
- In the SRE Agent portal, under **Tools**, turn on `RunAzCliReadCommands` and `RunAzCliWriteCommands`.
  Leave the write tool's permission on **Ask** so each command waits for your approval.

Then open the dashboard, select the **Azure SRE Agent** icon, and try:

> Order lookups have started failing. What's going on?

App Service starts the new process a minute or two after the restart call. If the agent checks sooner
than that, it reports that the app has not recovered yet; ask it to check again.

## Clean up

```powershell
az group delete -n orders-api-demo-rg --subscription <subscription-id>
```

The Azure Monitor workspace also creates a managed resource group named
`MA_orders-api-prometheus_<region>_managed`. Deleting the workspace removes it. The web app's endpoints
have no authentication, so don't leave the demo running longer than you need it.
