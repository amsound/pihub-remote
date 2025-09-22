#!/usr/bin/env python3
import asyncio
import sys
import os
import time
import yaml
import signal
import contextlib

from bluez_peripheral.util import get_message_bus, Adapter, is_bluez_available
from bluez_peripheral.advert import Advertisement
from bluez_peripheral.agent import NoIoAgent
from bluez_peripheral.gatt.service import Service, ServiceCollection
from bluez_peripheral.gatt.characteristic import characteristic, CharacteristicFlags as CharFlags
from bluez_peripheral.gatt.descriptor import DescriptorFlags as DescFlags

from dbus_fast.constants import MessageType
from dbus_fast import Variant

from dataclasses import dataclass


# --------------------------
# Connection state for console
# --------------------------
_connected_event = asyncio.Event()

# --------------------------
# Device identity / advert
# --------------------------

APPEARANCE   = 0x03C1  # Keyboard

# Report IDs
RID_KEYBOARD = 0x01
RID_CONSUMER = 0x02

# --------------------------
# Keyboard usages (page 0x07)
# --------------------------

# Path to external YAML map
KEYMAP_PATH = os.path.join(os.path.dirname(__file__), "keymap.yaml")

# Dicts that will be filled from YAML
NAME_TO_KEY = {}
NAME_TO_CC_USAGE = {}

# Minimal fallbacks (only used if YAML missing or not found)
DEFAULT_NAME_TO_KEY = {
    "enter": 0x28,  # Enter
    "esc":   0x29,  # Escape
}

DEFAULT_NAME_TO_CC_USAGE = {
    "play_pause": 0x00CD,  # Play/Pause
    "vol_up":     0x00E9,  # Volume Up
}

# --------------------------
# HID Report Map (Keyboard + Consumer bitfield)
# --------------------------
REPORT_MAP = bytes([
    # Keyboard (Boot) – Report ID 1
    0x05,0x01, 0x09,0x06, 0xA1,0x01,
      0x85,0x01,              # REPORT_ID (1)
      0x05,0x07,              #   USAGE_PAGE (Keyboard)
      0x19,0xE0, 0x29,0xE7,   #   USAGE_MIN/MAX (modifiers)
      0x15,0x00, 0x25,0x01,   #   LOGICAL_MIN 0 / MAX 1
      0x75,0x01, 0x95,0x08,   #   REPORT_SIZE 1, COUNT 8  (mod bits)
      0x81,0x02,              #   INPUT (Data,Var,Abs)
      0x95,0x01, 0x75,0x08,   #   reserved byte
      0x81,0x01,              #   INPUT (Const,Array,Abs)
      0x95,0x06, 0x75,0x08,   #   6 keys
      0x15,0x00, 0x25,0x65,   #   key range 0..0x65
      0x19,0x00, 0x29,0x65,   #   USAGE_MIN/MAX (keys)
      0x81,0x00,              #   INPUT (Data,Array,Abs)
    0xC0,

    # Consumer Control – 16‑bit *array* usage (Report ID 2) — 2‑byte value
    0x05,0x0C, 0x09,0x01, 0xA1,0x01,
      0x85,0x02,              # REPORT_ID (2)
      0x15,0x00,              # LOGICAL_MIN 0
      0x26,0xFF,0x03,         # LOGICAL_MAX 0x03FF
      0x19,0x00,              # USAGE_MIN 0x0000
      0x2A,0xFF,0x03,         # USAGE_MAX 0x03FF
      0x75,0x10,              # REPORT_SIZE 16
      0x95,0x01,              # REPORT_COUNT 1 (one slot)
      0x81,0x00,              # INPUT (Data,Array,Abs)
    0xC0,
])

def kb_payload(keys=(), modifiers=0):
    keys = list(keys)[:6] + [0]*(6-len(keys))
    return bytes([modifiers, 0] + keys)  # 8 bytes

