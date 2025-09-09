# pihub/core/config.py
from dataclasses import dataclass

@dataclass
class RoomConfig:
    room: str | None
    device_name: str
    mqtt_host: str
    mqtt_port: int
    mqtt_user: str | None
    mqtt_password: str | None
    prefix_bridge: str
    # --- pyatv ---
    pyatv_enabled: bool = False
    pyatv_address: str | None = None
    pyatv_companion: str | None = None
    pyatv_airplay: str | None = None
    # --- bluetooth / HID ---
    bt_enabled: bool = True
    bt_device_name: str | None = None

def load_room_config(path: str) -> RoomConfig:
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        y = yaml.safe_load(f) or {}

    mqtt = y.get("mqtt") or {}
    py   = y.get("pyatv") or {}
    bt   = y.get("bt") or {}

    return RoomConfig(
        room = y.get("room"),
        device_name = y.get("device_name") or "PiHub",
        mqtt_host   = mqtt.get("host") or "localhost",
        mqtt_port   = int(mqtt.get("port") or 1883),
        mqtt_user   = mqtt.get("username"),
        mqtt_password = mqtt.get("password"),
        prefix_bridge = mqtt.get("prefix_bridge") or "pihub/room",
        # pyatv
        pyatv_enabled = bool(py.get("enabled", False)),
        pyatv_address = py.get("address") or None,
        pyatv_companion = py.get("companion") or None,
        pyatv_airplay = py.get("airplay") or None,
        # bt / HID
        bt_enabled = bool(bt.get("enabled", True)),
        bt_device_name = bt.get("device_name") or None,
    )