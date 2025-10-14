# pihub/ha_mqtt/mqtt_bridge.py  — native paho-mqtt v1 bridge matching your app’s API

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass
from typing import Callable, Optional
from contextlib import suppress

import paho.mqtt.client as mqtt

# Use the modular helpers we built & tested
from .mqtt_config import MqttConfig as CoreCfg  # internal config for topics/client_id
from .mqtt_topics import build_topics
from .mqtt_publishers import publish_discovery, clear_retained_at_start
from .mqtt_stats_pi import get_stats

DEBUG_MQTT = False

@dataclass
class MqttConfig:
    host: str
    port: int
    user: str | None
    password: str | None
    prefix_bridge: str
    input_select_entity: str  # e.g. "living_room_activity"
    status_interval_sec: int = 10


class MqttBridge:
    """
    Native, resilient MQTT bridge using paho-mqtt v1.
    Preserves your existing API used by app.py and dispatcher.py.
    """

    def __init__(
        self,
        cfg: MqttConfig,
        on_activity_state: Callable[[str], None],
        on_command: Optional[Callable[[str, bytes], None]] = None,
    ):
        self.cfg = cfg
        self.on_activity_state = on_activity_state
        self.on_command = on_command
        
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Derive room from "<room>_activity"
        self._room = cfg.input_select_entity.removesuffix("_activity")

        # Internal core cfg used for topics + client_id
        self._core = CoreCfg(
            host=cfg.host,
            port=cfg.port,
            username=cfg.user,
            password=cfg.password,
            prefix_bridge=cfg.prefix_bridge,
            room=self._room,
            client_id=f"pihub:{self._room}",
            keepalive=15,
            tls=False,
        )

        # Topics (RX/TX strictly separated)
        self._topics = build_topics(self._core.prefix_bridge, self._core.room)
        # Your log prints this at startup:
        self.topic_activity_state = f"pihub/input_select/{cfg.input_select_entity}/state"

        # paho client (persistent session)
        self.client = mqtt.Client(
            client_id=self._core.client_id,
            clean_session=False,
            protocol=mqtt.MQTTv311,
            transport="tcp",
        )
        if self._core.username:
            self.client.username_pw_set(self._core.username, self._core.password)

        # LWT: availability on /status (plain string, retained)
        self.client.will_set(
            self._topics.status.topic,
            payload="offline",
            qos=1,
            retain=True,
        )

        # Tuning
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.max_inflight_messages_set(40)
        self.client.max_queued_messages_set(50)

        # Callbacks
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.on_subscribe = self._on_subscribe

        # Runner state
        self._status_task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()

        # Activity provider callable (used by your app, if needed later)
        self._activity_provider: Optional[Callable[[], str]] = None

    # --------------------- public API used by your app ---------------------

    async def start(self, activity_provider: Callable[[], str]) -> None:
        self._activity_provider = activity_provider
    
        # Capture the app loop so callbacks can schedule coroutines safely
        self._loop = asyncio.get_running_loop()
        
        self.client.connect(self._core.host, self._core.port, self._core.keepalive)
        self.client.loop_start()
        # Periodic status with real stats
        self._status_task = asyncio.create_task(self._status_heartbeat(), name="mqtt_status_hb")

    async def shutdown(self) -> None:
        """Publish offline, stop heartbeat, and disconnect cleanly."""
        self._stopping.set()
        if self._status_task:
            self._status_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._status_task
            self._status_task = None
        
        # Best-effort offline + disconnect
        try:
            if self.client.is_connected():
                self.client.publish(
                    self._topics.status.topic,
                    "offline",
                    qos=1,
                    retain=True,
                )
        except Exception:
            pass
        finally:
            with suppress(Exception):
                self.client.disconnect()
            with suppress(Exception):
                self.client.loop_stop()

    async def publish_json(self, topic: str, obj: dict) -> None:
        """Generic JSON publish (QoS1, non-retained)."""
        payload = json.dumps(obj, separators=(",", ":"))
        self.client.publish(topic, payload, qos=1, retain=False)

    async def publish_ha_service(self, domain: str, service: str, data: dict | None = None) -> None:
        payload = {
            "domain": str(domain),
            "service": str(service),
            "data": data or {},
        }
        js = json.dumps(payload, separators=(",", ":"))
        if DEBUG_MQTT: print(f"[mqtt:tx] topic={self._topics.ha_service_call.topic} qos=1 retain=False payload={js}")
        self._try_publish_or_buffer(
            self._topics.ha_service_call.topic,
            js.encode(),
            qos=1,
            retain=False,
            kind="ha_service",
        )
    
    async def publish_activity_intent(self, activity: str) -> None:
        act = (activity or "").strip().lower()
        if not act:
            return
        if DEBUG_MQTT: print(f"[mqtt:tx] topic={self._topics.activity.topic} qos=1 retain=False payload={act}")
        self._try_publish_or_buffer(
            self._topics.activity.topic,
            act.encode(),
            qos=1,
            retain=False,
            kind="activity",
        )
        
    def _try_publish_or_buffer(self, topic: str, payload: bytes, *, qos: int, retain: bool, kind: str) -> None:
        """
        Simplest policy: if we're offline, DROP the message (no buffering).
        Avoids stale floods and HA race conditions on reconnect.
        """
        if self.client.is_connected():
            self.client.publish(topic, payload, qos=qos, retain=retain)
            return
    
        # offline -> drop (but log)
        try:
            preview = payload.decode("utf-8", "replace")
        except Exception:
            preview = "<bytes>"
        print(f"[mqtt:drop] offline {kind} discarded: topic={topic} qos={qos} payload={preview}")

    # -------------------------- internal helpers --------------------------

    async def _status_heartbeat(self) -> None:
        """Immediate + periodic status publishes with real stats."""
        interval = max(5, int(self.cfg.status_interval_sec))
        try:
            # Initial full status (after connect callback also fires one)
            await asyncio.sleep(0.1)
            stats = get_stats()
            publish_status_bridge(self.client, self._topics.status_info.topic, stats)
            # Heartbeat
            while not self._stopping.is_set():
                await asyncio.sleep(interval)
                stats = get_stats()
                publish_status_bridge(self.client, self._topics.status_info.topic, stats)
        except asyncio.CancelledError:
            return

    # ----------------------------- callbacks ------------------------------

    def _on_connect(self, client: mqtt.Client, userdata, flags, rc, properties=None):
        sp = flags.get("session present") if isinstance(flags, dict) else flags
        session_present = bool(sp)
        print(f"[mqtt] connected; session_present={session_present}")

        # ONLINE immediately (plain string, retained)
        client.publish(self._topics.status.topic, "online", qos=1, retain=True)

        # Always (re)subscribe so changes to topics take effect
        # Use the statestream topic *with* the _activity suffix from config
        print(f"[mqtt] subscribing → {self.topic_activity_state}")
        client.subscribe(self.topic_activity_state, qos=1)
        
        print(f"[mqtt] subscribing → {self._topics.cmd_all.topic}")
        client.subscribe(self._topics.cmd_all.topic, qos=self._topics.cmd_all.qos)

        # If this is a fresh session, clear accidental retained on our TX command topics
        if not session_present:
            clear_retained_at_start_bridge(client, self._topics)

        # Always (re)publish discovery (retained + idempotent), then push full status
        publish_discovery_bridge(client, self._topics, self._room)
        stats = get_stats()
        publish_status_bridge(client, self._topics.status_info.topic, stats)

    def _on_disconnect(self, client: mqtt.Client, userdata, rc, properties=None):
        if rc != 0:
            print(f"[mqtt] disconnected unexpectedly rc={rc}; paho will reconnect")

    def _on_subscribe(self, client, userdata, mid, granted_qos, properties=None):
        print(f"[mqtt] subscribed mid={mid} qos={granted_qos}")

    def _on_message(self, client: mqtt.Client, userdata, msg: mqtt.MQTTMessage):
        t = msg.topic
        p = msg.payload
    
        # Activity statestream (HA → PiHub)
        if t == self.topic_activity_state:
            s = p.decode("utf-8", errors="replace") if p else ""
            self._call_handler(self.on_activity_state, s)
            return
    
        # Command bus (HA → PiHub): pihub/<room>/cmd with payload "category:action"
        if t == self._topics.cmd_all.topic:
            try:
                cmd = (p or b"").decode("utf-8", "replace").strip()
            except Exception:
                cmd = ""
            if cmd:
                # on_command accepts a single string now (e.g. "macro:atv-on")
                self._call_handler(self.on_command, cmd)
            else:
                print("[cmd] empty/invalid payload on command bus")
            return
                    
                    
    def _call_handler(self, handler, *args) -> None:
        """Dispatch handler to the app's asyncio loop (never run in paho thread).
        - If handler returns a coroutine, create a task for it.
        - If handler returns an already-scheduled Task/Future, leave it alone.
        - If no loop is available (very early), run on a fallback one-off loop.
        """
        if not handler:
            return
    
        loop = self._loop
        if loop and loop.is_running():
            def _runner():
                try:
                    res = handler(*args)
                    # Already a Task/Future? It's scheduled or will be awaited elsewhere.
                    if isinstance(res, (asyncio.Task, asyncio.Future)):
                        return
                    # Bare coroutine: turn it into a Task on this loop.
                    if asyncio.iscoroutine(res):
                        asyncio.create_task(res)
                except Exception as e:
                    print(f"[mqtt] handler error (in loop): {e!r}")
    
            try:
                loop.call_soon_threadsafe(_runner)
            except Exception as e:
                print(f"[mqtt] handler schedule error: {e!r}")
            return
    
        # Fallback: no loop captured yet (very early). Run sync or coroutine on a temp loop.
        print("[mqtt] WARNING: app loop not set/running; executing handler on fallback loop")
        def _fallback():
            try:
                res = handler(*args)
                if asyncio.iscoroutine(res):
                    asyncio.run(res)
                # If it's a Task/Future here, we can't safely bind it to this temp loop; ignore.
            except Exception as e:
                print(f"[mqtt] handler error (fallback): {e!r}")
        threading.Thread(target=_fallback, name="mqtt-fallback", daemon=True).start()

