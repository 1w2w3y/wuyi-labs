// Generates orders-api.json, the Grafana dashboard for the Orders API demo.
// Usage: node build-dashboard.js > orders-api.json
// The dashboard reads four sources through two Grafana data sources:
//   Azure Monitor data source: platform metrics, Application Insights logs, Log Analytics logs
//   Prometheus data source:    the app's own /metrics, stored in an Azure Monitor workspace

const AM = { type: 'grafana-azure-monitor-datasource', uid: '${am_ds}' };
const PROM = { type: 'prometheus', uid: '${prom_ds}' };

const SITE = '/subscriptions/$sub/resourceGroups/$rg/providers/Microsoft.Web/sites/$app';
const INSIGHTS = '/subscriptions/$sub/resourceGroups/$rg/providers/microsoft.insights/components/$insights';
const WORKSPACE = '/subscriptions/$sub/resourceGroups/$rg/providers/Microsoft.OperationalInsights/workspaces/$workspace';

const GREEN = '#37872D', RED = '#C4162A', ORANGE = '#FA6400', BLUE = '#1F60C4', PURPLE = '#8F3BB8', YELLOW = '#E0B400';

let nextId = 1;
const panels = [];
let y = 0;

function row(title) {
  panels.push({ id: nextId++, type: 'row', title, collapsed: false, gridPos: { h: 1, w: 24, x: 0, y }, panels: [] });
  y += 1;
}

function place(panel, x, w, h) {
  panels.push({ id: nextId++, ...panel, gridPos: { h, w, x, y } });
}

// ---- query helpers -------------------------------------------------------

function metric(refId, resourceType, resourceName, metricName, aggregation) {
  return {
    refId, datasource: AM, queryType: 'Azure Monitor', subscription: '$sub',
    azureMonitor: {
      resources: [{ subscription: '$sub', resourceGroup: '$rg', resourceName, metricNamespace: resourceType }],
      metricNamespace: resourceType, metricName, aggregation, timeGrain: 'auto', region: '$region',
      allowedTimeGrainsMs: [60000, 300000, 900000, 1800000, 3600000],
    },
  };
}

function logs(refId, resource, query, resultFormat) {
  return {
    refId, datasource: AM, queryType: 'Azure Log Analytics',
    azureLogAnalytics: { query, resources: [resource], resultFormat: resultFormat || 'time_series', dashboardTime: false },
  };
}

function prom(refId, expr, legendFormat) {
  return { refId, datasource: PROM, expr, legendFormat, range: true, editorMode: 'code' };
}

// Order lookups only: leaves out the /metrics scrapes and health probes.
const LOOKUPS = `requests
| where $__timeFilter(timestamp)
| where url has "/api/orders/"`;

// ---- panel helpers -------------------------------------------------------

function nameByRef(refId, name, color) {
  const properties = [{ id: 'displayName', value: name }];
  if (color) properties.push({ id: 'color', value: { mode: 'fixed', fixedColor: color } });
  return { matcher: { id: 'byFrameRefID', options: refId }, properties };
}

function stat({ title, description, datasource, targets, unit, decimals, steps, overrides, graph = true }) {
  return {
    type: 'stat', title, description, datasource, targets,
    fieldConfig: {
      defaults: { unit, decimals, color: { mode: 'thresholds' }, thresholds: { mode: 'absolute', steps }, noValue: 'no data' },
      overrides: overrides || [],
    },
    options: {
      colorMode: 'background_solid', graphMode: graph ? 'area' : 'none', justifyMode: 'center', textMode: 'value',
      reduceOptions: { calcs: ['lastNotNull'], fields: '', values: false },
    },
  };
}

