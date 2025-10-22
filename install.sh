#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

sudo iw reg set GB >/dev/null 2>&1 || true
sudo rfkill unblock all >/dev/null 2>&1 || true

REPO_DIR="/home/pi/pihub-remote"
CONF_DIR="$REPO_DIR/pihub/config"
ROOM_YAML="$CONF_DIR/room.yaml"
LOG_INSTALL="$REPO_DIR/install.log"

# ---------- helpers ----------
to_title() {  # "living_room" -> "Living Room"
  python3 - <<'PY'
import sys
s=sys.stdin.read().strip().replace('_',' ')
print(s.title())
PY
}
is_snake() { [[ "$1" =~ ^[a-z0-9]+(_[a-z0-9]+)*$ ]]; }
prompt() {
  local msg="$1" def="${2-}" out
  if [ -n "$def" ]; then
    read -rp "$msg [$def]: " out || true
    echo "${out:-$def}"
  else
    read -rp "$msg: " out || true
    echo "$out"
  fi
}

# ---------- repo refresh + venv (quiet pip) ----------
echo "[install] Ensuring repo layout and venv…"
cd /home/pi
if [ -d "$REPO_DIR/.git" ]; then
  cd "$REPO_DIR" && git pull --ff-only >>"$LOG_INSTALL" 2>&1
else
  git clone https://github.com/amsound/pihub-remote.git >>"$LOG_INSTALL" 2>&1
  cd "$REPO_DIR"
fi

python3 -m venv .venv >>"$LOG_INSTALL" 2>&1 || true
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q --upgrade pip >>"$LOG_INSTALL" 2>&1
pip install -q -r requirements.txt >>"$LOG_INSTALL" 2>&1

mkdir -p "$CONF_DIR"

# ---------- load current values (if any) ----------
_room=""; _host=""; _port=""; _user=""; _pass=""
if [ -f "$ROOM_YAML" ]; then
  echo "[install] Found existing $ROOM_YAML, will pre-fill from it."
  _room=$(sed -nE 's/^room:\s*"?([^"]+)"?/\1/p' "$ROOM_YAML" | head -n1 || true)
  _host=$(sed -nE 's/^\s*host:\s*"?([^"]+)"?/\1/p' "$ROOM_YAML"   | head -n1 || true)
  _port=$(sed -nE 's/^\s*port:\s*([0-9]+)/\1/p' "$ROOM_YAML"     | head -n1 || true)
  _user=$(sed -nE 's/^\s*username:\s*"?([^"]+)"?/\1/p' "$ROOM_YAML" | head -n1 || true)
  _pass=$(sed -nE 's/^\s*password:\s*"?([^"]+)"?/\1/p' "$ROOM_YAML" | head -n1 || true)
fi

# ---------- prompts (single-room input; clear UI) ----------
echo
echo "==== PiHub Room Setup ===="
echo

# Room (snake_case)
while :; do
  room=$(prompt "Room Name in 'snake_case'" "${_room:-living_room}")
  if is_snake "$room"; then break; fi
  echo "  -> must be snake_case (letters/numbers + underscores)"
done

prefix_bridge="pihub/$room"

# MQTT (with clear defaults shown)
mqtt_host=$(prompt "MQTT Host, press enter for default" "${_host:-192.168.70.24}")
mqtt_port=$(prompt "MQTT Port, press enter for default" "${_port:-1883}")
mqtt_user=$(prompt "MQTT Username, press enter for default" "${_user:-remote}")
mqtt_pass=$(prompt "MQTT Password, press enter for default" "${_pass:-remote}")

