#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "[prep] Updating system packages…"
sudo DEBIAN_FRONTEND=noninteractive \
     apt-get -y \
     -o Dpkg::Options::="--force-confdef" \
     -o Dpkg::Options::="--force-confold" \
     full-upgrade

echo "[prep] Disabling swap (dphys-swapfile)…"
sudo systemctl disable --now dphys-swapfile 2>/dev/null || true
sudo apt-get purge -y dphys-swapfile 2>/dev/null || true
swapon --show || echo "[prep] Swap successfully disabled"

echo "[prep] Installing minimal deps…"
sudo apt install -y git python3 python3-venv python3-pip \
  bluez rfkill jq mosquitto-clients ltunify

# ----- Firmware config paths (Bookworm uses /boot/firmware) -----
CFG="/boot/firmware/config.txt"
[ -f /boot/config.txt ] && CFG="/boot/config.txt"

echo "[prep] Disabling Wi-Fi overlay, ensuring BT enabled in $CFG…"
sudo sed -i '/^\s*dtoverlay=disable-wifi\s*$/d' "$CFG"
grep -q '^dtoverlay=disable-wifi' "$CFG" || echo 'dtoverlay=disable-wifi' | sudo tee -a "$CFG" >/dev/null
sudo sed -i '/^\s*dtoverlay=disable-bt\s*$/d' "$CFG"  # ensure BT not disabled

echo "[prep] rfkill: unblocking BT, blocking Wi-Fi…"
sudo rfkill unblock bluetooth || true
sudo rfkill block wifi || true

echo "[prep] BlueZ override for daemon flags…"
sudo mkdir -p /etc/systemd/system/bluetooth.service.d
sudo tee /etc/systemd/system/bluetooth.service.d/override.conf >/dev/null <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/libexec/bluetooth/bluetoothd -E
EOF
sudo systemctl daemon-reload
sudo systemctl enable bluetooth
sudo systemctl restart bluetooth

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
sudo sed -i "/^\[LE\]/,/^\[/{/MinConnectionInterval/d;/MaxConnectionInterval/d;/ConnectionLatency/d;/ConnectionSupervisionTimeout/d}" "$MC"
sudo sed -i "/^\[General\]/a ControllerMode = le\nFastConnectable = true\nPrivacy = off\nJustWorksRepairing = always" "$MC"
sudo sed -i "/^\[LE\]/a MinConnectionInterval = 12\nMaxConnectionInterval = 24\nConnectionLatency = 0\nConnectionSupervisionTimeout = 200" "$MC"
# Tune LE advertising interval (fast discovery, tight window)
sudo sed -i "/^\[LE\]/,/^\[/{/MinAdvertisementInterval/d;/MaxAdvertisementInterval/d}" "$MC"
sudo sed -i "/^\[LE\]/a MinAdvertisementInterval = 100\nMaxAdvertisementInterval = 150" "$MC"

sudo systemctl restart bluetooth
echo "[prep] Done. Reboot recommended to fully apply Wi-Fi disable."

echo "[prep] Verifying system state…"

# Swap should be disabled
if swapon --show | grep -q .; then
  echo "[prep][WARN] Swap still enabled:"
  swapon --show
else
  echo "[prep] Swap is disabled ✔"
fi

# rfkill status
echo "[prep] rfkill status:"
rfkill list || true

# Bluetooth service and daemon
echo "[prep] bluetooth.service status: $(systemctl is-active bluetooth || true)"
echo "[prep] bluetoothd path: $(command -v bluetoothd || echo 'not found')"

# Versions/tools
echo "[prep] BlueZ/ctl versions:"
bluetoothctl -v || true
btmgmt -h >/dev/null 2>&1 && echo "[prep] btmgmt available" || echo "[prep] btmgmt not found"
ltunify --version || echo "[prep][WARN] ltunify not found (optional)"

# Show the [LE] section we just wrote (including Min/MaxAdvertisementInterval)
echo "[prep] /etc/bluetooth/main.conf [LE] section:"
awk '/^\[LE\]/{print; f=1; next} /^\[/{f=0} f{print}' /etc/bluetooth/main.conf || true

# Quick adapter summary (settings reflect after bluetooth restart)
echo "[prep] btmgmt info (adapter capabilities/settings):"
btmgmt info || true
