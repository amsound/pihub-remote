# File: mqtt_config.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import yaml


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int
    username: str
    password: str
    prefix_bridge: str  # e.g. "pihub/living_room"
    room: str          # e.g. "living_room"
    client_id: str     # e.g. "pihub:living_room"
    keepalive: int = 30
    tls: bool = False


def load_config(path: str | Path) -> MqttConfig:
    data = yaml.safe_load(Path(path).read_text())

    room = str(data["room"]).strip()
    m = data["mqtt"]
    prefix_bridge = str(m["prefix_bridge"]).strip()

    client_id = f"pihub:{room}"

    return MqttConfig(
        host=str(m["host"]).strip(),
        port=int(m.get("port", 1883)),
        username=str(m.get("username", "")),
        password=str(m.get("password", "")),
        prefix_bridge=prefix_bridge,
        room=room,
        client_id=client_id,
        keepalive=int(m.get("keepalive", 30)),
        tls=bool(m.get("tls", False)),
    )