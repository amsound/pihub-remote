#!/usr/bin/env bash
set -euo pipefail

# --- Configurable constants ---
SERVICE_NAME="pihub"
APP_MODULE="pihub.app"             # python -m pihub.app
PYTHON_BIN="python3"
VENV_DIR=".venv"
REQ_FILE="requirements.txt"

# --- Paths ---
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
BT_OVERRIDE_DIR="/etc/systemd/system/bluetooth.service.d"
BT_OVERRIDE="${BT_OVERRIDE_DIR}/override.conf"
BT_MAIN="/etc/bluetooth/main.conf"
BT_BACKUP="/etc/bluetooth/main.conf.pihub.bak"

# --- Helpers ---
need_sudo() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "[installer] re-running with sudo…"
    exec sudo -E bash "$0" "$@"
  fi
}
line() { printf '%*s\n' "${COLUMNS:-80}" '' | tr ' ' -; }

# --- Require root ---
need_sudo "$@"

line
echo "[1/8] Python venv + requirements"
cd "$REPO_DIR"
if [[ ! -d "$VENV_DIR" ]]; then
  $PYTHON_BIN -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"
pip install --upgrade pip
pip install -r "$REQ_FILE"

line
echo "[2/8] Add current user to 'input' group (evdev access)"
RUNUSER="${SUDO_USER:-$(logname 2>/dev/null || echo pi)}"
if id -nG "$RUNUSER" | grep -qw input; then
  echo " - $RUNUSER already in input"
else
  usermod -aG input "$RUNUSER" || true
  echo " - Added $RUNUSER to input (reboot may be required)"
fi

line
echo "[3/8] bluetoothd --experimental (systemd drop-in)"
mkdir -p "$BT_OVERRIDE_DIR"
cat > "$BT_OVERRIDE" <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/libexec/bluetooth/bluetoothd --experimental
EOF
systemctl daemon-reload

line
echo "[4/8] BlueZ main.conf block (controller + LE timings)"
if [[ -f "$BT_MAIN" && ! -f "$BT_BACKUP" ]]; then
  cp -a "$BT_MAIN" "$BT_BACKUP"
fi

TAG_START="# --- PiHub managed begin ---"
TAG_END="# --- PiHub managed end ---"
BLOCK=$(cat <<'EOB'
# --- PiHub managed begin ---
ControllerMode = le
FastConnectable = true
Privacy = off
JustWorksRepairing = always

[LE]
MinConnectionInterval = 12
MaxConnectionInterval = 24
ConnectionLatency = 0
ConnectionSupervisionTimeout = 200
# --- PiHub managed end ---
EOB
)

if grep -qF "$TAG_START" "$BT_MAIN" 2>/dev/null; then
  awk -v start="$TAG_START" -v end="$TAG_END" -v repl="$BLOCK" '
    BEGIN{printed=0}
    { if($0==start){inblock=1; if(!printed){print repl; printed=1} next}
      if($0==end){inblock=0; next}
      if(!inblock) print $0
    }' "$BT_MAIN" > "${BT_MAIN}.tmp"
  mv "${BT_MAIN}.tmp" "$BT_MAIN"
else
  printf '\n%s\n' "$BLOCK" >> "$BT_MAIN"
fi

line
echo "[5/8] Optional: set hostname to match room (device_name)"
read -r -p "Set a new hostname now (leave blank to skip): " NEW_HOST
if [[ -n "${NEW_HOST// /}" ]]; then
  hostnamectl set-hostname "$NEW_HOST"
  if grep -qE "127\\.0\\.1\\.1" /etc/hosts; then
    sed -i "s/^127\\.0\\.1\\.1.*/127.0.1.1\\t${NEW_HOST}/" /etc/hosts || true
  else
    echo -e "127.0.1.1\\t${NEW_HOST}" >> /etc/hosts
  fi
  echo " - Hostname set to $NEW_HOST (reboot recommended)"
fi

line
echo "[6/8] Create systemd service: ${SERVICE_NAME}.service"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=PiHub Remote (BLE HID + MQTT)
After=bluetooth.service network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUNUSER}
WorkingDirectory=${REPO_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${REPO_DIR}/${VENV_DIR}/bin/python -m ${APP_MODULE}
Restart=on-failure
RestartSec=2
Environment=VIRTUAL_ENV=${REPO_DIR}/${VENV_DIR}
Environment=PATH=${REPO_DIR}/${VENV_DIR}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload

line
echo "[7/8] Restart bluetooth to apply config"
systemctl restart bluetooth || true

line
echo "[8/8] Enable + start service"
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

line
echo "Done."
echo "- Logs:    journalctl -u ${SERVICE_NAME} -f -o cat"
echo "- Health:  MQTT topic '<prefix>/health' (online/offline)"
echo "- Status:  MQTT topic '<prefix>/status/json'"
echo
echo "If hostname changed or you were added to 'input' group, reboot recommended."
