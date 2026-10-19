#!/usr/bin/env bash
# agent-bench container entrypoint.
#
#   docker run ... agent-bench                 # fetch corpus, then run SCENARIO
#   docker run ... agent-bench run --agents 4  # same, with runner flags
#   docker run ... agent-bench fetch           # fetch the corpus and stop
#   docker run ... agent-bench preflight       # check the model and stop
#   docker run ... agent-bench list            # list scenarios
#   docker run ... agent-bench shell           # poke around inside
set -euo pipefail

: "${AGENT_BENCH_HOME:=/opt/agent-bench}"
: "${CORPUS_DIR:=/corpus}"
: "${RESULTS_DIR:=/results}"
cd "$AGENT_BENCH_HOME"

cmd="${1:-run}"
[ $# -gt 0 ] && shift || true

# Usage needs no gateway, so it works in a bare `docker run <image> --help`.
case "$cmd" in
  -h|--help|help)
    exec python3 runner/run.py --help
    ;;
esac

need_url() {
  if [ -z "${LLM_BASE_URL:-}" ]; then
    echo "agent-bench: set LLM_BASE_URL to the gateway URL including /v1" >&2
    echo "  e.g. -e LLM_BASE_URL=https://gateway.example.com/v1" >&2
    exit 2
  fi
  case "$LLM_BASE_URL" in
    */v1|*/v1/) ;;
    *) echo "agent-bench: LLM_BASE_URL should end in /v1 (got $LLM_BASE_URL)" >&2 ;;
  esac
  if [ -z "${LLM_MODEL:-}" ]; then
    echo "agent-bench: set LLM_MODEL to the model the gateway serves" >&2
    exit 2
  fi
}

case "$cmd" in
  run)
    need_url
    # Cheap when the corpus is already present at the pinned ref. Fetch only
    # what this scenario needs, unless CORPUS_REPOS names others: cloning all
    # three for a single-corpus run wastes minutes on a cold volume.
    if [ "${FETCH_CORPUS:-1}" = "1" ]; then
      repos="${CORPUS_REPOS:-}"
      if [ -z "$repos" ]; then
        for candidate in "${SCENARIO:-}" "scenarios/${SCENARIO:-}.yaml" \
                         "scenarios/${SCENARIO:-}"; do
          if [ -n "$candidate" ] && [ -f "$candidate" ]; then
            repos="$(sed -n 's/^repo:[[:space:]]*//p' "$candidate" | head -1)"
            break
          fi
        done
        # An absolute path is a mounted repo, so there is nothing to clone.
        case "$repos" in /*) repos="" ;; esac
      fi
      corpus/fetch.sh $repos
    fi
    exec python3 runner/run.py "$@"
    ;;
  fetch)
    exec corpus/fetch.sh "$@"
    ;;
  preflight)
    need_url
    exec python3 runner/preflight.py "$LLM_BASE_URL" "$LLM_MODEL" "${LLM_API_KEY:-}" \
      "${PREFLIGHT_MAX_TOKENS:-512}"
    ;;
  report)
    exec python3 runner/report.py "$@"
    ;;
  list)
    echo "scenarios:"
    for f in scenarios/*.yaml; do
      name="$(basename "$f" .yaml)"
      desc="$(sed -n 's/^description:[[:space:]]*//p' "$f" | head -1)"
      printf '  %-26s %s\n' "$name" "$desc"
    done
    ;;
  shell|bash|sh)
    exec bash "$@"
    ;;
  *)
    # Anything else: treat it as a runner flag set, e.g. `--dry-run`.
    if [ "${cmd#-}" != "$cmd" ]; then
      need_url
      exec python3 runner/run.py "$cmd" "$@"
    fi
    echo "agent-bench: unknown command '$cmd'" >&2
    echo "  try: run | fetch | preflight | report | list | shell" >&2
    exit 2
    ;;
esac