function timeseries({ title, description, datasource, targets, unit, overrides, min, stack, bars, thresholdLine, decimals }) {
  const custom = {
    drawStyle: bars ? 'bars' : 'line', lineWidth: bars ? 0 : 2, fillOpacity: bars ? 85 : 12, gradientMode: bars ? 'none' : 'opacity',
    lineInterpolation: 'smooth', showPoints: 'never', spanNulls: 180000,
    stacking: { mode: stack ? 'normal' : 'none', group: 'A' },
    thresholdsStyle: { mode: thresholdLine ? 'dashed' : 'off' },
  };
  const defaults = { unit, min, decimals, custom, color: { mode: 'palette-classic' } };
  if (thresholdLine) {
    defaults.thresholds = { mode: 'absolute', steps: [{ color: 'transparent', value: null }, { color: RED, value: thresholdLine }] };
  }
  return {
    type: 'timeseries', title, description, datasource, targets,
    fieldConfig: { defaults, overrides: overrides || [] },
    options: {
      legend: { displayMode: 'list', placement: 'bottom', showLegend: true },
      tooltip: { mode: 'multi', sort: 'desc' },
    },
  };
}

// ---- Row 1: status -------------------------------------------------------

row('Status');

place(stat({
  title: 'Lookup success rate',
  description: 'Share of order lookups that returned a success code, per minute. Source: Application Insights requests.',
  datasource: AM, unit: 'percent', decimals: 1,
  steps: [{ color: RED, value: null }, { color: ORANGE, value: 95 }, { color: GREEN, value: 99 }],
  targets: [logs('A', INSIGHTS, `${LOOKUPS}
| where timestamp < bin(now(), 1m)
| summarize ['Success rate'] = 100.0 * countif(success == true) / count() by bin(timestamp, 1m)
| order by timestamp asc`)],
}), 0, 4, 5);

place(stat({
  title: 'Lookup time (P95)',
  description: 'P95 server-side duration of order lookups, per minute. A lookup that runs past 1.5 s fails with HTTP 503. Source: Application Insights requests.',
  datasource: AM, unit: 'ms', decimals: 0,
  steps: [{ color: GREEN, value: null }, { color: ORANGE, value: 500 }, { color: RED, value: 1200 }],
  targets: [logs('A', INSIGHTS, `${LOOKUPS}
| where timestamp < bin(now(), 1m)
| summarize P95 = percentile(duration, 95) by bin(timestamp, 1m)
| order by timestamp asc`)],
}), 4, 4, 5);

place(stat({
  title: 'Requests per minute',
  description: 'Requests the web app received, from the App Service platform metric Requests. Includes the metrics scraper and health probes.',
  datasource: AM, unit: 'short', decimals: 0,
  steps: [{ color: BLUE, value: null }],
  targets: [metric('A', 'microsoft.web/sites', '$app', 'Requests', 'Total')],
}), 8, 4, 5);

place(stat({
  title: 'Memory working set',
  description: 'Memory the web app is using, from the App Service platform metric MemoryWorkingSet. The B1 plan has 1.75 GB.',
  datasource: AM, unit: 'bytes', decimals: 0,
  steps: [{ color: GREEN, value: null }, { color: ORANGE, value: 400 * 1048576 }, { color: RED, value: 700 * 1048576 }],
  targets: [metric('A', 'microsoft.web/sites', '$app', 'MemoryWorkingSet', 'Average')],
}), 12, 4, 5);

place(stat({
  title: 'Cached orders',
  description: 'Orders held in the in-process order cache. Goes back to 0 when the process restarts. Source: the app\'s Prometheus gauge orders_cache_entries.',
  datasource: PROM, unit: 'short', decimals: 0,
  steps: [{ color: GREEN, value: null }, { color: ORANGE, value: 500 }, { color: RED, value: 1200 }],
  targets: [prom('A', 'max(orders_cache_entries{job="orders-api"})', 'Cached orders')],
}), 16, 4, 5);

place(stat({
  title: 'Process uptime',
  description: 'Time since the Node.js process last started. A drop to zero is a restart. Source: the app\'s Prometheus gauge orders_process_uptime_seconds.',
  datasource: PROM, unit: 's', decimals: 0, graph: false,
  steps: [{ color: PURPLE, value: null }],
  targets: [prom('A', 'max(orders_process_uptime_seconds{job="orders-api"})', 'Uptime')],
}), 20, 4, 5);

y += 5;

