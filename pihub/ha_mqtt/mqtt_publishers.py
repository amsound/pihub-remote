# pihub/ha_mqtt/mqtt_publishers.py
from __future__ import annotations

import platform
import socket
import time
from typing import Any, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    # type-only import to avoid circular import at runtime
    from .mqtt_topics import Topics


def _room_pretty(room: str) -> str:
    return room.replace("_", " ").title()


def clear_retained_at_start(bridge: Any, topics: "Topics") -> None:
    """
    Force-nuke any accidental retained payloads on activity & ha/service/call.
    `bridge` must expose publish_bytes(topic, payload, qos, retain).
    """
    for t in (topics.activity.topic, topics.ha_service_call.topic):
        bridge.publish_bytes(t, b"", qos=1, retain=True)


def clear_discovery(bridge: Any, topics: "Topics", room: str) -> None:
    disc = topics.disc_prefix
    uid = f"pihub_{room}"
    cfgs = [
        f"{disc}/binary_sensor/{uid}_online/config",
        f"{disc}/binary_sensor/{uid}_pi_undervoltage_now/config",
        f"{disc}/binary_sensor/{uid}_pi_undervoltage_ever/config",
        f"{disc}/sensor/{uid}_hostname/config",
        f"{disc}/sensor/{uid}_ip_addr/config",
        f"{disc}/sensor/{uid}_uptime_human/config",
        f"{disc}/sensor/{uid}_last_activity_cmd/config",
        f"{disc}/sensor/{uid}_last_ha_service/config",
        f"{disc}/sensor/{uid}_bt_count/config",
        f"{disc}/sensor/{uid}_bt_macs/config",
        f"{disc}/sensor/{uid}_cpu_load_pct/config",
        f"{disc}/sensor/{uid}_cpu_temp/config",
        f"{disc}/sensor/{uid}_disk_used_pct/config",
        f"{disc}/sensor/{uid}_mem_used_pct/config",
    ]
    for t in cfgs:
        bridge.publish_bytes(t, b"", qos=1, retain=True)


