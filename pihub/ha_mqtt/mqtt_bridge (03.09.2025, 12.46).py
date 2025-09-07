# pihub/ha_mqtt/mqtt_bridge.py
import asyncio
import contextlib
import json
from dataclasses import dataclass

from aiomqtt import Client, MqttError, Will


@dataclass
class MqttConfig:
    host: str          # always provided by room.yaml
    port: int          # always provided by room.yaml
    user: str | None   # always provided by room.yaml
    password: str | None
    prefix_bridge: str
    input_select_entity: str


class MqttBridge:
    """
    Minimal bridge:
      - RX: Subscribes to HA statestream state for the input_select and invokes on_activity_state(payload)
      - TX: Publishes HA service calls to {prefix}/ha/service/call with JSON payloads
      - Health: retained online/offline with LWT
    """

    def __init__(self, cfg: MqttConfig, on_activity_state):
        self.cfg = cfg
        self.on_activity_state = on_activity_state

        # Topics
        self.base = cfg.prefix_bridge.rstrip("/")
        self.topic_service_call = f"{self.base}/ha/service/call"
        self.topic_activity_state = f"{self.base}/input_select/{cfg.input_select_entity}/state"
        self.topic_health = f"{self.base}/health"

        # Client + lifecycle (use Will object, not a tuple)
        self.client = Client(
            hostname=cfg.host,
            port=cfg.port,
            username=cfg.user,
            password=cfg.password,
            will=Will(self.topic_health, b"offline", 1, True),
        )
        self._stop = asyncio.Event()

        # Outbound queue (producer: publish_ha_service; consumer: _tx_loop)
        self._out_q: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

    # ---------- Public API ----------

    async def start(self, activity_getter):
        """
        Establish the MQTT session, start RX+TX loops.
        'activity_getter' kept for signature compatibility (unused here).
        """
        backoff = 1.0
        while not self._stop.is_set():
            try:
                print(f"[mqtt] connecting to {self.cfg.host}:{self.cfg.port} …")
                async with self.client as c:
                    print("[mqtt] connected")
    
                    # Health: we are online now
                    await c.publish(self.topic_health, b"online", qos=1, retain=True)
    
                    # RX setup
                    await c.subscribe(self.topic_activity_state)
                    print(f"[mqtt] subscribed → {self.topic_activity_state}")
    
                    # Start loops
                    rx_task = asyncio.create_task(self._rx_loop(c), name="mqtt_rx")
                    tx_task = asyncio.create_task(self._tx_loop(c), name="mqtt_tx")
    
                    # Wait until asked to stop
                    await self._stop.wait()
    
                    # On clean shutdown: mark offline before disconnecting
                    with contextlib.suppress(Exception):
                        await c.publish(self.topic_health, b"offline", qos=1, retain=True)
    
                    # Teardown
                    rx_task.cancel(); tx_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await rx_task
                        await tx_task
                    backoff = 1.0  # reset on clean exit
    
            except MqttError as e:
                print(f"[mqtt] connection error: {e}; retrying in {backoff:.1f}s")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    backoff = min(backoff * 2, 30.0)
                    continue
            except Exception as e:
                print(f"[mqtt] fatal error: {e}; retrying in {backoff:.1f}s")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    backoff = min(backoff * 2, 30.0)
                    continue

    async def shutdown(self):
        """Signal loops to stop and let start() unwind."""
        self._stop.set()

    async def publish_ha_service(self, domain: str, service: str, data: dict | None = None):
        """
        Enqueue a Home Assistant service call to {base}/ha/service/call.
        """
        payload = json.dumps(
            {"domain": domain, "service": service, "data": data or {}},
            separators=(",", ":"),
        )
        await self._out_q.put((self.topic_service_call, payload))

    # ---------- Internal loops ----------

    async def _rx_loop(self, c: Client):
        """
        Consume messages and handle the input_select state topic.
        Compatible with aiomqtt variants where `messages` can be
        a method or a property.
        """
        # Get message iterator (supports both styles)
        messages_mgr = getattr(c, "messages", None)

        # Style A: messages() -> async context manager
        if callable(messages_mgr):
            async with c.messages() as messages:
                async for m in messages:
                    await self._handle_msg(m)
            return

        # Style B: messages -> iterator (no context manager)
        messages = messages_mgr
        async for m in messages:
            await self._handle_msg(m)

    async def _handle_msg(self, m):
        topic = str(m.topic)
        payload = m.payload.decode("utf-8", errors="replace").strip()
        if topic == self.topic_activity_state:
            print(f"[MQTT] state {topic} = '{payload}'")
            try:
                res = self.on_activity_state(payload)  # may be sync or async
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                print(f"[MQTT] on_activity_state error: {e}")

    async def _tx_loop(self, c: Client):
        """
        Drain the outbound queue and publish to MQTT (log each publish).
        """
        while not self._stop.is_set():
            topic, payload = await self._out_q.get()
            try:
                print(f"[MQTT] tx {topic} {payload}")
                await c.publish(topic, payload, qos=1, retain=False)
            finally:
                self._out_q.task_done()