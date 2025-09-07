#!/usr/bin/env python3
import asyncio
import signal
import sys
from pathlib import Path

from pihub.bt_le.hid_device import start_hid
from pihub.bt_le.hid_client import HIDClient

from pihub.core.keymaps import load_keymaps, watch_keymaps
from pihub.core.remote_evdev import load_remote_config, read_events_scancode
from pihub.core.dispatcher import Dispatcher, load_activities, watch_activities
from pihub.core.config import load_room_config
from pihub.ha_mqtt.mqtt_bridge import MqttBridge, MqttConfig
from pihub.macros import atv

# -------------------
# Paths / Config files
# -------------------
PKG_DIR = Path(__file__).resolve().parent
CONFIG_DIR = PKG_DIR / "config"

HID_KEYMAP_PATH    = CONFIG_DIR / "hid_keymap.yaml"
REMOTE_KEYMAP_PATH = CONFIG_DIR / "remote_keymap.yaml"
ACTIVITIES_PATH    = CONFIG_DIR / "activities.yaml"

_WATCH_ACTS_TASK = None  # guard: ensure we start the activities watcher only once

async def main():
    # --- room config ---
    room_cfg = load_room_config(CONFIG_DIR / "room.yaml")
    
    # --- expansion context for activities.yaml ---
    ctx = {
        "room": room_cfg.room,
        "mqtt": {
            "prefix_bridge": room_cfg.prefix_bridge,
        },
        # optional: passthrough of any entity ids you put in room.yaml
        "entities": getattr(room_cfg, "entities", {}) or {},
    }

    # --- bring up BLE HID ---
    class MiniConfig:
        device_name = room_cfg.device_name
        appearance  = 0x03C1

    runtime, shutdown = await start_hid(MiniConfig(), enable_console=False)
    hid = HIDClient(runtime.hid)

    # --- load HID keymaps (initial) ---
    km = load_keymaps(str(HID_KEYMAP_PATH))
    print(f"[Keymaps] Loaded: {len(km.keyboard)} kb, {len(km.consumer)} cc")

    # Debounce state for keymaps
    import hashlib, json, asyncio
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
        print(f"[Keymaps] Reloaded: {len(km.keyboard)} Keyboard, {len(km.consumer)} Consumer")

    def on_km_reload(new):
        # collapse bursts into one apply
        nonlocal km_debounce_task
        if km_debounce_task and not km_debounce_task.done():
            return
        async def _debounced():
            async with km_lock:
                await asyncio.sleep(0.10)  # 100 ms debounce window
                await _km_apply(new)
        km_debounce_task = asyncio.create_task(_debounced(), name="km_reload")

    km_task = asyncio.create_task(
        watch_keymaps(str(HID_KEYMAP_PATH), on_km_reload),
        name="watch_keymaps"
    )

    # --- command handler (HA -> PiHub sequences) ---
    async def on_cmd(name: str, payload: bytes):
        # Expect exactly these topics:
        # pihub/<room>/cmd/atv/on
        # pihub/<room>/cmd/atv/off
        if name == "atv/on":
            # optional: allow JSON payload {"ikd_ms": 400}
            ikd = 400
            try:
                import json
                obj = json.loads(payload.decode() or "{}")
                ikd = int(obj.get("ikd_ms", ikd))
            except Exception:
                pass
            asyncio.create_task(atv.atv_on(hid, ikd_ms=ikd))
            print("[macros] queued atv/on")
            return
    
        if name == "atv/off":
            ikd = 400
            try:
                import json
                obj = json.loads(payload.decode() or "{}")
                ikd = int(obj.get("ikd_ms", ikd))
            except Exception:
                pass
            asyncio.create_task(atv.atv_off(hid, ikd_ms=ikd))
            print("[macros] queued atv/off")
            return
    
        # Unknown command: just log
        print(f"[macros] unknown cmd: {name}")

    # --- dispatcher with 'null' activity until HA state arrives ---
    from pihub.core.dispatcher import Activities
    dispatcher = Dispatcher(
        hid_client=hid,
        keymaps=km,
        activities=Activities(default="null", activities={}),
    )
    dispatcher.set_activity("null")
    print("[State] Activity set to null (waiting to receive state from HA)")

    # --- substitutions for ${…} in activities.yaml (built from room.yaml) ---
    subs = {
        "room": room_cfg.room,
        "prefix_bridge": room_cfg.prefix_bridge,
    }
    # flatten entities.* into subs
    if hasattr(room_cfg, "entities") and isinstance(room_cfg.entities, dict):
        for k, v in room_cfg.entities.items():
            subs[f"entities.{k}"] = v
    # optional legacy key if you still reference ${radio_script}
    if getattr(room_cfg, "radio_script", None):
        subs["radio_script"] = room_cfg.radio_script
    
    # --- activities (seed silently, then watch) ---
    acts = load_activities(str(ACTIVITIES_PATH), subs=subs)
    dispatcher.activities = acts
    acts_last = tuple(sorted(acts.activities.keys()))
    
    def on_acts_reload(new_acts):
        nonlocal acts_last
        dispatcher.activities = new_acts
        digest = tuple(sorted(new_acts.activities.keys()))
        if digest != acts_last:
            print(f"[Dispatch] Activities updated: {list(digest)}")
            acts_last = digest
    
    acts_task = asyncio.create_task(
        watch_activities(str(ACTIVITIES_PATH), on_reload=on_acts_reload, subs=subs),
        name="watch_activities",
    )
    
    # --- hot-reload watcher (pass subs) ---
    def on_acts_reload(new_acts):
        dispatcher.activities = new_acts
        print(f"[Dispatch] Activities updated: {sorted(new_acts.activities.keys())}")
    
    acts_task = asyncio.create_task(
        watch_activities(str(ACTIVITIES_PATH), on_reload=on_acts_reload, subs=subs),
        name="watch_activities",
    )

    # Debounce + skip the very first watcher fire
    import hashlib, json, asyncio
    def _acts_digest(acts_obj: Activities) -> str:
        payload = {
            "default": acts_obj.default,
            "acts": {k: acts_obj.activities[k] for k in sorted(acts_obj.activities)},
        }
        return hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    acts_last = _acts_digest(acts)
    acts_debounce_task: asyncio.Task | None = None
    acts_lock = asyncio.Lock()
    _skip_first_acts_reload = True  # <— NEW: skip the first watcher event

    async def _acts_apply(new_acts):
        nonlocal acts_last
        new_hash = _acts_digest(new_acts)
        if new_hash == acts_last:
            return
        dispatcher.activities = new_acts
        acts_last = new_hash
        names2 = sorted(new_acts.activities.keys())
        print(f"[Activities] Reloaded ({len(names2)} sections)")
        print(f"[Dispatch] Activities updated: {names2}")

    def on_acts_reload(new_acts):
        nonlocal acts_debounce_task, _skip_first_acts_reload
        # Skip the first watcher callback after startup
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
        watch_activities(str(ACTIVITIES_PATH), on_reload=on_acts_reload, subs=subs),
        name="watch_activities"
    )

    # --- MQTT bridge (HA is the single publisher of activity state) ---
    
    def _obj_id(eid: str) -> str:
        # "input_select.living_room_activity" -> "living_room_activity"
        return eid.split(".", 1)[1] if isinstance(eid, str) and "." in eid else eid
    
    activity_obj_id = _obj_id(room_cfg.entities.get("input_select_activity", "living_room_activity"))
    
    mqtt = MqttBridge(
        MqttConfig(
            host=room_cfg.mqtt_host,
            port=room_cfg.mqtt_port,
            user=room_cfg.mqtt_user,
            password=room_cfg.mqtt_password,
            prefix_bridge=room_cfg.prefix_bridge,
            input_select_entity=activity_obj_id,   # IMPORTANT: object_id only
            substitutions={
                "entities.speakers": room_cfg.entities.get("speakers", ""),
                "radio_script": room_cfg.entities.get("radio_script", room_cfg.entities.get("speakers", "")),
            },
        ),
        on_activity_state=lambda a: dispatcher.set_activity(a),
        on_command=lambda name, payload: on_cmd(name, payload),
    )
    
    # (optional debug so you can see exactly what topic we’re on)
    print(f"[mqtt] will subscribe → {room_cfg.prefix_bridge.rstrip('/')}/input_select/{activity_obj_id}/state")
    
    dispatcher.mqtt = mqtt
    dispatcher.on_activity_change = None  # Pi never echoes state to HA

    # Start MQTT (creates internal rx/tx tasks; returns immediately)
    asyncio.create_task(mqtt.start(lambda: dispatcher.activity), name="mqtt")
    
    # --- remote (scancode-only) ---
    rcfg = load_remote_config(str(REMOTE_KEYMAP_PATH))
    
    # --- remote (scancode-only) ---
    rcfg = load_remote_config(str(REMOTE_KEYMAP_PATH))
    
    async def on_button(name, edge):
        print(f"[remote] {name} {'press' if edge=='down' else 'release'}")
        await dispatcher.handle(name, edge)
    
    stop_ev = asyncio.Event()
    remote_task = asyncio.create_task(
        read_events_scancode(
            rcfg,
            on_button,
            stop_event=stop_ev,
            # DEBUG knobs for this test run:
            msc_only=True,          # keep strict MSC first (your preference)
            debug_unmapped=False,    # show if we’re seeing IDs that aren’t mapped
            debug_trace=False,       # dump MSC_SCAN + key edges
        ),
        name="read_events",
    )
    print("[App] Remote Reader started")

    # --- signals / lifecycle ---
    loop = asyncio.get_running_loop()
    stop = loop.create_future()

    def _request_stop():
        if not stop.done():
            stop.set_result(None)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _request_stop)

    print("[PiHub] Ready: BLE + Remote + Activities + MQTT.")
    try:
        await stop
    finally:
        # orderly teardown
        stop_ev.set()
        import contextlib
        for t in (remote_task, km_task, acts_task, km_debounce_task, acts_debounce_task):
            if t:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t
        await mqtt.shutdown()
        await shutdown()
        print("[PiHub] Clean shutdown.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)