# ---------- write/update room.yaml ----------
if [ -f "$ROOM_YAML" ]; then
  echo "[install] Updating existing room.yaml fields…"
  tmp="$ROOM_YAML.tmp.$$"
  awk -v room="$room" -v host="$mqtt_host" -v port="$mqtt_port" \
      -v user="$mqtt_user" -v pass="$mqtt_pass" -v pfx="$prefix_bridge" '
    BEGIN{ in_mqtt=0; in_bt=0; saw_bt=0 }
    {
      # room
      if ($0 ~ /^room:/) { print "room: \"" room "\""; next }

      # bt
      if ($0 ~ /^bt:/)   { in_bt=1; saw_bt=1; print "bt:"; next }
      if (in_bt && $0 ~ /^\s*enabled:/)     { print "  enabled: true"; next }
      if (in_bt && $0 ~ /^\s*device_name:/) { print "  device_name: \"PiHub Remote\""; next }
      if ($0 ~ /^[^[:space:]]/) { in_bt=0 }

      # mqtt
      if ($0 ~ /^mqtt:/) { in_mqtt=1; print; next }
      if (in_mqtt && $0 ~ /^\s*host:/)          { print "  host: \"" host "\""; next }
      if (in_mqtt && $0 ~ /^\s*port:/)          { print "  port: " port; next }
      if (in_mqtt && $0 ~ /^\s*prefix_bridge:/) { print "  prefix_bridge: \"" pfx "\""; next }
      if (in_mqtt && $0 ~ /^\s*username:/)      { print "  username: \"" user "\""; next }
      if (in_mqtt && $0 ~ /^\s*password:/)      { print "  password: \"" pass "\""; next }
      if ($0 ~ /^[^[:space:]]/) { in_mqtt=0 }

      print
    }
    END {
      if (!saw_bt) {
        print "bt:";
        print "  enabled: true";
        print "  device_name: \"PiHub Remote\"";
      }
    }' "$ROOM_YAML" >"$tmp"
  mv "$tmp" "$ROOM_YAML"
else
  echo "[install] Creating new room.yaml…"
  cat >"$ROOM_YAML" <<'YAML'
room: "$room"

bt:
  enabled: true
  device_name: "PiHub Remote"

mqtt:
  host: "$mqtt_host"
  port: $mqtt_port
  prefix_bridge: "$prefix_bridge"
  username: "$mqtt_user"
  password: "$mqtt_pass"
YAML
fi

echo "[install] room.yaml written at $ROOM_YAML"
echo


# ---------- systemd service ----------
echo "[install] Installing systemd service pihub…"
sudo tee /etc/systemd/system/pihub.service >/dev/null <<'EOF'
[Unit]
Description=PiHub Remote
Requires=bluetooth.service
Wants=network-online.target
After=bluetooth.service network-online.target
PartOf=bluetooth.service

[Service]
Type=simple
WorkingDirectory=/home/pi/pihub-remote
ExecStart=/home/pi/pihub-remote/.venv/bin/python -m pihub.app
Environment=PYTHONUNBUFFERED=1
User=pi
Group=pi
Restart=on-failure
RestartSec=2
TimeoutStopSec=20

CPUSchedulingPolicy=fifo
CPUSchedulingPriority=30
CPUAffinity=2
Nice=-5
IOSchedulingClass=best-effort
IOSchedulingPriority=0

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now pihub >>"$LOG_INSTALL" 2>&1 || true
echo "[install] pihub service enabled and started."

# ---------- Hostname ----------
echo "[install] Setting hostname to $hostname…"
# Derive hostname from 'room' (snake_case -> kebab-case) with '-pihub' suffix
hostname="$(printf "%s" "$room" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9_]+/_/g; s/_+/-/g')-pihub"
sudo raspi-config nonint do_hostname "$hostname"
echo "[install] Hostname set successfully to $hostname"

# ---------- Cleanup ----------
BOOTSTRAP_FILE="/home/pi/bootstrap.sh"
if [ -f "$BOOTSTRAP_FILE" ]; then
  echo "[install] Cleaning up bootstrap file..."
  sudo rm -f "$BOOTSTRAP_FILE"
  sync; sleep 0.2   # ensure message flushes before reboot
fi

echo
echo "============================================================"
echo "[install] Hostname set to '$hostname'."
echo "[install] System will reboot automatically in 5 seconds..."
echo "============================================================"
echo
sleep 5
sudo reboot
