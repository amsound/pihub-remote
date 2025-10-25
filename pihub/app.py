#!/usr/bin/env python3
import asyncio
import signal
import sys
import json
import time
import inspect
import os
import hashlib
import contextlib

from pathlib import Path
from typing import Tuple, Callable, Awaitable

from pihub.bt_le.hid_device import start_hid
from pihub.bt_le.hid_client import HIDClient

from pihub.core.keymaps import load_keymaps, watch_keymaps
from pihub.core.remote_evdev import load_remote_config, read_events_scancode
from pihub.core.dispatcher import Dispatcher, load_activities, watch_activities, Activities
from pihub.core.config import load_room_config
from pihub.ha_mqtt.mqtt_bridge import MqttBridge, MqttConfig

# -------------------
# Paths / Config files
# -------------------
PKG_DIR = Path(__file__).resolve().parent
CONFIG_DIR = PKG_DIR / "config"

HID_KEYMAP_PATH    = CONFIG_DIR / "hid_keymap.yaml"
REMOTE_KEYMAP_PATH = CONFIG_DIR / "remote_keymap.yaml"
ACTIVITIES_PATH    = CONFIG_DIR / "activities.yaml"

# --- low-latency input→dispatch pipeline (top-level) ---
EVENT_QUEUE_MAX = 128
evt_q: asyncio.Queue[Tuple[str, str, int]] = asyncio.Queue(maxsize=EVENT_QUEUE_MAX)

# enqueue-only; never block the reader
async def on_button(name: str, edge: str) -> None:
    t_in = time.monotonic_ns()
    try:
        evt_q.put_nowait((name, edge, t_in))
    except asyncio.QueueFull:
        try:
            _ = evt_q.get_nowait()
            evt_q.task_done()
        except asyncio.QueueEmpty:
            pass
        await evt_q.put((name, edge, t_in))

DISPATCH_TIMEOUT_MS = 300
LOG_EVERY = int(os.getenv("PIHUB_LOG_EVERY", "0"))  # 0 disables sampling

# inject the handler (no global `dispatcher` here)
async def ble_sender_worker(handle_fn: Callable[[str, str], Awaitable | None]) -> None:
    count = 0
    while True:
        name, edge, t_in = await evt_q.get()
        try:
            res = handle_fn(name, edge)  # may be sync or async
            if inspect.isawaitable(res):
                await asyncio.wait_for(res, DISPATCH_TIMEOUT_MS / 1000)
            if LOG_EVERY:
                count += 1
                if count % LOG_EVERY == 0:
                    t_out = time.monotonic_ns()
                    print(f"[lat] {name}/{edge}: {(t_out - t_in)/1000:.1f} µs")
        except asyncio.TimeoutError:
            print(f"[dispatch] timeout {name}/{edge} after {DISPATCH_TIMEOUT_MS} ms")
        except Exception as e:
            print(f"[dispatch] error {name}/{edge}: {e}")
        finally:
            evt_q.task_done()