// ---- Row 2: Application Insights ----------------------------------------

row('What callers see · Application Insights');

place(timeseries({
  title: 'Order lookups by result code',
  description: 'Order lookups per minute, split by HTTP result code. 503 is a lookup that ran past its 1.5 s deadline (OrderCacheLookupTimeout). Source: Application Insights requests.',
  datasource: AM, unit: 'short', min: 0, stack: true, bars: true,
  targets: [logs('A', INSIGHTS, `${LOOKUPS}
| summarize Lookups = count() by bin(timestamp, 1m), resultCode
| order by timestamp asc`)],
  overrides: [
    { matcher: { id: 'byRegexp', options: '.*200.*' }, properties: [{ id: 'color', value: { mode: 'fixed', fixedColor: GREEN } }, { id: 'displayName', value: '200 OK' }] },
    { matcher: { id: 'byRegexp', options: '.*503.*' }, properties: [{ id: 'color', value: { mode: 'fixed', fixedColor: RED } }, { id: 'displayName', value: '503 lookup timeout' }] },
  ],
}), 0, 12, 8);

place(timeseries({
  title: 'Order lookup time (P50 / P95 / P99)',
  description: 'Server-side duration of order lookups. The dashed line is the 1.5 s lookup deadline: past it, the lookup fails with HTTP 503. Source: Application Insights requests.',
  datasource: AM, unit: 'ms', min: 0, thresholdLine: 1500,
  targets: [logs('A', INSIGHTS, `${LOOKUPS}
| summarize P50 = percentile(duration, 50), P95 = percentile(duration, 95), P99 = percentile(duration, 99) by bin(timestamp, 1m)
| order by timestamp asc`)],
  overrides: [
    { matcher: { id: 'byName', options: 'P50' }, properties: [{ id: 'color', value: { mode: 'fixed', fixedColor: GREEN } }] },
    { matcher: { id: 'byName', options: 'P95' }, properties: [{ id: 'color', value: { mode: 'fixed', fixedColor: ORANGE } }] },
    { matcher: { id: 'byName', options: 'P99' }, properties: [{ id: 'color', value: { mode: 'fixed', fixedColor: RED } }] },
  ],
}), 12, 12, 8);

y += 8;

// ---- Row 3: platform metrics --------------------------------------------

row('What the platform sees · Azure Monitor metrics');

place(timeseries({
  title: 'Web app memory working set',
  description: 'Memory used by the web app\'s processes. Source: App Service platform metric MemoryWorkingSet (Microsoft.Web/sites).',
  datasource: AM, unit: 'bytes', min: 0,
  targets: [metric('A', 'microsoft.web/sites', '$app', 'MemoryWorkingSet', 'Average')],
  overrides: [nameByRef('A', 'Memory working set', PURPLE)],
}), 0, 8, 8);

place(timeseries({
  title: 'App Service plan CPU and memory',
  description: 'CPU and memory use of the App Service plan\'s single B1 instance. Source: platform metrics CpuPercentage and MemoryPercentage (Microsoft.Web/serverfarms).',
  datasource: AM, unit: 'percent', min: 0,
  targets: [
    metric('A', 'microsoft.web/serverfarms', '$plan', 'CpuPercentage', 'Average'),
    metric('B', 'microsoft.web/serverfarms', '$plan', 'MemoryPercentage', 'Average'),
  ],
  overrides: [nameByRef('A', 'CPU %', BLUE), nameByRef('B', 'Memory %', PURPLE)],
}), 8, 8, 8);

place(timeseries({
  title: 'HTTP 5xx and response time at the front end',
  description: 'Server errors and average response time as measured by the App Service front end, before the app\'s own telemetry. Source: platform metrics Http5xx and HttpResponseTime (Microsoft.Web/sites).',
  datasource: AM, unit: 'short', min: 0,
  targets: [
    metric('A', 'microsoft.web/sites', '$app', 'Http5xx', 'Total'),
    metric('B', 'microsoft.web/sites', '$app', 'HttpResponseTime', 'Average'),
  ],
  overrides: [
    nameByRef('A', 'HTTP 5xx per minute', RED),
    { matcher: { id: 'byFrameRefID', options: 'B' }, properties: [
      { id: 'displayName', value: 'Average response time' }, { id: 'unit', value: 's' },
      { id: 'custom.axisPlacement', value: 'right' }, { id: 'color', value: { mode: 'fixed', fixedColor: ORANGE } },
      { id: 'custom.fillOpacity', value: 0 },
    ] },
  ],
}), 16, 8, 8);