# ---------------- inline bridges to avoid importing asyncio in publishers ----------------

def publish_status_bridge(client: mqtt.Client, status_info_topic: str, stats_extra: dict) -> None:
    client.publish(status_info_topic, json.dumps(stats_extra, separators=(",", ":")), qos=0, retain=False)

def publish_discovery_bridge(client: mqtt.Client, topics, room: str) -> None:
    # reuse the retained discovery we wrote in mqtt_publishers.py
    # by calling into it once; doing it here means no coroutine in this file
    try:
        # Build a tiny stub that matches PahoMqttBridge.publish_json / publish_bytes
        class _PubShim:
            def __init__(self, cli: mqtt.Client): self.client = cli
            def publish_json(self, topic: str, payload: dict, qos=0, retain=False):
                self.client.publish(topic, json.dumps(payload, separators=(",", ":")), qos=qos, retain=retain)
            def publish_bytes(self, topic: str, payload: bytes, qos=0, retain=False):
                self.client.publish(topic, payload, qos=qos, retain=retain)

        shim = _PubShim(client)
        publish_discovery(shim, topics, room)
    except Exception as e:
        print(f"[mqtt] discovery publish error: {e!r}")

def clear_retained_at_start_bridge(client: mqtt.Client, topics) -> None:
    try:
        client.publish(topics.activity.topic, b"", qos=1, retain=True)
        client.publish(topics.ha_service_call.topic, b"", qos=1, retain=True)
    except Exception:
        pass