def cc_payload_usage(usage_id: int) -> bytes:
    # 16-bit consumer usage in little-endian
    return bytes([usage_id & 0xFF, (usage_id >> 8) & 0xFF])
    
#####
async def _adv_unregister(advert):
    """Best-effort: prefer unregister(); fall back to stop()."""
    try:
        if hasattr(advert, "unregister"):
            await advert.unregister()
        else:
            await advert.stop()
    except Exception as e:
        print(f"[hid] adv unregister error: {e!r}")


async def _adv_register_and_start(bus, advert):
    """Best-effort: (re)register then start; tolerate already-registered."""
    try:
        # bluez_peripheral expects the dbus-fast connection here
        if hasattr(advert, "register"):
            await advert.register(bus)
        await advert.start()
    except Exception as e:
        print(f"[hid] adv register/start error: {e!r}")
#####

# --------------------------
# BlueZ object manager helpers
# --------------------------
def _get_bool(v):  # unwrap dbus_next.Variant or use raw bool
    return bool(v.value) if isinstance(v, Variant) else bool(v)


async def trust_device(bus, device_path):
    """Set org.bluez.Device1.Trusted = True for the connected peer."""
    try:
        root_xml = await bus.introspect("org.bluez", device_path)
        dev_obj = bus.get_proxy_object("org.bluez", device_path, root_xml)
        props = dev_obj.get_interface("org.freedesktop.DBus.Properties")
        await props.call_set("org.bluez.Device1", "Trusted", Variant("b", True))
    except Exception:
        pass
async def _get_managed_objects(bus):
    root_xml = await bus.introspect("org.bluez", "/")
    root = bus.get_proxy_object("org.bluez", "/", root_xml)
    om = root.get_interface("org.freedesktop.DBus.ObjectManager")
    return await om.call_get_managed_objects()

async def wait_for_any_connection(bus, poll_interval=0.25):
    """Return Device1 path once Connected=True (we then wait for ServicesResolved)."""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    # quick poll
    objs = await _get_managed_objects(bus)
    for path, props in objs.items():
        dev = props.get("org.bluez.Device1")
        if dev and _get_bool(dev.get("Connected", False)):
            return path

    def handler(msg):
        if msg.message_type is not MessageType.SIGNAL:
            return
        if msg.member == "InterfacesAdded":
            obj_path, ifaces = msg.body
            dev = ifaces.get("org.bluez.Device1")
            if dev and _get_bool(dev.get("Connected", False)) and not fut.done():
                fut.set_result(obj_path)
        elif msg.member == "PropertiesChanged" and msg.path:
            iface, changed, _ = msg.body
            if iface == "org.bluez.Device1" and "Connected" in changed and _get_bool(changed["Connected"]) and not fut.done():
                fut.set_result(msg.path)

    bus.add_message_handler(handler)
    try:
        while True:
            if fut.done():
                return await fut
            # polling fallback
            objs = await _get_managed_objects(bus)
            for path, props in objs.items():
                dev = props.get("org.bluez.Device1")
                if dev and _get_bool(dev.get("Connected", False)):
                    return path
            await asyncio.sleep(poll_interval)
    finally:
        bus.remove_message_handler(handler)

async def wait_until_services_resolved(bus, device_path, timeout_s=30, poll_interval=0.25):
    """Wait for Device1.ServicesResolved == True for this device."""
    import time
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        objs = await _get_managed_objects(bus)
        dev = objs.get(device_path, {}).get("org.bluez.Device1")
        if dev and _get_bool(dev.get("ServicesResolved", False)):
            return True
        await asyncio.sleep(poll_interval)
    return False

