#!/usr/bin/env bash
# Help pick a region and zone for a Spot H100 before provisioning.
#
#   infra/spot-regions.sh                            # eviction rate + price, all regions
#   infra/spot-regions.sh southeastasia uksouth ...  # also placement scores (max 8 regions)
#
# 1. Eviction rate and Linux Spot price per region, from the SpotResources
#    table in Azure Resource Graph. Query it at tenant scope: scoping the
#    query to a subscription returns no rows. az rest avoids needing the
#    resource-graph CLI extension.
# 2. Spot Placement Score per region and zone: how likely a request for one
#    VM is to succeed now (High / Medium / Low), plus zones where the SKU is
#    restricted for your subscription (RestrictedSkuNotAvailable) and whether
#    quota is available.
#
# A reasonable rule: keep regions in the lowest eviction bucket, prefer the
# lower price, then a zone that isn't restricted. "Low" placement is common
# for H100 Spot and still often allocates. The eviction rate is the chance of
# eviction within the next hour, from recent history: at 5% an hour, an
# eight-hour run has roughly a one-in-three chance of being evicted.
set -euo pipefail
export MSYS_NO_PATHCONV=1

SKU="${SKU:-Standard_NC40ads_H100_v5}"
SUBSCRIPTION="${SUBSCRIPTION:-$(az account show --query id -o tsv | tr -d '[:space:]')}"
ARG_URL="https://management.azure.com/providers/Microsoft.ResourceGraph/resources?api-version=2024-04-01"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
# Windows az.cmd can't read Git Bash paths such as /tmp/...
winpath() { if command -v cygpath >/dev/null; then cygpath -w "$1"; else echo "$1"; fi; }
sku_lc="$(echo "$SKU" | tr '[:upper:]' '[:lower:]')"

cat > "$TMP/evict.json" <<EOF
{"query":"SpotResources | where type =~ 'microsoft.compute/skuspotevictionrate/location' | where sku.name =~ '${sku_lc}' | project location, rate=tostring(properties.evictionRate) | order by rate asc, location asc"}
EOF
cat > "$TMP/price.json" <<EOF
{"query":"SpotResources | where type =~ 'microsoft.compute/skuspotpricehistory/ostype/location' | where sku.name =~ '${sku_lc}' and properties.osType =~ 'linux' | extend p=properties.spotPrices | mv-expand p | extend d=todatetime(p.effectiveDate), usd=toreal(p.priceUSD) | summarize arg_max(d, usd), minUsd=min(usd), maxUsd=max(usd) by location | project location, since=format_datetime(d, 'yyyy-MM'), usd=round(usd, 2), minUsd=round(minUsd, 2), maxUsd=round(maxUsd, 2)"}
EOF

echo "==> ${SKU}: eviction rate (%) and latest Linux Spot price (USD/hour, with the range seen)"
az rest --method post --url "$ARG_URL" --body @"$(winpath "$TMP/evict.json")" \
  --query "data[].[location,rate]" -o tsv | tr -d '\r' | sort > "$TMP/evict.tsv"
az rest --method post --url "$ARG_URL" --body @"$(winpath "$TMP/price.json")" \
  --query "data[].[location,since,usd,minUsd,maxUsd]" -o tsv | tr -d '\r' | sort > "$TMP/price.tsv"
printf '%-20s %-9s %-8s %-8s %s\n' region eviction usd/h since min-max
join -t $'\t' "$TMP/evict.tsv" "$TMP/price.tsv" \
  | sort -t $'\t' -k2,2n -k4,4n \
  | awk -F'\t' '{ printf "%-20s %-9s %-8s %-8s %s-%s\n", $1, $2, $4, $3, $5, $6 }'

[ "$#" -gt 0 ] || { echo "(pass up to 8 regions to also get placement scores)"; exit 0; }

echo "==> Spot Placement Score, 1 x ${SKU}"
regions="$(printf '"%s",' "$@")"
cat > "$TMP/score.json" <<EOF
{"desiredLocations":[${regions%,}],"desiredSizes":[{"sku":"${SKU}"}],"desiredCount":1,"availabilityZones":true}
EOF
# Any region works in the URL path; it's only the API endpoint.
az rest --method post \
  --url "https://management.azure.com/subscriptions/${SUBSCRIPTION}/providers/Microsoft.Compute/locations/${1}/placementScores/spot/generate?api-version=2025-06-05" \
  --body @"$(winpath "$TMP/score.json")" \
  --query "placementScores[].{region:region, zone:availabilityZone, score:score, quotaAvailable:isQuotaAvailable}" -o table
