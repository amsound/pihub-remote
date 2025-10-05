#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

LOG_BOOT=/var/log/pihub-bootstrap.log

echo "[bootstrap] Updating APT and installing git + Python…"
sudo apt-get update -qq >>"$LOG_BOOT" 2>&1
sudo apt-get install -y -qq git python3 python3-venv python3-pip >>"$LOG_BOOT" 2>&1

# Clone or pull repo
cd /home/pi
if [ -d pihub-remote/.git ]; then
  echo "[bootstrap] Repo exists, pulling latest…"
  cd pihub-remote && git pull --ff-only >>"$LOG_BOOT" 2>&1
else
  echo "[bootstrap] Cloning repo…"
  git clone https://github.com/amsound/pihub-remote.git >>"$LOG_BOOT" 2>&1
  cd pihub-remote
fi

chmod +x 00-system-prep.sh install.sh || true

echo "[bootstrap] Running system prep…"
bash 00-system-prep.sh

echo "[bootstrap] System prep done. Starting app installer…"
bash install.sh

echo "[bootstrap] Logs: $LOG_BOOT"