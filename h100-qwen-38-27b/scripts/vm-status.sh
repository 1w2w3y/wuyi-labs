#!/bin/bash
# Compact stack status, run on the VM:  scripts/vm-run.sh scripts/vm-status.sh
cd /opt/llm-self-hosting 2>/dev/null || exit 0
echo "compose-up unit: $(systemctl is-active llm-compose-up 2>/dev/null)"
echo "models on disk: $(du -sh /data/models 2>/dev/null | cut -f1)"
docker ps -a --format '{{.Names}}: {{.Status}}'
echo "--- compose-up log"
tail -n 3 /var/log/llm-compose-up.log 2>/dev/null | cut -c1-200
echo "--- qwen3.8-27b log"
docker logs --tail 5 qwen3.8-27b 2>&1 | cut -c1-240
nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu --format=csv,noheader
