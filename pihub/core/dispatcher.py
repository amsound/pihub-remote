import os
import asyncio
import yaml
from dataclasses import dataclass
from typing import Callable, Dict, Any, Optional

DEBUG_HID = True  # leave as-is if you already have it

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
        print(f"[Activities] reloaded ({len(acts.activities)} sections)")
    except FileNotFoundError:
        pass

    while True:
        try:
            m = os.path.getmtime(path)
            if m != last:
                last = m
                acts = load_activities(path)
                on_reload(acts)
                print(f"[Activities] reloaded ({len(acts.activities)} sections)")
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
        print(f"[Dispatch] activities updated: {list(self.activities.activities.keys())}")

    def set_activity(self, name: str):
        name = (name or "").strip()
        if not name:
            return
        if getattr(self, "activity", None) == name:
            return  # no change
        self.activity = name
        print(f"[Dispatch] activity → {name}")
        cb = getattr(self, "on_activity_change", None)
        if cb:
            try:
                cb(name)
            except Exception as e:
                print(f"[Dispatch] on_activity_change error: {e}")

    
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
                if DEBUG_HID: print(f"[dispatch→hid] consumer DOWN {name} (0x{usage:04X})")
                self.hid.consumer_down(usage)
            elif edge == "up":
                if DEBUG_HID: print(f"[dispatch→hid] consumer UP   {name} (→0)")
                self.hid.consumer_up()
            return
    
        if kind == "hid_keyboard":
            name = a["name"]
            code = self.keymaps.keyboard.get(name)
            if code is None:
                return
            if edge == "down":
                if DEBUG_HID: print(f"[dispatch→hid] keyboard DOWN {name} (0x{code:02X})")
                self.hid.key_down(code)
            elif edge == "up":
                if DEBUG_HID: print(f"[dispatch→hid] keyboard UP   {name}")
                self.hid.key_up()
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
                    print(f"[Dispatch→HA] {logical_name} {edge} -> {domain}.{service} {data}")
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
                    print(f"[Dispatch→HA] {logical_name} {edge} -> {domain}.{service} {data}")
                    await self.mqtt.publish_ha_service(domain, service, data)
            return
        # 3) Activity switching (PiHub requests HA to change via command topic)
        if kind == "set_activity":
            if edge == "down" and self.mqtt:
                target = a["to"]
                await self.mqtt.publish_activity_command(target)
                # Do NOT set self.activity here; wait for HA state echo on '.../activity'
            return
            
        # 4) Apple TV via pyATV (semantic gestures)
        if kind == "atv":
            svc = getattr(self, "atv", None)
            if not svc:
                print("[atv] not enabled (dispatcher.atv missing)")
                return

            # Defaults
            cmd      = (a.get("cmd") or "tap").lower()          # tap | hold | double
            key      = (a.get("key") or a.get("name") or "").lower()
            when     = (a.get("when") or "up").lower()
            delay_ms = int(a.get("delay_ms") or 0)
            hold_ms  = int(a.get("hold_ms") or 0)

            if not key:
                print("[atv] missing 'key' in action")
                return

            # Otherwise: direct call, respecting 'when'
            if when != edge:
                return

            try:
                if cmd == "tap":
                    await svc.tap(key)
                elif cmd == "hold":
                    await svc.hold(key, ms=(hold_ms or None))
                elif cmd in ("double", "doubletap"):
                    await svc.double(key)
                else:
                    print(f"[atv] unknown cmd '{cmd}'")
                    return

                if delay_ms > 0:
                    await asyncio.sleep(delay_ms / 1000.0)
            except Exception as e:
                print(f"[atv] error: {e}")
            return

        # 5) Sleep utility
        if kind == "sleep_ms":
            if edge == "down":
                await asyncio.sleep(a.get("ms", 0) / 1000)
            return

        # 6) Placeholder
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