#!/bin/bash
# Runs on the VM from scripts/deploy.sh, in /opt/llm-self-hosting.
# Reads KEY=value overrides (non-empty values from the workstation's .env) on
# stdin, and merges them into the VM's .env:
#   - creates .env from .env.example if it doesn't exist
#   - replaces existing keys, appends new ones, leaves other keys untouched
set -euo pipefail
umask 077

[ -f .env ] || cp .env.example .env

overrides="$(mktemp)"
trap 'shred -u "$overrides" 2>/dev/null || true' EXIT
cat > "$overrides"

if [ -s "$overrides" ]; then
  awk -F= '
    NR == FNR { ov[$1] = $0; order[++n] = $1; next }
    ($1 in ov) { print ov[$1]; done[$1] = 1; next }
    { print }
    END { for (i = 1; i <= n; i++) if (!(order[i] in done)) print ov[order[i]] }
  ' "$overrides" .env > .env.new
  mv .env.new .env
  echo "merged from workstation .env: $(cut -d= -f1 "$overrides" | tr '\n' ' ')"
fi

chmod 600 .env
