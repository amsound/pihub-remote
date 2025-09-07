# PiHub Remote

A Raspberry Pi–based Bluetooth HID remote that integrates with Home Assistant via MQTT.  
It bridges a USB IR/RF receiver (evdev) → BLE HID (keyboard + consumer control) and HA automations.

---

## 🔧 Installation

```bash
git clone https://github.com/amsound/pihub-remote.git
cd pihub-remote
chmod +x install.sh
./install.sh
```

The installer will:

- Create a Python virtualenv and install deps (`requirements.txt`)
- Configure **bluetoothd** with `--experimental`
- Apply a **BlueZ** config block (LE + fast connect, etc.)
- Create and enable a **systemd** service: `pihub.service`
- (Optional) Set your **hostname** interactively

> Reboot after install if you changed hostname or BlueZ config.

---

## ⚙️ Configuration

Edit just one file per room:

`pihub/config/room.yaml`:
```yaml
room: "living_room"
device_name: "PiHub-LivingRoom"   # Bluetooth advertised name (and suggested hostname)
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
```

`activities.yaml`, `keymap.yaml` and `macros/` are **static** and don’t need per-room edits.  
Entity names inside activities are substituted from `room.yaml`.

---

## 📡 MQTT Topics

- **Health (retained):** `pihub/<room>/health` → `online` / `offline`  
- **Status JSON:** `pihub/<room>/status/json` → CPU/Mem/Disk/IP/Uptime/Temp/Undervoltage/BT devices  
- **Activity state (HA → PiHub):**  
  `pihub/<room>/input_select/<input_select_activity>/state`  
- **Service calls (PiHub → HA):**  
  `pihub/<room>/ha/service/call` (payload: `{"domain":"...","service":"...","data":{...}}`)  
- **Commands/macros (HA → PiHub):**  
  `pihub/<room>/cmd/<namespace>/...` (e.g. `.../cmd/atv/on`)

---

## 🔌 Bluetooth (BlueZ) Settings

The installer adds:
- **bluetoothd** `--experimental` drop-in
- Main BlueZ options:
  ```
  ControllerMode = le
  FastConnectable = true
  Privacy = off
  JustWorksRepairing = always
  ```
- LE timing in the `[LE]` section:
  ```
  MinConnectionInterval = 12
  MaxConnectionInterval = 24
  ConnectionLatency = 0
  ConnectionSupervisionTimeout = 200
  ```

---

## ▶️ Service

- Start/stop:
  ```bash
  sudo systemctl start pihub
  sudo systemctl stop pihub
  ```
- Logs (follow):
  ```bash
  journalctl -u pihub -f -o cat
  ```

---

## ✅ Summary

- Install once → edit `room.yaml` → reboot → pair from TV/box  
- BLE HID + Home Assistant MQTT bridge  
- Clean, room-only config surface