async def main():
    # ── 1) Room config ──────────────────────────────────────────────────────────
    room_path = CONFIG_DIR / "room.yaml"
    try:
        room_cfg = load_room_config(room_path)
    except FileNotFoundError:
        # Fail fast with a clear, actionable message; exit code 78 = configuration error
        print(f'[app] missing "room.yaml" at: {room_path} → create this file and restart')
        sys.exit(78)
    
    def _pretty_room(name: str) -> str:
        s = (name or "").replace("_", " ").replace("-", " ").strip()
        return s.title()
    _room_pretty = _pretty_room(room_cfg.room)
    print(f'[app] "{_room_pretty}" config loaded')

    # ── 2) Load HID keymaps (initial) ──────────────────────────────────────────
    km = load_keymaps(str(HID_KEYMAP_PATH))
    print(f"[app] hid keymaps: {len(km.consumer)} consumer, {len(km.keyboard)} kb")

    # Debounce state for keymaps
    def _km_digest(km_obj) -> str:
        payload = {
            "kb": sorted(km_obj.keyboard.items()),
            "cc": sorted(km_obj.consumer.items()),
        }
        return hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    km_last = _km_digest(km)
    km_debounce_task: asyncio.Task | None = None
    km_lock = asyncio.Lock()

    async def _km_apply(new):
        nonlocal km, km_last
        new_hash = _km_digest(new)
        if new_hash == km_last:
            return
        km = new
        km_last = new_hash
        print(f"[app] hid keymaps reloaded: {len(km.consumer)} consumer, {len(km.keyboard)} kb")

    def on_km_reload(new):
        nonlocal km_debounce_task
        if km_debounce_task and not km_debounce_task.done():
            return
        async def _debounced():
            async with km_lock:
                await asyncio.sleep(0.10)
                await _km_apply(new)
        km_debounce_task = asyncio.create_task(_debounced(), name="km_reload")

    km_task = asyncio.create_task(
        watch_keymaps(str(HID_KEYMAP_PATH), on_km_reload),
        name="watch_keymaps"
    )

    # ── 3) Dispatcher (seed with Noop HID; we’ll attach real HID later) ─────────
    class NoopHID:
        async def key_down(self, *_args, **_kw): pass
        async def key_up(self): pass
        async def consumer_down(self, *_args, **_kw): pass
        async def consumer_up(self): pass
        async def consumer_tap(self, *_args, **_kw):
            # no-op placeholder until BLE HID consumer support is wired up
            pass

    dispatcher = Dispatcher(
        hid_client=NoopHID(),
        keymaps=km,
        activities=Activities(default="null", activities={}),
    )
    dispatcher.set_activity("null")
    print("[app] activity set to null, waiting for mqtt")

    # ── 4) Activities (seed + debounced hot reload) ────────────────────────────
    acts = load_activities(str(ACTIVITIES_PATH))
    dispatcher.activities = acts

    def _acts_digest(acts_obj: Activities) -> str:
        payload = {
            "default": acts_obj.default,
            "acts": {k: acts_obj.activities[k] for k in sorted(acts_obj.activities)},
        }
        return hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    acts_last = _acts_digest(acts)
    acts_debounce_task: asyncio.Task | None = None
    acts_lock = asyncio.Lock()
    _skip_first_acts_reload = True

    async def _acts_apply(new_acts):
        nonlocal acts_last
        new_hash = _acts_digest(new_acts)
        if new_hash == acts_last:
            return
        dispatcher.activities = new_acts
        acts_last = new_hash
        names2 = sorted(new_acts.activities.keys())
        print(f"[app] {len(names2)} activities reloaded")
        print(f"[app] activities updated: {names2}")

    def on_acts_reload(new_acts):
        nonlocal acts_debounce_task, _skip_first_acts_reload
        if _skip_first_acts_reload:
            _skip_first_acts_reload = False
            return
        if acts_debounce_task and not acts_debounce_task.done():
            return
        async def _debounced():
            async with acts_lock:
                await asyncio.sleep(0.10)
                await _acts_apply(new_acts)
        acts_debounce_task = asyncio.create_task(_debounced(), name="acts_reload")

    acts_task = asyncio.create_task(
        watch_activities(str(ACTIVITIES_PATH), on_reload=on_acts_reload),
        name="watch_activities"
    )

    # ── 5) New Command handler ──
    async def on_cmd(cmd: str):
        await dispatcher.handle_text_command(cmd)

    # ── 6) MQTT bridge (early, so HA state arrives fast) ───────────────────────
    if not room_cfg.room:
        raise RuntimeError("room: <name> is required in room.yaml (e.g. room: living_room)")
    activity_obj_id = f"{room_cfg.room}_activity"

    mqtt = MqttBridge(
        MqttConfig(
            host=room_cfg.mqtt_host,
            port=room_cfg.mqtt_port,
            user=room_cfg.mqtt_user,
            password=room_cfg.mqtt_password,
            prefix_bridge=room_cfg.prefix_bridge,
            input_select_entity=activity_obj_id,
        ),
        on_activity_state=lambda a: dispatcher.set_activity(a),
        on_command=on_cmd,
    )
    dispatcher.mqtt = mqtt
    dispatcher.on_activity_change = None
    asyncio.create_task(mqtt.start(lambda: dispatcher.activity), name="mqtt")

    # ── 7) Bring up BLE HID (optional), then attach to dispatcher ─────────────
    shutdown = None  # will be set for cleanup
    
    if room_cfg.bt_enabled:
        class MiniConfig:
            device_name = room_cfg.bt_device_name or room_cfg.device_name
            appearance  = 0x03C1  # keyboard
    
        runtime, shutdown = await start_hid(MiniConfig())
    
        # Wire HID client into dispatcher
        hid = HIDClient(runtime.hid)
        dispatcher.hid = hid
    else:
        async def shutdown():
            pass  # no-op when BLE disabled
        print("[app] bluetooth disabled via config; running without BLE HID")

    # ── 8) Remote reader (evdev) ──────────────────────────────────────────────
    rcfg = load_remote_config(str(REMOTE_KEYMAP_PATH))
    
    stop_ev = asyncio.Event()
    # Track ble-sender worker so we can cancel/await it on shutdown
    ble_sender_task = asyncio.create_task(ble_sender_worker(dispatcher.handle), name="ble-sender")
    
    # start the evdev reader (non-blocking on on_button)
    remote_task = asyncio.create_task(
        read_events_scancode(
            rcfg,
            on_button,          # enqueues only; fast
            stop_event=stop_ev,
            msc_only=False,     # keep KEY fast-path; MSC fallback for 9 codes
            debug_unmapped=False,
            debug_trace=False,
        ),
        name="read_events",
    )
    print("[app] usb remote reader started")

    # ── 9) Signals / lifecycle -───────────────────────────────────────────────
    app_tasks = [t for t in (ble_sender_task, remote_task, km_task, acts_task, km_debounce_task, acts_debounce_task) if t]
    
    _shutting_down = False
    async def shutdown_all() -> None:
        nonlocal _shutting_down
        if _shutting_down:
            return
        _shutting_down = True
    
        # Tell cooperative loops to exit
        stop_ev.set()
    
        # Cancel background tasks
        for t in list(app_tasks):
            if t and not t.done():
                t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*[t for t in app_tasks if t], return_exceptions=True)
    
        # Orderly teardown: MQTT first, then BLE HID stack
        with contextlib.suppress(Exception):
            await mqtt.shutdown()
        with contextlib.suppress(Exception):
            # 'shutdown' is the coroutine returned by start_hid(MiniConfig())
            await shutdown()
    
        print("[app] clean shutdown")
    
    # Wire signals to schedule shutdown (non-blocking handler)
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT,  lambda: asyncio.create_task(shutdown_all()))
    loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(shutdown_all()))
    
    print("[app] startup complete")
    
    # Keep running until a signal arrives (or you set stop_ev elsewhere)
    try:
        await stop_ev.wait()
    finally:
        await shutdown_all()

if __name__ == "__main__":
    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except Exception:
        pass

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)