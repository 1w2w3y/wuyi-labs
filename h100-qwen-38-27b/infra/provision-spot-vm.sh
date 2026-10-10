#!/usr/bin/env bash
# Provision a private Spot H100 VM for the self-hosted LLM stack.
#
# - No public IP on the VM. The NSG denies all inbound traffic from the
#   internet.
# - Outbound internet access (Docker, Hugging Face, ngrok, telemetry) goes
#   through a NAT gateway. New VNets no longer get implicit default outbound
#   access.
# - Spot priority with eviction policy Deallocate. Spot draws on the regional
#   low-priority vCPU quota, not the NCadsH100v5 family quota, which is often 0
#   in a new subscription. One NC40ads_H100_v5 needs 40 low-priority vCPUs.
# - Administration runs through `az vm run-command` (scripts/vm-run.sh), so no
#   SSH ingress is required.
#
# Reuse first: the script looks for an existing stack VM (tags
# project=llm-self-hosting role=llm-h100) in the subscription. If one exists,
# it reuses it (starting it if deallocated) and creates nothing. Only if none
# exists does it create a new resource group + VM named with the region, zone
# and today's date, e.g. rg-llm-h100-uksouth-z2-261005 / vm-llm-h100-uks-z2-261005
# for uksouth zone 2 on 2026-10-05. The VM name uses a short region code
# (region_short below). Pick the region and zone with infra/spot-regions.sh.
#
#   LOCATION=southeastasia ZONE=2 infra/provision-spot-vm.sh   # reuse, or create
#   FORCE_NEW=1 LOCATION=... ZONE=... infra/provision-spot-vm.sh
#   RG=... VM_NAME=... infra/provision-spot-vm.sh    # explicit names, no lookup
#
# Telemetry (optional): set IDENTITY_RG to the resource group holding the
# regional ingestion identities from infra/telemetry-identities.sh, and the
# one for LOCATION (id-llm-ingest-<region>) is attached to the VM.
#
# Safe to re-run: the network steps are idempotent, and the VM step is
# skipped if the VM already exists.
set -euo pipefail
export MSYS_NO_PATHCONV=1   # keep Git Bash on Windows from rewriting /subscriptions/... args

SUBSCRIPTION="${SUBSCRIPTION:-$(az account show --query id -o tsv | tr -d '[:space:]')}"   # default: the az CLI's current subscription
LOCATION="${LOCATION:-}"
ZONE="${ZONE:-}"
PREFIX="${PREFIX:-llm-h100}"
DATE_SUFFIX="${DATE_SUFFIX:-$(date +%y%m%d)}"   # yymmdd, e.g. 261009
FORCE_NEW="${FORCE_NEW:-0}"
VM_SIZE="${VM_SIZE:-Standard_NC40ads_H100_v5}"
IMAGE="${IMAGE:-Canonical:ubuntu-24_04-lts:server:latest}"
ADMIN_USER="${ADMIN_USER:-azureuser}"
OS_DISK_GB="${OS_DISK_GB:-128}"
DATA_DISK_GB="${DATA_DISK_GB:-512}"
VNET_CIDR="${VNET_CIDR:-10.60.0.0/16}"
SUBNET_CIDR="${SUBNET_CIDR:-10.60.1.0/24}"
IDENTITY_SUBSCRIPTION="${IDENTITY_SUBSCRIPTION:-$SUBSCRIPTION}"
IDENTITY_RG="${IDENTITY_RG:-}"
VNET="vnet-${PREFIX}"
SUBNET="snet-${PREFIX}"
NSG="nsg-${PREFIX}"
NAT="nat-${PREFIX}"
NAT_PIP="pip-nat-${PREFIX}"

# shellcheck source=../scripts/lib/find-vm.sh
. "$(dirname "$0")/../scripts/lib/find-vm.sh"
TAGS=(project="$STACK_TAG_PROJECT" role="$STACK_TAG_ROLE" created="$(date +%F)")

az() { command az "$@" --subscription "$SUBSCRIPTION"; }

REUSE=0
if [ -z "${RG:-}" ] && [ -z "${VM_NAME:-}" ] && [ "$FORCE_NEW" != 1 ]; then
  rc=0; find_stack_vm || rc=$?
  case "$rc" in
    0) REUSE=1; RG="$FOUND_RG"; VM_NAME="$FOUND_VM"; LOCATION="$FOUND_LOCATION" ;;
    1) ;;  # none exists: create today's below
    *) echo "==> Aborting: can't tell whether a stack VM exists" >&2; exit 1 ;;
  esac
fi
if [ "$REUSE" != 1 ] && { [ -z "$LOCATION" ] || [ -z "$ZONE" ]; }; then
  echo "usage: LOCATION=<region> ZONE=<1|2|3> $0   (pick them with infra/spot-regions.sh)" >&2
  exit 2
fi
# Short region code for the VM name (Azure CAF abbreviations); a region not
# listed here keeps its full name.
region_short() {
  case "$1" in
    uksouth) echo uks ;;            southcentralus) echo scus ;;
    westus3) echo wus3 ;;           westus2) echo wus2 ;;
    westus) echo wus ;;             eastus) echo eus ;;
    eastus2) echo eus2 ;;           centralus) echo cus ;;
    canadacentral) echo cnc ;;      northeurope) echo ne ;;
    westeurope) echo we ;;          swedencentral) echo sdc ;;
    germanywestcentral) echo gwc ;; spaincentral) echo spc ;;
    switzerlandnorth) echo szn ;;   uaenorth) echo uaen ;;
    southeastasia) echo sea ;;      japaneast) echo jpe ;;
    koreacentral) echo krc ;;       australiaeast) echo ae ;;
    centralindia) echo inc ;;       malaysiawest) echo myw ;;
    indonesiacentral) echo idc ;;
    *) echo "$1" ;;
  esac
}
RG="${RG:-rg-${PREFIX}-${LOCATION}-z${ZONE}-${DATE_SUFFIX}}"
VM_NAME="${VM_NAME:-vm-${PREFIX}-$(region_short "$LOCATION")-z${ZONE}-${DATE_SUFFIX}}"

