#!/usr/bin/env bash
# Run a local script or an inline command on the VM as root via
# `az vm run-command invoke`. The VM has no inbound network path; this goes
# through the Azure control plane. Only the last ~4 KB of output comes back.
#
#   scripts/vm-run.sh scripts/bootstrap-host.sh       # run a local script file
#   scripts/vm-run.sh -c 'docker compose ps'          # run an inline command
#
# Target: the newest stack VM found by tag (see scripts/lib/find-vm.sh), or
# RG + VM_NAME if both are set.
set -euo pipefail
export MSYS_NO_PATHCONV=1

SUBSCRIPTION="${SUBSCRIPTION:-$(az account show --query id -o tsv | tr -d '[:space:]')}"   # default: the az CLI's current subscription
if [ -z "${RG:-}" ] || [ -z "${VM_NAME:-}" ]; then
  # shellcheck source=lib/find-vm.sh
  . "$(dirname "$0")/lib/find-vm.sh"
  rc=0; find_stack_vm || rc=$?
  if [ "$rc" = 1 ]; then echo "no stack VM found in subscription ${SUBSCRIPTION}; run infra/provision-spot-vm.sh" >&2; exit 1; fi
  [ "$rc" = 0 ] || exit 1
  RG="$FOUND_RG"; VM_NAME="$FOUND_VM"
fi
echo "[vm-run] ${VM_NAME} (${RG})" >&2

if [[ "${1:-}" == "-c" ]]; then
  shift
  SCRIPT="$*"
else
  FILE="${1:?usage: vm-run.sh <script-file> | -c '<command>'}"
  # Windows az.cmd can't resolve Git Bash paths such as /tmp/...
  command -v cygpath >/dev/null && FILE="$(cygpath -w "$FILE")"
  SCRIPT="@${FILE}"
fi

az vm run-command invoke --subscription "$SUBSCRIPTION" -g "$RG" -n "$VM_NAME" \
  --command-id RunShellScript --scripts "$SCRIPT" \
  --query 'value[0].message' -o tsv
