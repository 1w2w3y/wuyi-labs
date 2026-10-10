#!/usr/bin/env bash
# Create one user-assigned managed identity per region for OTLP ingestion into
# Application Insights, grant each "Monitoring Metrics Publisher" on the
# Application Insights data collection rule (DCR), and restrict each identity
# to compute resources in its own region:
#   isolationScope = Regional             -> only same-region resources may use it
#   assignmentRestrictions.providers = [Microsoft.Compute]  -> only VMs/VMSS
# Neither property is in every az CLI version yet, so they're set through the
# REST API (isolationScope is GA in 2024-11-30; assignmentRestrictions needs
# 2025-05-31-preview). A cross-region attach fails at deployment with
# "does not have access to identity ... with isolation scope 'Regional'";
# ARM preflight validation doesn't catch it, so create one per region you use.
#
#   ID_RG=rg-llm-telemetry DCR_ID=/subscriptions/.../dataCollectionRules/... \
#     REGIONS="southeastasia uksouth" infra/telemetry-identities.sh
#
# Then provision with IDENTITY_RG=rg-llm-telemetry so infra/provision-spot-vm.sh
# attaches id-llm-ingest-<region>. Idempotent: existing identities and role
# assignments are reused or updated.
set -euo pipefail
export MSYS_NO_PATHCONV=1

ID_SUBSCRIPTION="${ID_SUBSCRIPTION:-$(az account show --query id -o tsv | tr -d '[:space:]')}"
ID_RG="${ID_RG:?set ID_RG to the resource group that holds the identities}"
DCR_ID="${DCR_ID:?set DCR_ID to the resource ID of the Application Insights data collection rule}"
REGIONS="${REGIONS:?set REGIONS to the regions the VM may run in, e.g. \"southeastasia uksouth\"}"
ID_PREFIX="${ID_PREFIX:-id-llm-ingest}"
IDENTITY_API="2025-05-31-preview"
ROLE="Monitoring Metrics Publisher"
TAGS=(project=llm-self-hosting purpose=otlp-ingestion)

for region in $REGIONS; do
  name="${ID_PREFIX}-${region}"
  echo "==> Identity ${name} (${region})"
  az identity create --subscription "$ID_SUBSCRIPTION" -g "$ID_RG" -n "$name" -l "$region" \
    --tags "${TAGS[@]}" region="$region" -o none
  id="$(az identity show --subscription "$ID_SUBSCRIPTION" -g "$ID_RG" -n "$name" --query id -o tsv | tr -d '[:space:]')"
  principal="$(az identity show --subscription "$ID_SUBSCRIPTION" -g "$ID_RG" -n "$name" --query principalId -o tsv | tr -d '[:space:]')"

  echo "    restrict: Regional isolation, Microsoft.Compute only"
  az rest --method patch --url "https://management.azure.com${id}?api-version=${IDENTITY_API}" \
    --body '{"properties":{"isolationScope":"Regional","assignmentRestrictions":{"providers":["Microsoft.Compute"]}}}' -o none

  echo "    role: ${ROLE} on the DCR"
  # Match on principalId: --assignee needs a Graph lookup, which fails for a
  # freshly created identity (replication lag) and for callers without Graph rights.
  if [ -z "$(az role assignment list --scope "$DCR_ID" \
      --query "[?principalId=='${principal}' && roleDefinitionName=='${ROLE}'].id | [0]" -o tsv)" ]; then
    # A new identity can take a few seconds to replicate; retry.
    for attempt in 1 2 3 4 5 6; do
      az role assignment create --scope "$DCR_ID" --role "$ROLE" \
        --assignee-object-id "$principal" --assignee-principal-type ServicePrincipal -o none && break
      echo "    retry ${attempt} (identity replication)"; sleep 15
    done
  fi
done

echo "==> Ingestion identities"
az identity list --subscription "$ID_SUBSCRIPTION" -g "$ID_RG" \
  --query "[?starts_with(name,'${ID_PREFIX}-')].{name:name, region:location, clientId:clientId}" -o table