async def wait_for_disconnect(bus, device_path, poll_interval=0.5):
    """Block until this device disconnects."""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def handler(msg):
        if msg.message_type is not MessageType.SIGNAL:
            return
        if msg.member != "PropertiesChanged" or msg.path != device_path:
            return
        iface, changed, _ = msg.body
        if iface == "org.bluez.Device1" and "Connected" in changed and not _get_bool(changed["Connected"]) and not fut.done():
            fut.set_result(None)

    bus.add_message_handler(handler)
    try:
        while True:
            if fut.done():
                await fut
                return
            objs = await _get_managed_objects(bus)
            dev = objs.get(device_path, {}).get("org.bluez.Device1")
            if not dev or not _get_bool(dev.get("Connected", False)):
                return
            await asyncio.sleep(poll_interval)
    finally:
        bus.remove_message_handler(handler)

async def watch_link(bus, advert, hid: "HIDService"):
    """
    Wait for a connection, for ServicesResolved, then enable HID sends (_link_ready = True).
    Prints a concise pairing log and CCCD snapshot once per connection.
    Restarts advertising after disconnect.
    """
    while True:
        dev_path = await wait_for_any_connection(bus)
        with contextlib.suppress(Exception):
            await trust_device(bus, dev_path)
        print(f"[hid] connected: {dev_path}")
    
        # Wait for GATT discovery to finish
        await wait_until_services_resolved(bus, dev_path, timeout_s=30)
        await asyncio.sleep(0.8)  # let the host write CCCDs
    
        # Link gate ON
        hid._link_ready = True
        try:
            kb, boot, cc = hid._notif_state()
            print(f"[hid] link ready (services resolved) — notify: kb={kb} boot={boot} cc={cc}")
        except Exception:
            print("[hid] link ready (services resolved)")
    
        # Fully remove the advertisement while connected
        with contextlib.suppress(Exception):
            await asyncio.sleep(0.1)  # tiny grace, optional
            await _adv_unregister(advert)
            print("[hid] advertising unregistered (connected device)")
        
        # Block here until disconnect
        await wait_for_disconnect(bus, dev_path)
        
        # Link gate OFF
        hid._link_ready = False
        print("[hid] disconnected")
        
        # Re-register + start the advertisement for next client
        with contextlib.suppress(Exception):
            await _adv_register_and_start(bus, advert)
            print("[hid] advertising restarted")

# --------------------------
# GATT Services
# --------------------------
# --- Battery Service (0x180F) ---
class BatteryService(Service):
    def __init__(self, initial_level: int = 100):
        super().__init__("180F", True)
        lvl = max(0, min(100, int(initial_level)))
        self._level = bytearray([lvl])

    @characteristic("2A19", CharFlags.READ | CharFlags.NOTIFY)
    def battery_level(self, _):
        # 0..100
        return bytes(self._level)

    # Convenience: call this to update + notify
    def set_level(self, pct: int):
        pct = max(0, min(100, int(pct)))
        if self._level[0] != pct:
            self._level[0] = pct
            try:
                self.battery_level.changed(bytes(self._level))
            except Exception:
                pass

class DeviceInfoService(Service):
    def __init__(self, manufacturer="PiKB Labs", model="PiKB-1", vid=0xFFFF, pid=0x0001, ver=0x0100):
        super().__init__("180A", True)
        self._mfg   = manufacturer.encode("utf-8")
        self._model = model.encode("utf-8")
        self._pnp   = bytes([0x02, vid & 0xFF, (vid>>8)&0xFF, pid & 0xFF, (pid>>8)&0xFF, ver & 0xFF, (ver>>8)&0xFF])

    @characteristic("2A29", CharFlags.READ | CharFlags.ENCRYPT_READ)
    def manufacturer_name(self, _):
        return self._mfg

    @characteristic("2A24", CharFlags.READ | CharFlags.ENCRYPT_READ)
    def model_number(self, _):
        return self._model

    @characteristic("2A50", CharFlags.READ | CharFlags.ENCRYPT_READ)
    def pnp_id(self, _):
        return self._pnp

