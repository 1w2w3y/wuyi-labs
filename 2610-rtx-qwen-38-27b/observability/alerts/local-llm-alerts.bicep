// Prometheus alert rules for a local model served by vLLM on a Windows laptop
// with unified memory, evaluated by Azure Monitor against the workspace that
// the OpenTelemetry Collector sends metrics to.
//
//   az deployment group create -g <resource-group> -f observability/alerts/local-llm-alerts.bicep \
//     -p azureMonitorWorkspaceId=<workspace-resource-id> actionGroupId=<action-group-resource-id>
//
// Every rule groups by machinename, so one rule group covers several laptops.
// No data means no alert: a laptop that is asleep or not serving stays quiet.

@description('Resource ID of the Azure Monitor workspace that receives the laptop metrics.')
param azureMonitorWorkspaceId string

@description('Resource ID of an action group to notify. Leave empty to record alerts without notifications.')
param actionGroupId string = ''

@description('The Azure Monitor workspace region.')
param location string = resourceGroup().location

var actions = empty(actionGroupId) ? [] : [
  {
    actionGroupId: actionGroupId
  }
]

var resolve = {
  autoResolved: true
  timeToResolve: 'PT10M'
}

resource localLlmAlerts 'Microsoft.AlertsManagement/prometheusRuleGroups@2023-03-01' = {
  name: 'local-llm-laptop'
  location: location
  properties: {
    description: 'Memory pressure, slow first tokens, preemptions and stale GPU metrics on laptops that serve a local model.'
    scopes: [
      azureMonitorWorkspaceId
    ]
    enabled: true
    interval: 'PT1M'
    rules: [
      {
        // Windows, WSL, the GPU's share and shared GPU memory all come out of
        // one pool. vLLM's startup dips below this for a moment; five
        // minutes is sustained pressure.
        alert: 'LocalLlmWindowsMemoryLow'
        expression: 'min by (machinename) (windows_memory_available_bytes) < 2 * 1024 * 1024 * 1024'
        for: 'PT5M'
        severity: 2
        annotations: {
          description: 'Windows has had less than 2 GiB of memory available for 5 minutes on {{ $labels.machinename }}. Close other work, lower the WSL memory cap, or stop the model.'
        }
        enabled: true
        actions: actions
        resolveConfiguration: resolve
      }
      {
        // In the blog post's runs, the agent dropped requests that had waited
        // about five minutes for a first token, and neither vLLM nor LiteLLM
        // logged an error, so warn well before that.
        alert: 'LocalLlmSlowFirstToken'
        expression: 'histogram_sum(sum by (machinename) (rate(vllm:time_to_first_token_seconds[10m]))) / histogram_count(sum by (machinename) (rate(vllm:time_to_first_token_seconds[10m]))) > 120'
        for: 'PT5M'
        severity: 3
        annotations: {
          description: 'Requests on {{ $labels.machinename }} waited more than 2 minutes for their first token on average over 10 minutes. Too many parallel subagents are queuing; reduce the agent\'s fan-out.'
        }
        enabled: true
        actions: actions
        resolveConfiguration: resolve
      }
      {
        alert: 'LocalLlmPreemptions'
        expression: 'sum by (machinename) (increase(vllm:num_preemptions_total[10m])) > 0'
        for: 'PT1M'
        severity: 3
        annotations: {
          description: 'vLLM on {{ $labels.machinename }} preempted requests in the last 10 minutes: the KV cache ran out of room and work will be recomputed.'
        }
        enabled: true
        actions: actions
        resolveConfiguration: resolve
      }
      {
        // windows_exporter keeps serving the last file if the collector
        // stops, so check the file's age as well as its success flag.
        alert: 'LocalLlmGpuMetricsStale'
        expression: '(time() - max by (machinename) (nvidia_gpu_exporter_last_success_unixtime_seconds) > 120) or (max by (machinename) (nvidia_gpu_exporter_up) == 0)'
        for: 'PT5M'
        severity: 4
        annotations: {
          description: 'GPU power, temperature and memory on {{ $labels.machinename }} have not updated for 5 minutes. Check the LocalLlm-NvidiaGpuMetrics scheduled task.'
        }
        enabled: true
        actions: actions
        resolveConfiguration: resolve
      }
    ]
  }
}

output ruleGroupId string = localLlmAlerts.id
