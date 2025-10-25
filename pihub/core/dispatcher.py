import os
import asyncio
import yaml
from dataclasses import dataclass
from typing import Callable, Dict, Any, Optional

from pihub.macros import atv as macros_atv
from pihub.macros import sys as macros_sys
from pihub.macros import ble as macros_ble

DEBUG_DISPATCH = False

# -----------------
# Activities model
# -----------------
@dataclass
class Activities:
    default: str
    activities: Dict[str, Any]

def load_activities(path: str) -> Activities:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    default = (data.get("defaults") or {}).get("activity", "watch")
    return Activities(default=default, activities=data.get("activities") or {})

async def watch_activities(
    path: str,
    on_reload: Callable[[Activities], None],
    poll: float = 0.5,
):
    """Hot-reload activities.yaml (quiet, no dup logs)."""
    last = None

    # Initial load (once)
    try:
        acts = load_activities(path)
        on_reload(acts)
        try:
            last = os.path.getmtime(path)
        except FileNotFoundError:
            last = None
        print(f"[dispatcher] {len(acts.activities)} activities loaded")
    except FileNotFoundError:
        pass

    while True:
        try:
            m = os.path.getmtime(path)
            if m != last:
                last = m
                acts = load_activities(path)
                on_reload(acts)
                print(f"[dispatcher] {len(acts.activities)} activities reloaded")
        except FileNotFoundError:
            pass
        await asyncio.sleep(poll)

