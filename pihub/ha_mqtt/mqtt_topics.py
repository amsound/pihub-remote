# File: mqtt_topics.py
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TopicPolicy:
    topic: str
    direction: str  # "tx" | "rx"
    qos: int
    retain: bool
    desc: str = ""


@dataclass(frozen=True)
class Topics:
    # RX
    activity_state: TopicPolicy    # pihub/input_select/<room>_activity/state (retained by HA, QoS1)
    cmd_all: TopicPolicy           # pihub/<room>/cmd/# (QoS1, non-retained)

    # TX
    activity: TopicPolicy          # <prefix_bridge>/activity (NEVER retained; nuked at start; QoS1)
    ha_service_call: TopicPolicy   # <prefix_bridge>/ha/service/call (NEVER retained; nuked at start; QoS1)

    # TX (status split)
    status: TopicPolicy            # <prefix_bridge>/status (availability only: "online"/"offline", retained, QoS1)
    status_info: TopicPolicy       # <prefix_bridge>/status/info (stats JSON, non-retained, QoS0)

    # Discovery base
    disc_prefix: str               # e.g. "homeassistant"


def build_topics(prefix_bridge: str, room: str, disc_prefix: str = "homeassistant") -> Topics:
    base = f"{prefix_bridge}"

    return Topics(
        # RX
        activity_state=TopicPolicy(
            topic=f"pihub/input_select/{room}_activity/state",
            direction="rx", qos=1, retain=True,
            desc="Current activity from HA statestream (retained @ broker)",
        ),
        cmd_all=TopicPolicy(
            topic=f"pihub/{room}/cmd", 
            direction="rx", qos=1, retain=False,
            desc="Unified command bus. Payload 'category:action' (e.g. 'macro:atv-on').",
        ),

        # TX
        activity=TopicPolicy(
            topic=f"{base}/activity",
            direction="tx", qos=1, retain=False,
            desc="App's current mapping/activity. Never retained; nuked at start.",
        ),
        ha_service_call=TopicPolicy(
            topic=f"{base}/ha/service/call",
            direction="tx", qos=1, retain=False,
            desc="One-shot HA service call requests. Never retained; nuked at start.",
        ),

        # TX (status split)
        status=TopicPolicy(
            topic=f"{base}/status",
            direction="tx", qos=1, retain=True,
            desc='Availability only: plain "online"/"offline". Retained for instant HA availability.',
        ),
        status_info=TopicPolicy(
            topic=f"{base}/status/info",
            direction="tx", qos=0, retain=False,
            desc="Stats/health snapshot JSON (ephemeral, non-retained).",
        ),

        # Discovery
        disc_prefix=disc_prefix,
    )