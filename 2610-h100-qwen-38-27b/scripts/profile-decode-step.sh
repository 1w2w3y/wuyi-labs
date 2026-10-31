#!/bin/bash
# Profile one request's decode steps with vLLM's torch profiler, on the VM:
#   scripts/vm-run.sh scripts/profile-decode-step.sh
#
# The model must be running with the profiler armed, which costs some speed,
# so it is off by default (QWEN38_27B_PROFILER_CONFIG={} in .env). To arm it,
# set in the VM's /opt/llm-self-hosting/.env (single quotes: bash also reads it)
#   QWEN38_27B_PROFILER_CONFIG='{"profiler":"torch","torch_profiler_dir":"/root/.cache/vllm/profile","torch_profiler_with_stack":false}'
# and recreate the model container
#   scripts/vm-run.sh -c 'cd /opt/llm-self-hosting && docker compose up -d qwen3.8-27b'
# then remove the line and recreate it again afterwards.
#
# The run: POST /start_profile with a window of ITERATIONS engine steps after
# DELAY (one short prompt prefills in a step or two, the rest are decode
# steps), send one code-generation request at temperature 0 with thinking
# off, POST /stop_profile, then read the phases out of the trace with
# scripts/decode-step-phases.py. Traces land in /data/vllm-cache/profile
# (a few MB each, gzipped).
cd /opt/llm-self-hosting || exit 1
CTR=qwen3.8-27b
MODEL=qwen3.8-27b
V=http://127.0.0.1:8096
DELAY="${DELAY:-20}"
ITERATIONS="${ITERATIONS:-60}"
PROFILE_DIR=/data/vllm-cache/profile

if ! docker inspect "$CTR" --format '{{join .Args " "}}' 2>/dev/null | grep -q '"profiler": *"torch"\|"profiler":"torch"'; then
  echo "${CTR} is not running with the torch profiler; set QWEN38_27B_PROFILER_CONFIG in .env (see this script's header) and recreate it"
  exit 1
fi
[ "$(curl -s -o /dev/null -w '%{http_code}' $V/health)" = 200 ] || { echo "vLLM is not healthy"; exit 1; }
mkdir -p "$PROFILE_DIR"
before="$(ls -1 "$PROFILE_DIR" 2>/dev/null | wc -l)"

echo "--- warm-up request"
curl -s $V/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model":"'"$MODEL"'","max_tokens":64,"temperature":0,
  "chat_template_kwargs":{"enable_thinking":false},
  "messages":[{"role":"user","content":"Say OK."}]}' > /dev/null

echo "--- profiling ${ITERATIONS} engine steps after ${DELAY}"
curl -s -X POST $V/start_profile -H 'Content-Type: application/json' \
  -d "{\"delay_iterations\":${DELAY},\"max_iterations\":${ITERATIONS}}" -o /dev/null -w 'start_profile: %{http_code}\n'
start=$(date +%s.%N)
gen=$(curl -s $V/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model":"'"$MODEL"'","max_tokens":512,"temperature":0,"ignore_eos":true,
  "chat_template_kwargs":{"enable_thinking":false},
  "messages":[{"role":"user","content":"Write a Python module with a small LRU cache class (get, put, eviction) and unit tests for it."}]}')
end=$(date +%s.%N)
toks=$(echo "$gen" | jq -r '.usage.completion_tokens')
echo "completion_tokens=$toks wall=$(echo "$end - $start" | bc | cut -c1-6)s tok/s=$(echo "scale=1; $toks / ($end - $start)" | bc) (profiler on: slower than unprofiled)"
curl -s -X POST $V/stop_profile -o /dev/null -w 'stop_profile: %{http_code}\n'
# The trace is flushed after stop_profile returns.
for _ in $(seq 1 60); do
  [ "$(ls -1 "$PROFILE_DIR" 2>/dev/null | wc -l)" -gt "$before" ] && [ -z "$(find "$PROFILE_DIR" -name '*.tmp' -newer /proc/self 2>/dev/null)" ] && break
  sleep 2
done
sleep 5
docker logs --since 3m "$CTR" 2>&1 | grep -E -o 'Mean acceptance length: [0-9.]+' | tail -1
ls -1t "$PROFILE_DIR" | head -3

echo "--- phases"
python3 scripts/decode-step-phases.py "$PROFILE_DIR"