def publish_discovery(bridge: Any, topics: "Topics", room: str) -> None:
    """
    MQTT Discovery for your full PiHub device with multiple entities, all driven by ONE JSON status topic.
    Names DO NOT include the room; the device name groups them under "<Room Pretty> - PiHub".
    """
    disc = topics.disc_prefix
    pretty = _room_pretty(room)
    device = {
        "identifiers": [f"pihub_{room}"],
        "name": f"{pretty} - PiHub",
        "manufacturer": "PiHub",
        "model": "PiHub Remote Bridge",
        "sw_version": "paho-mqtt",
    }
    state_topic = topics.status_json.topic

    # Availability from JSON {state: "online"/"offline"}
    avail = {
        "availability_topic": state_topic,
        "availability_template": "{{ 'online' if value_json.state == 'online' else 'offline' }}",
        "payload_available": "online",
        "payload_not_available": "offline",
    }

    def pub(kind: str, uid_suffix: str, cfg: dict) -> None:
        uid = f"pihub_{room}_{uid_suffix}"
        cfg.setdefault("object_id", f"{room}_pihub_{uid_suffix}")
        cfg.setdefault("unique_id", uid)
        bridge.publish_json(f"{disc}/{kind}/{uid}/config", cfg, qos=1, retain=True)

    # ---- Binary sensors ----
    pub("binary_sensor", "online", {
        "name": "Online",
        "unique_id": f"pihub_{room}_online",
        "device_class": "connectivity",
        "state_topic": state_topic,
        "value_template": "{{ 'ON' if value_json.state == 'online' else 'OFF' }}",
        "payload_on": "ON",
        "payload_off": "OFF",
        "icon": "mdi:check-network-outline",
        **avail,
        "device": device,
    })

    pub("binary_sensor", "pi_undervoltage_now", {
        "name": "Pi Undervoltage (Now)",
        "unique_id": f"pihub_{room}_pi_undervoltage_now",
        "device_class": "problem",
        "state_topic": state_topic,
        "value_template": "{{ 'ON' if value_json.attr.pi_undervolt else 'OFF' }}",
        "payload_on": "ON",
        "payload_off": "OFF",
        "icon": "mdi:alert",
        "entity_category": "diagnostic",
        **avail,
        "device": device,
    })

    pub("binary_sensor", "pi_undervoltage_ever", {
        "name": "Pi Undervoltage (Ever)",
        "unique_id": f"pihub_{room}_pi_undervoltage_ever",
        "device_class": "problem",
        "state_topic": state_topic,
        "value_template": "{{ 'ON' if value_json.attr.pi_undervolt_ever else 'OFF' }}",
        "payload_on": "ON",
        "payload_off": "OFF",
        "icon": "mdi:alert-circle-outline",
        "entity_category": "diagnostic",
        **avail,
        "device": device,
    })
    
    # Activity display (reads your TX topic directly; not retained)
    pub("sensor", "activity_display", {
        "name": "Activity",
        "unique_id": f"pihub_{room}_activity_display",
        "state_topic": topics.activity.topic,  # e.g. pihub/<room>/activity
        "value_template": "{{ value }}",       # payload is raw string like "watch"
        "icon": "mdi:remote",
        # availability still driven by the status JSON topic:
        **avail,
        "device": device,
    })
    
    # HA service call display (reads your TX JSON topic directly; not retained)
    pub("sensor", "ha_service_call", {
        "name": "HA Service",
        "unique_id": f"pihub_{room}_ha_service_call",
        "state_topic": topics.ha_service_call.topic,  # e.g. pihub/<room>/ha/service/call
        "value_template": "{{ value_json.domain ~ '.' ~ value_json.service if value_json is defined else '-' }}",
        "icon": "mdi:home-assistant",
        **avail,
        "device": device,
    })

    # ---- Helper for sensors ----
    def sensor(uid_suffix: str, name: str, value_tpl: str, extra: dict | None = None):
        cfg = {
            "name": name,
            "unique_id": f"pihub_{room}_{uid_suffix}",
            "state_topic": state_topic,
            "value_template": value_tpl,
            **avail,
            "device": device,
        }
        if extra:
            cfg.update(extra)
        pub("sensor", uid_suffix, cfg)

    # Core sensors (with nicer icons and names)
    sensor("hostname", "Hostname", "{{ value_json.attr.host }}", {"icon": "mdi:server"})
    sensor("ip_addr", "IP Address", "{{ value_json.attr.ip_addr }}", {"icon": "mdi:ip-network"})
    sensor(
        "uptime_human",
        "Uptime",
        "{{ (value_json.attr.uptime_s // 86400) | int }}d {{ ((value_json.attr.uptime_s % 86400) // 3600) | int }}h",
        {"icon": "mdi:calendar-clock", "entity_category": "diagnostic"},
    )

    sensor("bt_count", "BT Connected", "{{ value_json.attr.bt_connected_count }}",
           {"icon": "mdi:bluetooth", "state_class": "measurement", "entity_category": "diagnostic"})
    sensor("bt_macs", "BT Devices", "{{ value_json.attr.bt_connected_macs or '-' }}",
           {"icon": "mdi:bluetooth-connect", "entity_category": "diagnostic"})
    sensor("cpu_load_pct", "CPU Load", "{{ value_json.attr.cpu_load_pct }}",
           {"icon": "mdi:chip", "unit_of_measurement": "%", "state_class": "measurement", "entity_category": "diagnostic"})
    sensor("cpu_temp", "CPU Temp", "{{ value_json.attr.cpu_temp_c }}",
           {"icon": "mdi:thermometer", "device_class": "temperature", "unit_of_measurement": "°C",
            "state_class": "measurement", "entity_category": "diagnostic"})
    sensor("disk_used_pct", "Disk Used", "{{ value_json.attr.disk_used_pct }}",
           {"icon": "mdi:harddisk", "unit_of_measurement": "%", "state_class": "measurement", "entity_category": "diagnostic"})
    sensor("mem_used_pct", "Memory Used", "{{ value_json.attr.mem_used_pct }}",
           {"icon": "mdi:memory", "unit_of_measurement": "%", "state_class": "measurement", "entity_category": "diagnostic"})


def publish_status(bridge: Any, topics: "Topics", *, online: bool, extra: Dict[str, Any] | None = None) -> None:
    """
    Publish combined state + attributes JSON to the status topic (QoS0, non-retained).
    `bridge` must expose publish_json(topic, payload, qos, retain).
    """
    attrs: Dict[str, Any] = {
        "ts": int(time.time()),
        "host": socket.gethostname(),
        "py": platform.python_version(),
    }
    if extra:
        attrs.update(extra)

    payload = {
        "state": "online" if online else "offline",
        "attr": attrs,
    }
    bridge.publish_json(topics.status_json.topic, payload, qos=0, retain=False)