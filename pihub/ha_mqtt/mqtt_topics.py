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
    activity_state: TopicPolicy  # pihub/input_select/<room>_activity/state (retained by HA, QoS1)
    cmd_all: TopicPolicy         # pihub/<room>/cmd/# (QoS1, non-retained)
    # TX
    activity: TopicPolicy        # <prefix_bridge>/activity (NEVER retained; nuked at start; QoS1)
    ha_service_call: TopicPolicy # <prefix_bridge>/ha/service/call (NEVER retained; nuked at start; QoS1)
    status_json: TopicPolicy     # <prefix_bridge>/status (combined health + stats, non-retained)
    # Discovery base
    disc_prefix: str             # e.g. "homeassistant"


def build_topics(prefix_bridge: str, room: str, disc_prefix: str = "homeassistant") -> Topics:
    return Topics(
        # RX
        activity_state=TopicPolicy(
            topic=f"pihub/input_select/{room}_activity/state", direction="rx", qos=1, retain=True,
            desc="Current activity from HA statestream (retained @ broker)",
        ),
        cmd_all=TopicPolicy(
            topic=f"pihub/{room}/cmd/#", direction="rx", qos=1, retain=False,
            desc="Command bus from HA/macros (non-retained)",
        ),
        # TX
        activity=TopicPolicy(
            topic=f"{prefix_bridge}/activity", direction="tx", qos=1, retain=False,
            desc="App's current mapping/activity (shadow). Never retained; nuked at start.",
        ),
        ha_service_call=TopicPolicy(
            topic=f"{prefix_bridge}/ha/service/call", direction="tx", qos=1, retain=False,
            desc="One-shot HA service call requests. Never retained; nuked at start.",
        ),
        status_json=TopicPolicy(
            topic=f"{prefix_bridge}/status", direction="tx", qos=0, retain=False,
            desc="Combined health + stats JSON (non-retained)",
        ),
        disc_prefix=disc_prefix,
    )