class HIDService(Service):
    def __init__(self):
        super().__init__("1812", True)
        self._proto = bytearray([1])  # Report Protocol
        self._link_ready: bool = False
        self._last_notify_state: tuple[bool, bool, bool] | None = None  # (kb, boot, cc)

    # -------- subscription helpers --------
    def _is_subscribed(self, char) -> bool:
        # bluez_peripheral exposes `.is_notifying`; some older builds had `.notifying`
        if hasattr(char, "is_notifying"):
            return bool(getattr(char, "is_notifying"))
        if hasattr(char, "notifying"):
            return bool(getattr(char, "notifying"))
        # If library doesn’t expose state, assume subscribed so we don’t suppress sends.
        return True
        
    def _cccd_snapshot(self) -> str:
        kb   = getattr(self.input_keyboard,      "is_notifying", getattr(self.input_keyboard,      "notifying", False))
        boot = getattr(self.boot_keyboard_input, "is_notifying", getattr(self.boot_keyboard_input, "notifying", False))
        cc   = getattr(self.input_consumer,      "is_notifying", getattr(self.input_consumer,      "notifying", False))
        return f"kb={bool(kb)} boot={bool(boot)} cc={bool(cc)}"

    def _notif_state(self) -> tuple[bool, bool, bool]:
        kb   = self._is_subscribed(self.input_keyboard)
        boot = self._is_subscribed(self.boot_keyboard_input)
        cc   = self._is_subscribed(self.input_consumer)
        return (kb, boot, cc)

    def _log_notify_change(self, state: tuple[bool, bool, bool]):
        if state != self._last_notify_state:
            kb, boot, cc = state
            print(f"[hid] notify state: kb={kb} boot={boot} cc={cc}")
            self._last_notify_state = state

    # ---------------- GATT Characteristics ----------------

    # Protocol Mode (2A4E): READ/WRITE (encrypted both)
    @characteristic("2A4E", CharFlags.READ | CharFlags.WRITE | CharFlags.ENCRYPT_READ | CharFlags.ENCRYPT_WRITE)
    def protocol_mode(self, _):
        return bytes(self._proto)
    @protocol_mode.setter
    def protocol_mode_set(self, value, _):
        self._proto[:] = value

    # HID Information (2A4A): READ (encrypted)
    @characteristic("2A4A", CharFlags.READ | CharFlags.ENCRYPT_READ)
    def hid_info(self, _):
        return bytes([0x11, 0x01, 0x00, 0x03])  # bcdHID=0x0111, country=0, flags=0x03

    # HID Control Point (2A4C): WRITE (encrypted)
    @characteristic("2A4C", CharFlags.WRITE | CharFlags.WRITE_WITHOUT_RESPONSE | CharFlags.ENCRYPT_WRITE)
    def hid_cp(self, _):
        return b""
    @hid_cp.setter
    def hid_cp_set(self, _value, _):
        pass

    # Report Map (2A4B): READ (encrypted)
    @characteristic("2A4B", CharFlags.READ | CharFlags.ENCRYPT_READ)
    def report_map(self, _):
        return REPORT_MAP

    # Keyboard input (Report-mode, RID 1) — 8-byte payload
    @characteristic("2A4D", CharFlags.READ | CharFlags.NOTIFY)
    def input_keyboard(self, _):
        return bytes([0,0,0,0,0,0,0,0])
    @input_keyboard.descriptor("2908", DescFlags.READ)
    def input_keyboard_ref(self, _):
        return bytes([RID_KEYBOARD, 0x01])

    # Consumer input (RID 2) — 2-byte payload (16-bit usage)
    @characteristic("2A4D", CharFlags.READ | CharFlags.NOTIFY)
    def input_consumer(self, _):
        return bytes([0,0])
    @input_consumer.descriptor("2908", DescFlags.READ)
    def input_consumer_ref(self, _):
        return bytes([RID_CONSUMER, 0x01])

    # Boot Keyboard Input (2A22) — 8-byte payload (no report ID)
    @characteristic("2A22", CharFlags.READ | CharFlags.NOTIFY)
    def boot_keyboard_input(self, _):
        return bytes([0,0,0,0,0,0,0,0])

    # ---------------- Send helpers ----------------
    def send_keyboard(self, payload: bytes):
        if not self._link_ready:
            return
        # log CCCD state only when it changes
        self._log_notify_change(self._notif_state())
        try:
            self.input_keyboard.changed(payload)       # report-mode
        except Exception:
            pass
        try:
            self.boot_keyboard_input.changed(payload)  # boot
        except Exception:
            pass

    def send_consumer(self, payload: bytes):
        if not self._link_ready:
            return
        self._log_notify_change(self._notif_state())
        try:
            self.input_consumer.changed(payload)
        except Exception:
            pass

    async def key_tap(self, usage, hold_ms=40, modifiers=0):
        down = kb_payload([usage], modifiers)
        self.send_keyboard(down)
        await asyncio.sleep(hold_ms/1000)
        up = kb_payload([], 0)
        self.send_keyboard(up)

    def cc_payload_usage(self, usage_id: int) -> bytes:
        return bytes([usage_id & 0xFF, (usage_id >> 8) & 0xFF])

    async def consumer_tap(self, usage_id, hold_ms=60):
        self.send_consumer(self.cc_payload_usage(usage_id))
        await asyncio.sleep(hold_ms/1000)
        self.send_consumer(self.cc_payload_usage(0))

    def release_all(self):
        self.send_keyboard(kb_payload([], 0))
        self.send_consumer(self.cc_payload_usage(0))

