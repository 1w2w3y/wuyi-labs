#!/usr/bin/env bash
# Shared helper, sourced by infra/provision-spot-vm.sh and scripts/vm-run.sh.
#
# Every stack VM is tagged project=llm-self-hosting role=llm-h100, and its
# resource group and VM names carry the GPU count, region, zone and a
# creation-date suffix (yymmdd), e.g. rg-llm-2xh100-uksouth-z2-261011 /
# vm-llm-2xh100-uks-z2-261011 (the VM name with a short region code).
#
# find_stack_vm: sets FOUND_RG, FOUND_VM, FOUND_LOCATION for the most recently
# created tagged VM of size STACK_VM_SIZE in $SUBSCRIPTION (by timeCreated:
# two VMs made the same day in the same zone share a VM name). The size filter
# keeps the scripts off a one-GPU VM from another sample with the same tags.
#   returns 0 = found, 1 = none exists, 2 = the lookup itself failed
# Callers must not treat 2 as "none": that would create a duplicate VM.
# Warns when more than one is found.

STACK_TAG_PROJECT="llm-self-hosting"
STACK_TAG_ROLE="llm-h100"
STACK_VM_SIZE="${STACK_VM_SIZE:-Standard_NC80adis_H100_v5}"

find_stack_vm() {
  local raw rows count
  if ! raw="$(command az vm list --subscription "$SUBSCRIPTION" \
      --query "[?tags.project=='${STACK_TAG_PROJECT}' && tags.role=='${STACK_TAG_ROLE}' && hardwareProfile.vmSize=='${STACK_VM_SIZE}'].[timeCreated, name, resourceGroup, location]" \
      -o tsv)"; then
    echo "==> ERROR: listing VMs in subscription ${SUBSCRIPTION} failed (az login?)" >&2
    return 2
  fi
  rows="$(printf '%s' "$raw" | tr -d '\r' | sed '/^$/d' | sort -r)"
  [ -n "$rows" ] || return 1
  count="$(printf '%s\n' "$rows" | wc -l | tr -d ' ')"
  if [ "$count" -gt 1 ]; then
    echo "==> NOTE: ${count} stack VMs of size ${STACK_VM_SIZE} found; using the newest. Others:" >&2
    printf '%s\n' "$rows" | tail -n +2 | sed 's/^/      /' >&2
  fi
  read -r _ FOUND_VM FOUND_RG FOUND_LOCATION <<<"$(printf '%s\n' "$rows" | head -n 1)"
  # az vm list may return the RG name upper-cased; names are case-insensitive.
  FOUND_RG="$(printf '%s' "$FOUND_RG" | tr '[:upper:]' '[:lower:]')"
}
