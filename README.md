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

## MQTT Topics

# -------------------------
# RX (broker → PiHub)
# -------------------------
pihub/input_select/<room>_activity/state    # QoS 1, retained by HA statestream
pihub/<room>/cmd                             # QoS 1, non-retained
#   Payload examples (plain text):
#     macro:atv-on
#     macro:atv-off
#     sys:restart-pihub
#     sys:reboot
#     ble:unpair-all

# -------------------------
# TX (PiHub → broker)
# -------------------------
<prefix_bridge>/activity                     # QoS 1, non-retained
#   String payload of current activity intent (e.g. "watch"). Never retained.
#   On fresh session, PiHub force-clears any accidental retained payload.

<prefix_bridge>/ha/service/call              # QoS 1, non-retained
#   JSON payload (one-shot Home Assistant service request), e.g.:
#   {"domain":"media_player","service":"volume_up","data":{"entity_id":"speakers"}}
#   On fresh session, PiHub force-clears any accidental retained payload.

<prefix_bridge>/status                       # QoS 1, RETAINED  (availability)
#   "online" | "offline"
#   - LWT publishes "offline" retained if PiHub dies
#   - On connect, PiHub immediately publishes "online" retained
#   - On graceful shutdown, PiHub publishes "offline" retained

<prefix_bridge>/status/info                  # QoS 0, non-retained (stats)
#   JSON snapshot (no "state" wrapper), published periodically:
#   {
#     "host":"PiHub-LivingRoom",
#     "ip_addr":"192.168.70.231",
#     "uptime_s":123456,
#     "last_activity_cmd": null,
#     "last_ha_service": null,
#     "bt_connected_count":1,
#     "bt_connected_macs":"AA:BB:CC:DD:EE:FF",
#     "cpu_load_pct":3.4,
#     "cpu_temp_c":52.1,
#     "disk_used_pct":17.7,
#     "mem_used_pct":18.6,
#     "pi_undervolt":false,
#     "pi_undervolt_ever":false
#   }

# -------------------------
# Home Assistant Discovery
# -------------------------
homeassistant/...                            # QoS 1, RETAINED (entity configs)
# - All entities use availability_topic = <prefix_bridge>/status
# - Two display sensors read TX topics directly:
#     * Activity display      → <prefix_bridge>/activity         (string)
#     * HA service display    → <prefix_bridge>/ha/service/call  (domain.service from JSON)
# - Other diagnostic sensors (host/IP/uptime/CPU etc.) parse <prefix_bridge>/status/info

# -------------------------
# Notes
# -------------------------
# * <room> comes from room.yaml (e.g. living_room)
# * <prefix_bridge> comes from room.yaml (e.g. pihub/living_room)
# * TX is “fire-and-forget” while offline (no queue); commands are dropped if disconnected
#   to avoid stale floods on reconnect.
# * All command handling is strict one-way: no mirroring; RX and TX topics are discrete.

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