@dataclass
class HidRuntime:
    bus: any
    adapter: any
    advert: any
    hid: any
    tasks: list

async def start_hid(config, *, enable_console: bool = False) -> tuple[HidRuntime, callable]:
    """
    Start the BLE HID server. Returns (runtime, shutdown) where shutdown is an async callable.
    - config.device_name   : BLE local name (string)
    - config.appearance    : GAP appearance (int, default 0x03C1)
    """
    from bluez_peripheral.util import get_message_bus, Adapter, is_bluez_available
    from bluez_peripheral.advert import Advertisement
    from bluez_peripheral.agent import NoIoAgent

    device_name = getattr(config, "device_name", None) or os.uname().nodename
    appearance  = int(getattr(config, "appearance", 0x03C1))  # keyboard

    bus = await get_message_bus()
    if not await is_bluez_available(bus):
        raise RuntimeError("BlueZ not available on system DBus.")

    # Adapter
    adapter_name = "hci0"
    xml = await bus.introspect("org.bluez", f"/org/bluez/{adapter_name}")
    proxy = bus.get_proxy_object("org.bluez", f"/org/bluez/{adapter_name}", xml)
    adapter = Adapter(proxy)
    await adapter.set_alias(device_name)

    # Agent
    agent = NoIoAgent()
    await agent.register(bus, default=True)
    
    # Services
    dis = DeviceInfoService()
    bas = BatteryService(initial_level=100)
    hid = HIDService()
    global _hid_service_singleton
    _hid_service_singleton = hid
    
    
    app = ServiceCollection()
    app.add_service(dis)
    app.add_service(bas)
    app.add_service(hid)
    
    async def _power_cycle_adapter():
        try:
            await adapter.set_powered(False)
            await asyncio.sleep(0.4)
            await adapter.set_powered(True)
            await asyncio.sleep(0.8)
        except Exception as e:
            print(f"[BLE] adapter power-cycle failed: {e}")
    
    # --- Register GATT application (with one retry) ---
    try:
        await app.register(bus, adapter=adapter)
    except Exception as e:
        print(f"[BLE] service register failed: {e} — retrying after power-cycle")
        await _power_cycle_adapter()
        try:
            await app.register(bus, adapter=adapter)
        except Exception as e2:
            # Hard fail: do not return None
            raise RuntimeError(f"GATT application register failed after retry: {e2}") from e2
    
    # --- Register advertising (with one retry) ---
    advert = Advertisement(
        localName=device_name,
        serviceUUIDs=["1812", "180F", "180A"],
        appearance=appearance,
        timeout=0,
        discoverable=True,
    )
    try:
        await advert.register(bus, adapter)
    except Exception as e:
        print(f"[BLE] advert register failed: {e} — retrying after power-cycle")
        with contextlib.suppress(Exception):
            await advert.unregister()
        await _power_cycle_adapter()
        # Recreate a fresh advert object (fresh DBus path)
        advert = Advertisement(
            localName=device_name,
            serviceUUIDs=["1812", "180F", "180A"],
            appearance=appearance,
            timeout=0,
            discoverable=True,
        )
        try:
            await advert.register(bus, adapter)
        except Exception as e2:
            # Hard fail: do not return None
            with contextlib.suppress(Exception):
                await app.unregister()
            raise RuntimeError(f"Advertising register failed after retry: {e2}") from e2
            
    # --- Flip link gate when a device connects & services resolve ---
    async def _watch_link():
        global _hid_service_singleton
        while True:
            dev_path = await wait_for_any_connection(bus)
            # trust for smooth reconnects
            with contextlib.suppress(Exception):
                await trust_device(bus, dev_path)

            await wait_until_services_resolved(bus, dev_path, timeout_s=30)
            await asyncio.sleep(0.8)  # let host write CCCDs

            if _hid_service_singleton:
                _hid_service_singleton._link_ready = True
                print("[hid] link ready (services resolved)")

            await wait_for_disconnect(bus, dev_path)

            if _hid_service_singleton:
                _hid_service_singleton._link_ready = False
                print("[hid] link not ready (disconnected)")

    link_task = asyncio.create_task(_watch_link(), name="hid_link_watch")
    
    print(f"[BT] Advertising as {device_name} on {adapter_name}. Ready to pair.")
    
    # Start link watcher (flips _link_ready and prints concise pairing log)
    link_task = asyncio.create_task(watch_link(bus, advert, hid), name="hid_watch_link")
    tasks = [link_task]
    
    # Make sure nothing is logically "held" at startup
    with contextlib.suppress(Exception):
        hid.release_all()
    
    async def shutdown():
        # stop watcher first
        for t in list(getattr(locals(), "tasks", [])) + []:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        # Unregister advert
        with contextlib.suppress(Exception):
            await advert.unregister()
        # Unregister services
        with contextlib.suppress(Exception):
            await app.unregister()
        # Release any held keys
        with contextlib.suppress(Exception):
            hid.release_all()
    