# Regional OTLP ingestion identity from infra/telemetry-identities.sh. Each one
# has isolationScope=Regional (usable only in its own region), so the name
# follows LOCATION.
INGEST_IDENTITY_ID="${INGEST_IDENTITY_ID:-}"
if [ -z "$INGEST_IDENTITY_ID" ] && [ -n "$IDENTITY_RG" ]; then
  INGEST_IDENTITY_ID="/subscriptions/${IDENTITY_SUBSCRIPTION}/resourceGroups/${IDENTITY_RG}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-llm-ingest-${LOCATION}"
fi

if [ "$REUSE" = 1 ]; then
  echo "==> Reusing existing VM ${VM_NAME} in ${RG} (${LOCATION}); nothing new is created"
  power="$(az vm get-instance-view -g "$RG" -n "$VM_NAME" \
    --query "instanceView.statuses[?starts_with(code,'PowerState/')].code | [0]" -o tsv | tr -d '[:space:]')"
  echo "    power state: ${power#PowerState/}"
  if [ "$power" != "PowerState/running" ]; then
    echo "==> Starting ${VM_NAME} (Spot start can fail if the region has no capacity)"
    az vm start -g "$RG" -n "$VM_NAME" -o none
  fi
else
# No existing stack VM: create a new RG + VM named with today's date.

echo "==> Resource group ${RG} (${LOCATION})"
az group create -n "$RG" -l "$LOCATION" --tags "${TAGS[@]}" -o none

echo "==> NSG ${NSG} (default rules only: deny inbound from the internet)"
az network nsg create -g "$RG" -n "$NSG" -l "$LOCATION" --tags "${TAGS[@]}" -o none

echo "==> NAT gateway ${NAT} for outbound-only internet access"
az network public-ip create -g "$RG" -n "$NAT_PIP" -l "$LOCATION" \
  --sku Standard --allocation-method Static --zone "$ZONE" --tags "${TAGS[@]}" -o none
az network nat gateway create -g "$RG" -n "$NAT" -l "$LOCATION" \
  --public-ip-addresses "$NAT_PIP" --idle-timeout 10 --zone "$ZONE" \
  --tags "${TAGS[@]}" -o none

echo "==> VNet ${VNET} / subnet ${SUBNET}"
az network vnet create -g "$RG" -n "$VNET" -l "$LOCATION" \
  --address-prefixes "$VNET_CIDR" --tags "${TAGS[@]}" -o none
az network vnet subnet create -g "$RG" --vnet-name "$VNET" -n "$SUBNET" \
  --address-prefixes "$SUBNET_CIDR" \
  --network-security-group "$NSG" --nat-gateway "$NAT" \
  --default-outbound-access false -o none

IDENTITIES=('[system]')
if [ -n "$INGEST_IDENTITY_ID" ] && command az identity show --ids "$INGEST_IDENTITY_ID" -o none 2>/dev/null; then
  IDENTITIES+=("$INGEST_IDENTITY_ID")
else
  echo "==> WARNING: no ingestion identity for ${LOCATION} (set IDENTITY_RG; see infra/telemetry-identities.sh); telemetry export won't authenticate"
fi

if az vm show -g "$RG" -n "$VM_NAME" -o none 2>/dev/null; then
  echo "==> VM ${VM_NAME} already exists; skipping create"
else
  echo "==> Spot VM ${VM_NAME} (${VM_SIZE}, zone ${ZONE}), no public IP"
  # Trusted Launch with Secure Boot off: Secure Boot rejects unsigned
  # out-of-tree NVIDIA kernel modules. Security type "Standard" needs the
  # Microsoft.Compute/UseStandardSecurityType feature registered on the
  # subscription.
  az vm create -g "$RG" -n "$VM_NAME" -l "$LOCATION" --zone "$ZONE" \
    --size "$VM_SIZE" --image "$IMAGE" \
    --security-type TrustedLaunch --enable-secure-boot false --enable-vtpm true \
    --priority Spot --eviction-policy Deallocate --max-price -1 \
    --vnet-name "$VNET" --subnet "$SUBNET" \
    --public-ip-address "" --nsg "" \
    --admin-username "$ADMIN_USER" --generate-ssh-keys \
    --assign-identity "${IDENTITIES[@]}" \
    --os-disk-size-gb "$OS_DISK_GB" --storage-sku Premium_LRS \
    --data-disk-sizes-gb "$DATA_DISK_GB" --data-disk-caching ReadOnly \
    --tags "${TAGS[@]}" -o table
fi
fi  # REUSE

# No auto-shutdown schedule: a Spot VM runs until it is evicted or you stop
# it with scripts/vm-power.sh stop (deallocate, so the GPU stops billing).

echo "==> Remote check through run-command (no network path to the VM needed)"
az vm run-command invoke -g "$RG" -n "$VM_NAME" --command-id RunShellScript \
  --scripts 'hostname; uname -r; lsblk -o NAME,SIZE,TYPE,MOUNTPOINT; lspci | grep -i nvidia; curl -s -m 10 https://ifconfig.me; echo' \
  --query 'value[0].message' -o tsv

echo "==> Done. VM private IP:"
az vm list-ip-addresses -g "$RG" -n "$VM_NAME" \
  --query '[0].virtualMachine.network.{private:privateIpAddresses[0], public:publicIpAddresses[0].ipAddress}' -o table
