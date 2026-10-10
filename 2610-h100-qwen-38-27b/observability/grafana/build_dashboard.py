"""Generate the Grafana dashboard `h100-llm` for the H100 stack.

    python observability/grafana/build_dashboard.py   # writes h100-llm.json

Import h100-llm.json into Grafana (for example Azure Managed Grafana) and pick
the Prometheus data source of the Azure Monitor workspace the collector sends
metrics to (the `datasource` variable). Notes on the data:
- OTLP histograms land as native histograms: histogram_quantile() takes the
  bare metric name, with no _bucket suffix and no `le`.
- OTel-native names keep their dots, so they are written {__name__="..."},
  and dotted labels are quoted in `by ("container.name")`.
- container.cpu.usage.total and the dcgm *_violation counters are in
  nanoseconds: /1e9 gives cores and the throttled share of time.
"""
import json
import pathlib

DS = {"type": "prometheus", "uid": "${datasource}"}
RI = "$__rate_interval"

panels = []
_id = 0
_y = 0


def nid():
    global _id
    _id += 1
    return _id


# Stat tiles without thresholds get a gradient background, cycling through
# this palette from the start of each row.
PALETTE = ["yellow", "blue", "purple", "green", "orange"]
_tile = 0


def row(title):
    global _y, _tile
    _tile = 0
    panels.append({"type": "row", "id": nid(), "title": title, "collapsed": False,
                   "gridPos": {"x": 0, "y": _y, "w": 24, "h": 1}, "panels": []})
    _y += 1


def target(expr, legend="", ref="A", instant=False):
    t = {"datasource": DS, "expr": expr, "legendFormat": legend or "__auto", "refId": ref}
    if instant:
        t.update({"instant": True, "range": False})
    return t


def place(panel, x, w, h):
    panel["gridPos"] = {"x": x, "y": _y, "w": w, "h": h}
    panels.append(panel)


def stat(title, expr, unit, x, w=4, h=4, thresholds=None, decimals=None, desc="", color_mode="background",
         mappings=None, no_value=None):
    """A single number from an instant query: either a total over the selected
    range ([$__range]) or the value now. Instant, so a stat never shows a stale
    last point after the VM stops. Thresholds, when given, color the tile;
    otherwise it takes the next palette color."""
    global _tile
    steps = thresholds
    if steps is None:
        steps = [{"color": PALETTE[_tile % len(PALETTE)], "value": None}]
        _tile += 1
    p = {
        "type": "stat", "id": nid(), "title": title, "description": desc, "datasource": DS,
        "targets": [target(expr, instant=True)],
        "fieldConfig": {"defaults": {"unit": unit, "thresholds": {"mode": "absolute", "steps": steps},
                                     "color": {"mode": "thresholds"}, "mappings": mappings or []},
                        "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": color_mode, "graphMode": "none", "textMode": "value",
                    "justifyMode": "center", "orientation": "auto", "showPercentChange": False},
    }
    if decimals is not None:
        p["fieldConfig"]["defaults"]["decimals"] = decimals
    if no_value is not None:
        p["fieldConfig"]["defaults"]["noValue"] = no_value
    place(p, x, w, h)