#    runtime = HidRuntime(bus=bus, adapter=adapter, advert=advert, hid=hid, tasks=[])
#    runtime.tasks.append(link_task)
#    return runtime, shutdown
    
    runtime = HidRuntime(bus=bus, adapter=adapter, advert=advert, hid=hid, tasks=tasks)
    return runtime, shutdown


# --------------------------
# Console: interactive test
# --------------------------
async def console(hid):
    """
    Commands:
      enter esc space up down left right 0..9
      play next prev stop mute volup voldown home
      k <hex>     (send arbitrary keyboard usage)
      help, exit, q
    """
    help_text = (
        "Commands:\n"
        "  enter esc space up down left right 0..9\n"
        "  (consumer) play_pause vol_up vol_down mute scan_next scan_prev stop\n"
        "             fast_forward rewind record ac_home menu menu_pick\n"
        "             menu_up menu_down menu_left menu_right power tv_guide\n"
        "  k <hex>   (keyboard usage, e.g. k 0x28)\n"
        "  cc <hex>  (consumer usage, e.g. cc 0x00E9)\n"
        "  help, exit, q\n"
    )
    print(help_text, end="")
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            continue
        cmd = line.strip().lower()
    
        if cmd in ("exit", "q"):
            print("Exiting…")
            try:
                hid.release_all()
            finally:
                sys.exit(0)
    
        if cmd in ("help", "?"):
            print(help_text, end="")
            continue
    
        if not _connected_event.is_set():
            print("Not connected yet. Connect from the phone/Apple TV first.")
            continue
    
        # -------- Named consumer actions (from YAML) --------
        if cmd in NAME_TO_CC_USAGE:
            await hid.consumer_tap(NAME_TO_CC_USAGE[cmd], hold_ms=60)
            print(f"Sent consumer usage: {cmd} (0x{NAME_TO_CC_USAGE[cmd]:04X})")
            continue
    
        # -------- Named keyboard actions (from YAML) --------
        if cmd in NAME_TO_KEY:
            await hid.key_tap(NAME_TO_KEY[cmd], hold_ms=60)
            print(f"Sent key: {cmd}")
            continue
    
        # -------- Raw keyboard usage: k 0x28 --------
        if cmd.startswith("k "):
            try:
                usage = int(cmd.split(" ", 1)[1], 0)
                await hid.key_tap(usage, hold_ms=60)
                print(f"Sent key usage: 0x{usage:02X}")
            except Exception:
                print("Invalid usage. Example: k 0x28")
            continue
    
        # -------- Raw consumer usage: cc 0x00E9 --------
        if cmd.startswith("cc "):
            try:
                usage = int(cmd.split(" ", 1)[1], 0)
                await hid.consumer_tap(usage, hold_ms=60)
                print(f"Sent consumer usage: 0x{usage:04X}")
            except Exception:
                print("Invalid usage. Example: cc 0x00E9")
            continue
    
        # (Optional) single-character digits, if you still keep KEY_DIGITS:
        if len(cmd) == 1 and 'KEY_DIGITS' in globals() and cmd in KEY_DIGITS:
            await hid.key_tap(KEY_DIGITS[cmd], hold_ms=60)
            print(f"Sent key: {cmd}")
            continue
    
        print("Unknown command. Type 'help' for options.")

