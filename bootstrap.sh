#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

LOG_BOOT=/var/log/pihub-bootstrap.log
TARGET_USER=${SUDO_USER:-pi}
REPO_DIR="/home/$TARGET_USER/pihub-remote"

as_user() {
  # Run a command as the non-root user (usually pi)
  sudo -u "$TARGET_USER" bash -lc "$*"
}

echo "[bootstrap] Starting PiHub setup…"
echo "[bootstrap] Logs: $LOG_BOOT"

# --- Persist Wi-Fi country so kernel/BlueZ don't nag ---
sudo raspi-config nonint do_wifi_country GB >/dev/null 2>&1 || true
sudo iw reg set GB >/dev/null 2>&1 || true
sudo rfkill unblock all >/dev/null 2>&1 || true

# ---------- 1. Install base packages (as root) ----------
echo "[bootstrap] Updating APT and installing git + Python…"
sudo apt-get update -qq >>"$LOG_BOOT" 2>&1
sudo apt-get install -y -qq git python3 python3-venv python3-pip >>"$LOG_BOOT" 2>&1

# ---------- 2. Clone or pull repo (as pi user) ----------
echo "[bootstrap] Preparing repo at $REPO_DIR…"
as_user "
  cd /home/$TARGET_USER
  if [ -d '$REPO_DIR/.git' ]; then
    echo '[bootstrap] Repo exists, pulling latest…'
    cd '$REPO_DIR' && git pull --ff-only
  else
    echo '[bootstrap] Cloning repo…'
    git clone https://github.com/amsound/pihub-remote.git '$REPO_DIR'
  fi
"

# ---------- 3. Fix ownership (in case root touched anything) ----------
sudo chown -R "$TARGET_USER:$TARGET_USER" "$REPO_DIR"

# ---------- 4. Run system prep (root) ----------
echo "[bootstrap] Running system prep…"
cd "$REPO_DIR"
bash 00-system-prep.sh || true
echo "[bootstrap] System prep complete."

# ---------- 5. Run install script (as pi) ----------
echo "[bootstrap] Running PiHub installer as $TARGET_USER…"
as_user "cd '$REPO_DIR' && bash install.sh"
