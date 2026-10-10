#!/usr/bin/env bash
# Sync this repo's working tree to /opt/llm-self-hosting on the VM through
# `az vm run-command` (the VM has no inbound path and needs no Git access),
# then optionally start the stack.
#
#   scripts/deploy.sh          # sync files only
#   scripts/deploy.sh up       # sync, then `docker compose up -d` in the background
#   COMPOSE_FILES="-f compose.yaml" scripts/deploy.sh up   # model + gateway only
#
# By default `up` starts the full stack: compose.yaml plus the telemetry
# (compose.observability.yaml) and ngrok (compose.ngrok.yaml) overlays.
#
# Tracked and untracked-but-not-ignored files are sent as the payload.
# Secrets: put them in this machine's .env (gitignored; same keys as
# .env.example). Only its non-empty KEY=value lines are sent, and
# scripts/merge-env.sh merges them into the VM's .env. Keys left empty locally
# keep their VM values. The secrets pass through the run-command script; the VM-side
# copy of that script is truncated after use.
#
# TELEMETRY_UAMI_CLIENT_ID is looked up from the ingestion identity attached to
# the VM (id-llm-ingest-<region>, attached by infra/provision-spot-vm.sh) and
# overrides any local value: the identities are region-locked, so only the
# attached one can authenticate.
set -euo pipefail
export MSYS_NO_PATHCONV=1
cd "$(dirname "$0")/.."

ACTION="${1:-sync}"
case "$ACTION" in sync|up) ;; *) echo "usage: deploy.sh [sync|up]" >&2; exit 2 ;; esac
REMOTE_DIR="/opt/llm-self-hosting"
COMPOSE_FILES="${COMPOSE_FILES:--f compose.yaml -f compose.observability.yaml -f compose.ngrok.yaml}"

PAYLOAD="$(git ls-files -z --cached --others --exclude-standard \
  | tar --null -T - -czf - | base64 -w0)"
echo "payload: $(( ${#PAYLOAD} / 1024 )) KiB (base64)"

# Resolve the VM once here; scripts/vm-run.sh uses RG + VM_NAME when both are set.
SUBSCRIPTION="${SUBSCRIPTION:-$(az account show --query id -o tsv | tr -d '[:space:]')}"   # default: the az CLI's current subscription
if [ -z "${RG:-}" ] || [ -z "${VM_NAME:-}" ]; then
  # shellcheck source=lib/find-vm.sh
  . scripts/lib/find-vm.sh
  rc=0; find_stack_vm || rc=$?
  if [ "$rc" = 1 ]; then echo "no stack VM found in subscription ${SUBSCRIPTION}; run infra/provision-spot-vm.sh" >&2; exit 1; fi
  [ "$rc" = 0 ] || exit 1
  RG="$FOUND_RG"; VM_NAME="$FOUND_VM"
fi
export SUBSCRIPTION RG VM_NAME

ENV_LINES=""
if [ -f .env ]; then
  ENV_LINES="$(tr -d '\r' < .env | grep -E '^[A-Za-z_][A-Za-z0-9_]*=.+' || true)"
fi

# The ingestion identity attached to the VM decides the collector's client ID.
INGEST_ID="$(az vm show --subscription "$SUBSCRIPTION" -g "$RG" -n "$VM_NAME" \
  --query "keys(identity.userAssignedIdentities || \`{}\`)[?contains(@, '/id-llm-ingest-')] | [0]" \
  -o tsv | tr -d '\r')"
if [ -n "$INGEST_ID" ]; then
  UAMI_CLIENT_ID="$(az identity show --ids "$INGEST_ID" --query clientId -o tsv | tr -d '\r')"
  echo "ingestion identity: ${INGEST_ID##*/} (client ID ${UAMI_CLIENT_ID})"
  ENV_LINES="$(printf '%s\n' "$ENV_LINES" | grep -v -e '^TELEMETRY_UAMI_CLIENT_ID=' -e '^$' || true)"
  ENV_LINES="${ENV_LINES:+$ENV_LINES$'\n'}TELEMETRY_UAMI_CLIENT_ID=${UAMI_CLIENT_ID}"
else
  echo "WARNING: no id-llm-ingest-* identity attached to ${VM_NAME}; telemetry export won't authenticate" >&2
fi

ENV_B64=""
if [ -n "$ENV_LINES" ]; then
  ENV_B64="$(printf '%s' "$ENV_LINES" | base64 -w0)"
  echo "env keys sent: $(printf '%s\n' "$ENV_LINES" | cut -d= -f1 | tr '\n' ' ')"
fi

REMOTE_SCRIPT="$(cat <<EOF
#!/bin/bash
set -euo pipefail
mkdir -p $REMOTE_DIR && cd $REMOTE_DIR
echo '$PAYLOAD' | base64 -d | tar -xzf - --no-same-owner
echo '$ENV_B64' | base64 -d | bash scripts/merge-env.sh
mkdir -p /data/models /data/vllm-cache
echo "synced to $REMOTE_DIR"
if [ "$ACTION" = "up" ]; then
  # Detach through systemd: image pulls and the model download can take far
  # longer than run-command allows. Progress: /var/log/llm-compose-up.log
  # On a fresh VM compose once declared the model unhealthy during its first
  # load and gave up, leaving the gateway and ngrok created but never started,
  # although the model went healthy minutes later. A second up waits for it
  # and starts them, so retry up to twice.
  systemctl reset-failed llm-compose-up 2>/dev/null || true
  systemd-run --unit=llm-compose-up --collect --working-directory=$REMOTE_DIR \
    bash -c "up() { docker compose $COMPOSE_FILES up -d >> /var/log/llm-compose-up.log 2>&1; }; \
      up || { echo '== compose up failed; retrying in 60 s' >> /var/log/llm-compose-up.log; sleep 60; up; } \
         || { echo '== compose up failed; retrying in 60 s' >> /var/log/llm-compose-up.log; sleep 60; up; }"
  echo "started llm-compose-up; follow with: scripts/vm-run.sh -c 'tail -n 30 /var/log/llm-compose-up.log'"
fi
# This script may carry secrets; don't leave it in the waagent download dir.
# (\$0 is escaped so it expands on the VM, not in this heredoc.)
truncate -s 0 "\$0" 2>/dev/null || true
EOF
)"

# Pass the script as a file: on Windows `az` is a .cmd wrapper limited to an
# ~8 KB command line.
TMP_SCRIPT="$(mktemp)"
trap 'rm -f "$TMP_SCRIPT"' EXIT
printf '%s\n' "$REMOTE_SCRIPT" > "$TMP_SCRIPT"
scripts/vm-run.sh "$TMP_SCRIPT"
