#!/usr/bin/env python3
import asyncio
import signal
import sys
import json
from pathlib import Path
import hashlib
import contextlib

from pihub.bt_le.hid_device import start_hid
from pihub.bt_le.hid_client import HIDClient

from pihub.core.keymaps import load_keymaps, watch_keymaps
from pihub.core.remote_evdev import load_remote_config, read_events_scancode
from pihub.core.dispatcher import Dispatcher, load_activities, watch_activities, Activities
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

async def main():
    # ── 1) Room config ──────────────────────────────────────────────────────────
    room_cfg = load_room_config(CONFIG_DIR / "room.yaml")

    # ── 2) Load HID keymaps (initial) ──────────────────────────────────────────
    km = load_keymaps(str(HID_KEYMAP_PATH))
    print(f"[Keymaps] Loaded: {len(km.keyboard)} kb, {len(km.consumer)} cc")

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
        print(f"[Keymaps] Reloaded: {len(km.keyboard)} Keyboard, {len(km.consumer)} Consumer")

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
        def key_down(self, *_args, **_kw): pass
        def key_up(self): pass
        def consumer_down(self, *_args, **_kw): pass
        def consumer_up(self): pass

    dispatcher = Dispatcher(
        hid_client=NoopHID(),
        keymaps=km,
        activities=Activities(default="null", activities={}),
    )
    dispatcher.set_activity("null")
    print("[State] Activity set to null (waiting to receive state from HA)")

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
        print(f"[Activities] Reloaded ({len(names2)} sections)")
        print(f"[Dispatch] Activities updated: {names2}")

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

    # ── 5) Command handler (uses dispatcher.hid so it works before/after BLE) ──
    async def on_cmd(name: str, payload: bytes):
        # Expect topics under: pihub/<room>/cmd/...
        # e.g. atv/on  or  atv/off  (optional: {"ikd_ms": 400})
        if name == "atv/on":
            ikd = 400
            try:
                obj = json.loads((payload or b"").decode() or "{}")
                ikd = int(obj.get("ikd_ms", ikd))
            except Exception:
                pass
            asyncio.create_task(atv.atv_on(dispatcher.hid, ikd_ms=ikd))
            print("[macros] queued atv/on")
            return

        if name == "atv/off":
            ikd = 400
            try:
                obj = json.loads((payload or b"").decode() or "{}")
                ikd = int(obj.get("ikd_ms", ikd))
            except Exception:
                pass
            asyncio.create_task(atv.atv_off(dispatcher.hid, ikd_ms=ikd))
            print("[macros] queued atv/off")
            return

        print(f"[macros] unknown cmd: {name}")

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
        on_command=lambda name, payload: asyncio.create_task(on_cmd(name, payload)),
    )
    print(f"[mqtt] will subscribe → {room_cfg.prefix_bridge.rstrip('/')}/input_select/{activity_obj_id}/state")
    dispatcher.mqtt = mqtt
    dispatcher.on_activity_change = None
    asyncio.create_task(mqtt.start(lambda: dispatcher.activity), name="mqtt")

    # ── 7) Bring up BLE HID (optional), then attach to dispatcher ─────────────
    #     (do it *after* MQTT so /cmd macros are available immediately)
    shutdown = None  # set below for cleanup
    if room_cfg.bt_enabled:
        class MiniConfig:
            device_name = room_cfg.bt_device_name or room_cfg.device_name
            appearance  = 0x03C1  # keyboard

        runtime, shutdown = await start_hid(MiniConfig(), enable_console=False)
        hid = HIDClient(runtime.hid)
        dispatcher.hid = hid  # <- attach real HID now
        print(f"[BT] enabled (advertising as {MiniConfig.device_name})")
    else:
        async def shutdown():
            pass
        print("[BT] disabled via config; running without BLE HID")

    # ── 8) Remote reader (evdev) ──────────────────────────────────────────────
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
            msc_only=True,
            debug_unmapped=False,
            debug_trace=False,
        ),
        name="read_events",
    )
    print("[App] Remote Reader started")

    # ── 9) Apple TV (optional; last) ──────────────────────────────────────────
    atv_service = None

    def _publish_atv_state(state: dict):
        print(f"[pyatv→mqtt] {state}")
        topic = f"{room_cfg.prefix_bridge}/pyatv/state"
        asyncio.create_task(mqtt.publish_json(topic, state))

    if room_cfg.pyatv_enabled:
        try:
            from pihub.pyatv.atv_service import AppleTvService, PyAtvCreds
            atv_service = AppleTvService(
                PyAtvCreds(
                    address   = room_cfg.pyatv_address,
                    companion = room_cfg.pyatv_companion,
                    airplay   = room_cfg.pyatv_airplay,
                ),
                on_state=_publish_atv_state,
            )
            asyncio.create_task(atv_service.start(), name="pyatv")
            dispatcher.atv = atv_service
        except ModuleNotFoundError as e:
            print(f"[pyatv] disabled or missing dependency: {e}")

    # ── 10) Signals / lifecycle ───────────────────────────────────────────────
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
        # orderly teardown (MQTT first, then BLE)
        stop_ev.set()
        for t in (remote_task, km_task, acts_task, km_debounce_task, acts_debounce_task):
            if t:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t
        if atv_service:
            await atv_service.stop()
        await mqtt.shutdown()
        await shutdown()
        print("[PiHub] Clean shutdown.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)