#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/home/pi/pihub-remote"
CONF_DIR="$REPO_DIR/pihub/config"
ROOM_YAML="$CONF_DIR/room.yaml"

# Helpers
to_title() {  # "living_room" -> "Living Room"
  python3 - <<'PY'
import sys
s=sys.stdin.read().strip().replace('_',' ')
print(s.title())
PY
}
to_camel_suffix() {  # ensure one-word CamelCase
  python3 - <<'PY'
import sys,re
s=sys.stdin.read().strip()
# Accept already CamelCase; otherwise make TitleCase and strip spaces/underscores
parts=re.split(r'[_\s]+', s)
print(''.join(p[:1].upper()+p[1:] for p in parts if p))
PY
}

prompt() {
  local msg="$1" def="${2-}"
  if [ -n "$def" ]; then
    read -rp "$msg [$def]: " val || true
    echo "${val:-$def}"
  else
    read -rp "$msg: " val || true
    echo "$val"
  fi
}

echo "[install] Ensuring repo layout and venv…"
cd /home/pi
if [ -d "$REPO_DIR/.git" ]; then
  cd "$REPO_DIR" && git pull --ff-only
else
  git clone https://github.com/amsound/pihub-remote.git
  cd "$REPO_DIR"
fi

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

mkdir -p "$CONF_DIR"

# Load existing values if present
if [ -f "$ROOM_YAML" ]; then
  echo "[install] Found existing $ROOM_YAML, will update selected fields."
  _room=$(grep -E '^room:' "$ROOM_YAML" | sed -E 's/room:\s*"?(.*?)"?\s*$/\1/')
  _btname=$(grep -E '^\s*device_name:' -n "$ROOM_YAML" | sed -E 's/.*device_name:\s*"?(.*?)"?\s*$/\1/')
  _host=$(grep -E '^\s*host:' "$ROOM_YAML" | sed -E 's/.*host:\s*"?(.*?)"?\s*$/\1/')
  _port=$(grep -E '^\s*port:' "$ROOM_YAML" | sed -E 's/.*port:\s*([0-9]+)\s*$/\1/')
  _user=$(grep -E '^\s*username:' "$ROOM_YAML" | sed -E 's/.*username:\s*"?(.*?)"?\s*$/\1/')
  _pass=$(grep -E '^\s*password:' "$ROOM_YAML" | sed -E 's/.*password:\s*"?(.*?)"?\s*$/\1/')
else
  _room=""
  _btname=""
  _host=""
  _port=""
  _user=""
  _pass=""
fi

# ---- Prompts ----
room=$(prompt "Room (snake_case, e.g. living_room)" "${_room:-}")
while [ -z "$room" ]; do room=$(prompt "Room (snake_case, e.g. living_room)"); done

room_title=$(printf "%s" "$room" | to_title)
suffix_in=$(prompt "Bluetooth device suffix (CamelCase, e.g. LivingRoom)")
bt_suffix=$(printf "%s" "$suffix_in" | to_camel_suffix)
bt_name="PiHub-$bt_suffix"

mqtt_host=$(prompt "MQTT host" "${_host:-192.168.70.24}")
mqtt_port=$(prompt "MQTT port" "${_port:-1883}")
mqtt_user=$(prompt "MQTT username" "${_user:-remote}")
mqtt_pass=$(prompt "MQTT password" "${_pass:-remote}")

prefix_bridge="pihub/$room"
hostname="PiHub-$bt_suffix"

echo
echo "[install] Summary:"
echo "  room:           $room"
echo "  bt.device_name: $bt_name"
echo "  mqtt.host:      $mqtt_host"
echo "  mqtt.port:      $mqtt_port"
echo "  mqtt.username:  $mqtt_user"
echo "  mqtt.password:  (hidden)"
echo "  prefix_bridge:  $prefix_bridge"
echo "  hostname:       $hostname"
echo

# ---- Write or update room.yaml (respect layout) ----
if [ -f "$ROOM_YAML" ]; then
  echo "[install] Updating existing room.yaml fields…"
  tmp="$ROOM_YAML.tmp.$$"
  awk -v room="$room" \
      -v btname="$bt_name" \
      -v host="$mqtt_host" \
      -v port="$mqtt_port" \
      -v user="$mqtt_user" \
      -v pass="$mqtt_pass" \
      -v pfx="$prefix_bridge" '
    BEGIN{ in_bt=0; in_mqtt=0 }
    {
      if ($0 ~ /^room:/) {
        print "room: \"" room "\""
        next
      }
      if ($0 ~ /^bt:/) { in_bt=1; in_mqtt=0; print; next }
      if ($0 ~ /^mqtt:/) { in_mqtt=1; in_bt=0; print; next }
      if (in_bt && $0 ~ /^\s*device_name:/) { print "  device_name: \"" btname "\""; next }
      if (in_mqtt && $0 ~ /^\s*host:/) { print "  host: \"" host "\""; next }
      if (in_mqtt && $0 ~ /^\s*port:/) { print "  port: " port; next }
      if (in_mqtt && $0 ~ /^\s*prefix_bridge:/) { print "  prefix_bridge: \"" pfx "\""; next }
      if (in_mqtt && $0 ~ /^\s*username:/) { print "  username: \"" user "\""; next }
      if (in_mqtt && $0 ~ /^\s*password:/) { print "  password: \"" pass "\""; next }
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

# ---- Systemd service ----
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
StartLimitIntervalSec=30
StartLimitBurst=10

TimeoutStopSec=20
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now pihub

# ---- Offer to set hostname and reboot (default YES) ----
read -rp "Set hostname to \"$hostname\" and reboot now? [Y/n]: " yn || true
yn=${yn,,}
if [ -z "$yn" ] || [ "$yn" = "y" ] || [ "$yn" = "yes" ]; then
  echo "[install] Setting hostname to $hostname…"
  sudo hostnamectl set-hostname "$hostname"
  # ensure /etc/hosts has 127.1 mapping
  if ! grep -qE "127\.0\.1\.1\s+$hostname" /etc/hosts; then
    echo "127.0.1.1 $hostname" | sudo tee -a /etc/hosts >/dev/null
  fi
  echo "[install] Rebooting…"
  sudo reboot
else
  echo "[install] Skipping reboot. Please reboot manually to apply firmware Wi-Fi disable if you ran system prep."
fi