y += 8;

// ---- Row 4: Prometheus ---------------------------------------------------

row('What the app says about itself · Prometheus');

place(timeseries({
  title: 'Order cache: entries and size',
  description: 'Orders held in the in-process cache, and the bytes they take. Both go back to 0 when the process restarts. Source: Prometheus gauges orders_cache_entries and orders_cache_bytes.',
  datasource: PROM, unit: 'short', min: 0,
  targets: [
    prom('A', 'max(orders_cache_entries{job="orders-api"})', 'Cached orders'),
    prom('B', 'max(orders_cache_bytes{job="orders-api"})', 'Cache size'),
  ],
  overrides: [
    { matcher: { id: 'byName', options: 'Cached orders' }, properties: [{ id: 'color', value: { mode: 'fixed', fixedColor: YELLOW } }] },
    { matcher: { id: 'byName', options: 'Cache size' }, properties: [
      { id: 'unit', value: 'bytes' }, { id: 'custom.axisPlacement', value: 'right' },
      { id: 'color', value: { mode: 'fixed', fixedColor: PURPLE } }, { id: 'custom.fillOpacity', value: 0 },
    ] },
  ],
}), 0, 8, 8);

place(timeseries({
  title: 'Lookup duration from the app (P50 / P95)',
  description: 'Lookup time as the app measures it, from the histogram orders_lookup_duration_seconds. The largest finite bucket is 2.5 s. The dashed line is the 1.5 s lookup deadline.',
  datasource: PROM, unit: 's', min: 0, thresholdLine: 1.5,
  targets: [
    prom('A', 'histogram_quantile(0.5, sum by (le) (rate(orders_lookup_duration_seconds_bucket{job="orders-api"}[2m])))', 'P50'),
    prom('B', 'histogram_quantile(0.95, sum by (le) (rate(orders_lookup_duration_seconds_bucket{job="orders-api"}[2m])))', 'P95'),
  ],
  overrides: [
    { matcher: { id: 'byName', options: 'P50' }, properties: [{ id: 'color', value: { mode: 'fixed', fixedColor: GREEN } }] },
    { matcher: { id: 'byName', options: 'P95' }, properties: [{ id: 'color', value: { mode: 'fixed', fixedColor: ORANGE } }] },
  ],
}), 8, 8, 8);

place(timeseries({
  title: 'Lookups per second by status',
  description: 'Rate of order lookups the app completed, by HTTP status. Throughput falls when lookups slow down, because callers wait for each answer. Source: Prometheus counter orders_requests_total.',
  datasource: PROM, unit: 'reqps', min: 0, stack: true, decimals: 2,
  targets: [prom('A', 'sum by (code) (rate(orders_requests_total{job="orders-api"}[2m]))', '{{code}}')],
  overrides: [
    { matcher: { id: 'byName', options: '200' }, properties: [{ id: 'color', value: { mode: 'fixed', fixedColor: GREEN } }] },
    { matcher: { id: 'byName', options: '503' }, properties: [{ id: 'color', value: { mode: 'fixed', fixedColor: RED } }] },
  ],
}), 16, 8, 8);

y += 8;

// ---- Row 5: logs ---------------------------------------------------------

row('What the logs say · Log Analytics');

