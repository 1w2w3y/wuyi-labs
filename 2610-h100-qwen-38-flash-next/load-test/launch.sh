#!/usr/bin/env bash
# Load the model with real coding agents: one read-only opencode session per
# repository folder, GAP_S seconds apart, until MAX_AGENTS run or free memory
# falls under MIN_FREE_GB. Then wait for them to finish.
#
#   LOCAL_LLM_URL=https://<your-domain>.ngrok-free.dev \
#   REPOS="$HOME/src/repo-a $HOME/src/repo-b" load-test/launch.sh
#
# Settings (environment):
#   LOCAL_LLM_URL  the gateway's base URL, without /v1 (required)
#   REPOS          the folders to review, space-separated (required)
#   MAX_AGENTS     at most this many agents (default 8)
#   MIN_FREE_GB    stop launching under this much free memory (default 8);
#                  each opencode process holds ~0.7-0.9 GB
#   GAP_S          seconds between launches (default 3)
#   PROMPT         the task for every agent
#   OUT            run folder (default ./results/load-test-<UTC time>)
#
# Every agent runs with review-readonly.json as OPENCODE_CONFIG_CONTENT, the
# highest-precedence opencode config below managed settings: read, glob, grep
# and list only (no .env files), no sub-agents, none of the MCP servers it
# names, and no plugins (--pure). Each agent reads its repository and sends
# the files to the model, so point it only at code you may send there.
#
# Writes OUT/started.txt ("HH:MM:SS <pid> <name> free=<GB>GB") and
# OUT/logs/<name>.log, where <name> is <parent folder>_<repo folder>.
# load-test/stop.sh stops the agents of a run.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
: "${LOCAL_LLM_URL:?set LOCAL_LLM_URL to the gateway URL, e.g. https://<your-domain>.ngrok-free.dev}"
: "${REPOS:?set REPOS to the repository folders to review}"
MAX_AGENTS="${MAX_AGENTS:-8}"
MIN_FREE_GB="${MIN_FREE_GB:-8}"
GAP_S="${GAP_S:-3}"
PROMPT="${PROMPT:-Review every file in this repository, one file at a time, and list the bugs you find with their file and line. Do not stop until you have covered every file.}"
OUT="${OUT:-results/load-test-$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$OUT/logs"
: > "$OUT/started.txt"
export LOCAL_LLM_URL
export OPENCODE_CONFIG_CONTENT="$(cat "$HERE/review-readonly.json")"
echo "run dir: $OUT"

# Free memory in GB: Linux, Git Bash on Windows, or macOS. Empty if unknown.
free_gb() {
  if [ -r /proc/meminfo ]; then
    # MemAvailable on Linux; Git Bash on Windows only reports MemFree.
    awk '/^MemAvailable:/ { a = $2 } /^MemFree:/ { f = $2 } END { printf "%d", (a ? a : f) / 1048576 }' /proc/meminfo
  elif command -v vm_stat >/dev/null; then
    vm_stat | awk '/page size of/ { ps = $8 } /Pages (free|inactive)/ { n += $3 } END { printf "%d", n * ps / 1073741824 }'
  fi
}

count=0
for dir in $REPOS; do
  [ "$count" -ge "$MAX_AGENTS" ] && break
  [ -d "$dir" ] || { echo "skip $dir: not a folder"; continue; }
  name="$(basename "$(dirname "$dir")")_$(basename "$dir")"
  free="$(free_gb)"
  if [ -n "$free" ] && [ "$free" -lt "$MIN_FREE_GB" ]; then
    echo "$(date -u +%H:%M:%S) stop launching: ${free} GB free" | tee -a "$OUT/started.txt"
    break
  fi
  (cd "$dir" && exec opencode run --pure -m litellm/local-llm --title "agent-test $name" "$PROMPT") \
    > "$OUT/logs/$name.log" 2>&1 &
  echo "$(date -u +%H:%M:%S) $! $name free=${free:-?}GB" >> "$OUT/started.txt"
  count=$((count + 1))
  sleep "$GAP_S"
done
echo "$(date -u +%H:%M:%S) launched $count" | tee -a "$OUT/started.txt"
wait
echo "$(date -u +%H:%M:%S) all exited" | tee -a "$OUT/started.txt"
