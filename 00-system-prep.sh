#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "[prep] Updating system packages…"
# --- Suppress rfkill kernel warnings ---
# set a valid regulatory domain so BlueZ doesn't whine
sudo iw reg set GB >/dev/null 2>&1 || true
# pre-unblock everything to prevent the "Wi-Fi is blocked" warning
sudo rfkill unblock all >/dev/null 2>&1 || true

sudo DEBIAN_FRONTEND=noninteractive \
     apt-get -y -qq \
     -o Dpkg::Options::="--force-confdef" \
     -o Dpkg::Options::="--force-confold" \
     full-upgrade >> /var/log/pihub-bootstrap.log 2>&1

echo "[prep] Disabling swap (dphys-swapfile)…"
sudo systemctl disable --now dphys-swapfile >> /var/log/pihub-bootstrap.log 2>&1 || true
sudo apt-get purge -y -qq dphys-swapfile >> /var/log/pihub-bootstrap.log 2>&1 || true

echo "[prep] Installing minimal deps…"
sudo apt-get install -y -qq git python3 python3-venv python3-pip \
  bluez rfkill jq mosquitto-clients ltunify >> /var/log/pihub-bootstrap.log 2>&1


# ----- Firmware config paths (Bookworm uses /boot/firmware) -----
if [ -f /boot/firmware/config.txt ]; then
  CFG="/boot/firmware/config.txt"
elif [ -f /boot/config.txt ]; then
  CFG="/boot/config.txt"
else
  echo "[prep][WARN] Could not find config.txt, skipping Wi-Fi disable"
  CFG=""
fi

if [ -n "$CFG" ]; then
  echo "[prep] Disabling Wi-Fi overlay, ensuring BT enabled in $CFG…"
  sudo sed -i '/^\s*dtoverlay=disable-wifi\s*$/d' "$CFG"
  grep -q '^dtoverlay=disable-wifi' "$CFG" || echo 'dtoverlay=disable-wifi' | sudo tee -a "$CFG" >/dev/null
  sudo sed -i '/^\s*dtoverlay=disable-bt\s*$/d' "$CFG"  # ensure BT not disabled
else
  echo "[prep][WARN] Skipped firmware overlay edits (no config.txt found)"
fi

echo "[prep] Disabling Wi-Fi user-space (wpa_supplicant)…"
sudo systemctl disable --now wpa_supplicant >/dev/null 2>&1 || true
sudo systemctl mask wpa_supplicant >/dev/null 2>&1 || true

echo "[prep] Ensuring Bluetooth unblocked…"
sudo rfkill unblock bluetooth >/dev/null 2>&1 || true

echo "[prep] BlueZ override for daemon flags…"
sudo mkdir -p /etc/systemd/system/bluetooth.service.d
sudo tee /etc/systemd/system/bluetooth.service.d/override.conf >/dev/null <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/libexec/bluetooth/bluetoothd -E
# Runtime priority for BT daemon (lower latency)
CPUSchedulingPolicy=rr
CPUSchedulingPriority=20
CPUAffinity=2
Nice=-5
IOSchedulingClass=best-effort
IOSchedulingPriority=0
EOF
sudo systemctl daemon-reload >/dev/null 2>&1 || true
sudo systemctl enable bluetooth >/dev/null 2>&1 || true

echo "[prep] Ensuring BlueZ config keys in /etc/bluetooth/main.conf…"
MC=/etc/bluetooth/main.conf
sudo touch "$MC"

ensure_section () {
  local sec="$1"; shift
  grep -q "^\[$sec\]" "$MC" || echo -e "\n[$sec]" | sudo tee -a "$MC" >/dev/null
}

set_kv () {
  local sec="$1" key="$2" val="$3"
  sudo awk -v sec="[$sec]" -v key="$key" -v val="$val" '
    BEGIN{FS=OFS="="}
    {print} END{
      # no-op, actual insertion below with sed
    }' "$MC" >/dev/null
  # sed in-place: within section, replace or append key
  sudo sed -i "/^\[$sec\]/,/^\[/{s/^\($key\s*=\s*\).*/\1"$(printf %s "$val")"/;t};\$a$key = $(printf %s "$val")" "$MC"
}

ensure_section General
ensure_section LE
# General
sudo sed -i "/^\[General\]/,/^\[/{/ControllerMode/d;/FastConnectable/d;/Privacy/d;/JustWorksRepairing/d}" "$MC"
sudo sed -i "/^\[General\]/a ControllerMode = le\nFastConnectable = true\nPrivacy = off\nJustWorksRepairing = always" "$MC"
# Tune LE advertising interval (fast discovery, tight window)
sudo sed -i "/^\[LE\]/,/^\[/{/MinAdvertisementInterval/d;/MaxAdvertisementInterval/d}" "$MC"
sudo sed -i "/^\[LE\]/a MinAdvertisementInterval = 100\nMaxAdvertisementInterval = 150" "$MC"

echo "[prep] Done. Reboot or restart BlueZ required"

echo "[prep] Verifying system state…"

# Swap should be disabled
if swapon --show | grep -q .; then
  echo "[prep][WARN] Swap still enabled:"
  swapon --show
else
  echo "[prep] Swap is disabled!"
fi
# Bluetooth service and daemon
echo "[prep] bluetooth.service status: $(systemctl is-active bluetooth || true)"

if command -v ltunify >/dev/null 2>&1; then
  ver=$(ltunify --version 2>&1 | grep -Eo '[0-9]+\.[0-9]+' | head -n1)
  echo "[prep] ltunify installed (v${ver:-unknown})"
else
  echo "[prep][WARN] ltunify not found (optional)"
fi