place({
  type: 'logs', title: 'Web app console log',
  description: 'Standard output and error of the Node.js process. The app logs its cache size every 60 lookups and one line for each failed lookup. Source: AppServiceConsoleLogs in the Log Analytics workspace.',
  datasource: AM,
  targets: [logs('A', WORKSPACE, `AppServiceConsoleLogs
| where $__timeFilter(TimeGenerated)
| project TimeGenerated, Message = ResultDescription, Level
| order by TimeGenerated desc
| take 300`, 'logs')],
  options: { showTime: true, wrapLogMessage: true, sortOrder: 'Descending', dedupStrategy: 'none', enableLogDetails: true, prettifyLogMessage: false },
}, 0, 14, 9);

place({
  type: 'table', title: 'Console errors by kind',
  description: 'Error lines in the console log, grouped by the error name at the start of the message. Source: AppServiceConsoleLogs in the Log Analytics workspace.',
  datasource: AM,
  targets: [logs('A', WORKSPACE, `AppServiceConsoleLogs
| where $__timeFilter(TimeGenerated)
| where Level =~ "Error"
| extend Kind = extract(@"^([A-Z][A-Za-z]+):", 1, ResultDescription)
| where isnotempty(Kind)
| summarize Count = count(), ['First seen'] = min(TimeGenerated), ['Last seen'] = max(TimeGenerated), Example = take_any(ResultDescription) by Kind
| order by Count desc`, 'table')],
  fieldConfig: { defaults: { custom: { cellOptions: { type: 'auto' }, filterable: false } }, overrides: [
    { matcher: { id: 'byName', options: 'Count' }, properties: [{ id: 'custom.width', value: 80 }] },
    { matcher: { id: 'byName', options: 'Kind' }, properties: [{ id: 'custom.width', value: 220 }] },
  ] },
  options: { showHeader: true, cellHeight: 'sm' },
}, 14, 10, 9);

y += 9;

// ---- dashboard -----------------------------------------------------------

function constant(name, label, value) {
  return { name, label, type: 'constant', query: value, current: { text: value, value }, hide: 2 };
}

const dashboard = {
  uid: 'orders-api', title: 'Orders API', tags: ['orders-api', 'app-service', 'application-insights', 'prometheus'],
  description: 'Health of the Orders API web app: what callers see (Application Insights), what the platform sees (Azure Monitor metrics), what the app says about itself (Prometheus) and what the logs say (Log Analytics).',
  timezone: 'utc', editable: true, graphTooltip: 1, schemaVersion: 39, version: 1,
  time: { from: 'now-1h', to: 'now' }, refresh: '30s',
  annotations: { list: [
    { builtIn: 1, datasource: { type: 'grafana', uid: '-- Grafana --' }, enable: true, hide: true, iconColor: 'rgba(0, 211, 255, 1)', name: 'Annotations & Alerts', type: 'dashboard' },
    { name: 'Process start', datasource: PROM, enable: true, iconColor: BLUE,
      expr: 'max(orders_process_uptime_seconds{job="orders-api"}) < 75', step: '60s', titleFormat: 'Process started', textFormat: 'The Node.js process started (restart or deployment).' },
  ] },
  templating: { list: [
    { name: 'am_ds', label: 'Azure Monitor', type: 'datasource', query: 'grafana-azure-monitor-datasource', current: { text: 'Azure Monitor', value: 'azure-monitor' }, hide: 2 },
    { name: 'prom_ds', label: 'Prometheus', type: 'datasource', query: 'prometheus', current: { text: 'Orders API Prometheus', value: 'orders-api-prometheus' }, hide: 2 },
    constant('sub', 'Subscription', process.env.DEMO_SUBSCRIPTION || '<subscription-id>'),
    constant('rg', 'Resource group', process.env.DEMO_RESOURCE_GROUP || 'orders-api-demo-rg'),
    constant('region', 'Region', process.env.DEMO_LOCATION || 'westus3'),
    constant('app', 'Web app', process.env.DEMO_APP || '<web-app-name>'),
    constant('plan', 'App Service plan', 'orders-api-plan'),
    constant('insights', 'Application Insights', 'orders-api-insights'),
    constant('workspace', 'Log Analytics workspace', 'orders-api-logs'),
  ] },
  panels,
};

process.stdout.write(JSON.stringify(dashboard, null, 2) + '\n');
