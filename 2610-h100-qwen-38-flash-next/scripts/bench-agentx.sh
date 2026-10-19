#!/bin/bash
# AgentX agentic-coding benchmark, run on the VM:
#   scripts/vm-run.sh scripts/bench-agentx.sh
#
# Where bench.sh measures the engine on synthetic prompts, this replays real
# coding-agent sessions: SemiAnalysis's AgentX corpus, 393 opt-in Claude Code
# traces, driven by NVIDIA's AIPerf. The shape is the point -- median 43 turns
# per session, median 142K input tokens against median 444 output -- so it
# exercises prefill and prefix caching the way agent traffic actually does,
# and the result is comparable with numbers other people publish.
#
# It replays fixed token shapes, so it does not depend on the model's own
# agentic behaviour. For that, see ../../agent-bench, which drives real
# opencode sessions and reports how much work they completed.
#
# Overrides, e.g.:
#   scripts/vm-run.sh -c 'CONCURRENCY="8 32" DURATION=600 bash /opt/llm-self-hosting/scripts/bench-agentx.sh'
cd /opt/llm-self-hosting || exit 1
CONCURRENCY="${CONCURRENCY:-8 32 64}"
DURATION="${DURATION:-900}"
# Pin the seed and a date-stamped corpus alias for runs you intend to compare.
SEED="${SEED:-20260707}"
DATASET="${DATASET:-semianalysis_cc_traces_weka_with_subagents}"
MODEL_NAME="${MODEL_NAME:-qwen3.8-flash-next-vllm}"
MODEL_DIR="$(grep -E '^MODEL_DIR=' .env | tail -1 | cut -d= -f2)"
MODEL_DIR="${MODEL_DIR:-Qwen3.8-Flash-Next-FP8}"
CTR=qwen3.8-flash-next-vllm
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RES="/opt/llm-self-hosting/results/agentx-${STAMP}"
mkdir -p "$RES"

[ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8096/health)" = 200 ] \
  || { echo "vLLM is not healthy"; exit 1; }

# AIPerf runs in its own container: it needs Python 3.11+ and pulls the corpus
# from HuggingFace, and we would rather not touch the model container.
# --network host so 127.0.0.1:8096 is the same vLLM the rest of the stack uses.
# The HF cache is a volume, so the corpus downloads once.
docker volume create agentx-hf-cache >/dev/null 2>&1

AIPERF_IMAGE="${AIPERF_IMAGE:-python:3.12-slim}"
echo "model=${MODEL_NAME} dataset=${DATASET} seed=${SEED} results=${RES}"
nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader

run_one() {
  local c="$1"
  echo "--- concurrency ${c} for ${DURATION}s ---"
  docker run --rm --network host \
    -v agentx-hf-cache:/root/.cache/huggingface \
    -v "${RES}:/out" \
    -v "/opt/llm-self-hosting/models/${MODEL_DIR}:/tokenizer:ro" \
    -e HF_HOME=/root/.cache/huggingface \
    "$AIPERF_IMAGE" \
    bash -lc "pip install --quiet aiperf && \
      aiperf profile \
        --scenario inferencex-agentx-mvp \
        --public-dataset ${DATASET} \
        --url http://127.0.0.1:8096 \
        --endpoint-type chat \
        --model ${MODEL_NAME} \
        --tokenizer /tokenizer \
        --concurrency ${c} \
        --benchmark-duration ${DURATION} \
        --random-seed ${SEED} \
        --use-server-token-count \
        --artifact-directory /out/c${c} \
        --ui simple" > "${RES}/c${c}.log" 2>&1

  python3 - "${RES}/c${c}" "$c" <<'PY' || { echo "c=$c: FAILED"; tail -5 "${RES}/c${c}.log" | cut -c1-200; }
import json, pathlib, sys
root, c = pathlib.Path(sys.argv[1]), sys.argv[2]
files = list(root.rglob("profile_export_aiperf.json"))
if not files:
    raise SystemExit("no profile export")
d = json.loads(files[0].read_text())
meta = d.get("metadata") or {}
def stat(name, field="avg"):
    rec = (d.get("records") or {}).get(name) or d.get(name) or {}
    return rec.get(field) if isinstance(rec, dict) else rec
print("c={:<4} valid={} complete={}  out_tok/s={}  TTFT p50={} p99={}  ITL p50={}  overflow={}".format(
    c, meta.get("submission_valid"), meta.get("is_complete"),
    stat("output_token_throughput"), stat("time_to_first_token", "p50"),
    stat("time_to_first_token", "p99"), stat("inter_token_latency", "p50"),
    stat("context_overflow_count", "count")))
if meta.get("submission_valid") is False:
    print("  invalid:", meta.get("submission_invalid_reasons"))
PY
}

for c in $CONCURRENCY; do
  run_one "$c"
done

docker logs --since 30m "$CTR" 2>&1 \
  | grep -E -o 'Mean acceptance length: [0-9.]+|Prefix cache hit rate: [0-9.]+%' | tail -2
nvidia-smi --query-gpu=memory.used,power.draw --format=csv,noheader
echo "full results in ${RES}"
