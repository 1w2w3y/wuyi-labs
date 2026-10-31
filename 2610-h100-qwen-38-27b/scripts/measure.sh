#!/bin/bash
# Measure the running vLLM configuration end to end, on the VM, under a label:
#   scripts/vm-run.sh -c 'LABEL=before bash /opt/llm-self-hosting/scripts/measure.sh'
# One call per variant of an A/B: it records what is running (image, vLLM
# version, model runner, async scheduling, KV cache size), then runs
#   - scripts/smoke-test.sh: health, exact chat output, tool call, single-stream
#     decode through the gateway
#   - scripts/realtext.py at concurrency 1, 4 and 32: real-text decode against
#     vLLM directly, which is where speculative decoding shows
#   - scripts/bench.sh: random 2,000-in / 512-out at 1/4/16/32 and the
#     shared-prefix run (TTFT, ITL)
# The full log goes to /opt/llm-self-hosting/results/measure-<label>-<stamp>/
# on the VM; the summary printed at the end fits run-command's ~4 KB. About
# 7 minutes.
cd /opt/llm-self-hosting || exit 1
CTR=qwen3.8-27b
LABEL="${LABEL:-$1}"
[ -n "$LABEL" ] || { echo "usage: LABEL=<name> bash scripts/measure.sh" >&2; exit 2; }
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RES="results/measure-${LABEL}-${STAMP}"
mkdir -p "$RES"
{
  echo "=== ${LABEL} ${STAMP} container=${CTR}"
  docker inspect "$CTR" --format 'image={{.Config.Image}} started={{.State.StartedAt}}'
  docker logs "$CTR" 2>&1 | grep -E -o -i 'vLLM API server version [0-9a-z.+]+|using (the )?(v2 )?model runner[ a-z0-9]*|ModelRunner(V2)?\b|async_scheduling[=: ]+(True|False)|Model loading took [0-9.]+ GiB|torch.compile takes [0-9.]+ s|graph capturing finished in [0-9]+ secs' | sort -u | cut -c1-120
  docker logs "$CTR" 2>&1 | grep -E -o 'GPU KV cache size: [0-9,]+ tokens|Maximum concurrency for [0-9,]+ tokens per request: [0-9.]+x' | sort -u
  echo "--- smoke"; bash scripts/smoke-test.sh
  # The first real-text pass after a start runs 40-50% slower at 4 and 32
  # streams than the next (JIT and cache warm-up), so warm up first.
  echo "--- realtext warm-up"; python3 scripts/realtext.py 4 32
  echo "--- realtext"; python3 scripts/realtext.py 1 4 32
  docker logs --since 5m "$CTR" 2>&1 | grep -E -o 'Mean acceptance length: [0-9.]+' | tail -1 | sed 's/^/realtext /'
  echo "--- bench"; bash scripts/bench.sh
} > "${RES}/measure.log" 2>&1
echo "=== ${LABEL}: full log in ${RES}/measure.log"
grep -E 'image=|version|runner|async_scheduling|KV cache size|ALL PASS|^FAIL|tok/s=|acceptance|^realtext|^random-c|^prefix-c' "${RES}/measure.log" | cut -c1-160
