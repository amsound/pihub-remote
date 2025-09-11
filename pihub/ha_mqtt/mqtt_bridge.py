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
        
        self.on_activity_state = on_activity_state
        self.on_command = on_command  # optional callback (name:str, payload:bytes)

        # Topics
        self.base = cfg.prefix_bridge.rstrip("/")
        
        # HA Statestream base ("pihub" from "pihub/<room>") NEW
        self.ss_base = self.base.split("/", 1)[0]
        
        # Derive a stable room key from prefix: "pihub/living_room" -> "living_room"
        self._room = (self.base.split("/", 1)[1] if "/" in self.base else self.base).replace("/", "_")
        self._dev_id = f"pihub:{self._room}"           # device identifier
        self._dev_name = f"{self._room.replace('_',' ').title()} - PiHub"
        self._disc_prefix = "homeassistant"
        
        self.topic_service_call   = f"{self.base}/ha/service/call"
        # OLD self.topic_activity_state = f"{self.base}/input_select/{cfg.input_select_entity}/state"
        
        # Listen to HA statestream here (no room segment in the path)
        self.topic_activity_state = f"{self.ss_base}/input_select/{cfg.input_select_entity}/state"
        
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
                        
                        # Publish discovery configs (retained) so HA creates/updates the Device
                        with contextlib.suppress(Exception):
                            await self._publish_discovery(c)
                        
                        # Nuke any previously-retained activity intent so HA doesn't replay it
                        with contextlib.suppress(Exception):
                            await c.publish(self.topic_activity, b"", qos=1, retain=True)

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

                        # Wait for either: a) stop requested, or b) disconnect (pump ends)
                        stop_wait = asyncio.create_task(self._stop.wait(), name="mqtt_stopwait")
                        done, _ = await asyncio.wait(
                            {stop_wait, pump_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )

                        # Best-effort OFFLINE (bounded)
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(
                                c.publish(self.topic_health, b"offline", qos=1, retain=True),
                                timeout=1.0,
                            )

                        # Teardown loops
                        for t in (pump_task, tx_task, status_task, stop_wait):
                            if not t.done():
                                t.cancel()
                        for t in (pump_task, tx_task, status_task, stop_wait):
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
            # We are completely out of `async with Client` here.
            # Disarm paho so its __del__ can't poke a closed loop later.
            self._disarm_paho()
            self._stopped.set()
            # Best-effort post-teardown OFFLINE (fresh short-lived client)
            await self._publish_offline_once()

    def _disarm_paho(self):
        """Detach paho's socket callbacks so __del__/GC won't poke a closed loop."""
        try:
            # aiomqtt.Client -> underlying paho.mqtt.client.Client is in ._client
            pc = getattr(self.client, "_client", None)
            if pc is None:
                return
            for cb in (
                "on_socket_open",
                "on_socket_close",
                "on_socket_register_write",
                "on_socket_unregister_write",
            ):
                try:
                    setattr(pc, cb, None)
                except Exception:
                    pass
        except Exception:
            pass
    
    async def shutdown(self):
        """Signal loops to stop and let start() unwind everything cleanly."""
        self._stop.set()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopped.wait(), timeout=3.0)

    async def publish_ha_service(self, domain: str, service: str, data: dict | None = None):
        """
        Enqueue a Home Assistant service call to {base}/ha/service/call.
        """
        payload = json.dumps(
            {"domain": domain, "service": service, "data": data or {}},
            separators=(",", ":"),
        )
        await self._out_q.put((self.topic_service_call, payload))
        
    async def publish_activity_intent(self, activity: str):
        """Publish simple activity intents Pi→HA, e.g. watch|listen|power_off."""
        topic = f"{self.base}/activity"   # e.g. pihub/living_room/activity
        payload = (activity or "").strip().lower()
        if payload:
            await self._out_q.put((topic, payload))
        
    async def publish_json(self, topic: str, obj: dict):
        """
        Publish a JSON object via the existing TX queue (non-retained).
        Usage: await mqtt.publish_json("pihub/<room>/pyatv/state", state_dict)
        """
        payload = json.dumps(obj, separators=(",", ":"))
        await self._out_q.put((topic, payload))

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
            except Exception:
                # Requeue once and exit so start() can reconnect and respawn us
                with contextlib.suppress(Exception):
                    self._out_q.put_nowait((topic, payload))
                return
            finally:
                self._out_q.task_done()

    # ---------- Internal: periodic status publisher ----------

    async def _status_loop(self, c: Client):
        # small initial delay so we don't spam on immediate connect
        await asyncio.sleep(1.0)
        interval = max(5, int(self.cfg.status_interval_sec or 120))
        while not self._stop.is_set():
            # Reassert availability so HA that just restarted will see it
            try:
                await c.publish(self.topic_health, b"online", qos=1, retain=True)
            except Exception as e:
                print(f"[MQTT] health publish error: {e}")
    
            # Gather and publish status snapshot (non-retained)
            data = await asyncio.get_running_loop().run_in_executor(None, self._gather_status)
            try:
                await c.publish(
                    self.topic_status_json,
                    json.dumps(data, separators=(",", ":")),
                    qos=0,
                    retain=False,
                )
            except Exception as e:
                print(f"[MQTT] status publish error: {e}")
    
            # Wait with cancellation support
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _publish_status_once(self, c: Client):
        """Publish one immediate status snapshot (called after connect)."""
        data = await asyncio.get_running_loop().run_in_executor(None, self._gather_status)
        try:
            await c.publish(self.topic_status_json, json.dumps(data, separators=(",", ":")), qos=0, retain=False)
        except Exception:
            return  # quiet on initial connect hiccups
            
    # ---------- Autodiscover Device ----------
            
    async def _publish_discovery(self, c: Client):
        """Publish Home Assistant MQTT Discovery configs (retained)."""
        dev = {
            "identifiers": [self._dev_id],
            "manufacturer": "PiHub",
            "model": "PiHub Remote Bridge",
            "name": self._dev_name,
            "suggested_area": self._room.replace("_"," ").title(),
        }
        avail = [{"topic": self.topic_health, "payload_available": "online", "payload_not_available": "offline"}]
    
        def j(d): import json; return json.dumps(d, separators=(",", ":"))
    
        # Online binary_sensor
        bs_uid = f"pihub_{self._room}_online"
        bs_cfg = {
            "name": f"{self._dev_name} Online",
            "uniq_id": bs_uid,
            "dev_cla": "connectivity",
            "stat_t": self.topic_health,
            "pl_on": "online",
            "pl_off": "offline",
            "avty": avail,
            "dev": dev,
        }
        await c.publish(f"{self._disc_prefix}/binary_sensor/{bs_uid}/config", j(bs_cfg), qos=1, retain=True)
    
        # CPU temp
        s_uid = f"pihub_{self._room}_cpu_temp"
        s_cfg = {
            "name": f"{self._dev_name} CPU Temp",
            "uniq_id": s_uid,
            "stat_t": self.topic_status_json,
            "unit_of_meas": "°C",
            "val_tpl": "{{ value_json.cpu_temp_c }}",
            "avty": avail,
            "dev": dev,
            "icon": "mdi:thermometer",
            "entity_category": "diagnostic",
        }
        await c.publish(f"{self._disc_prefix}/sensor/{s_uid}/config", j(s_cfg), qos=1, retain=True)
    
        # CPU load
        s2_uid = f"pihub_{self._room}_cpu_load"
        s2_cfg = {
            "name": f"{self._dev_name} CPU Load",
            "uniq_id": s2_uid,
            "stat_t": self.topic_status_json,
            "unit_of_meas": "%",
            "val_tpl": "{{ value_json.cpu_load_pct }}",
            "avty": avail,
            "dev": dev,
            "icon": "mdi:chip",
            "entity_category": "diagnostic",
        }
        await c.publish(f"{self._disc_prefix}/sensor/{s2_uid}/config", j(s2_cfg), qos=1, retain=True)
    
        # IP
        s3_uid = f"pihub_{self._room}_ip"
        s3_cfg = {
            "name": f"{self._dev_name} IP",
            "uniq_id": s3_uid,
            "stat_t": self.topic_status_json,
            "val_tpl": "{{ value_json.ip }}",
            "avty": avail,
            "dev": dev,
            "icon": "mdi:ip-network",
        }
        await c.publish(f"{self._disc_prefix}/sensor/{s3_uid}/config", j(s3_cfg), qos=1, retain=True)
        
        # Memory used %
        s_uid = f"pihub_{self._room}_mem_used"
        s_cfg = {
            "name": f"{self._dev_name} Memory Used",
            "uniq_id": s_uid,
            "stat_t": self.topic_status_json,
            "unit_of_meas": "%",
            "val_tpl": "{{ value_json.mem_used_pct }}",
            "avty": avail,
            "dev": dev,
            "icon": "mdi:memory",
            "entity_category": "diagnostic",
        }
        await c.publish(f"{self._disc_prefix}/sensor/{s_uid}/config", j(s_cfg), qos=1, retain=True)
        
        # Disk used %
        s_uid = f"pihub_{self._room}_disk_used"
        s_cfg = {
            "name": f"{self._dev_name} Disk Used",
            "uniq_id": s_uid,
            "stat_t": self.topic_status_json,
            "unit_of_meas": "%",
            "val_tpl": "{{ value_json.disk_used_pct }}",
            "avty": avail,
            "dev": dev,
            "icon": "mdi:harddisk",
            "entity_category": "diagnostic",
        }
        await c.publish(f"{self._disc_prefix}/sensor/{s_uid}/config", j(s_cfg), qos=1, retain=True)
        
        # Uptime (days)
        s_uid = f"pihub_{self._room}_uptime_days"
        s_cfg = {
            "name": f"{self._dev_name} Uptime (Days)",
            "uniq_id": s_uid,
            "stat_t": self.topic_status_json,
            "unit_of_meas": "d",
            "dev_cla": "duration",
            "val_tpl": "{{ (value_json.uptime_sec | float(0) / 86400) | round(2) }}",
            "avty": avail,
            "dev": dev,
            "icon": "mdi:calendar-clock",
            "entity_category": "diagnostic",
        }
        await c.publish(f"{self._disc_prefix}/sensor/{s_uid}/config", j(s_cfg), qos=1, retain=True)
        
        # Hostname
        s_uid = f"pihub_{self._room}_hostname"
        s_cfg = {
            "name": f"{self._dev_name} Hostname",
            "uniq_id": s_uid,
            "stat_t": self.topic_status_json,
            "val_tpl": "{{ value_json.hostname }}",
            "avty": avail,
            "dev": dev,
            "icon": "mdi:server",
        }
        await c.publish(f"{self._disc_prefix}/sensor/{s_uid}/config", j(s_cfg), qos=1, retain=True)
        
        # Undervoltage now (binary)
        bs_uid = f"pihub_{self._room}_undervoltage_now"
        bs_cfg = {
            "name": f"{self._dev_name} Undervoltage (Now)",
            "uniq_id": bs_uid,
            "dev_cla": "problem",
            "stat_t": self.topic_status_json,
            "pl_on": "true",
            "pl_off": "false",
            "val_tpl": "{{ (value_json.undervoltage_now | default(false)) | string | lower }}",
            "avty": avail,
            "dev": dev,
            "icon": "mdi:alert",
            "entity_category": "diagnostic",
        }
        await c.publish(f"{self._disc_prefix}/binary_sensor/{bs_uid}/config", j(bs_cfg), qos=1, retain=True)
        
        # Undervoltage ever (binary)
        bs_uid = f"pihub_{self._room}_undervoltage_ever"
        bs_cfg = {
            "name": f"{self._dev_name} Undervoltage (Ever)",
            "uniq_id": bs_uid,
            "dev_cla": "problem",
            "stat_t": self.topic_status_json,
            "pl_on": "true",
            "pl_off": "false",
            "val_tpl": "{{ (value_json.undervoltage_ever | default(false)) | string | lower }}",
            "avty": avail,
            "dev": dev,
            "icon": "mdi:alert-circle-outline",
            "entity_category": "diagnostic",
        }
        await c.publish(f"{self._disc_prefix}/binary_sensor/{bs_uid}/config", j(bs_cfg), qos=1, retain=True)
        
        # Bluetooth connected count
        s_uid = f"pihub_{self._room}_bt_connected_count"
        s_cfg = {
            "name": f"{self._dev_name} BT Connected",
            "uniq_id": s_uid,
            "stat_t": self.topic_status_json,
            "val_tpl": "{{ value_json.bt_connected_devices.count }}",
            "avty": avail,
            "dev": dev,
            "icon": "mdi:bluetooth",
            "entity_category": "diagnostic",
        }
        await c.publish(f"{self._disc_prefix}/sensor/{s_uid}/config", j(s_cfg), qos=1, retain=True)
        
        # Bluetooth connected MACs (nice-to-have text sensor)
        s_uid = f"pihub_{self._room}_bt_connected_list"
        s_cfg = {
            "name": f"{self._dev_name} BT Devices",
            "uniq_id": s_uid,
            "stat_t": self.topic_status_json,
            "val_tpl": "{{ (value_json.bt_connected_devices.macs | default([])) | join(', ') }}",
            "avty": avail,
            "dev": dev,
            "icon": "mdi:format-list-bulleted",
            "entity_category": "diagnostic",
        }
        
        # HA service call display
        await c.publish(f"{self._disc_prefix}/sensor/{s_uid}/config", j(s_cfg), qos=1, retain=True)
        
        svc_head_uid = f"pihub_{self._room}_ha_service_call"
        svc_head_cfg = {
            "name": f"{self._dev_name} HA Service",
            "uniq_id": svc_head_uid,
            "stat_t": f"{self.base}/ha/service/call",
            "val_tpl": "{{ value_json.domain ~ '.' ~ value_json.service if value_json is defined else '' }}",
            "icon": "mdi:home-assistant",
            "avty": [{"topic": self.topic_health, "payload_available": "online", "payload_not_available": "offline"}],
            "dev": dev,
        }
        await c.publish(f"{self._disc_prefix}/sensor/{svc_head_uid}/config", j(svc_head_cfg), qos=1, retain=True)
        
        # Activity display 
        act_uid = f"pihub_{self._room}_activity_display"
        act_cfg = {
            "name": f"{self._dev_name} Activity",
            "uniq_id": act_uid,
            "stat_t": f"{self.base}/activity",   # e.g. pihub/living_room/activity
            # No cmd_t → read-only
            "avty": [{"topic": self.topic_health, "payload_available": "online", "payload_not_available": "offline"}],
            "dev": dev,
            "icon": "mdi:remote",
        }
        await c.publish(f"{self._disc_prefix}/sensor/{act_uid}/config", j(act_cfg), qos=1, retain=True)


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
        """Best-effort 'offline' publish after teardown, without aiomqtt (no loop)."""
        try:
            import paho.mqtt.client as paho

            def _do():
                cli = paho.Client(protocol=paho.MQTTv311)
                if self.cfg.user:
                    cli.username_pw_set(self.cfg.user, self.cfg.password or "")
                try:
                    cli.connect(self.cfg.host, self.cfg.port, keepalive=5)
                except Exception:
                    return
                try:
                    cli.loop_start()
                    cli.publish(self.topic_health, b"offline", qos=1, retain=True)
                    # small wait so it actually sends before we tear down
                    cli.loop_stop()  # stop will join the network thread
                finally:
                    with contextlib.suppress(Exception):
                        cli.disconnect()

            # run blocking publish in a thread so we don't touch the event loop
            await asyncio.get_running_loop().run_in_executor(None, _do)
        except Exception:
            pass