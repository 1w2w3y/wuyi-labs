#!/bin/bash
# Serving throughput benchmark, run on the VM:  scripts/vm-run.sh scripts/bench.sh
# Runs `vllm bench serve` inside the model container against vLLM directly
# (not the gateway), so the numbers measure the engine, not LiteLLM.
#   1. random prompts (IN tokens in, OUT tokens out, ignore_eos) at each
#      concurrency in CONCURRENCY: aggregate tok/s, TTFT and ITL
#   2. shared-prefix prompts at concurrency 8, the agentic-coding shape that
#      prefix caching serves
# Prints one compact line per run (run-command returns only the last ~4 KB);
# full JSON results go to /opt/llm-self-hosting/results/bench-<timestamp>/.
# Random token prompts understate MTP acceptance; compare the single-stream
# decode figure from smoke-test.sh for real text.
#
# Overrides, e.g.:  scripts/vm-run.sh -c 'CONCURRENCY="1 16" bash /opt/llm-self-hosting/scripts/bench.sh'
cd /opt/llm-self-hosting || exit 1
CONCURRENCY="${CONCURRENCY:-1 4 16 32}"
IN="${IN:-2000}"
OUT="${OUT:-512}"
CTR=qwen3.8-27b
MODEL_DIR="$(grep -E '^MODEL_DIR=' .env | tail -1 | cut -d= -f2)"
MODEL_DIR="${MODEL_DIR:-Qwen3.8-27B-FP8}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RES="/opt/llm-self-hosting/results/bench-${STAMP}"
mkdir -p "$RES"

[ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8096/health)" = 200 ] \
  || { echo "vLLM is not healthy"; exit 1; }
echo "model=${MODEL_DIR} in=${IN} out=${OUT} results=${RES}"
nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader
docker logs "$CTR" 2>&1 | grep -E -o 'GPU KV cache size: [0-9,]+ tokens|Model loading took [0-9.]+ GiB' | sort -u

# bench <name> <concurrency> <num-prompts> <dataset args...>
bench() {
  local name="$1" c="$2" n="$3"; shift 3
  docker exec "$CTR" vllm bench serve \
    --base-url http://127.0.0.1:8080 --endpoint /v1/completions \
    --model qwen3.8-27b --tokenizer "/models/${MODEL_DIR}" \
    --max-concurrency "$c" --num-prompts "$n" --seed 0 --ignore-eos \
    --percentile-metrics ttft,itl --metric-percentiles 50,99 \
    --save-result --result-dir /tmp/bench --result-filename "${name}.json" \
    "$@" > "${RES}/${name}.log" 2>&1
  docker cp "${CTR}:/tmp/bench/${name}.json" "${RES}/" >/dev/null 2>&1
  python3 - "${RES}/${name}.json" "$name" "$c" <<'PY' || { echo "$name: FAILED"; tail -5 "${RES}/${name}.log" | cut -c1-200; }
import json, sys
r = json.load(open(sys.argv[1]))
print(f"{sys.argv[2]:<14} c={sys.argv[3]:<3} ok={r['completed']:<3} "
      f"out={r['output_throughput']:7.1f} tok/s  per-stream={r['output_throughput']/int(sys.argv[3]):6.1f}  "
      f"TTFT p50/p99={r['median_ttft_ms']:6.0f}/{r['p99_ttft_ms']:6.0f} ms  "
      f"ITL p50/p99={r['median_itl_ms']:5.1f}/{r['p99_itl_ms']:5.1f} ms")
PY
}

for c in $CONCURRENCY; do
  n=$(( c * 4 )); [ "$n" -lt 8 ] && n=8
  bench "random-c${c}" "$c" "$n" \
    --dataset-name random --random-input-len "$IN" --random-output-len "$OUT"
done

# 8 distinct 16K-token prefixes, 4 prompts each with a 1K-token unique suffix.
bench "prefix-c8" 8 32 \
  --dataset-name prefix_repetition \
  --prefix-repetition-prefix-len 16000 --prefix-repetition-suffix-len 1000 \
  --prefix-repetition-num-prefixes 8 --prefix-repetition-output-len "$OUT"

docker logs --since 15m "$CTR" 2>&1 | grep -E -o 'Mean acceptance length: [0-9.]+|Prefix cache hit rate: [0-9.]+%' | tail -2
nvidia-smi --query-gpu=memory.used,power.draw --format=csv,noheader