def ts(title, targets, unit, x, w=12, h=8, desc="", stack=False, minv=0, maxv=None, draw="line",
       overrides=None, legend_calcs=("mean", "max"), thresholds=None, no_value=None):
    custom = {"drawStyle": draw, "lineWidth": 2, "fillOpacity": 0 if not stack else 60,
              "showPoints": "never", "spanNulls": False, "axisSoftMin": minv,
              "stacking": {"mode": "normal" if stack else "none", "group": "A"},
              "gradientMode": "none", "barAlignment": 0}
    if draw == "bars":
        custom.update({"fillOpacity": 80, "lineWidth": 1})
    if thresholds:
        custom["thresholdsStyle"] = {"mode": "dashed"}
    d = {"unit": unit, "custom": custom, "color": {"mode": "palette-classic"}}
    if maxv is not None:
        d["max"] = maxv
    if no_value:
        d["noValue"] = no_value
    if thresholds:
        d["thresholds"] = {"mode": "absolute", "steps": thresholds}
    p = {
        "type": "timeseries", "id": nid(), "title": title, "description": desc, "datasource": DS,
        "targets": targets,
        "fieldConfig": {"defaults": d, "overrides": overrides or []},
        "options": {"legend": {"displayMode": "table", "placement": "bottom", "showLegend": True,
                               "calcs": list(legend_calcs)},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
    }
    place(p, x, w, h)


def text(title, content, x, w, h):
    place({"type": "text", "id": nid(), "title": title,
           "options": {"mode": "markdown", "content": content}}, x, w, h)


def advance(h):
    global _y
    _y += h


GOOD, WARN, BAD = "green", "orange", "red"

# "Now" stats use a fixed 2-minute rate (an instant query has no
# $__rate_interval to match a graph) and read 0 when the VM is off.
NOW = "2m"
IN_C = 'vllm:prompt_tokens_by_source_total{source="local_compute"}'
IN_H = 'vllm:prompt_tokens_by_source_total{source="local_cache_hit"}'
OUT = 'vllm:generation_tokens_total'
MAX_SEQS = 32  # vLLM --max-num-seqs (QWEN38_27B_MAX_NUM_SEQS in .env)

# ---------------------------------------------------------------- Overview
row("Overview")
stat("Requests", 'sum(increase(litellm_e2e_requests_total[$__range]))', "none", 0, w=3,
     decimals=0, desc="Chat requests through LiteLLM in the selected time range.")
stat("Error rate", 'sum(increase(litellm_e2e_requests_total{outcome="failure"}[$__range])) / '
     'sum(increase(litellm_e2e_requests_total[$__range]))', "percentunit", 3, w=3,
     thresholds=[{"color": GOOD, "value": None}, {"color": WARN, "value": 0.01}, {"color": BAD, "value": 0.05}],
     desc="Share of LiteLLM requests that failed in the selected range.")
stat("Input tokens/s", f'sum(rate(vllm:prompt_tokens_total[{NOW}]))', "short", 6, w=3, decimals=0, no_value="0",
     desc="Prompt (input) tokens per second across all requests now, prefix-cache hits included.")
stat("Output tokens/s", f'sum(rate({OUT}[{NOW}]))', "short", 9, w=3, decimals=0, no_value="0",
     desc="Generated (output) tokens per second across all requests now.")
stat("Total tokens/s", f'sum(rate(vllm:prompt_tokens_total[{NOW}])) + sum(rate({OUT}[{NOW}]))', "short", 12, w=3,
     decimals=0, no_value="0", desc="Input plus output tokens per second now.")
stat("Request tok/s", f'1 / histogram_quantile(0.5, sum(rate(vllm:inter_token_latency_seconds[{NOW}])))',
     "short", 15, w=3, decimals=0, no_value="Idle",
     desc="Output tokens per second one request sees now (1 / median inter-token latency).")
stat("Concurrency", 'sum(vllm:num_requests_running)', "short", 18, w=3, decimals=0, no_value="0",
     thresholds=[{"color": "blue", "value": None}, {"color": WARN, "value": MAX_SEQS}],
     desc=f"Requests vLLM is serving now. The server runs at most {MAX_SEQS} at once (--max-num-seqs); more wait in the queue.")
stat("Model server", 'max(up{"service.name"="vllm"})', "none", 21, w=3,
     thresholds=[{"color": BAD, "value": None}, {"color": GOOD, "value": 1}], color_mode="background",
     mappings=[{"type": "value", "options": {"0": {"text": "Down"}, "1": {"text": "Up"}}},
               {"type": "special", "options": {"match": "null", "result": {"text": "Off", "color": "text"}}}],
     desc="Whether the collector's last scrape of vLLM /metrics succeeded. Off means no scrape in the last 5 minutes: the VM is stopped or evicted.")
advance(4)

# ---------------------------------------------------------------- Economics
# Prices come from the dashboard variables (USD per 1M tokens, VM USD/hour).
# VM hours = minutes in which the vLLM scrape succeeded, so time the VM was
# off or evicted is neither billed nor counted.
row("Token economics")
VM_HOURS = 'sum_over_time(max(up{"service.name"="vllm"})[$__range:1m]) / 60'
REVENUE = (f'(sum(increase({IN_C}[$__range])) * $price_input'
           f' + sum(increase({IN_H}[$__range])) * $price_cached_input'
           f' + sum(increase({OUT}[$__range])) * $price_output) / 1e6')
stat("VM hours", VM_HOURS, "h", 0, w=4, decimals=1,
     desc="Hours the model server was up in the selected range: the hours you pay the VM for.")
stat("VM cost", f'({VM_HOURS}) * $vm_usd_per_hour', "currencyUSD", 4, w=4, decimals=2,
     desc="VM hours times the VM price variable.")
stat("Token revenue", REVENUE, "currencyUSD", 8, w=4, decimals=2,
     desc="What the tokens served in the selected range would sell for at the price variables: "
          "computed input, cached input and output priced separately.")
stat("Revenue per VM hour", f'({REVENUE}) / ({VM_HOURS})', "currencyUSD", 12, w=4, decimals=2,
     desc="Token revenue divided by VM hours. Compare it with the VM price per hour.")
stat("Output tokens per VM hour", f'sum(increase({OUT}[$__range])) / ({VM_HOURS})', "short", 16, w=4, decimals=1, desc="Generated tokens per hour the VM was up, at the load it actually had.")
stat("VM cost per 1M output", f'({VM_HOURS}) * $vm_usd_per_hour / (sum(increase({OUT}[$__range])) / 1e6)',
     "currencyUSD", 20, w=4, decimals=2,
     desc="What one million generated tokens cost in VM time at the load the VM had. Idle time raises it; a busier VM lowers it.")
advance(4)
ts("Revenue per hour vs VM cost", [
    target(f'sum(rate({OUT}[{RI}])) * $price_output * 3600 / 1e6', "output tokens"),
    target(f'sum(rate({IN_C}[{RI}])) * $price_input * 3600 / 1e6', "computed input", "B"),
    target(f'sum(rate({IN_H}[{RI}])) * $price_cached_input * 3600 / 1e6', "cached input", "C"),
    target(f'vector($vm_usd_per_hour) and on() max(up{{"service.name"="vllm"}})', "VM cost", "D")],
   "currencyUSD", 0, w=16, h=10, stack=True,
   overrides=[{"matcher": {"id": "byName", "options": "VM cost"},
               "properties": [{"id": "custom.stacking", "value": {"mode": "none", "group": "B"}},
                              {"id": "custom.fillOpacity", "value": 0},
                              {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}},
                              {"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}}]}],
   desc="Hourly revenue rate at the price variables, stacked by token type; the dashed line is the VM price. "
        "Revenue above the line is margin.")
text("Assumptions",
     "Prices are the dashboard variables at the top, in USD:\n\n"
     "| Item | USD |\n|---|---|\n"
     "| VM | **${vm_usd_per_hour}** per hour |\n"
     "| Input (computed) | **${price_input}** per 1M tokens |\n"
     "| Input (prefix-cache hit) | **${price_cached_input}** per 1M tokens |\n"
     "| Output | **${price_output}** per 1M tokens |\n\n"
     "Token prices default to a public list price for Qwen3.8-27B in October 2026: "
     "$0.425 per 1M input tokens, $0.085 cached input and $2.55 output. Set them to the "
     "prices you want to compare against.\n\n"
     "Token counts come from vLLM. Output includes reasoning tokens. VM hours count only "
     "minutes in which the model server answered its scrape.\n\n"
     f"Capacity reference (benchmark, real text, 512-token replies): about **1,550 output tokens/s** "
     f"at {MAX_SEQS} concurrent requests, **166 tokens/s** for a single request.",
     16, 8, 10)
advance(10)

# ---------------------------------------------------------------- Tokens
# Input = prompt tokens, output = generated tokens, both counted by vLLM.
row("Throughput: input, output and per request")
# Peaks use a 1-minute rate sampled every 15 s (the scrape interval), so a
# short burst counts; $__rate_interval would smooth it away.
PEAK = 'max_over_time(sum(rate({m}[1m]))[$__range:15s])'
stat("Avg input/s", 'sum(increase(vllm:prompt_tokens_total[$__range])) / $__range_s', "short", 0, w=3,
     decimals=0, desc="Input tokens per second averaged over the selected range.")
stat("Peak input/s", PEAK.format(m="vllm:prompt_tokens_total"), "short", 3, w=3, decimals=0,
     desc="Highest 1-minute input rate in the selected range, prefix-cache hits included.")
stat("Avg output/s", f'sum(increase({OUT}[$__range])) / $__range_s', "short", 6, w=3,
     decimals=0, desc="Output tokens per second averaged over the selected range.")
stat("Peak output/s", PEAK.format(m=OUT), "short", 9, w=3, decimals=0,
     desc="Highest 1-minute output rate in the selected range.")
stat("Avg total/s", f'(sum(increase(vllm:prompt_tokens_total[$__range])) + sum(increase({OUT}[$__range]))) / $__range_s',
     "short", 12, w=3, decimals=0,
     desc="Input plus output tokens per second averaged over the selected range.")
stat("Peak total/s", f'max_over_time((sum(rate(vllm:prompt_tokens_total[1m])) + sum(rate({OUT}[1m])))[$__range:15s])',
     "short", 15, w=3, decimals=0,
     desc="Highest 1-minute input-plus-output rate in the selected range.")
stat("Input per output", 'sum(increase(vllm:prompt_tokens_total[$__range])) / '
     f'sum(increase({OUT}[$__range]))', "short", 18, w=3, decimals=0,
     desc="Input tokens per output token in the selected range. Agentic workloads resend long contexts, so this runs high.")
stat("Input cached", f'sum(increase({IN_H}[$__range])) / '
     'sum(increase(vllm:prompt_tokens_by_source_total[$__range]))', "percentunit", 21, w=3, decimals=0, desc="Share of input tokens served from the prefix cache instead of computed in prefill.")
advance(4)
ts("Input tokens per second", [
    target(f'sum(rate({IN_C}[{RI}]))', "computed (prefill)"),
    target(f'sum(rate({IN_H}[{RI}]))', "prefix-cache hit", "B")],
   "short", 0, stack=True,
   desc="Prompt tokens per second, stacked: the top of the stack is the total input rate. "
        "Only the computed part costs GPU time; cache hits are reused KV blocks.")
ts("Output tokens per second", [target(f'sum(rate({OUT}[{RI}]))', "all requests")],
   "short", 12, desc="Generated tokens per second summed over every running request.")
advance(8)
stat("Peak per-request output/s",
     'max_over_time((1 / histogram_quantile(0.5, sum(rate(vllm:inter_token_latency_seconds[1m]))))[$__range:15s])',
     "short", 0, w=4, h=9, decimals=0,
     desc="Highest 1-minute median speed one request saw in the selected range (1 / median inter-token latency). "
          "About 166 tok/s is the ceiling with no other load; long prompts being prefilled at the same time pull it down. "
          "Per-request maxima from the histograms are skewed by very short replies, so this uses the median.")
ts("Per-request output speed", [
    target(f'1 / histogram_quantile(0.5, sum(rate(vllm:inter_token_latency_seconds[{RI}])))', "median request"),
    target(f'1 / histogram_quantile(0.95, sum(rate(vllm:inter_token_latency_seconds[{RI}])))', "slow requests (p95 latency)", "B")],
   "short", 4, w=8, h=9, desc="Output tokens per second one request sees: 1 / inter-token latency. Falls as concurrency and prefill load rise.")
ts("Concurrent requests", [
    target('sum(vllm:num_requests_running)', "running in vLLM"),
    target('sum(vllm:num_requests_waiting)', "waiting in vLLM", "B"),
    target('sum(litellm_in_flight_requests)', "in flight at LiteLLM", "C")],
   "short", 12, w=12, h=9, maxv=MAX_SEQS + 4,
   thresholds=[{"color": "transparent", "value": None}, {"color": WARN, "value": MAX_SEQS}],
   desc=f"Requests in progress. The dashed line is the {MAX_SEQS}-request limit (--max-num-seqs): "
        "above it requests wait for a slot. LiteLLM counts a request from arrival to its last byte, so it reads slightly higher.")
advance(9)
ts("Input tokens per request", [
    target(f'histogram_quantile(0.5, sum(rate(vllm:request_prompt_tokens[{RI}])))', "p50"),
    target(f'histogram_quantile(0.95, sum(rate(vllm:request_prompt_tokens[{RI}])))', "p95", "B")],
   "short", 0, w=12, desc="Prompt length of finished requests.")
ts("Output tokens per request", [
    target(f'histogram_quantile(0.5, sum(rate(vllm:request_generation_tokens[{RI}])))', "p50"),
    target(f'histogram_quantile(0.95, sum(rate(vllm:request_generation_tokens[{RI}])))', "p95", "B")],
   "short", 12, w=12, desc="Generated length of finished requests, reasoning included.")
advance(8)

# ---------------------------------------------------------------- Gateway
# LiteLLM's own counters. Its per-token latency and token-count histograms
# mix float and histogram samples in the workspace, so they're left out.
row("LiteLLM gateway")
stat("Gateway input tokens", 'sum(increase(litellm_input_tokens_metric_total[$__range]))', "short", 0, w=4,
     decimals=1, desc="Prompt tokens LiteLLM counted in the selected range. Should match vLLM's count.")
stat("Gateway output tokens", 'sum(increase(litellm_output_tokens_metric_total[$__range]))', "short", 4, w=4,
     decimals=1, desc="Completion tokens LiteLLM counted in the selected range, reasoning included.")
stat("Reasoning share of output", 'sum(increase(litellm_output_reasoning_tokens_metric_total[$__range])) / '
     'sum(increase(litellm_output_tokens_metric_total[$__range]))', "percentunit", 8, w=4, decimals=0,
     desc="Share of output tokens spent thinking before the visible answer.")
stat("Peak in flight", 'max_over_time(sum(litellm_in_flight_requests)[$__range:15s])', "short", 12, w=4,
     decimals=0, desc="Most requests LiteLLM held open at once in the selected range.")
stat("p50 time to first token", 'histogram_quantile(0.5, sum(rate(litellm_e2e_time_to_first_token_seconds[$__range])))',
     "s", 16, w=4, decimals=1, desc="Median wait for the first streamed token, measured at the gateway.")
stat("p95 end-to-end latency", 'histogram_quantile(0.95, sum(rate(litellm_e2e_request_duration_seconds[$__range])))',
     "s", 20, w=4, decimals=1, desc="95th percentile request duration at the gateway in the selected range.")
advance(4)
ts("Gateway tokens per second", [
    target(f'sum(rate(litellm_input_tokens_metric_total[{RI}]))', "input"),
    target(f'sum(rate(litellm_output_tokens_metric_total[{RI}]))', "output", "B"),
    target(f'sum(rate(litellm_output_reasoning_tokens_metric_total[{RI}]))', "of which reasoning", "C")],
   "short", 0, w=12, h=9,
   overrides=[{"matcher": {"id": "byName", "options": "input"},
               "properties": [{"id": "custom.axisPlacement", "value": "right"}]}],
   desc="Tokens per second counted by LiteLLM when each request finishes (input on the right axis). "
        "Requests are counted at the end, so this is burstier than vLLM's rates.")
ts("Gateway time to first token", [
    target(f'histogram_quantile(0.5, sum(rate(litellm_e2e_time_to_first_token_seconds[{RI}])))', "p50"),
    target(f'histogram_quantile(0.95, sum(rate(litellm_e2e_time_to_first_token_seconds[{RI}])))', "p95", "B")],
   "s", 12, w=12, h=9, desc="Time from the client's request to the first streamed token, gateway included.")
advance(9)

# ---------------------------------------------------------------- Traffic
row("Traffic and latency")
ts("Requests by HTTP status", [target(f'sum by (http_status) (rate(litellm_e2e_requests_total[{RI}]))',
                                      "{{http_status}}")], "reqps", 0, w=24,
   desc="Gateway requests per second by response status.",
   overrides=[{"matcher": {"id": "byName", "options": "200"}, "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "green"}}]},
              {"matcher": {"id": "byRegexp", "options": "^5.."}, "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}}]},
              {"matcher": {"id": "byRegexp", "options": "^4.."}, "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "orange"}}]}])
advance(8)
ts("End-to-end latency (gateway)", [
    target(f'histogram_quantile(0.5, sum(rate(litellm_e2e_request_duration_seconds[{RI}])))', "p50"),
    target(f'histogram_quantile(0.95, sum(rate(litellm_e2e_request_duration_seconds[{RI}])))', "p95", "B")],
   "s", 0, w=8, desc="Request duration measured at LiteLLM. Long generations dominate p95.")
ts("Time to first token", [
    target(f'histogram_quantile(0.5, sum(rate(vllm:time_to_first_token_seconds[{RI}])))', "p50"),
    target(f'histogram_quantile(0.95, sum(rate(vllm:time_to_first_token_seconds[{RI}])))', "p95", "B")],
   "s", 8, w=8, desc="Queueing plus prefill, measured by vLLM.")
ts("Time per output token", [
    target(f'histogram_quantile(0.5, sum(rate(vllm:inter_token_latency_seconds[{RI}])))', "p50"),
    target(f'histogram_quantile(0.95, sum(rate(vllm:inter_token_latency_seconds[{RI}])))', "p95", "B")],
   "s", 16, w=8, desc="Gap between output tokens during decode. Rises with concurrency.")
advance(8)

# ---------------------------------------------------------------- Throughput
row("Cache and scheduling")
ts("KV cache usage", [target('max(vllm:kv_cache_usage_perc)', "KV cache")], "percentunit", 0, w=12,
   maxv=1, thresholds=[{"color": "transparent", "value": None}, {"color": WARN, "value": 0.9}],
   desc="Share of KV cache blocks in use. Near 100% vLLM starts preempting requests.", legend_calcs=("max",))
ts("Speculative decoding acceptance", [
    target(f'sum(rate(vllm:spec_decode_num_accepted_tokens_total[{RI}])) / sum(rate(vllm:spec_decode_num_draft_tokens_total[{RI}]))', "accepted / drafted")],
   "percentunit", 12, w=12, maxv=1, legend_calcs=("mean",),
   desc="Share of MTP draft tokens the model accepted. Higher means more tokens per forward pass.")
advance(8)
ts("Finished requests by reason", [target(f'sum by (finished_reason) (rate(vllm:request_success_total[{RI}])) > 0', "{{finished_reason}}")],
   "reqps", 0, w=8,
   desc="Why generations ended. 'length' means max_tokens cut the answer short.")
ts("Prefix cache hit rate", [
    target(f'sum(rate(vllm:prefix_cache_hits_total[{RI}])) / sum(rate(vllm:prefix_cache_queries_total[{RI}]))', "hit rate")],
   "percentunit", 8, w=8, maxv=1, legend_calcs=("mean",),
   desc="Share of prompt tokens served from the prefix cache instead of recomputed.")
ts("Preemptions per second", [target(f'sum(rate(vllm:num_preemptions_total[{RI}]))', "preemptions")], "short", 16, w=8,
   legend_calcs=("max",), desc="Requests evicted from the KV cache under memory pressure. Should stay at 0.")
advance(8)

# ---------------------------------------------------------------- GPU
row("GPU")
ts("GPU and SM activity", [target('max(dcgm_fi_dev_gpu_util) / 100', "GPU busy"),
                           target('max(dcgm_fi_prof_sm_active)', "SM active", "B"),
                           target('max(dcgm_fi_prof_pipe_tensor_active)', "tensor cores active", "C")],
   "percentunit", 0, w=8, h=9, maxv=1,
   desc="GPU busy counts any running kernel; SM and tensor-core activity show how much of the chip does work.")
ts("Power", [target('max(dcgm_fi_dev_power_usage)', "draw"),
             target('max(dcgm_fi_dev_power_mgmt_limit)', "limit", "B")], "watt", 8, w=8, h=9,
   overrides=[{"matcher": {"id": "byName", "options": "limit"},
               "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}},
                              {"id": "color", "value": {"mode": "fixed", "fixedColor": "text"}}]}],
   desc="Board power against the enforced limit.")
ts("Temperature", [target('max(dcgm_fi_dev_gpu_temp)', "GPU"),
                   target('max(dcgm_fi_dev_memory_temp)', "HBM", "B")], "celsius", 16, w=8, h=9, minv=None,
   desc="GPU die and HBM memory temperature.")
advance(9)
ts("GPU memory", [target('max(dcgm_fi_dev_fb_used) * 1024 * 1024', "used"),
                  target('max(dcgm_fi_dev_fb_used + dcgm_fi_dev_fb_free + dcgm_fi_dev_fb_reserved) * 1024 * 1024', "total", "B")],
   "bytes", 0, w=8, h=10,
   overrides=[{"matcher": {"id": "byName", "options": "total"},
               "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}},
                              {"id": "color", "value": {"mode": "fixed", "fixedColor": "text"}}]}],
   desc="Framebuffer in use. vLLM reserves most of it for the KV cache at start-up, so this stays flat.")
ts("Throttled time", [target(f'max(rate(dcgm_fi_dev_power_violation[{RI}])) / 1e9', "power"),
                      target(f'max(rate(dcgm_fi_dev_thermal_violation[{RI}])) / 1e9', "thermal", "B"),
                      target(f'max(rate(dcgm_fi_dev_board_limit_violation[{RI}])) / 1e9', "board limit", "C"),
                      target(f'max(rate(dcgm_fi_dev_reliability_violation[{RI}])) / 1e9', "reliability", "D")],
   "percentunit", 8, w=8, h=10, maxv=1, desc="Share of time clocks were held down, by cause.")
stat("Xid errors", f'max(increase(dcgm_exp_xid_errors_count[$__range]))', "short", 16, w=4, h=10, decimals=0,
     thresholds=[{"color": GOOD, "value": None}, {"color": BAD, "value": 1}], color_mode="background",
     desc="GPU Xid errors in the selected range. Any Xid deserves a look at the host kernel log.")
stat("GPU health", 'max(dcgm_exp_gpu_health_status)', "none", 20, w=4, h=10,
     thresholds=[{"color": GOOD, "value": None}, {"color": WARN, "value": 10}, {"color": BAD, "value": 20}],
     color_mode="background",
     mappings=[{"type": "value", "options": {"0": {"text": "Pass"}, "10": {"text": "Warn"}, "20": {"text": "Fail"}}},
               {"type": "special", "options": {"match": "null", "result": {"text": "No data"}}}],
     desc="Worst DCGM health-watch result. Warn (10) appears with power throttling under normal load; Fail (20) needs action.")
advance(10)

# ---------------------------------------------------------------- Host
row("Host and containers")
ts("Host CPU and memory", [target('1 - max({__name__="system.cpu.utilization", state="idle"})', "CPU busy"),
                           target('max({__name__="system.memory.utilization", state="used"})', "memory used", "B")],
   "percentunit", 0, w=8, h=10, maxv=1, desc="Host CPU busy share and memory in use (page cache excluded).")
ts("Container CPU", [target(f'sum by ("container.name") (rate({{__name__="container.cpu.usage.total"}}[{RI}])) / 1e9', "{{container.name}}")],
   "short", 8, w=8, h=10, desc="CPU cores used per container.")
ts("Container memory", [target('max by ("container.name") ({__name__="container.memory.usage.total"})', "{{container.name}}")],
   "bytes", 16, w=8, h=10, desc="Memory per container. vLLM holds model weights in host memory while loading.")
advance(10)
p = {"type": "bargauge", "id": nid(), "title": "Disk usage", "datasource": DS,
     "description": "Used share per filesystem. /data holds the model weights and Docker.",
     "targets": [target('max by (mountpoint) ({__name__="system.filesystem.utilization"})', "{{mountpoint}}")],
     "fieldConfig": {"defaults": {"unit": "percentunit", "min": 0, "max": 1, "decimals": 0,
                                  "thresholds": {"mode": "absolute", "steps": [{"color": "blue", "value": None},
                                                                                {"color": WARN, "value": 0.8},
                                                                                {"color": BAD, "value": 0.9}]},
                                  "color": {"mode": "thresholds"}}, "overrides": []},
     "options": {"orientation": "horizontal", "displayMode": "basic", "showUnfilled": True,
                 "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                 "valueMode": "color", "namePlacement": "left", "sizing": "auto"}}
place(p, 0, 8, 8)
ts("Host network", [target(f'sum by (direction) (rate({{__name__="system.network.io"}}[{RI}]))', "{{direction}}")],
   "Bps", 8, w=8, desc="Bytes per second across host interfaces. Model downloads show up as large receive spikes.")
ts("Container restarts", [target(f'max by ("container.name") (increase({{__name__="container.restarts"}}[{RI}])) > 0', "{{container.name}}")],
   "short", 16, w=8, draw="bars", legend_calcs=("max",), no_value="None",
   desc="Restarts per container within each interval. 'None' means no container restarted.")
advance(8)

# ---------------------------------------------------------------- Pipeline
row("Telemetry pipeline")
ts("Spans and logs exported", [target(f'sum(rate(otelcol_exporter_sent_spans[{RI}]))', "spans"),
                               target(f'sum(rate(otelcol_exporter_sent_log_records[{RI}]))', "log records", "B")],
   "short", 0, w=8, desc="Items per second the collector sent to App Insights.")
ts("Metric points exported", [target(f'sum(rate(otelcol_exporter_sent_metric_points[{RI}]))', "metric points")],
   "short", 8, w=8, desc="Points per second sent to the Azure Monitor workspace.")
ts("Export queue fill", [target('max(otelcol_exporter_queue_size / otelcol_exporter_queue_capacity)', "queue fill")],
   "percentunit", 16, w=8, maxv=1, legend_calcs=("max",),
   thresholds=[{"color": "transparent", "value": None}, {"color": WARN, "value": 0.5}],
   desc="Share of the persistent sending queue in use. Growth means Azure is rejecting data or unreachable.")
advance(8)

# ---------------------------------------------------------------- Traces and logs
# Spans and logs land in Application Insights (Log Analytics), so these panels
# query it with KQL through the Azure Monitor data source. They project chosen
# fields only: LLM-call spans also carry the client's tool definitions, and
# request spans the public host name.
AZMON = {"type": "grafana-azure-monitor-datasource", "uid": "${azmon}"}


def kql(query, fmt="table"):
    return {"refId": "A", "datasource": AZMON, "queryType": "Azure Log Analytics",
            "azureLogAnalytics": {"query": query, "resources": ["$appinsights"], "resultFormat": fmt}}


row("Traces and logs (Application Insights)")
LLM_CALLS = """dependencies
| where $__timeFilter(timestamp)
| where name startswith "chat "
| extend d = customDimensions
| project Time = timestamp,
    ['Result'] = iff(tobool(success), "ok", "error"),
    ['Duration'] = duration / 1000.0,
    ['Time to first token'] = todouble(d["gen_ai.response.time_to_first_chunk"]),
    ['Input tokens'] = tolong(d["gen_ai.usage.input_tokens"]),
    ['Output tokens'] = tolong(d["gen_ai.usage.output_tokens"]),
    ['Finish reason'] = tostring(todynamic(tostring(d["gen_ai.response.finish_reasons"]))[0]),
    ['Trace ID'] = operation_Id
| order by Time desc
| take 200"""
place({"type": "table", "id": nid(), "title": "LLM calls (traces)", "datasource": AZMON,
       "description": "One row per model call, from the LiteLLM spans in Application Insights: duration, "
                      "time to first token, token counts and why generation stopped. Errors carry no token counts. Open the trace ID in "
                      "Application Insights or Grafana Explore for the whole request.",
       "targets": [kql(LLM_CALLS)],
       "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}},
                       "overrides": [
                           {"matcher": {"id": "byRegexp", "options": "^(Duration|Time to first token)$"},
                            "properties": [{"id": "unit", "value": "s"}, {"id": "decimals", "value": 1}]},
                           {"matcher": {"id": "byName", "options": "Time to first token"},
                            "properties": [{"id": "custom.cellOptions", "value": {"type": "color-text"}},
                                           {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                                               {"color": GOOD, "value": None}, {"color": WARN, "value": 10},
                                               {"color": BAD, "value": 30}]}}]},
                           {"matcher": {"id": "byName", "options": "Input tokens"},
                            "properties": [{"id": "unit", "value": "short"}, {"id": "min", "value": 0},
                                           {"id": "max", "value": 262144},
                                           {"id": "custom.cellOptions",
                                            "value": {"type": "gauge", "mode": "basic", "valueDisplayMode": "text"}},
                                           {"id": "color", "value": {"mode": "fixed", "fixedColor": "blue"}}]},
                           {"matcher": {"id": "byName", "options": "Output tokens"},
                            "properties": [{"id": "unit", "value": "short"}]},
                           {"matcher": {"id": "byName", "options": "Result"},
                            "properties": [{"id": "custom.cellOptions", "value": {"type": "color-text"}},
                                           {"id": "custom.width", "value": 90},
                                           {"id": "mappings", "value": [{"type": "value", "options": {
                                               "ok": {"color": GOOD, "index": 0},
                                               "error": {"color": BAD, "index": 1}}}]}]},
                           {"matcher": {"id": "byName", "options": "Trace ID"},
                            "properties": [{"id": "custom.width", "value": 300}]}]},
       "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}}},
      0, 14, 12)
ENGINE_LOG = r"""traces
| where $__timeFilter(timestamp)
| where tostring(customDimensions["service"]) == "local-llm"
| where message has "Avg prompt throughput" or severityLevel >= 2
| project timestamp,
    Message = trim_end(@"\s+", coalesce(extract(@"(Engine \d+: [^\r\n]*)", 1, message), message)),
    level = case(severityLevel >= 3, "error", severityLevel == 2, "warning", "info")
| order by timestamp desc
| take 300"""
place({"type": "logs", "id": nid(), "title": "Model server log (vLLM engine stats and warnings)",
       "datasource": AZMON,
       "description": "vLLM's own log lines from the model container, shipped by the collector's filelog "
                      "receiver: the engine stats it prints every 10 seconds, plus any warning or error.",
       "targets": [kql(ENGINE_LOG, "logs")],
       "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending",
                   "enableLogDetails": True, "dedupStrategy": "none", "prettifyLogMessage": False,
                   "showLabels": False, "showCommonLabels": False}},
      14, 10, 12)
advance(12)

# Token prices default to a public list price for Qwen3.8-27B in October 2026.
VARIABLES = [  # name, label, default (USD); edit them at the top of the dashboard
    ("vm_usd_per_hour", "VM $/hour", "2"),
    ("price_input", "Input $/1M", "0.425"),
    ("price_cached_input", "Cached input $/1M", "0.085"),
    ("price_output", "Output $/1M", "2.55"),
]


DATASOURCE = {"type": "datasource", "name": "datasource", "label": "Data source", "query": "prometheus",
              "hide": 0, "current": {}, "options": [], "refresh": 1, "regex": ""}


def textbox(name, label, default):
    return {"type": "textbox", "name": name, "label": label, "query": default, "hide": 0,
            "current": {"text": default, "value": default},
            "options": [{"selected": True, "text": default, "value": default}]}


AZMON_VAR = {"type": "datasource", "name": "azmon", "label": "Azure Monitor", "query": "grafana-azure-monitor-datasource",
             "hide": 0, "refresh": 1, "regex": "", "current": {}, "options": []}
# Application Insights resource ID for the traces and logs panels. Grafana's Import dialog asks for it
# (the VAR_APPINSIGHTS input) and writes it into this hidden constant variable.
APPINSIGHTS_VAR = {"type": "constant", "name": "appinsights", "label": "Application Insights", "hide": 2,
                   "query": "${VAR_APPINSIGHTS}",
                   "current": {"text": "${VAR_APPINSIGHTS}", "value": "${VAR_APPINSIGHTS}"},
                   "options": [{"selected": True, "text": "${VAR_APPINSIGHTS}", "value": "${VAR_APPINSIGHTS}"}]}
INPUTS = [{"name": "VAR_APPINSIGHTS", "type": "constant", "label": "Application Insights resource ID",
           "description": "The Application Insights resource with OTLP support that receives the traces and logs",
           "value": "/subscriptions/<subscription-id>/resourceGroups/<resource-group>"
                    "/providers/Microsoft.Insights/components/<application-insights>"}]


dashboard = {
    "__inputs": INPUTS,
    "uid": "h100-llm",
    "title": "h100-llm",
    "description": "Self-hosted Qwen3.8-27B on an Azure Spot H100 VM: token throughput and economics, gateway traffic, vLLM serving, GPU, host and telemetry pipeline.",
    "tags": ["llm-self-hosting", "vllm", "gpu"],
    "timezone": "utc",
    "editable": True,
    "graphTooltip": 1,
    "refresh": "30s",
    "time": {"from": "now-6h", "to": "now"},
    "schemaVersion": 39,
    "templating": {"list": [DATASOURCE] + [textbox(*v) for v in VARIABLES] + [AZMON_VAR, APPINSIGHTS_VAR]},
    "annotations": {"list": []},
    "links": [],
    "panels": panels,
}

out = pathlib.Path(__file__).with_name("h100-llm.json")
out.write_text(json.dumps(dashboard, indent=2) + "\n")
print(f"{len(panels)} panels -> {out}")