# -----------
# Dispatcher
# -----------
class Dispatcher:
    def __init__(self, hid_client, keymaps, activities: Activities,
                 mqtt=None, on_activity_change: Callable[[str], None] | None = None):
        self.hid = hid_client
        self.keymaps = keymaps
        self.activities = activities
        self.activity = activities.default
        self.mqtt = mqtt
        self.on_activity_change = on_activity_change

        # input handling state
        self._repeat_tasks: dict[str, asyncio.Task] = {}
        self._held_keys: set[str] = set()
        self._hold_tasks: dict[str, asyncio.Task] = {}

        # optional integrations
        self.atv = None            # set in app.py


    def set_keymaps(self, keymaps):
        self.keymaps = keymaps

    def replace_activities(self, activities: Activities):
        """Swap in new Activities; keep current activity if still valid, else fall back to default."""
        old = self.activity
        self.activities = activities
        if old in self.activities.activities:
            # keep current activity
            pass
        else:
            # switch to default if current vanished
            self.set_activity(self.activities.default)
        print(f"[dispatcher] activities updated: {list(self.activities.activities.keys())}")

    def set_activity(self, name: str):
        name = (name or "").strip()
        if not name:
            return
        if getattr(self, "activity", None) == name:
            return  # no change
        self.activity = name
        print(f"[dispatcher] activity→{name}")
        cb = getattr(self, "on_activity_change", None)
        if cb:
            try:
                cb(name)
            except Exception as e:
                print(f"[dispatcher] on_activity_change error: {e}")
                
                
    async def handle_text_command(self, cmd: str) -> None:
        """
        Handle unified command payloads from MQTT: "<category>:<action>"
        Examples: "macro:atv-on", "macro:atv-off"
        - QoS1, non-retained (enforced by the MQTT publisher)
        - Fire-and-forget: no acks returned
        """
        if not cmd:
            print("[dispatcher→mqtt] cmd:rx - payload empty!")
            return
    
        parts = cmd.split(":", 1)
        if len(parts) != 2:
            print(f"[dispatcher→mqtt] cmd:rx - bad format (expected 'cat:action'): {cmd!r}")
            return
    
        cat, action = parts[0].strip().lower(), parts[1].strip().lower()
    
        # --------- category: macro (BLE key macros) ---------
        if cat == "macro":
            if DEBUG_DISPATCH: print(f'[dispatcher→mqtt] cmd:rx - "{cat}:{action}"')
        
            if action == "atv-on":
                if DEBUG_DISPATCH: print("[dispatcher] running macro atv-on…")
                await macros_atv.atv_on(self.hid, ikd_ms=400)
                if DEBUG_DISPATCH: print("[macros] atv-on done")
                return
        
            if action == "atv-off":
                if DEBUG_DISPATCH: print("[dispatcher] running macro atv-off…")
                await macros_atv.atv_off(self.hid, ikd_ms=400)
                if DEBUG_DISPATCH: print("[macros] atv-off done")
                return
        
            if DEBUG_DISPATCH: print(f"[dispatcher→mqtt] cmd:rx - unknown macro: {action!r}")
            return
    
        # --------- category: sys (service/system controls) ---------
        if cat == "sys":
            if DEBUG_DISPATCH: print(f'[dispatcher→mqtt] cmd:rx - "{cat}:{action}"')
        
            if action == "restart-pihub":
                if DEBUG_DISPATCH: print("[dispatcher] running sys restart-pihub…")
                await macros_sys.restart_pihub()
                if DEBUG_DISPATCH: print("[macros] restart-pihub done")
                return
        
            if action == "reboot-pi":
                if DEBUG_DISPATCH: print("[dispatcher] running sys reboot")
                await macros_sys.reboot_host()
                if DEBUG_DISPATCH: print("[macros] reboot-pi done")
                return
        
            if DEBUG_DISPATCH: print(f"[dispatcher→mqtt] cmd:rx - unknown sys: {action!r}")
            return
            
        # --------- category: ble (Bluetooth maintenance) ---------
        if cat == "ble":
            if DEBUG_DISPATCH: print(f'[dispatcher→mqtt] cmd:rx - "{cat}:{action}"')
        
            if action == "unpair-all":
                if DEBUG_DISPATCH: print("[dispatcher] running ble unpair-all")
                await macros_ble.unpair_all(adapter="hci0")
                if DEBUG_DISPATCH: print("[macros] unpair-all done")
                return
        
            if DEBUG_DISPATCH: print(f"[dispatcher→mqtt] cmd:rx - unknown ble: {action!r}")
            return

    
    async def handle(self, logical_name: str, edge: str):
        # Normalize edge names early
        if edge in ("press", "down"):
            edge = "down"
        elif edge in ("release", "up"):
            edge = "up"
        else:
            return  # ignore anything else
    
        act = self.activities.activities.get(self.activity, {})
        mapping = (act.get("map") or {}).get(logical_name)
        if not mapping:
            return
    
        # Track edge state for long-hold gating
        if edge == "down":
            self._held_keys.add(logical_name)
        elif edge == "up":
            self._held_keys.discard(logical_name)
            t = self._hold_tasks.pop(logical_name, None)
            if t:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
    
        actions = mapping if isinstance(mapping, list) else [mapping]
        for a in actions:
            await self._run_action(a, logical_name, edge)
    
    async def _run_action(self, a: dict, logical_name: str, edge: str):
        kind = a.get("do")
    
        # ---- Long-hold gate: only fire if still held after min_hold_ms ----
        min_ms = a.get("min_hold_ms")
        if min_ms is not None:
            if edge == "down":
                async def waiter():
                    try:
                        await asyncio.sleep(min_ms / 1000.0)
                        if logical_name in self._held_keys:
                            gated = dict(a)
                            gated.pop("min_hold_ms", None)
                            await self._run_action(gated, logical_name, "down")
                    except asyncio.CancelledError:
                        pass
    
                t_old = self._hold_tasks.pop(logical_name, None)
                if t_old:
                    t_old.cancel()
                    try:
                        await t_old
                    except asyncio.CancelledError:
                        pass
                t = asyncio.create_task(waiter(), name=f"hold:{logical_name}")
                self._hold_tasks[logical_name] = t
            elif edge == "up":
                t = self._hold_tasks.pop(logical_name, None)
                if t:
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
            return
    
        # 1) BLE HID passthrough (exact down/up mirror)
        if kind == "hid_consumer":
            name = a["name"]
            usage = self.keymaps.consumer.get(name)
            if usage is None:
                return
            if edge == "down":
                if DEBUG_DISPATCH: print(f"[dispatch→hid] consumer DOWN {name} (0x{usage:04X})")
                await self.hid.consumer_down(usage)
            elif edge == "up":
                if DEBUG_DISPATCH: print(f"[dispatch→hid] consumer UP   {name} (→0)")
                await self.hid.consumer_up()
            return
    
        if kind == "hid_keyboard":
            name = a["name"]
            code = self.keymaps.keyboard.get(name)
            if code is None:
                return
            if edge == "down":
                if DEBUG_DISPATCH: print(f"[dispatch→hid] keyboard DOWN {name} (0x{code:02X})")
                await self.hid.key_down(code)
            elif edge == "up":
                if DEBUG_DISPATCH: print(f"[dispatch→hid] keyboard UP   {name}")
                await self.hid.key_up()
            return

        # 2) Home Assistant service (via MQTT), supports repeat on HOLD
        if kind == "ha_service":
            if not self.mqtt:
                return

            domain  = a.get("domain")
            service = a.get("service")
            data    = a.get("data", {})
            rep     = a.get("repeat")

            if rep:
                if edge == "down":
                    # fire once immediately (log it)
                    if DEBUG_DISPATCH: print(f"[dispatch→HA] {logical_name} {edge} -> {domain}.{service} {data}")
                    await self.mqtt.publish_ha_service(domain, service, data)
                    # then start repeat timer (first repeat after initial_ms)
                    await self._start_repeat(
                        logical_name,
                        lambda: self.mqtt.publish_ha_service(domain, service, data),
                        rep,
                    )
                elif edge == "up":
                    await self._stop_repeat(logical_name)
            else:
                # single fire; default to 'up' unless overridden
                when = a.get("when", "up")
                if when == "both" or (when == edge):
                    if DEBUG_DISPATCH: print(f"[dispatch→HA] {logical_name} {edge} -> {domain}.{service} {data}")
                    await self.mqtt.publish_ha_service(domain, service, data)
            return
            
        # 3) Publish a simple activity intent (Pi → HA)
        if kind == "activity_intent":
            if not self.mqtt:
                return
            # default to send on button DOWN (feels snappier), change to 'up' if you prefer
            when = (a.get("when") or "down").lower()
            if when != edge:
                return
            target = (a.get("to") or a.get("name") or "").lower()
            if target in ("watch", "listen", "power_off"):
                await self.mqtt.publish_activity_intent(target)
                if DEBUG_DISPATCH: print(f"[dispatch→HA] intent → {target}")
            else:
                if DEBUG_DISPATCH: print(f"[Dispatch→HA] unknown activity intent: {target!r}")
            return

        # 4) Sleep utility
        if kind == "sleep_ms":
            if edge == "down":
                await asyncio.sleep(a.get("ms", 0) / 1000)
            return

        # 5) Placeholder
        if kind == "noop":
            return

    async def _start_repeat(self, key: str, coro_factory, rep: dict):
        if key in self._repeat_tasks:
            return
        initial = rep.get("initial_ms", 500) / 1000.0
        every   = rep.get("every_ms",   500) / 1000.0

        async def repeater():
            await asyncio.sleep(initial)
            while key in self._repeat_tasks:
                await coro_factory()
                await asyncio.sleep(every)

        t = asyncio.create_task(repeater(), name=f"repeat:{key}")
        self._repeat_tasks[key] = t

    async def _stop_repeat(self, key: str):
        t = self._repeat_tasks.pop(key, None)
        if t:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass