#!/bin/bash
# Smoke test, run on the VM:  scripts/vm-run.sh scripts/smoke-test.sh
# Output stays compact because run-command returns only the last ~4 KB.
cd /opt/llm-self-hosting || exit 1
GW=http://127.0.0.1:4000
fail=0
check() { if [ "$2" = "$3" ]; then echo "PASS $1"; else echo "FAIL $1 (got: $2, want: $3)"; fail=1; fi; }

echo "--- vLLM startup facts"
docker logs qwen3.8-27b 2>&1 | grep -E -o 'GPU KV cache size: [0-9,]+ tokens|Maximum concurrency for [0-9,]+ tokens per request: [0-9.]+x|Model loading took [0-9.]+ GiB' | sort -u

echo "--- checks"
check "vLLM /health" "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8096/health)" 200
check "gateway /v1/models (no auth)" "$(curl -s -o /dev/null -w '%{http_code}' $GW/v1/models)" 200
check "gateway /metrics/ (no auth)" "$(curl -s -o /dev/null -w '%{http_code}' $GW/metrics/)" 200

ans=$(curl -s $GW/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model":"local-llm","max_tokens":64,"temperature":0,
  "messages":[{"role":"user","content":"Reply with exactly: PONG"}],
  "chat_template_kwargs":{"enable_thinking":false}}' | jq -r '.choices[0].message.content' | tr -d '[:space:]')
check "chat exact output" "$ans" PONG

tool=$(curl -s $GW/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model":"local-llm","max_tokens":512,"temperature":0,
  "chat_template_kwargs":{"enable_thinking":false},
  "messages":[{"role":"user","content":"Read the file /etc/hostname using the tool."}],
  "tools":[{"type":"function","function":{"name":"read_file","description":"Read a file",
    "parameters":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}}}],
  "tool_choice":"required"}')
check "tool call name" "$(echo "$tool" | jq -r '.choices[0].message.tool_calls[0].function.name')" read_file
echo "tool args: $(echo "$tool" | jq -c -r '.choices[0].message.tool_calls[0].function.arguments' | cut -c1-120)"
[ "$(echo "$tool" | jq -r '.error // empty')" ] && echo "tool error: $(echo "$tool" | jq -c .error | cut -c1-300)"

echo "--- decode speed (512 tokens, thinking off, single stream)"
start=$(date +%s.%N)
gen=$(curl -s $GW/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model":"local-llm","max_tokens":512,"temperature":0,"ignore_eos":true,
  "chat_template_kwargs":{"enable_thinking":false},
  "messages":[{"role":"user","content":"Write a detailed explanation of how TCP congestion control works."}]}')
end=$(date +%s.%N)
toks=$(echo "$gen" | jq -r '.usage.completion_tokens')
echo "completion_tokens=$toks wall=$(echo "$end - $start" | bc | cut -c1-6)s tok/s=$(echo "scale=2; $toks / ($end - $start)" | bc)"
docker logs --since 2m qwen3.8-27b 2>&1 | grep -E -o 'Mean acceptance length: [0-9.]+|Avg Draft acceptance rate: [0-9.]+%' | tail -2

nvidia-smi --query-gpu=memory.used,memory.total,power.draw --format=csv,noheader

if docker inspect llm-otel-collector >/dev/null 2>&1; then
  echo "--- telemetry (compose.observability.yaml)"
  check "otel-collector health" "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:13133/)" 200
  errs=$(docker logs --since 10m llm-otel-collector 2>&1 | grep -c -E '"(error|warn)"|\berror\b' || true)
  echo "collector error/warn lines in last 10m: $errs"
  docker logs --since 10m llm-otel-collector 2>&1 | grep -E -i 'error|warn' | tail -3 | cut -c1-300
fi
[ $fail = 0 ] && echo "ALL PASS" || echo "SOME CHECKS FAILED"
