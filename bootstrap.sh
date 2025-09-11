#!/usr/bin/env bash
set -euo pipefail

echo "[bootstrap] Updating APT and installing git + Python…"
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip

# Clone or pull repo
cd /home/pi
if [ -d pihub-remote/.git ]; then
  echo "[bootstrap] Repo exists, pulling latest…"
  cd pihub-remote && git pull --ff-only
else
  echo "[bootstrap] Cloning repo…"
  git clone https://github.com/amsound/pihub-remote.git
  cd pihub-remote
fi

# Make scripts executable (you said you keep them in repo root)
chmod +x 00-system-prep.sh install.sh || true

echo "[bootstrap] Running system prep (Bluetooth on, Wi-Fi off, BlueZ tuned)…"
bash 00-system-prep.sh

echo "[bootstrap] System prep done. A reboot is recommended, but the installer will also offer one."
echo "[bootstrap] Starting app installer…"
bash install.sh
