# PiHub Remote

A Raspberry Pi–based Universal remote that integrates with Home Assistant via MQTT and includes BLE control.  
It bridges a Logitech Unifying USB receiver with a button-only Harmony remote control using evdev. Dynamic button mappings allow BLE keyboard/consumed + HA mixed on one device.

---

## Quick Install (fresh Pi)

```bash
# 1) Get bootstrap and run it
cd /home/pi
wget https://raw.githubusercontent.com/amsound/pihub-remote/main/bootstrap.sh
chmod +x bootstrap.sh
./bootstrap.sh
```

What this does:
- Installs `git`, `python3`, `python3-venv`, `python3-pip`
- Clones (or updates) `~/pihub-remote`
- Runs `00-system-prep.sh` (turns **Bluetooth on**, disables **Wi-Fi** overlay if requested, applies BlueZ tuning)
- Runs `install.sh` (creates `.venv`, installs deps, creates `/home/pi/pihub-remote/pihub/config/room.yaml`, sets hostname `PiHub-<Room>`, enables `pihub.service`)

After install, the service starts automatically and on boot.

> Already have the repo cloned? You can still run `./bootstrap.sh`—it will just pull latest and continue.

## Configuration

Your per-Pi config lives at:
```
/home/pi/pihub-remote/pihub/config/room.yaml
```

Example:
```yaml
room: "living_room"

bt:
  enabled: true
  device_name: "PiHub-LivingRoom"

mqtt:
  host: "192.168.xx.xx"
  port: 1883
  prefix_bridge: "pihub/living_room"
  username: "remote"
  password: "remote"

pyatv:
  enabled: false
```

> The repo contains `pihub/config/room.example.yaml` and **ignores** your real `room.yaml`, so updates won’t overwrite your local settings.

## Service control

```bash
sudo systemctl status pihub
sudo systemctl restart pihub
sudo journalctl -u pihub -f
```

## MQTT Topics (contract)

PiHub publishes/subscribes on these topics:

- **Availability** (retained):  
  `pihub/<room>/health` → `online` / `offline`

- **Activity (from HA statestream)** (retained in HA):  
  `pihub/input_select/<room>_activity/state` → e.g. `power_off|watch|listen`

- **Activity intent (Pi → HA)** (non-retained):  
  `pihub/<room>/activity` → current activity string

- **Service call (HA → PiHub)** (non-retained, QoS 1):  
  `pihub/<room>/ha/service/call`  
  Payload:
  ```json
  {"domain":"media_player","service":"volume_up","data":{"entity_id":"speakers"}}
  ```

- **Commands (HA → PiHub)** (non-retained):  
  `pihub/<room>/cmd/#` → arbitrary namespaced commands

- **Status snapshot (Pi → HA)** (non-retained, ~every 120s):  
  `pihub/<room>/status/json` → `{ "cpu_temp_c": 44.5, "cpu_load_pct": 3.2, "mem_used_pct": 28.1, "disk_used_pct": 11.4, "ip": "…", "uptime_sec": 12345, "hostname": "PiHub-Kitchen", "bt_connected_devices": { "count": 1, "macs": ["AA:BB:…"] }, "undervoltage_now": false, "undervoltage_ever": false, "ts": 1694… }`

Home Assistant MQTT Discovery is published under `homeassistant/` (retained) so the Pi shows up as a device with diagnostic sensors automatically.

## Updating code

**Dev Pi:**
```bash
cd ~/pihub-remote
git add .
git commit -m "Message"
git push
```

**Client Pi(s):**
```bash
cd ~/pihub-remote
git fetch origin
git reset --hard origin/main
sudo systemctl restart pihub
```

Your `pihub/config/room.yaml` is untracked/ignored, so it won’t be touched.
