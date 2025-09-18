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
- Runs `00-system-prep.sh` (turns **Bluetooth on**, disables **Wi-Fi** overlay, applies BlueZ tuning etc.)
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

PiHub exchanges MQTT messages with Home Assistant via the following topics.

---

#### RX (PiHub subscribes)

| Topic                                | Retained | QoS | Purpose                          |
|--------------------------------------|----------|-----|----------------------------------|
| `pihub/input_select/<room>_activity/state` | Yes      | 1   | Current activity from HA statestream |
| `pihub/<room>/cmd/#`                 | No       | 1   | Arbitrary inbound commands from HA |

---

#### TX (PiHub publishes)

| Topic                        | Retained | QoS | Purpose                          |
|------------------------------|----------|-----|----------------------------------|
| `pihub/<room>/activity`      | No       | 1   | Current activity intent (e.g. watch) |
| `pihub/<room>/ha/service/call` | No    | 1   | Request HA service calls (JSON payload) |
| `pihub/<room>/status/json`   | Yes      | 1   | Availability + status snapshot (online/offline + attrs) |

---

#### Status Snapshot Payload

Published on: `pihub/<room>/status/json`

```json
{
  "state": "online",
  "attr": {
    "ts": 1694123456,
    "host": "PiHub-LivingRoom",
    "ip_addr": "192.168.70.23",
    "uptime_s": 12345,
    "cpu_temp_c": 44.5,
    "cpu_load_pct": 3.2,
    "mem_used_pct": 28.1,
    "disk_used_pct": 11.4,
    "bt_connected_count": 1,
    "bt_connected_macs": ["AA:BB:CC:DD:EE:FF"],
    "pi_undervoltage": false,
    "pi_undervoltage_ever": false
  }
}
```

---

#### Home Assistant Discovery

- Published under `homeassistant/...` (retained, QoS 1).  
- Auto-creates a device `<Room Pretty> - PiHub` with diagnostic sensors (host, IP, uptime, BT, CPU, memory, disk, undervoltage).  

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