# --------------------------
# Connection watcher
# --------------------------
async def on_connect_cycle(bus):
    global _hid_service_singleton

    while True:
        dev_path = await wait_for_any_connection(bus)
        print(f"Connected: {dev_path}")

        # Trust immediately (so future reconnects are smooth)
        with contextlib.suppress(Exception):
            await trust_device(bus, dev_path)

        # Wait for services to resolve (so HID is visible to host)
        await wait_until_services_resolved(bus, dev_path, timeout_s=30)

        # ----- Ensure the link is PAIRED before enabling HID sends -----
        try:
            root_xml = await bus.introspect("org.bluez", dev_path)
            dev_obj = bus.get_proxy_object("org.bluez", dev_path, root_xml)
            props = dev_obj.get_interface("org.freedesktop.DBus.Properties")
            devif = dev_obj.get_interface("org.bluez.Device1")

            # helper to read Paired flag
            async def is_paired() -> bool:
                v = await props.call_get("org.bluez.Device1", "Paired")
                return bool(v.value if isinstance(v, Variant) else v)

            # trigger pairing if not already paired
            if not await is_paired():
                print("[hid] requesting bonding (Device1.Pair)")
                with contextlib.suppress(Exception):
                    await devif.call_pair()

            # wait up to ~5s for Paired=True
            deadline = time.time() + 5.0
            while time.time() < deadline and not await is_paired():
                await asyncio.sleep(0.2)

            paired_now = await is_paired()
        except Exception as e:
            print(f"[hid] pairing check error: {e}")
            paired_now = False

        # give the host a moment to write CCCDs after pairing
        await asyncio.sleep(0.8)

        # enable link
        if _hid_service_singleton:
            _hid_service_singleton._link_ready = True
            # one-shot debug snapshot (best-effort)
            with contextlib.suppress(Exception):
                print(f"[hid] CCCD (best-effort): "
                      f"{_hid_service_singleton._cccd_snapshot()} (Paired={paired_now})")

        _connected_event.set()
        print("Ready for input.")

        # wait until this device disconnects
        await wait_for_disconnect(bus, dev_path)
        _connected_event.clear()

        if _hid_service_singleton:
            _hid_service_singleton._link_ready = False

        print("Disconnected. Waiting for next connection…")


