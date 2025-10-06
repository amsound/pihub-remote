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
_room=""; _btname=""; _host=""; _port=""; _user=""; _pass=""
if [ -f "$ROOM_YAML" ]; then
  echo "[install] Found existing $ROOM_YAML, will pre-fill from it."
  _room=$(sed -nE 's/^room:\s*"?([^"]+)"?/\1/p' "$ROOM_YAML" | head -n1 || true)
  _btname=$(sed -nE 's/^\s*device_name:\s*"?([^"]+)"?/\1/p' "$ROOM_YAML" | head -n1 || true)
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

# Derive CamelCase suffix and names (inline conversion)
bt_suffix=$(python3 -c "print(''.join(p.capitalize() for p in '${room}'.split('_') if p))")
if [ -z "$bt_suffix" ]; then
  echo "[install] ERROR: could not derive suffix from room '$room'" >&2
  exit 1
fi

bt_name="PiHub-$bt_suffix"
hostname="PiHub-$bt_suffix"
prefix_bridge="pihub/$room"

# MQTT (with clear defaults shown)
mqtt_host=$(prompt "MQTT Host, press enter for default" "${_host:-192.168.70.24}")
mqtt_port=$(prompt "MQTT Port, press enter for default" "${_port:-1883}")
mqtt_user=$(prompt "MQTT Username, press enter for default" "${_user:-remote}")
mqtt_pass=$(prompt "MQTT Password, press enter for default" "${_pass:-remote}")

# ---------- summary + confirmation ----------
echo
echo "==== PiHub Room Setup ===="
echo
echo "Room Summary:"
printf "  %-22s %s\n" "Room name:" "$room"
printf "  %-22s %s\n" "Bluetooth & Hostname:" "$hostname"
printf "  %-22s %s\n" "MQTT Host:" "$mqtt_host"
printf "  %-22s %s\n" "MQTT Port:" "$mqtt_port"
printf "  %-22s %s/%s\n" "MQTT Usr & pass:" "$mqtt_user" "$mqtt_pass"
printf "  %-22s %s\n" "MQTT Prefix Bridge:" "$prefix_bridge"
echo

read -rp "Proceed with these settings? [Y/n]: " yn || true
yn=${yn,,}
if [ -n "$yn" ] && [ "$yn" != "y" ] && [ "$yn" != "yes" ]; then
  echo "[install] Aborted."
  exit 1
fi


# ---------- write/update room.yaml ----------
if [ -f "$ROOM_YAML" ]; then
  echo "[install] Updating existing room.yaml fields…"
  tmp="$ROOM_YAML.tmp.$$"
  awk -v room="$room" -v btname="$bt_name" -v host="$mqtt_host" -v port="$mqtt_port" \
      -v user="$mqtt_user" -v pass="$mqtt_pass" -v pfx="$prefix_bridge" '
    BEGIN{ in_bt=0; in_mqtt=0 }
    {
      if ($0 ~ /^room:/) { print "room: \"" room "\""; next }
      if ($0 ~ /^bt:/)   { in_bt=1; in_mqtt=0; print; next }
      if ($0 ~ /^mqtt:/) { in_mqtt=1; in_bt=0; print; next }
      if (in_bt && $0 ~ /^\s*device_name:/)         { print "  device_name: \"" btname "\""; next }
      if (in_mqtt && $0 ~ /^\s*host:/)              { print "  host: \"" host "\""; next }
      if (in_mqtt && $0 ~ /^\s*port:/)              { print "  port: " port; next }
      if (in_mqtt && $0 ~ /^\s*prefix_bridge:/)     { print "  prefix_bridge: \"" pfx "\""; next }
      if (in_mqtt && $0 ~ /^\s*username:/)          { print "  username: \"" user "\""; next }
      if (in_mqtt && $0 ~ /^\s*password:/)          { print "  password: \"" pass "\""; next }
      print
    }' "$ROOM_YAML" >"$tmp"
  mv "$tmp" "$ROOM_YAML"
else
  echo "[install] Creating new room.yaml…"
  cat >"$ROOM_YAML" <<EOF
room: "$room"

bt:
  enabled: true
  device_name: "$bt_name"

mqtt:
  host: "$mqtt_host"
  port: $mqtt_port
  prefix_bridge: "$prefix_bridge"
  username: "$mqtt_user"
  password: "$mqtt_pass"

pyatv:
  enabled: false
EOF
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

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now pihub >>"$LOG_INSTALL" 2>&1 || true
echo "[install] pihub service enabled and started."

echo "[install] Setting hostname to $hostname…"

if grep -qE "^127\.0\.1\.1\s" /etc/hosts; then
  sudo sed -i "s/^127\.0\.1\.1\s*.*/127.0.1.1\t$hostname/" /etc/hosts
else
  echo "127.0.1.1 $hostname" | sudo tee -a /etc/hosts >/dev/null
fi

echo "$hostname" | sudo tee /etc/hostname >/dev/null

sudo hostnamectl set-hostname "$hostname" >/dev/null 2>&1 || true

# ---------- Cleanup ----------
BOOTSTRAP_FILE="/home/pi/bootstrap.sh"
if [ -f "$BOOTSTRAP_FILE" ]; then
  echo "[install] Cleaning up bootstrap file..."
  sudo rm -f "$BOOTSTRAP_FILE"
fi

echo
echo "============================================================"
echo "[install] Hostname set to '$hostname'."
echo "[install] System will reboot automatically in 5 seconds..."
echo "============================================================"
echo
sleep 5
sudo reboot
