#!/usr/bin/env bash
set -euo pipefail
cd /home/pi/pihub-remote

echo "[update] pulling latest…"
git fetch --all
git reset --hard origin/main

echo "[update] syncing deps…"
source .venv/bin/activate
pip install --upgrade pip wheel
pip install -r requirements.txt
deactivate

# Pick whichever unit name this Pi uses
if systemctl list-unit-files | grep -q '^pihub.service'; then
  svc=pihub
elif systemctl list-unit-files | grep -q '^pihub-remote.service'; then
  svc=pihub-remote
else
  echo "[update] WARNING: service not found; skipping restart"
  exit 0
fi

echo "[update] restarting $svc…"
sudo systemctl restart "$svc"
systemctl status "$svc" --no-pager -n 10
