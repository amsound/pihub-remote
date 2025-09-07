#!/usr/bin/env bash
set -euo pipefail

# ---- SETTINGS (edit if you don't run as 'pi') ----
APP_USER="${SUDO_USER:-pi}"
APP_GROUP="$APP_USER"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$APP_DIR/.venv"
SERVICE_NAME="pihub"
PYBIN="$VENV_DIR/bin/python"

# ---- sanity checks ----
if [[ "$(id -u)" -ne 0 ]]; then
  echo "Please run: sudo $0"
  exit 1
fi

echo "[install] user=$APP_USER app_dir=$APP_DIR"

# ---- OS packages (minimal) ----
echo "[install] installing apt deps…"
apt-get update -y
apt-get install -y --no-install-recommends \
  python3-venv python3-pip \
  bluetooth bluez \
  jq

# ---- venv + python deps ----
if [[ ! -d "$VENV_DIR" ]]; then
  echo "[install] creating venv…"
  python3 -m venv "$VENV_DIR"
fi
echo "[install] installing python requirements…"
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt"

# ---- add user to groups (evdev + bt) ----
echo "[install] ensuring $APP_USER is in 'input' and 'bluetooth' groups…"
usermod -aG input,bluetooth "$APP_USER"

# ---- config bootstrap ----
CFG_DIR="$APP_DIR/pihub/config"
mkdir -p "$CFG_DIR"

if [[ ! -f "$CFG_DIR/room.yaml" ]]; then
  echo "[install] creating pihub/config/room.yaml"
  cat > "$CFG_DIR/room.yaml" <<'YAML'
room: "living_room"
device_name: "PiHub-LivingRoom"

mqtt:
  host: "192.168.70.24"
  port: 1883
  prefix_bridge: "pihub/living_room"
  username: "remote"
  password: "remote"

entities:
  input_select_activity: "input_select.living_room_activity"
  speakers: "media_player.living_room_speakers_unified"
  radio_script: "script.living_room_universal_radio_tune"
  mute_script: "script.living_room_toggle_mute"
YAML
fi

# ---- systemd unit ----
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
echo "[install] writing systemd unit → $UNIT_PATH"
cat > "$UNIT_PATH" <<EOF
[Unit]
Description=PiHub Remote (BLE HID + MQTT)
Wants=network-online.target bluetooth.service
After=network-online.target bluetooth.service
StartLimitIntervalSec=0

[Service]
Type=simple
User=${APP_USER}
Group=${APP_GROUP}
WorkingDirectory=${APP_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${PYBIN} -m pihub.app
Restart=always
RestartSec=1
# Give it a short time to stop cleanly (publishes offline LWT etc.)
TimeoutStopSec=5
# Ensure the process sees the correct locale if needed:
Environment=LC_ALL=C.UTF-8
Environment=LANG=C.UTF-8

[Install]
WantedBy=multi-user.target
EOF

# ---- enable + start ----
echo "[install] reloading systemd & enabling service…"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"

echo "[install] starting ${SERVICE_NAME}…"
systemctl restart "${SERVICE_NAME}.service"

echo
echo "✅ Install complete."
echo "Next steps:"
echo "  1) Log out & back in (group changes), or reboot."
echo "  2) Tail logs:  sudo journalctl -u ${SERVICE_NAME} -f"
echo "  3) Edit config: ${CFG_DIR}/room.yaml"
echo
