# Qwen3.8-Flash-Next on two H100s in an Azure Spot VM

Sample code for the blog post [*Qwen3.8-Flash-Next on two H100s in an Azure Spot VM*](https://techcommunity.microsoft.com/blog/appsonazureblog/qwen3-8-flash-next-on-two-h100s-in-an-azure-spot-vm/4563893).

It serves [Qwen3.8-Flash-Next](https://huggingface.co/Qwen/Qwen3.8-Flash-Next-FP8) (official FP8 checkpoint), a
125B mixture-of-experts model with 6B parameters active per token, from one `Standard_NC80adis_H100_v5` VM
(2x H100 NVL, 94 GB each) on [Azure Spot Virtual Machines](https://learn.microsoft.com/azure/virtual-machines/spot-vms),
with Docker Compose:

```text
client
  -> ngrok public URL (IP allow-list)
    -> LiteLLM gateway  127.0.0.1:4000   alias "local-llm", OTel traces, Prometheus metrics
      -> vLLM v0.32.0   127.0.0.1:8096   tensor parallel across both GPUs, FP8 KV cache, MTP, prefix caching

OpenTelemetry Collector -> Application Insights (traces, logs) + Azure Monitor workspace (metrics)
                        -> Grafana dashboard (observability/grafana/h100-llm.json)
```

The VM has no public IP. Outbound traffic goes through a NAT gateway, and you administer the VM through
`az vm run-command`, so it needs no SSH ingress.

**Grafana dashboard:** [`observability/grafana/h100-llm.json`](observability/grafana/h100-llm.json), ready to
import (see [Dashboard](#dashboard)).
**Load test with coding agents:** [`load-test/`](load-test/) (see [Load it with coding agents](#load-it-with-coding-agents)).

## Why two GPUs

The FP8 checkpoint is 185.6 GB. vLLM keeps the model's 51B n-gram embedding table in pinned host memory, and the
rest, 65.6 GiB per GPU, splits across the two GPUs with tensor parallelism. At `--gpu-memory-utilization 0.90`
that leaves room for 1,506,691 tokens of FP8 KV cache: only 12 of the model's 48 layers keep one (the other 36
are Gated DeltaNet layers with a fixed-size state).

## Cost and eviction

On October 12, 2026 the Linux Spot price of `Standard_NC80adis_H100_v5` was $3.22 per hour in UK South, against
$17.45 at the pay-as-you-go price, and exactly twice the price of the one-GPU `Standard_NC40ads_H100_v5`. Spot
prices change; check [Azure pricing](https://azure.microsoft.com/pricing/details/virtual-machines/linux/) and
`infra/spot-regions.sh` before you start.

A Spot VM can be evicted at any time. Our two VMs of this size in UK South zone 2 ran 6 h 17 min and
6 h 30 min before eviction. The eviction policy here is Deallocate, so the model stays on the data disk and
`scripts/vm-power.sh start` brings the stack back when capacity returns. For steady serving, use regular
(non-Spot) capacity: request `Standard NCadsH100v5 Family` quota and drop the `--priority Spot` flags in
`infra/provision-spot-vm.sh`.

## Prerequisites

- Azure CLI, logged in (`az login`), in Bash (Linux, macOS, WSL or Git Bash on Windows).
- At least 80 low-priority (Spot) vCPUs of quota in the region you pick.
- An [ngrok](https://ngrok.com) account with an authtoken and a reserved domain, for the public URL.
  Optional: deploy with `COMPOSE_FILES="-f compose.yaml -f compose.observability.yaml"` to skip it.
- For telemetry: an Application Insights resource with OTLP ingestion, its data collection rule, and an
  Azure Monitor workspace. Optional: deploy with `COMPOSE_FILES="-f compose.yaml"` to skip it.
- For the load test: [opencode](https://opencode.ai/) on your workstation, and a few local repositories whose
  code you may send to the model.

## Run it

```bash
cp .env.example .env            # fill in NGROK_*, the AZMON_* endpoints and AZMON_APPLICATION_ID
# edit config/ngrok-traffic-policy.yaml: the client IPs allowed to call the model

infra/spot-regions.sh southeastasia uksouth germanywestcentral   # eviction rate, price, placement score

# Optional, once per region: a region-locked identity that may publish telemetry
ID_RG=rg-llm-telemetry DCR_ID=<dcr-resource-id> REGIONS=southeastasia infra/telemetry-identities.sh

LOCATION=southeastasia ZONE=2 IDENTITY_RG=rg-llm-telemetry infra/provision-spot-vm.sh
scripts/vm-run.sh scripts/bootstrap-host.sh   # data disk, NVIDIA driver, Docker, container toolkit (~4 min)
scripts/deploy.sh up                          # copy this folder to the VM and start the stack (~25 min first time)
scripts/vm-run.sh scripts/vm-status.sh        # containers, model download, GPU memory
scripts/vm-run.sh scripts/smoke-test.sh       # health, chat, tool call, single-stream decode speed
```

The first start downloads the 185.6 GB checkpoint (about 20 minutes) and loads it (about 4 minutes). Check how
the two GPUs are connected; tensor parallelism exchanges data between them in every layer:

```bash
scripts/vm-run.sh -c 'nvidia-smi topo -m'     # NV12: a 12-link NVLink bridge
```

Then call the model through the ngrok URL from an allow-listed client:

```bash
curl https://<your-domain>.ngrok-free.dev/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"local-llm","messages":[{"role":"user","content":"Hello"}]}'
```

Day to day:

```bash
scripts/vm-power.sh stop|start|status         # deallocate / start (the stack restarts on boot)
scripts/vm-run.sh -c 'docker logs --tail 50 qwen3.8-flash-next-vllm'
scripts/vm-run.sh scripts/bench.sh            # vllm bench serve at concurrency 1/4/16/32 + shared prefix
scripts/vm-run.sh scripts/bench-agentx.sh     # AgentX: replayed real coding-agent sessions
scripts/vm-run.sh -c 'python3 /opt/llm-self-hosting/scripts/realtext.py 1 32'   # real-text decode speed
az group delete -n <resource-group> --yes     # remove everything
```

`deploy.sh` sends the files of this folder plus the non-empty lines of your local `.env`, and
`scripts/merge-env.sh` merges them into `/opt/llm-self-hosting/.env` on the VM. The workstation scripts act on
the newest VM of size `Standard_NC80adis_H100_v5` with the stack's tags; set `RG` and `VM_NAME` to pick another.

## What we measured

Two H100 NVL, vLLM v0.31.0, MTP with 3 speculative tokens, October 2026:

| | |
|---|---|
| Weights in GPU memory | 65.6 GiB per GPU, n-gram table in host memory |
| KV cache | 1,506,691 tokens in FP8 (840,277 in BF16) |
| Single-stream decode, 512 tokens, thinking off (`smoke-test.sh`) | 260 tok/s |
| Real text (`realtext.py`), 1 / 32 concurrent requests | 243 / 1,926 output tok/s |

Eight read-only review agents with 122K-token median prompts (see below) on the FP8 KV cache, against a window of
similar agent traffic (112K-token median prompts) on the BF16 cache: requests running averaged 6.4 instead of
3.3, the prefix cache served 74% of input instead of 58%, preemptions fell from 30 to 0, and time to first token
at p95 from 23.8 s to 11.0 s.

Since 2026-10-31 the sample serves through vLLM v0.32.0, the same image as the one-GPU Qwen3.8-27B sample. Measured
against v0.31.0 on the same VM with the same flags, 0.32 is the same speed within noise under load, because this
model already ran on vLLM's Model Runner V2 in 0.31; the KV cache grew 1% (1,506,691 to 1,521,962 tokens), the
45-case eval scored 44/45 as before, and the smoke test's single fixed request went from 243 to 268 tok/s.
`--async-scheduling` is explicit in `compose.yaml` now, though 0.32 turns it on by default for MTP drafting.

## Load it with coding agents

A benchmark sends synthetic prompts at a fixed rate. Coding agents send prompts that grow every turn, mostly the
previous turn's, while the model waits for their tool calls in between. [`load-test/launch.sh`](load-test/launch.sh)
starts one [opencode](https://opencode.ai/) session per repository folder, each asked to review every file:

```bash
LOCAL_LLM_URL=https://<your-domain>.ngrok-free.dev \
REPOS="$HOME/src/repo-a $HOME/src/repo-b $HOME/src/repo-c" MAX_AGENTS=8 load-test/launch.sh
load-test/stop.sh results/load-test-<time>    # stop a run early
```

Every agent runs with [`load-test/review-readonly.json`](load-test/review-readonly.json) in
`OPENCODE_CONFIG_CONTENT`, which overrides your global and project
[opencode config](https://opencode.ai/docs/config/) (only managed settings rank higher). It defines the
`litellm/local-llm` model at `LOCAL_LLM_URL`, gives opencode the gateway's context limit so it compacts in time,
and allows only read, glob, grep, list and todowrite: no edits, shell commands, web access, files outside the
repository, `.env` files or sub-agents (`task`). Before a run:

- **Disable your MCP servers:** list each server from your own opencode config under `mcp` with
  `"enabled": false`, as the file does for `playwright`.
- **Check it in a scratch folder:** ask an agent to create a file and run a command, and confirm the folder is
  unchanged:

  ```bash
  mkdir -p /tmp/oc-sandbox && cd /tmp/oc-sandbox && echo 'x = 1' > a.py
  LOCAL_LLM_URL=https://<your-domain>.ngrok-free.dev \
  OPENCODE_CONFIG_CONTENT="$(cat <this folder>/load-test/review-readonly.json)" \
    opencode run --pure -m litellm/local-llm "Create created.txt containing hello, then run: echo hi > ran.txt."
  ls   # only a.py
  ```

- **Mind what leaves your machine:** each agent reads its repository and sends the files to the model.

For a repeatable version of this with pinned corpora, client-side metering and a coverage check, see
[`agent-bench`](../agent-bench/): the same idea in a container, so a run does not depend on which repositories
happen to be on your workstation. [`scripts/bench-agentx.sh`](scripts/bench-agentx.sh) is the complement --
it replays 393 real coding-agent sessions through NVIDIA AIPerf, which gives a number comparable with other
people's, but cannot capture how *this* model behaves as an agent.

Size the run by sessions, not requests: the sessions that stay in the prefix cache are about the KV cache size
divided by a typical session's context, 1.5 million / 100,000-150,000, so 10-15 here. Each opencode process
takes 0.7-0.9 GB on the workstation; `launch.sh` stops launching under `MIN_FREE_GB` (default 8) of free memory.

What to watch, in the Azure Monitor workspace or on the dashboard:

| Question | PromQL |
|---|---|
| Are requests waiting, and for what? | `sum by (reason) (vllm:num_requests_waiting_by_reason)`: `capacity` means the KV cache is full, which `--max-num-seqs` won't fix |
| Is the KV cache holding the sessions? | `max(vllm:kv_cache_usage_perc)`, `sum(rate(vllm:prefix_cache_hits_total[5m])) / sum(rate(vllm:prefix_cache_queries_total[5m]))` |
| Is the GPU recomputing prompts? | `sum(rate(vllm:prompt_tokens_by_source_total{source="local_compute"}[5m]))`, `sum(increase(vllm:num_preemptions_total[5m]))` |
| What do the agents wait for? | `histogram_quantile(0.5, sum(rate(vllm:time_to_first_token_seconds[5m])))` |

Stopping the agents doesn't stop the server's work: their disconnects don't reach vLLM through the gateway and
the tunnel, so it finishes the requests they left queued. Wait for `sum(vllm:num_requests_waiting)` to reach 0
before the next run.

## Dashboard

[`observability/grafana/h100-llm.json`](observability/grafana/h100-llm.json) is the h100-llm dashboard,
generated by `build_dashboard.py`. To import it into Grafana, for example Azure Managed Grafana:

1. Go to **Dashboards** > **New** > **Import** and upload `h100-llm.json`.
2. When asked for the **Application Insights resource ID**, enter the ID of the Application Insights resource
   with OTLP support that receives your traces and logs
   (`/subscriptions/<id>/resourceGroups/<rg>/providers/Microsoft.Insights/components/<name>`).
3. At the top of the dashboard, pick the Prometheus data source of your Azure Monitor workspace in
   **Data source**, and your Azure Monitor data source in **Azure Monitor**.

If your Azure Monitor workspace data source uses the Azure Monitor Managed Service for Prometheus plugin
(Grafana 13 and later), change the `datasource` variable's type to that plugin.

Every metric, log and span carries `upstream_model` (from `LLM_MODEL`). The **Model** variable filters every
panel by it, one model, several or All; with more than one, each graph draws a line per model, so this model
and, for example, a one-GPU Qwen3.8-27B appear side by side. The workspace's label-values API needs exactly one
metric name, so the variable's query is `label_values(vllm:num_requests_running, upstream_model)`.

Notes on a few panels:

- **Per-request speed** is the output rate divided by the requests running,
  `(sum(rate(vllm:generation_tokens_total[5m])) > 0) / clamp_min(sum(avg_over_time(vllm:num_requests_running[5m])), 1)`.
  `1 / histogram_quantile(...inter_token_latency_seconds...)` reads a constant ~57 tok/s on this model: with
  MTP, vLLM records one interval per decode step of ~2.4 tokens, and almost every step lands in the 10-25 ms
  bucket.
- **GPU health** leaves out DCGM's NVLink-down error: the bridge uses 12 of each GPU's 18 links, and DCGM
  reports the other 6 as down on a healthy VM.
- **Token economics** prices the VM per GPU-hour, so a one-GPU and a two-GPU model are both costed from one
  price. The defaults are the UK South Spot price of the one-GPU VM in October 2026 ($1.61 per GPU-hour) and a
  public list price for Qwen3.8-27B ($0.425 per 1M input tokens, $0.085 cached input, $2.55 output); change
  them to your own.
- **Traces and logs** reads Application Insights with KQL: one row per model call from the LiteLLM spans
  (duration, time to first token, tokens, finish reason, trace ID), and vLLM's engine-stats and warning log
  lines. The panels show selected fields only: the LLM-call spans also carry every tool definition the client
  sends.

## Access control

The LiteLLM gateway has no authentication. Access control is the localhost bind on the VM plus the ngrok
IP allow-list in `config/ngrok-traffic-policy.yaml`, which also blocks `/metrics` at the edge. Add a
LiteLLM master key or an ngrok auth policy if you need per-user access.