def _merge_keymaps(kb_map, cc_usages):
    def norm_keys(d):
        return {str(k).strip().lower(): int(v, 0) if isinstance(v, str) else int(v)
                for k, v in d.items()}
    name_to_key = dict(DEFAULT_NAME_TO_KEY)
    name_to_cc  = dict(DEFAULT_NAME_TO_CC_USAGE)
    if isinstance(kb_map, dict):
        name_to_key.update(norm_keys(kb_map))
    if isinstance(cc_usages, dict):
        name_to_cc.update(norm_keys(cc_usages))
    return name_to_key, name_to_cc

def load_keymap_file(path=KEYMAP_PATH):
    global NAME_TO_KEY, NAME_TO_CC_USAGE
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        kb = data.get("keyboard", {}) or {}
        # prefer consumer_usages; fall back to legacy consumer_bits if present
        cc_u = data.get("consumer_usages", {}) or {}
        NAME_TO_KEY, NAME_TO_CC_USAGE = _merge_keymaps(kb, cc_u)
        return True, "loaded"
    except FileNotFoundError:
        NAME_TO_KEY, NAME_TO_CC_USAGE = _merge_keymaps({}, {})
        return False, "missing"
    except Exception as e:
        return False, f"error: {e}"

async def watch_keymap(interval=1.0, path=KEYMAP_PATH):
    """Hot‑reload keymap.yaml when it changes."""
    last_mtime = None
    # initial load
    ok, why = load_keymap_file(path)
    print(f"[keymap] {why}; {len(NAME_TO_KEY)} keys, {len(NAME_TO_CC_USAGE)} consumer usages")
    while True:
        try:
            mtime = os.path.getmtime(path)
        except FileNotFoundError:
            mtime = None
        if mtime != last_mtime:
            last_mtime = mtime
            ok, why = load_keymap_file(path)
            print(f"[keymap] {why}; {len(NAME_TO_KEY)} keys, {len(NAME_TO_CC_USAGE)} consumer usages")
        await asyncio.sleep(interval)
        
####################

# --------------------------
# Main
# --------------------------
async def main():
    bus = await get_message_bus()
    if not await is_bluez_available(bus):
        raise RuntimeError("BlueZ not available on system DBus.")

    # Adapter
    adapter_name = "hci0"
    xml = await bus.introspect("org.bluez", f"/org/bluez/{adapter_name}")
    proxy = bus.get_proxy_object("org.bluez", f"/org/bluez/{adapter_name}", xml)
    adapter = Adapter(proxy)

    # ---- Device name / appearance (ensure they're defined) ----
    device_name = os.uname().nodename  # or inject from your app
    appearance  = APPEARANCE
    with contextlib.suppress(Exception):
        await adapter.set_alias(device_name)

    # Agent (no IO: auto-accept from the Pi side)
    agent = NoIoAgent()
    await agent.register(bus, default=True)

    # Services: Device Info + Battery + HID
    dis = DeviceInfoService()
    bas = BatteryService(initial_level=100)
    hid = HIDService()

    app = ServiceCollection()
    app.add_service(dis)
    app.add_service(bas)
    app.add_service(hid)
    await app.register(bus, adapter=adapter)

    # Advertise
    advert = Advertisement(
        localName=device_name,
        serviceUUIDs=["1812", "180F", "180A"],  # HID + Battery + Device Info
        appearance=appearance,
        timeout=0,
        discoverable=True,
    )
    await advert.register(bus, adapter)

    print(f"Advertising as {device_name} on {adapter_name}. Ready to pair.")

    # Start connection watcher + interactive console
    asyncio.create_task(watch_keymap())
    asyncio.create_task(on_connect_cycle(bus))  # this flips _link_ready when paired/resolved
    asyncio.create_task(console(hid))

    # Clean shutdown
    def cleanup(*_):
        try:
            hid.release_all()
        finally:
            sys.exit(0)
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    await asyncio.get_running_loop().create_future()

if __name__ == "__main__":
    asyncio.run(main())