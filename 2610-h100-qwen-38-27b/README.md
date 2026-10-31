# Qwen3.8-27B on an H100 Azure Spot VM

Sample code for the blog post [*Qwen3.8-27B on a $2 H100 Azure Spot VM: does it pay off?*](https://techcommunity.microsoft.com/blog/appsonazureblog/qwen3-8-27b-on-a-2-h100-azure-spot-vm-does-it-pay-off/4563387).

It serves [Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B-FP8) (official FP8 checkpoint) from one
`Standard_NC40ads_H100_v5` VM (1x H100 NVL, 94 GB) on
[Azure Spot Virtual Machines](https://learn.microsoft.com/azure/virtual-machines/spot-vms), with Docker Compose:

```text
client
  -> ngrok public URL (IP allow-list)
    -> LiteLLM gateway  127.0.0.1:4000   alias "local-llm", OTel traces, Prometheus metrics
      -> vLLM v0.32.0   127.0.0.1:8096   Qwen3.8-27B FP8, MTP speculative decoding, prefix caching

OpenTelemetry Collector -> Application Insights (traces, logs) + Azure Monitor workspace (metrics)
                        -> Grafana dashboard (observability/grafana/h100-llm.json)
```

The VM has no public IP. Outbound traffic goes through a NAT gateway, and you administer the VM through
`az vm run-command`, so it needs no SSH ingress.

**Grafana dashboard:** [`observability/grafana/h100-llm.json`](observability/grafana/h100-llm.json), ready to
import (see [Dashboard](#dashboard)).

## Cost and eviction

In October 2026 the Linux Spot price of `Standard_NC40ads_H100_v5` ranged from $1.29 to $3.28 per hour
across the regions in the lowest eviction-rate bucket (0-5%), and the regions we ran in were $1.55-$1.84.
Spot prices change; check [Azure pricing](https://azure.microsoft.com/pricing/details/virtual-machines/linux/)
and `infra/spot-regions.sh` before you start.

A Spot VM can be evicted at any time. Over about 40 hours of running in five regions, our VM was evicted
seven times, between one minute and 8.5 hours after it started. The eviction policy here is Deallocate, so the
model stays on the data disk and `scripts/vm-power.sh start` brings the stack back when capacity returns.
For steady serving, use regular (non-Spot) capacity: request `Standard NCadsH100v5 Family` quota and drop
the `--priority Spot` flags in `infra/provision-spot-vm.sh`.

## Prerequisites

- Azure CLI, logged in (`az login`), in Bash (Linux, macOS, WSL or Git Bash on Windows).
- At least 40 low-priority (Spot) vCPUs of quota in the region you pick.
- An [ngrok](https://ngrok.com) account with an authtoken and a reserved domain, for the public URL.
  Optional: deploy with `COMPOSE_FILES="-f compose.yaml -f compose.observability.yaml"` to skip it.
- For telemetry: an Application Insights resource with OTLP ingestion, its data collection rule, and an
  Azure Monitor workspace. Optional: deploy with `COMPOSE_FILES="-f compose.yaml"` to skip it.

## Run it

```bash
cp .env.example .env            # fill in NGROK_*, the AZMON_* endpoints and AZMON_APPLICATION_ID
# edit config/ngrok-traffic-policy.yaml: the client IPs allowed to call the model

infra/spot-regions.sh southeastasia uksouth germanywestcentral   # eviction rate, price, placement score

# Optional, once per region: a region-locked identity that may publish telemetry
ID_RG=rg-llm-telemetry DCR_ID=<dcr-resource-id> REGIONS=southeastasia infra/telemetry-identities.sh

LOCATION=southeastasia ZONE=2 IDENTITY_RG=rg-llm-telemetry infra/provision-spot-vm.sh
scripts/vm-run.sh scripts/bootstrap-host.sh   # data disk, NVIDIA driver, Docker, container toolkit (~4 min)
scripts/deploy.sh up                          # copy this folder to the VM and start the stack (~20 min first time)
scripts/vm-run.sh scripts/vm-status.sh        # containers, model download, GPU memory
scripts/vm-run.sh scripts/smoke-test.sh       # health, chat, tool call, single-stream decode speed
```

Then call the model through the ngrok URL from an allow-listed client:

```bash
curl https://<your-domain>.ngrok-free.dev/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"local-llm","messages":[{"role":"user","content":"Hello"}]}'
```

Day to day:

```bash
scripts/vm-power.sh stop|start|status         # deallocate / start (the stack restarts on boot)
scripts/vm-run.sh -c 'docker logs --tail 50 qwen3.8-27b'
scripts/vm-run.sh scripts/bench.sh            # vllm bench serve at concurrency 1/4/16/32 + shared prefix
scripts/vm-run.sh -c 'python3 /opt/llm-self-hosting/scripts/realtext.py 1 32'   # real-text decode speed
scripts/vm-run.sh -c 'LABEL=before bash /opt/llm-self-hosting/scripts/measure.sh'   # smoke test + real text + bench under a label, for A/B tests
scripts/vm-run.sh scripts/profile-decode-step.sh   # where a decode step's time goes (needs QWEN38_27B_PROFILER_CONFIG, see its header)
az group delete -n <resource-group> --yes     # remove everything
```

`deploy.sh` sends the files of this folder plus the non-empty lines of your local `.env`, and
`scripts/merge-env.sh` merges them into `/opt/llm-self-hosting/.env` on the VM.

## What we measured

One H100 NVL, MTP with 3 speculative tokens, FP8 KV cache, October 2026. The first two columns are the post's
figures on NVIDIA's NGC vLLM 26.07 image (vLLM 0.24); the third is the same VM and flags on vLLM 0.32, which the
sample serves through now (see [What changed since the post](#what-changed-since-the-post)):

| | BF16 checkpoint, NGC 26.07 | FP8 checkpoint, NGC 26.07 | FP8 checkpoint, vLLM 0.32 |
|---|---|---|---|
| Weights in GPU memory | 51.9 GiB | 29.0 GiB | 29.0 GiB |
| KV cache | 805K tokens (GPU memory 0.90) | 1.5M tokens (GPU memory 0.93) | 1.5M tokens (GPU memory 0.93) |
| Single-stream decode, 512 tokens, thinking off | 124 tok/s | 165-172 tok/s | 180 tok/s |
| Inter-token latency p50, single stream | not measured | 17.7 ms | 15.8 ms |
| Random prompts (2,000 in / 512 out), 1 / 4 / 16 / 32 streams | not measured | 146 / 376 / 931 / 1,161 tok/s | 154 / 487 / 1,115 / 1,233 tok/s |

The post's real-text figure, ~1,550 output tok/s at 32 concurrent requests, was measured on the NGC image with
`realtext.py`; the random-prompt row is `bench.sh` on one VM before and after the image change, which is the
like-for-like comparison.

## What changed since the post

The post describes the model on NVIDIA's NGC vLLM 26.07 image (vLLM 0.24), with a derived image that patched
xgrammar so tool calls would work. On 2026-10-31 the sample moved to vLLM's own `vllm/vllm-openai:v0.32.0` image
after an A/B on one fresh Spot VM, same flags, MTP with 3 draft tokens on both:

- **vLLM 0.32 runs this model on Model Runner V2, and the gain is largest where a shared server lives.**
  Single-stream decode through the gateway went from 172.0 to 179.7 tok/s (inter-token latency p50 17.7 to
  15.8 ms); on random 2,000-in / 512-out prompts from 145.7 to 153.6 tok/s at 1 stream, 376.1 to 487.0 at 4,
  931.1 to 1,115.3 at 16 and 1,160.9 to 1,233.1 at 32. The KV cache is 0.6% smaller, 1,505,497 to 1,496,710
  tokens, because 0.32 reserves CUDA-graph memory up front. A 45-case capability eval scored 44/45 on both
  images; on 10 Terminal-Bench 2.0 tasks driven by opencode the model went from 3-4/10 to 7/10, with the
  "one tool call, then stop" failure down from 3-6 tasks to 1. A cold start takes about 5 minutes (32 s of
  torch.compile), a warm restart under 2.
- **The xgrammar workaround is gone.** 0.32 bundles an xgrammar with `normalize_tool_choice`, so
  `compose.yaml` pulls the stock image, the Dockerfile that installed xgrammar 0.2.3 on the NGC image is
  deleted, and the tool-call check in `smoke-test.sh` passes.
- **`--async-scheduling` is on by default.** The explicit flag measured no difference (179.65 against
  179.66 tok/s, identical real-text figures): vLLM 0.32 resolves it to on for MTP and DFlash drafting. It
  stays in `compose.yaml` as documentation of the setting.
- **A DFlash 2 drafter was tried and left off.** `incoai/Qwen3.8-27B-DFlash2` (7 draft tokens, FP8, needs
  `--block-size 64`) is the fastest configuration for one request, 218 tok/s through the gateway and 234 at
  1 stream on random prompts (+20-50%), but a shared server pays for it: 10-12% less throughput at 16-32
  streams, 25% less KV cache (1,118,692 tokens) and 29% less shared-prefix throughput, with prefix-cache hits
  halved and time to first token tripled, so MTP 3 stays the default. `model-download` fetches the drafter
  beside the checkpoint; to switch, set `QWEN38_27B_SPEC_CONFIG` to the DFlash line commented in
  `.env.example` and `QWEN38_27B_BLOCK_SIZE=64`, then redeploy.
- **Where a decode step goes.** `scripts/profile-decode-step.sh` records 60 engine steps of one code request
  with vLLM's torch profiler, and `scripts/decode-step-phases.py` reads the trace: 15.7 ms per step with the
  GPU busy 96% of it, 46% in the FP8 MLP GEMMs, 19% in the BF16 lm_head (read once per MTP draft token plus
  once for the target) and 8% in the Gated DeltaNet projections. With almost no idle time per step there is
  little left for async scheduling to hide, which is why the explicit flag measured nothing.

`scripts/measure.sh` is the before/after measurement behind these numbers: smoke test, real text at 1/4/32
streams and `bench.sh`, under a label, with a warm-up pass first (the first real-text pass after a start runs
40-50% slower at 4 and 32 streams than the next).

## Dashboard

[`observability/grafana/h100-llm.json`](observability/grafana/h100-llm.json) is the h100-llm dashboard,
generated by `build_dashboard.py`. To import it into Grafana, for example Azure Managed Grafana:

1. Go to **Dashboards** > **New** > **Import** and upload `h100-llm.json`.
2. When asked for the **Application Insights resource ID**, enter the ID of the Application Insights resource
   with OTLP support that receives your traces and logs
   (`/subscriptions/<id>/resourceGroups/<rg>/providers/Microsoft.Insights/components/<name>`).
3. At the top of the dashboard, pick the Prometheus data source of your Azure Monitor workspace in
   **datasource**, and your Azure Monitor data source in **Azure Monitor**.

If your Azure Monitor workspace data source uses the Azure Monitor Managed Service for Prometheus plugin
(Grafana 13 and later), change the `datasource` variable's type to that plugin.

The **Token economics** row compares what the
tokens served would sell for, at the price variables at the top of the dashboard, with the VM price per
hour. The defaults are a $2 VM hour and a public list price for Qwen3.8-27B in October 2026
($0.425 per 1M input tokens, $0.085 cached input, $2.55 output); change them to your own.

The **Traces and logs** row reads Application Insights with KQL through the Azure Monitor data source: one
row per model call from the LiteLLM spans (duration, time to first token, tokens, finish reason, trace ID),
and vLLM's engine-stats and warning log lines, from the Application Insights resource you entered on import
(the hidden `appinsights` constant; change it under **Settings** > **Variables**). The panels show selected
fields only: the LLM-call spans also carry every tool definition the client sends.

Every metric, log and span carries `upstream_model` (from `LLM_MODEL`), so runs of different models on the
same VM can be compared.

## Access control

The LiteLLM gateway has no authentication. Access control is the localhost bind on the VM plus the ngrok
IP allow-list in `config/ngrok-traffic-policy.yaml`, which also blocks `/metrics` at the edge. Add a
LiteLLM master key or an ngrok auth policy if you need per-user access.
