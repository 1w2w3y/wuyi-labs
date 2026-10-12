#!/usr/bin/env bash
# Stop the agents of a load-test run:  load-test/stop.sh <run dir>
#
# Stops each process launch.sh recorded in <run dir>/started.txt. The model
# server doesn't stop with them: the agents' disconnects don't reach vLLM
# through the gateway and the tunnel, so it finishes the requests they left
# queued. Wait for sum(vllm:num_requests_waiting) to reach 0 before the next
# run.
set -u
RUN="${1:?usage: load-test/stop.sh <run dir>}"
awk '$2 ~ /^[0-9]+$/ { print $2, $3 }' "$RUN/started.txt" | while read -r pid name; do
  if kill "$pid" 2>/dev/null; then echo "stopped $name ($pid)"; fi
done
