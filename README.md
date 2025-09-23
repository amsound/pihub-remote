# PiHub Remote

A Raspberry Pi–based Universal remote that integrates with Home Assistant via MQTT and includes BLE control.  
It bridges a Logitech Unifying USB receiver with a button-only Harmony 'smart' remote control using evdev. Dynamic button mappings allow BLE keyboard/consumer + Home Assistant mixed on one device.

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

The per-Pi config lives at:
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

> The repo contains `pihub/config/room.example.yaml` and **ignores** the real `room.yaml`, so updates won’t overwrite your local settings.

## Service control

```bash
sudo systemctl status pihub
sudo systemctl restart pihub
sudo journalctl -u pihub -f
```


## MQTT Topics
### Pi → HA (TX)

* **Activity intent**

  * Topic: `pihub/<room>/activity`
  * QoS: `1`
  * Retain: `false`
  * Payload: `"watch" | "listen" | "power_off"`
  
* **Home Assistant service call**

  * Topic: `pihub/<room>/ha_service`
  * QoS: `1`
  * Retain: `false`
  * Payload (JSON):

    ```json
    {
      "domain": "media_player",
      "service": "volume_up",
      "data": { "entity_id": "speakers" }
    }
    ```

* **Status (LWT)**

  * Topic: `pihub/<room>/status`
  * QoS: `1`
  * Retain: `true`
  * Payload: `"online" | "offline"`

* **HW Info**

  * Topic: `pihub/<room>/status/info`
  * QoS: `0`
  * Retain: `false`
  * Payload (JSON): runtime stats such as uptime, CPU, memory, BLE state.

---

### HA → Pi (RX)

* **Command execution**

  * Topic: `pihub/<room>/cmd`
  * QoS: `1`
  * Retain: `false`
  * Payload format: `<category>:<action>`
    * Currently working:
      * `macro:atv-on`
      * `macro:atv-off`
      * `sys:restart-pihub`
      * `sys:reboot`
      * `ble:unpair-all`


* **Activity state (HA → Pi)**

  * Topic: `pihub/input_select/<room>_activity/state`
  * QoS: `1`
  * Retain: `false`
  * Payload: `string` (selected activity, e.g. `watch`)

---

### Internals / Buffering Policy

* Activity intent:

  * Only the **latest** is buffered while offline (older dropped).
* HA service calls:

  * Stored in a bounded queue.
  * Each entry expires after TTL (default \~10s) if not delivered.

---

### Summary

* **Pi → HA**: `activity`, `ha_service`, `status`, `info`
* **HA → Pi**: `cmd`, `state`
* Payloads are deliberately simple: strings for activity/commands, JSON for HA service calls.



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
