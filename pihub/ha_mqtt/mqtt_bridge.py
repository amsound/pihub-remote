# pihub/ha_mqtt/mqtt_bridge.py
import asyncio
import contextlib
import json
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass

from aiomqtt import Client, MqttError, Will


@dataclass
class MqttConfig:
    host: str
    port: int
    user: str | None
    password: str | None
    prefix_bridge: str
    input_select_entity: str
    status_interval_sec: int = 120
    substitutions: dict[str, str] | None = None


class MqttBridge:
    """
    Single-file bridge:
      - Health: retained online/offline with LWT
      - RX: Statestream of HA input_select → on_activity_state(payload)
      - RX: Commands on {prefix}/cmd/# → optional on_command(name, payload)
      - TX: HA service calls → {prefix}/ha/service/call  (JSON)
      - TX: Periodic status JSON → {prefix}/status/json  (non-retained)
    """

    def __init__(self, cfg: MqttConfig, on_activity_state, on_command=None):
        self.cfg = cfg
        
        # flatten substitutions: {"entities.speakers": "media_player.living_room", ...}
        def _flatten(d, prefix=""):
            flat = {}
            if not isinstance(d, dict):
                return flat
            for k, v in d.items():
                key = f"{prefix}.{k}" if prefix else k
                if isinstance(v, dict):
                    flat.update(_flatten(v, key))
                else:
                    flat[key] = v
            return flat
        
        self._subs = {}
        if cfg.substitutions:
            # allow both flat {"a.b": "..."} and nested {"a": {"b": "..."}}
            self._subs.update(_flatten(cfg.substitutions))
            self._subs.update({k: v for k, v in cfg.substitutions.items() if isinstance(v, str)})
        
        self.on_activity_state = on_activity_state
        self.on_command = on_command  # optional callback (name:str, payload:bytes)

        # Topics
        self.base = cfg.prefix_bridge.rstrip("/")
        self.topic_service_call   = f"{self.base}/ha/service/call"
        self.topic_activity_state = f"{self.base}/input_select/{cfg.input_select_entity}/state"
        self.topic_health         = f"{self.base}/health"
        self.topic_cmd_base       = f"{self.base}/cmd"
        self.topic_status_json    = f"{self.base}/status/json"

        # Client + lifecycle
        self.client = Client(
            hostname=cfg.host,
            port=cfg.port,
            username=cfg.user,
            password=cfg.password,
            will=Will(self.topic_health, b"offline", 1, True),
            keepalive=15,  # short so LWT flips quickly on hard exits
        )
        self._stop = asyncio.Event()
        self._stopped = asyncio.Event()   # set after start() fully unwinds

        # Outbound queue (producer: publish_ha_service; consumer: _tx_loop)
        self._out_q: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

    # ---------- Public API ----------

    async def start(self, activity_getter):
        """
        Establish the MQTT session and run RX/TX + status loops.
        'activity_getter' kept for signature compatibility (unused here).
        """
        backoff = 1.0
        try:
            while not self._stop.is_set():
                try:
                    print(f"[mqtt] connecting to {self.cfg.host}:{self.cfg.port} …")
                    async with self.client as c:
                        print("[mqtt] connected")

                        # Health ONLINE (retained)
                        await c.publish(self.topic_health, b"online", qos=1, retain=True)

                        # Push an initial status snapshot immediately
                        with contextlib.suppress(Exception):
                            await self._publish_status_once(c)

                        # Subscriptions
                        await c.subscribe(self.topic_activity_state)
                        await c.subscribe(self.topic_cmd_base + "/#")
                        print(f"[mqtt] subscribed → {self.topic_activity_state}")
                        print(f"[mqtt] subscribed → {self.topic_cmd_base}/#")

                        # Background loops
                        pump_task   = asyncio.create_task(self._pump_messages(c), name="mqtt_pump")
                        tx_task     = asyncio.create_task(self._tx_loop(c),      name="mqtt_tx")
                        status_task = asyncio.create_task(self._status_loop(c),  name="mqtt_status")

                        # Wait for stop
                        await self._stop.wait()

                        # Best-effort OFFLINE (bounded)
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(
                                c.publish(self.topic_health, b"offline", qos=1, retain=True),
                                timeout=1.0,
                            )

                        # Teardown loops
                        for t in (pump_task, tx_task, status_task):
                            t.cancel()
                        for t in (pump_task, tx_task, status_task):
                            with contextlib.suppress(asyncio.CancelledError):
                                await t

                        backoff = 1.0  # reset backoff on clean loop exit

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
        finally:
            # Signal completely unwound
            self._stopped.set()
            # Belt-and-braces: publish OFFLINE once more with a fresh client
            await self._publish_offline_once()

    async def shutdown(self):
        """Signal loops to stop and let start() unwind."""
        self._stop.set()
        # Optionally wait until fully unwound (used by app.py on clean exit)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopped.wait(), timeout=3.0)

    async def publish_ha_service(self, domain: str, service: str, data: dict | None = None):
        """
        Enqueue a Home Assistant service call to {base}/ha/service/call.
        Expands ${...} placeholders in the data before publishing.
        """
        resolved = self._resolve_placeholders(data or {})
        payload = json.dumps(
            {"domain": domain, "service": service, "data": resolved},
            separators=(",", ":"),
        )
        await self._out_q.put((self.topic_service_call, payload))
        
        
    def _resolve_placeholders(self, obj):
        """Replace strings like '${path.to.value}' using self._subs."""
        def subst(val):
            if isinstance(val, str) and val.startswith("${") and val.endswith("}"):
                key = val[2:-1]
                return self._subs.get(key, val)  # leave as-is if not found
            return val
    
        if isinstance(obj, dict):
            return {k: self._resolve_placeholders(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._resolve_placeholders(x) for x in obj]
        return subst(obj)

    # ---------- Internal: message pump / dispatch ----------

    async def _pump_messages(self, c: Client):
        """
        Unified message pump (supports aiomqtt .messages() method or .messages iterator).
        """
        messages_mgr = getattr(c, "messages", None)
        if callable(messages_mgr):
            async with c.messages() as messages:
                async for m in messages:
                    await self._dispatch_message(m)
        else:
            async for m in messages_mgr:
                await self._dispatch_message(m)

    async def _dispatch_message(self, m):
        topic = str(m.topic)
        payload_bytes = m.payload or b""
        payload = payload_bytes.decode("utf-8", errors="replace").strip()

        if topic == self.topic_activity_state:
            print(f"[MQTT] state {topic} = '{payload}'")
            try:
                res = self.on_activity_state(payload)  # may be sync or async
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                print(f"[MQTT] on_activity_state error: {e}")
            return
            
        if topic.startswith(self.topic_cmd_base + "/"):
            cmd_name = topic[len(self.topic_cmd_base) + 1:]  # e.g. "tv/on"
            print(f"[MQTT] cmd '{cmd_name}' payload='{payload}'")
            if self.on_command:
                try:
                    res = self.on_command(cmd_name, m.payload or b"")
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as e:
                    print(f"[MQTT] on_command error: {e}")
            return

    # ---------- Internal: TX queue loop ----------

    async def _tx_loop(self, c: Client):
        while not self._stop.is_set():
            topic, payload = await self._out_q.get()
            try:
                print(f"[MQTT] tx {topic} {payload}")
                await c.publish(topic, payload, qos=1, retain=False)
            finally:
                self._out_q.task_done()

    # ---------- Internal: periodic status publisher ----------

    async def _status_loop(self, c: Client):
        # small initial delay so we don't spam on immediate connect
        await asyncio.sleep(1.0)
        interval = max(5, int(self.cfg.status_interval_sec or 120))
        while not self._stop.is_set():
            data = await asyncio.get_running_loop().run_in_executor(None, self._gather_status)
            try:
                await c.publish(self.topic_status_json, json.dumps(data, separators=(",", ":")), qos=0, retain=False)
            except Exception as e:
                print(f"[MQTT] status publish error: {e}")
            # wait with cancellation support
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _publish_status_once(self, c: Client):
        """Publish one immediate status snapshot (called after connect)."""
        data = await asyncio.get_running_loop().run_in_executor(None, self._gather_status)
        try:
            await c.publish(self.topic_status_json, json.dumps(data, separators=(",", ":")), qos=0, retain=False)
        except Exception as e:
            print(f"[MQTT] initial status publish error: {e}")

    # ---------- Internal: status helpers (best-effort, no hard deps) ----------

    def _gather_status(self) -> dict:
        import shutil, socket, subprocess, os, time

        # ---------- CPU load % ----------
        try:
            with open("/proc/loadavg", "r", encoding="utf-8") as f:
                la1 = float(f.read().split()[0])
            cpu_count = os.cpu_count() or 1
            cpu_load_pct = min(100.0, (la1 / cpu_count) * 100.0)
        except Exception:
            cpu_load_pct = None

        # ---------- Mem used % ----------
        try:
            mem_total = mem_avail = None
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        mem_total = int(line.split()[1]) * 1024
                    elif line.startswith("MemAvailable:"):
                        mem_avail = int(line.split()[1]) * 1024
            mem_used_pct = (1.0 - (mem_avail / mem_total)) * 100.0 if (mem_total and mem_avail is not None) else None
        except Exception:
            mem_used_pct = None

        # ---------- Disk used % ----------
        try:
            du = shutil.disk_usage("/")
            disk_used_pct = (du.used / du.total) * 100.0
        except Exception:
            disk_used_pct = None

        # ---------- IP ----------
        ip = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
        except Exception:
            try:
                ip = socket.gethostbyname(socket.gethostname())
            except Exception:
                ip = None

        # ---------- Uptime / Hostname ----------
        try:
            with open("/proc/uptime", "r", encoding="utf-8") as f:
                uptime_sec = int(float(f.read().split()[0]))
        except Exception:
            uptime_sec = None
        try:
            hostname = socket.gethostname()
        except Exception:
            hostname = None

        # ---------- CPU temp ----------
        cpu_temp_c = None
        for cmd in (["/usr/bin/vcgencmd", "measure_temp"], ["vcgencmd", "measure_temp"]):
            try:
                out = subprocess.check_output(cmd, timeout=0.8).decode()
                cpu_temp_c = float(out.split("=")[1].split("'")[0])  # "temp=42.0'C"
                break
            except Exception:
                continue
        if cpu_temp_c is None:
            try:
                with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
                    cpu_temp_c = int(f.read().strip()) / 1000.0
            except Exception:
                pass

        # ---------- Power health (vcgencmd get_throttled) ----------
        throttled_hex = None
        undervoltage_now = None
        undervoltage_ever = None
        for cmd in (["/usr/bin/vcgencmd", "get_throttled"], ["vcgencmd", "get_throttled"]):
            try:
                out = subprocess.check_output(cmd, timeout=0.8).decode().strip()  # e.g. "throttled=0x0"
                if "throttled=" in out:
                    val = out.split("=", 1)[1].strip()  # "0x0", "0x50005", ...
                    flags = int(val, 16)
                    throttled_hex = val
                    undervoltage_now = bool(flags & 0x1)   # bit 0: under-voltage now
                    undervoltage_ever = bool(flags & 0x2)  # bit 1: under-voltage occurred
                break
            except Exception:
                continue

        # ---------- Bluetooth connected devices (MACs) ----------
        bt_macs = []
        try:
            out = subprocess.check_output(["bluetoothctl", "info"], timeout=1.5, stderr=subprocess.STDOUT).decode(errors="ignore")
            for block in out.split("\n\n"):
                if "Connected: yes" in block:
                    for line in block.splitlines():
                        if line.startswith("Device "):
                            parts = line.split()
                            if len(parts) >= 2:
                                bt_macs.append(parts[1])
        except Exception:
            pass

        return {
            "cpu_load_pct": round(cpu_load_pct, 1) if cpu_load_pct is not None else None,
            "mem_used_pct": round(mem_used_pct, 1) if mem_used_pct is not None else None,
            "disk_used_pct": round(disk_used_pct, 1) if disk_used_pct is not None else None,
            "ip": ip,
            "uptime_sec": uptime_sec,
            "hostname": hostname,
            "cpu_temp_c": round(cpu_temp_c, 1) if cpu_temp_c is not None else None,

            # Power health
            "throttled_hex": throttled_hex,             # e.g. "0x0" or "0x50005"
            "undervoltage_now": undervoltage_now,       # True/False/None
            "undervoltage_ever": undervoltage_ever,     # True/False/None

            # Bluetooth
            "bt_connected_devices": {
                "count": len(bt_macs),
                "macs": bt_macs if bt_macs else None,   # publish null instead of [] to render as "-"
            },

            "ts": int(time.time()),
        }

    async def _publish_offline_once(self):
        """Best-effort 'offline' publish after teardown, using a short-lived client."""
        try:
            async with Client(
                hostname=self.cfg.host,
                port=self.cfg.port,
                username=self.cfg.user,
                password=self.cfg.password,
                keepalive=5,
            ) as c2:
                await c2.publish(self.topic_health, b"offline", qos=1, retain=True)
        except Exception:
            pass