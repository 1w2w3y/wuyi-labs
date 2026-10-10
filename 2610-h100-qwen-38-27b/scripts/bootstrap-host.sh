#!/usr/bin/env bash
# One-time host bootstrap for the H100 VM. Runs as root on the VM, normally
# via `az vm run-command invoke` (see scripts/vm-run.sh). Idempotent.
#
# - Formats and mounts the 512 GB data disk (Azure LUN 0) at /data
# - Installs the NVIDIA datacenter driver (open kernel modules)
# - Installs Docker Engine + Compose, with data-root on /data/docker
# - Installs the NVIDIA Container Toolkit and registers it with Docker
set -euo pipefail
exec > >(tee -a /var/log/llm-bootstrap.log) 2>&1
export DEBIAN_FRONTEND=noninteractive

NVIDIA_DRIVER_BRANCH="${NVIDIA_DRIVER_BRANCH:-595}"
DATA_DEV="/dev/disk/azure/scsi1/lun0"
DATA_MNT="/data"

echo "==> $(date -Is) bootstrap start"

echo "==> Data disk"
if ! mountpoint -q "$DATA_MNT"; then
  DEV="$(readlink -f "$DATA_DEV")"
  if ! blkid "$DEV" >/dev/null 2>&1; then
    mkfs.ext4 -q -L llmdata "$DEV"
  fi
  mkdir -p "$DATA_MNT"
  UUID="$(blkid -s UUID -o value "$DEV")"
  grep -q "$UUID" /etc/fstab || echo "UUID=$UUID $DATA_MNT ext4 defaults,nofail,discard 0 2" >> /etc/fstab
  mount "$DATA_MNT"
fi
mkdir -p "$DATA_MNT"/{docker,hf-cache,vllm-cache}
df -h "$DATA_MNT" | tail -1

# A fresh VM runs unattended-upgrades on first boot; wait for its dpkg lock
# instead of failing.
echo 'DPkg::Lock::Timeout "1200";' > /etc/apt/apt.conf.d/99lock-timeout

echo "==> Base packages"
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg jq >/dev/null

echo "==> NVIDIA driver ${NVIDIA_DRIVER_BRANCH}-server-open"
if ! command -v nvidia-smi >/dev/null || ! nvidia-smi >/dev/null 2>&1; then
  apt-get install -y -qq "nvidia-driver-${NVIDIA_DRIVER_BRANCH}-server-open" \
    "nvidia-utils-${NVIDIA_DRIVER_BRANCH}-server" >/dev/null
  modprobe nvidia || echo "modprobe failed; a reboot is required"
fi
systemctl enable --now nvidia-persistenced 2>/dev/null || true

echo "==> Docker Engine"
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
fi

echo "==> NVIDIA Container Toolkit"
if ! command -v nvidia-ctk >/dev/null; then
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#' \
    > /etc/apt/sources.list.d/nvidia-container-toolkit.list
  apt-get update -qq
  apt-get install -y -qq nvidia-container-toolkit >/dev/null
fi

echo "==> Docker daemon config"
mkdir -p /etc/docker
cat > /etc/docker/daemon.json.new <<'EOF'
{
  "data-root": "/data/docker",
  "log-driver": "json-file",
  "log-opts": { "max-size": "50m", "max-file": "5" }
}
EOF
# Add the nvidia runtime to the candidate file before comparing, so a re-run
# sees no difference and doesn't restart Docker (and its containers).
nvidia-ctk runtime configure --runtime=docker --config=/etc/docker/daemon.json.new >/dev/null 2>&1
if ! cmp -s /etc/docker/daemon.json.new /etc/docker/daemon.json; then
  mv /etc/docker/daemon.json.new /etc/docker/daemon.json
  systemctl restart docker
else
  rm /etc/docker/daemon.json.new
  echo "daemon.json unchanged; Docker not restarted"
fi
usermod -aG docker azureuser 2>/dev/null || true

echo "==> Verify"
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader || true
docker --version
docker compose version
docker info --format '{{.DockerRootDir}} runtimes={{range $k,$v := .Runtimes}}{{$k}} {{end}}'
echo "==> $(date -Is) bootstrap done"
