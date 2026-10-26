# Qwen3.8-27B on an RTX Spark laptop

Sample code for the blog post *Qwen3.8-27B on a laptop: run it on RTX Spark, watch it in Azure Monitor*.

It serves [Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B) on a Windows on Arm laptop with NVIDIA
RTX Spark and 64 GB of unified memory, with Docker Compose, for one coding agent at a time. It also
watches the laptop the way you'd watch a server, with an OpenTelemetry Collector that sends metrics and
LiteLLM's traces to Azure Monitor and Application Insights over OTLP:

```text
coding agent (OpenCode)
  -> LiteLLM gateway   127.0.0.1:4000   alias "local-llm", one JSON line per request, /metrics/
    -> vLLM            127.0.0.1:8096   Unsloth NVFP4 checkpoint + DFlash 2 drafter (FP8),
                                        262,144-token context, one request at a time

Windows (host)                                     Docker Desktop (WSL 2, linux/arm64)
  nvidia-smi -> scripts\Export-NvidiaGpuMetrics.ps1     vLLM /metrics   LiteLLM /metrics/ + spans (OTLP)
    -> C:\ProgramData\local-llm\textfile                      \             /
  windows_exporter 127.0.0.1:9182  <-------------------  OpenTelemetry Collector (contrib)
                                                              |  OTLP, Microsoft Entra ID
                                                              v
                       Application Insights with OTLP support
                         metrics -> Azure Monitor workspace (managed Prometheus)
                                      -> Azure Managed Grafana: observability/grafana/rtx-spark-qwen38.json
                                      -> Prometheus alert rules: observability/alerts/local-llm-alerts.bicep
                         traces  -> Application Insights (Log Analytics)
```

| Path | What it is |
|---|---|
| `compose.yaml` | vLLM and the LiteLLM gateway, with the profile the post measured |
| `images/vllm/Dockerfile` | vLLM 0.31 (official ARM64 image) with both checkpoints downloaded at pinned revisions |
| `litellm/` | gateway config (`config.otel.yaml` adds OpenTelemetry spans), and a callback that logs one JSON line and `litellm_e2e_*` metrics per request |
| `config/wslconfig.example`, `config/opencode.json` | the WSL memory cap and the OpenCode provider block |
| `compose.observability.yaml`, `observability/otelcol/config.yaml` | the OpenTelemetry Collector: scrape, receive spans, label, send over OTLP to Azure Monitor |
| `observability/windows/windows-exporter.yaml` | windows_exporter: memory, GPU adapter memory, and the GPU textfile |
| `scripts/Export-NvidiaGpuMetrics.ps1`, `scripts/Install-NvidiaGpuMetricsTask.ps1` | GPU power, temperature, clocks and memory from `nvidia-smi`, as a background task |
| `observability/grafana/rtx-spark-qwen38.json` | the dashboard in the post's screenshots, ready to import |
| `observability/alerts/local-llm-alerts.bicep` | four Prometheus alert rules for a laptop |
| `scripts/Test-LocalLlm.ps1` | smoke test: exact answer, image, required tool call, metrics endpoints |
| `scripts/Invoke-SpeedBench.ps1`, `scripts/speed_bench.py` | the post's code and long-prompt speed benchmarks |

## Prerequisites

- A laptop with NVIDIA RTX Spark and 64 GB of unified memory, on Windows 11 on Arm.
- NVIDIA's current Windows driver and the NVIDIA app.
- Docker Desktop with the WSL 2 backend.
- Disk space for the vLLM image plus about 23 GB of checkpoints.
- For monitoring: an Application Insights resource with OTLP support (which comes with an Azure Monitor
  workspace), an Azure Managed Grafana instance that can read that workspace, and permission to create a
  Microsoft Entra ID application.

## Run it

