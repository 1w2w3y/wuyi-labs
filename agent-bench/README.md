# agent-bench

Load a self-hosted LLM with real concurrent coding agents, and report both what
the server delivered and whether the agents did any work for it.

```bash
docker run --rm \
  -e LLM_BASE_URL=https://your-gateway.example.com/v1 \
  -e LLM_MODEL=local-llm \
  -e SCENARIO=agent-framework-review -e AGENTS=8 \
  -v agent-bench-corpus:/corpus -v "$PWD/results:/results" \
  ghcr.io/1w2w3y/wuyi-labs/agent-bench:latest
```

That starts eight [opencode](https://opencode.ai/) sessions reviewing Microsoft
Agent Framework through your gateway, and prints something like:

```text
Server load
  output            1312.4 tok/s  (2361120 tokens)
  prompt            18204.9 tok/s (32768800 tokens)
  prompt cached     74.1% of input
  concurrency       6.4 mean, 9 peak in flight
  TTFT              p50 3.1s   p95 11.0s
  per-stream rate   p50 58.2 tok/s   slowest decile 24.7 tok/s
  prompt size       p50 122880 tok   p95 196608 tok
  usable to code against at 20 tok/s floor: yes

Agent productivity
  files reviewed    412 of 437 (94.3%)
  abandoned         25 files
  sessions          74 run, 6 re-driven, 0 timed out
  cost per file     5731.4 output tok, 79536.9 prompt tok
```

## Why not an existing benchmark

Use one as well — they answer a different question.

[AgentX](https://inferencex.semianalysis.com/blog/agentic-benchmark-agent-benchmark-guide)
on [NVIDIA AIPerf](https://github.com/ai-dynamo/aiperf) replays 393 real
coding-agent sessions as deterministic load, and gives you a number comparable
to everyone else's:

```bash
pip install aiperf
aiperf profile --scenario inferencex-agentx-mvp \
  --public-dataset semianalysis_cc_traces_weka_with_subagents \
  --url your-vllm-host:8096 --endpoint-type chat --model your-model \
  --concurrency 32 --random-seed 20260707 --ui simple
```

A replay sends fixed token shapes, which cuts open the loop that matters here:
your model's own output decides how many turns, how many tool calls and how
much context each session costs. A weaker model re-reads files, loops, and
burns several times the tokens for the same review. So:

- **AgentX** answers *how fast is my serving stack on agent-shaped traffic* —
  tune vLLM, compare hardware.
- **agent-bench** answers *how many coding agents can actually get work done on
  this box, with this model* — capacity planning.

## How it works

```text
runner/run.py
  |- builds a deterministic work queue from the corpus (sorted, size-filtered)
  |- shards it into contiguous slices, one per agent
  |- preflight: 5 wire checks + one real opencode run
  |- N x opencode run --pure   (one session per batch of files)
  |      \- http://127.0.0.1:1880N/v1   runner/proxy.py  (meters everything)
  |                                        \- your gateway -> vLLM
  |- scrapes vLLM /metrics before and after
  \- runner/report.py -> summary.txt, summary.json, requests.jsonl
```

### The work queue, not a prompt

Asking an agent to "review every file and don't stop" measures the model's
willingness to keep going, not the server. A model that quits after three files
generates less load, and no two models are comparable.

So the harness holds the queue. It enumerates the corpus, shards it, and hands
each agent one batch at a time. After every session it checks **from the wire**
which files the agent actually read, and re-drives the ones it skipped
(`retries_per_batch`). Total work is fixed by the corpus, so the throughput
number means the same thing for every model. Set `retries_per_batch: 0` to
measure instead how much work a model abandons when left alone.

Coverage comes from the `read` tool calls the proxy saw, never from the agent's
own account of itself: a model that reports findings on a file it never opened
counts as not having reviewed it.

### The `/goal` mode

[`opencode/command/goal.md`](opencode/command/goal.md) is the review
instruction: read every assigned file in full, one heading per file, `NONE` for
a clean file, never ask questions, never stop early. It is a real opencode
custom command, so inside the container you can also drive it by hand:

```bash
docker run --rm -it ... agent-bench shell
cd /corpus/agent-framework && opencode    # then: /goal dotnet/src/Foo.cs
```

The runner renders the same file itself rather than relying on command
discovery, so both paths use one source of truth.

### Metering at the wire

The proxy records, per request: prompt and completion tokens, cached prompt
tokens, reasoning tokens, TTFT, mean inter-token latency, tool calls, the files
read, and the finish reason. It adds `stream_options.include_usage` so the
gateway returns exact counts; agents ignore that chunk.

This is deliberate rather than reading opencode's own telemetry.
`opencode run --format json` emitted **nothing** on runs that otherwise worked,
and its event shape is not a documented interface, so anything built on it
breaks on upgrade. The wire format is OpenAI's and stable, the timings are the
ones the agent actually saw, and the same harness meters any agent CLI that
speaks OpenAI.

### Preflight

A model can look healthy to curl and still do no work. Preflight checks, before
any load:

1. the model is listed on `/v1/models`
2. a non-streaming completion returns text
3. a streaming completion returns `content`, not reasoning alone
4. a tool definition comes back as a tool call
5. streaming and tools together behave — the real agent shape
6. **one actual opencode run** on a throwaway file

Check 6 earns its keep: a model can pass every wire check — streaming
content, well-formed streamed tool calls — and still leave opencode producing
nothing at all, in which case the run reports plausible throughput for no work
done. We saw `gpt-oss-120b` behind a LiteLLM gateway do exactly that, twice,
where `gpt-4.1-mini` on the same gateway finished the same task in about ten
seconds. Only driving the real agent finds it.

The check is deliberately retried. We have also seen `gpt-4.1-mini` itself
time out once on a slow minute, and aborting a half-hour benchmark over one
slow attempt is worse than waiting twice — so a model is only declared
unusable after every attempt fails. Raise `SMOKE_SECONDS` if your gateway is
habitually slow to first token. The check also warms opencode's provider
package, so the first real session is not slower than the rest.

Override with `SKIP_PREFLIGHT=1` when you are deliberately measuring a broken
route.

## Running it

### Scenarios

| Scenario | Corpus | Shape |
|---|---|---|
| `dotnet-review` | `dotnet/runtime` → `System.Text.Json` | dense C#, generics and spans |
| `typescript-review` | `microsoft/TypeScript` → `packages/typescript/src` | the package's own .ts sources |
| `agent-framework-review` | `microsoft/agent-framework` | C# and Python in one run |

```bash
docker run --rm ghcr.io/1w2w3y/wuyi-labs/agent-bench:latest list
```

Each is a YAML file under [`scenarios/`](scenarios/); copy one and edit it. The
dial that matters most is `batch_size`: each session reads its whole batch, so
the prompt grows file by file. Raise it to push sessions towards the 100K+ token
prompts real coding agents produce; lower it to keep prefill cheap.

### Your own code

Mount a repo and point `repo` at it with an absolute path:

```bash
docker run --rm \
  -e LLM_BASE_URL=... -e LLM_MODEL=... -e SCENARIO=/work/my-scenario.yaml \
  -v "$HOME/src/my-repo:/work/repo:ro" -v "$PWD/my-scenario.yaml:/work/my-scenario.yaml:ro" \
  -v "$PWD/results:/results" ghcr.io/1w2w3y/wuyi-labs/agent-bench:latest
```

Every agent reads its files and sends them to the model, so only point this at
code you may send there.

### Useful knobs

| Variable | Default | Meaning |
|---|---|---|
| `LLM_BASE_URL` | — | gateway base URL, **including `/v1`** |
| `LLM_MODEL` | — | model name the gateway serves |
| `LLM_API_KEY` | empty | bearer token, if the gateway wants one |
| `SCENARIO` | `agent-framework-review` | name or path |
| `AGENTS` | from scenario | concurrent agents |
| `BATCH_SIZE` | from scenario | files per session |
| `MAX_FILES` | all | truncate the queue, for smoke tests |
| `WALL_SECONDS` | 1800 | budget; agents stop at the deadline |
| `GAP_SECONDS` | 3 | stagger between agent starts |
| `VLLM_METRICS_URL` | empty | scraped before and after the run |
| `SKIP_PREFLIGHT` | 0 | skip the compatibility gate |
| `FETCH_CORPUS` | 1 | set 0 once the volume is warm |

Check the plan without calling the model:

```bash
docker run --rm -e LLM_BASE_URL=... -e LLM_MODEL=... \
  -v agent-bench-corpus:/corpus ghcr.io/1w2w3y/wuyi-labs/agent-bench:latest \
  run --dry-run
```

Other entrypoints: `fetch` (corpus only), `preflight`, `report <dir>`, `list`,
`shell`.

### Sizing a run

- Each opencode process holds roughly **0.7–0.9 GB**. Eight agents need ~8 GB;
  `compose.yaml` sets `mem_limit` to 12g, so raise it with `AGENTS`.
- Sessions that stay in the prefix cache are about the KV cache size divided by
  a typical session's context — for a 1.5M-token cache and 100–150K sessions,
  **10–15**. Past that, sessions start evicting each other.
- **Run the client somewhere other than the GPU box.** Eight opencode processes
  competing with vLLM for CPU is a real measurement error.

## Results

```text
results/<scenario>-<utc>/
  summary.txt        the table above
  summary.json       the same numbers, for plotting
  requests.jsonl     one line per request, as the proxy saw it
  meta.json          scenario, corpus SHA, preflight, per-session outcomes
  logs/agent-NN.log  what each agent said
  vllm-{before,after}.prom
```

Re-render a report from an existing bundle: `agent-bench report results/<dir>`.

The headline number is **sustained output tok/s at the largest `AGENTS` where
the slowest decile of streams still beats `interactivity_floor_tps`**. Sweep it:

```bash
for n in 1 2 4 8 16; do
  AGENTS=$n docker compose run --rm agent-bench
done
```

Read it with the productivity block. High tok/s with low coverage means the
model is busy talking to itself, and that throughput buys nothing.

## Telemetry

Set one environment variable and the run exports OTLP over HTTP: spans, metrics
and logs, no sidecar and no extra dependency in the image.

```bash
docker run --rm   -e LLM_BASE_URL=... -e LLM_MODEL=...   -e OTEL_EXPORTER_OTLP_ENDPOINT=https://otel.example.com   -v agent-bench-corpus:/corpus -v "$PWD/results:/results"   ghcr.io/1w2w3y/wuyi-labs/agent-bench:latest
```

### One trace from agent to model

The interesting part is not that agent-bench has its own spans. It is that the
metering proxy puts a W3C `traceparent` on every upstream call, and
[LiteLLM extracts an inbound traceparent](https://docs.litellm.ai/docs/observability/opentelemetry_v2)
and parents its own GenAI spans onto it. So one trace covers the whole path:

```text
agent-bench run <scenario>                 (the run: queue size, agents, corpus SHA)
  session agent-03 batch 7                 (one opencode session, files assigned/read)
    chat <model>                           (one LLM request, as the agent saw it)
      Received Proxy Server Request        (LiteLLM, in your App Insights)
        litellm_request -> vLLM            (the gateway's own view)
```

That is the join you cannot get from server-side telemetry alone: a slow p95
TTFT becomes *which agent, on which batch, reading which file* stalled. If you
would rather keep the traces separate, set
`OTEL_IGNORE_CONTEXT_PROPAGATION=true` on the gateway.

### Where to send it

| You want | Set |
|---|---|
| Your own OTLP endpoint | `OTEL_EXPORTER_OTLP_ENDPOINT=https://otel.example.com` |
| The stack's collector, on the VM | `OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318` (join its compose network) |
| Azure Monitor / App Insights directly | the three `OTEL_EXPORTER_OTLP_*_ENDPOINT` URLs below |

An App Insights **resource ID is not an ingestion endpoint** — there is nothing
to POST to. Reaching it directly means its data collection endpoint, which puts
traces and logs on the logs-ingest host and metrics on a different
metrics-ingest host, each with a DCR id in the path. So give per-signal URLs,
the same ones the flash-next stack already has in its `.env`:

```bash
-e OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="https://<dce>.<region>-1.ingest.monitor.azure.com/dataCollectionRules/<dcr-id>/streams/Microsoft-OTLP-Traces/otlp/v1/traces"
-e OTEL_EXPORTER_OTLP_LOGS_ENDPOINT="https://<dce>.<region>-1.ingest.monitor.azure.com/dataCollectionRules/<dcr-id>/streams/Microsoft-OTLP-Logs/otlp/v1/logs"
-e OTEL_EXPORTER_OTLP_METRICS_ENDPOINT="https://<dce>.<region>-1.metrics.ingest.monitor.azure.com/dataCollectionRules/<dcr-id>/streams/Microsoft-OtelMetrics/otlp/v1/metrics"
-e AZMON_APPLICATION_ID=<app-insights-application-id>
```

Those endpoints need an Entra token for `https://monitor.azure.com/.default`.
On an Azure VM, agent-bench fetches one from IMDS automatically — set
`TELEMETRY_UAMI_CLIENT_ID` to the user-assigned identity that holds *Monitoring
Metrics Publisher* on the DCR, exactly as the collector does. Anywhere else,
pass a token yourself:

```bash
-e OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer $(az account get-access-token      --resource https://monitor.azure.com --query accessToken -o tsv)"
```

`AZMON_APPLICATION_ID` matters only on this direct path: the collector's
resource processor normally adds `microsoft.applicationId` for you, and without
it App Insights will not attribute the data.

**Going through the collector is easier and better** — it already adds
`microsoft.applicationId`, `upstream_model`, Azure resource detection, retries
and an on-disk queue. Post straight to the DCE only when agent-bench runs
somewhere the collector cannot be reached.

### What you get

Every metric carries `run_id`, `scenario`, `upstream_model` and `agent` as
**data point** attributes, not resource attributes. That is deliberate: the
metrics DCR keeps only `service.*`, `host.name` and `cloud.region` from the
resource and drops everything else, so dimensions set there would vanish and
separate runs would collapse into one series. `upstream_model` is the same
label the rest of the stack splits by, so these line up with vLLM's and the
gateway's series and the **Model** variable on the h100-llm dashboard filters
them too.

| Metric | Type | Dimensions beyond the defaults |
|---|---|---|
| `agent_bench.requests` | counter | `status`, `outcome` |
| `agent_bench.tokens` | counter | `kind` = input / output / cached / reasoning |
| `agent_bench.tool_calls` | counter | `tool` |
| `agent_bench.sessions` | counter | `outcome` = complete / incomplete / timeout |
| `agent_bench.files_reviewed`, `...files_unread` | counter | |
| `agent_bench.requests.in_flight`, `...agents.running` | gauge | sampled every 5s |
| `agent_bench.ttft`, `...request.duration` | histogram | seconds |
| `agent_bench.prompt.tokens`, `...per_stream_tps` | histogram | |
| `agent_bench.run.*` | gauge | the summary: coverage, tok/s, percentiles, cost per file |

Useful PromQL once it is flowing:

```promql
# output tok/s this run drew from the server
sum(rate(agent_bench_tokens{kind="output"}[5m])) by (run_id)

# are the agents actually working, or just talking?
sum(rate(agent_bench_files_reviewed[5m])) by (run_id)

# the interactivity question, at the slow end
histogram_quantile(0.1, sum(rate(agent_bench_per_stream_tps_bucket[5m])) by (le, run_id))
```

Logs: one record per finished session (`agent-03 batch 7: reviewed 5 of 6
files in 94s`) and one run summary, both carrying trace and span ids so App
Insights links them to their spans. No prompt or file content is ever sent —
the same stance as the gateway's `turn_off_message_logging: true`.

### If it goes quiet

Telemetry never fails a run. Export errors are counted and the first one is
printed; the run's last line reports the totals:

```text
telemetry: exported 412 spans, 37 metrics, 19 logs
```

A wrong endpoint shows up as `export to <url> rejected: HTTP 400 ...` once,
then silence. Check it on its own with:

```bash
curl -i -X POST "$OTEL_EXPORTER_OTLP_ENDPOINT/v1/traces"   -H 'Content-Type: application/json' -d '{"resourceSpans":[]}'
```

A healthy OTLP endpoint answers `200` with `{"partialSuccess":{}}`.

## Caveats

- **Sessions are per batch, not per agent.** opencode's `--continue` resumes
  "the last session", which is ambiguous with several agents running at once,
  and the CLI exposes no stable session id. So `batch_size` sets how far a
  session's context grows rather than the slice size.
- **The corpus is pinned by SHA** in [`corpus/repos.tsv`](corpus/repos.tsv).
  Bumping it changes the benchmark; record the change with your numbers. If a
  pinned SHA is gone from the server, `fetch.sh` falls back to `HEAD` and says
  so — numbers from that run are not comparable.
- **Reasoning tokens count as output.** They are billed and they occupy the
  decoder, so they belong in the throughput figure; `reasoning_tokens` is
  reported separately if you want it excluded.
- **`doom_loop` is set to `deny`**, not opencode's default `ask`, which would
  hang forever with no terminal. `question` is denied for the same reason.
- **Per-stream rate is noisy when outputs are tiny** — a 50-token reply over a
  0.2 s decode window reads as hundreds of tok/s. Trust it when sessions are
  long.

## Building

```bash
docker build -t agent-bench agent-bench/                     # corpus at run time
docker build --build-arg BAKE_CORPUS=1 -t agent-bench agent-bench/   # in the image
docker build --build-arg OPENCODE_VERSION=1.19.0 -t agent-bench agent-bench/
```

Behind a corporate network that blocks `registry.npmjs.org`, build through your
mirror:

```bash
docker build --build-arg NPM_REGISTRY="$(npm config get registry)"   -t agent-bench agent-bench/
```

[`.github/workflows/agent-bench-image.yml`](../.github/workflows/agent-bench-image.yml)
publishes `ghcr.io/1w2w3y/wuyi-labs/agent-bench` on every push to `main` that
touches this folder, tagged `latest`, the commit SHA and the build date. Pull
requests build and smoke-test without pushing.

opencode is pinned in the image because the agent is part of the measurement:
a different agent version plans differently and produces different traffic.
Every result records the version it ran.