**1. Give the GPU 32 GB.** In the NVIDIA app, set **Dedicated GPU Memory (GB)** under **System** >
**Advanced** to 32, and restart. Then cap the WSL virtual machine, so it doesn't compete with the model for the same memory:
copy `config/wslconfig.example` to `%UserProfile%\.wslconfig`, or merge it into the one you have, and run:

```powershell
wsl --shutdown
```

**2. Build and serve.** The build downloads both checkpoints into the image, so the stack starts offline.
Stop the model before large downloads or builds: they need memory of their own.

```powershell
Copy-Item .env.example .env
docker compose build
docker compose up -d
docker compose ps                     # qwen3.8-27b turns healthy after a few minutes
docker logs qwen3.8-27b 2>&1 | Select-String 'KV cache size|Maximum concurrency'
```

The startup log should report 263,927 tokens of KV cache, 1.01 full-length requests.

**3. Check it,** then point your agent at it. Merge `config/opencode.json` into your OpenCode
configuration; from another machine, use the laptop's LAN address and see `LITELLM_HOST_BIND` in `.env`.

```powershell
.\scripts\Test-LocalLlm.ps1
```

The metrics checks at the end pass once you've set up monitoring below.

## Monitor it

**1. windows_exporter.** Install the ARM64 MSI from the
[windows_exporter releases](https://github.com/prometheus-community/windows_exporter/releases), then, in
an elevated PowerShell:

```powershell
Copy-Item observability\windows\windows-exporter.yaml 'C:\Program Files\windows_exporter\config.yaml'
Restart-Service windows_exporter
```

**2. GPU metrics.** The scheduled task runs `nvidia-smi` every 5 seconds for the signed-in user and
writes `C:\ProgramData\local-llm\textfile\nvidia_gpu.prom`, which windows_exporter serves:

```powershell
.\scripts\Install-NvidiaGpuMetricsTask.ps1
(Invoke-WebRequest -UseBasicParsing http://127.0.0.1:9182/metrics).Content -split "`n" | Select-String '^nvidia_gpu_power'
```

**3. Somewhere to send it.** Create an Application Insights resource with **OTLP support** set to **On**
([Ingest OTLP data into Azure Monitor with the OpenTelemetry Collector](https://learn.microsoft.com/azure/azure-monitor/containers/opentelemetry-protocol-ingestion)).
That provisions an Azure Monitor workspace for metrics, a Log Analytics workspace for traces, and the
data collection endpoint and rule that route OTLP into them. On the resource's **Overview** page, copy
the application ID, the OTLP endpoints for traces and metrics, and the data collection rule ID. Then
create an application that may publish to that rule:

```powershell
az ad sp create-for-rbac --name local-llm-telemetry --role "Monitoring Metrics Publisher" --scopes <data-collection-rule-resource-id>
```

Put the application ID and the two endpoints into `.env` as `AZMON_APPLICATION_ID`,
`AZMON_TRACES_ENDPOINT` and `AZMON_METRICS_ENDPOINT`, and the output's `tenant`, `appId` and `password`
as `AZURE_TENANT_ID`, `AZURE_CLIENT_ID` and `AZURE_CLIENT_SECRET`. A new role assignment can take up to
about 30 minutes to take effect; until then the collector logs HTTP 403 from the ingestion endpoints.

**4. Start the collector** next to the model:

```powershell
docker compose -f compose.yaml -f compose.observability.yaml up -d
docker logs --since 5m otel-collector
```

The collector ([`observability/otelcol/config.yaml`](observability/otelcol/config.yaml)) scrapes vLLM,
LiteLLM and windows_exporter every 10 seconds. It forwards only the windows_exporter series the
dashboard and alerts use, since Azure Monitor bills per sample ingested, and labels every data point and
span with `machinename` (the Windows computer name, or `MACHINE_NAME` in `.env`). The overlay also
restarts LiteLLM with `litellm/config.otel.yaml`, which adds the `otel` callback, so every model call
becomes a span with its token counts and time to first token; find them in the Application Insights
transaction view or with KQL. With `NO_CONTENT`, spans carry no prompts or responses, but they do carry
the tool definitions the agent declares. Its health endpoint is http://127.0.0.1:13133/.

**5. Import the dashboard.** In Azure Managed Grafana, go to **Dashboards** > **New** > **Import** and
upload `observability/grafana/rtx-spark-qwen38.json`. At the top, pick the Prometheus data source of
the Azure Monitor workspace that Application Insights created in **Data source**; **Machine** and
**Model** fill in from the data. If that data source uses the Azure Monitor Managed Service for
Prometheus plugin, change the `datasource` variable's type to that plugin.

OTLP histograms arrive in the workspace as native histograms, with no `_sum`, `_count` or `_bucket`
series, so the dashboard's per-request panels use `histogram_sum` and `histogram_count`:

```promql
histogram_sum(sum(increase(vllm:request_generation_tokens[10m])))
  / histogram_sum(sum(increase(vllm:request_decode_time_seconds[10m])))
```

**6. Add alerts** (optional). The rule group evaluates in Azure Monitor, so it works while you're away
from the laptop:

```powershell
az deployment group create -g <resource-group> -f observability\alerts\local-llm-alerts.bicep `
  -p azureMonitorWorkspaceId=<workspace-resource-id> actionGroupId=<action-group-resource-id>
```

| Alert | Fires when |
|---|---|
| `LocalLlmWindowsMemoryLow` | Windows has had under 2 GiB available for 5 minutes |
| `LocalLlmSlowFirstToken` | the mean time to first token over 10 minutes passes 2 minutes |
| `LocalLlmPreemptions` | vLLM preempted a request in the last 10 minutes |
| `LocalLlmGpuMetricsStale` | the GPU metrics file hasn't updated for 5 minutes |

Each rule groups by `machinename`, so one rule group covers every laptop that writes to the workspace,
and a laptop that's asleep raises nothing.

## Measure it

**Speed.** The code benchmark sends three short code-generation prompts (512 tokens, temperature 0,
thinking off) straight to vLLM, one at a time; the long-prompt cases cut prompts from a Git repository's
text files, by default the one this sample is in:

```powershell
.\scripts\Invoke-SpeedBench.ps1 -Label baseline
```

Results land in `results/`, which Git ignores.

**Agent load.** [agent-bench](../agent-bench/) drives real OpenCode agents through the gateway with a
fixed work queue and reports both the load and how much review work the agents finished. Its
`agent-framework-review` and `typescript-review` scenarios review the same two repositories as the post's
agent session. Run it from a second machine, since each agent needs memory of its own, and start with
`AGENTS=1`: this profile runs one request at a time.

## What we measured

Lenovo Yoga 9n 2-in-1 with the 20-core RTX Spark N1X and 64 GB, 32 GB for the GPU, vLLM 0.31, the
NVFP4 checkpoint and the DFlash 2 drafter in FP8 proposing seven tokens, an FP8 KV cache:

| | Result |
|---|---|
| Code benchmark, one request | 44.7 tok/s decode, mean of three; 5.04 tokens per decode step |
| 98K-token prompt, one request | 44.3 tok/s decode, 1,033 tok/s prefill |
| Three hours of an OpenCode agent reviewing two repositories | about 27 tok/s decode per request; GPU 41 W on average, 67 °C at peak |
| KV cache | 263,927 tokens: one full-length request |

The two benchmarks ran on the 196,608-token build of this profile; on the 262,144-token profile here,
the code benchmark measured 44.1 tok/s.

## Access control

The LiteLLM gateway has no authentication. It listens on 127.0.0.1 unless you set `LITELLM_HOST_BIND`,
and then Windows Firewall should allow port 4000 only from a trusted subnet. vLLM, windows_exporter and
the collector's health endpoint listen on 127.0.0.1 only; the collector's OTLP receiver is reachable only
from the Compose network. `.env` holds the Entra ID application's secret and is ignored by Git.
