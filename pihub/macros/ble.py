# pihub/macros/ble.py
from __future__ import annotations
import asyncio
from typing import List

from dbus_fast.aio import MessageBus
from dbus_fast import BusType

BLUEZ_SERVICE = "org.bluez"
OBJ_MANAGER = "org.freedesktop.DBus.ObjectManager"
ADAPTER_IFACE = "org.bluez.Adapter1"

async def unpair_all(adapter: str = "hci0") -> None:
    """
    Remove all bonded devices under /org/bluez/<adapter>/dev_* using BlueZ Adapter1.RemoveDevice.
    No advertising/pairable toggles here (your HID code handles re-advertising).
    """
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        # ObjectManager on "/"
        root_introspect = await bus.introspect(BLUEZ_SERVICE, "/")
        root_proxy = bus.get_proxy_object(BLUEZ_SERVICE, "/", root_introspect)
        om = root_proxy.get_interface(OBJ_MANAGER)

        # dbus-fast: use call_get_managed_objects()
        objects = await om.call_get_managed_objects()

        # Gather device paths for this adapter
        prefix = f"/org/bluez/{adapter}/dev_"
        dev_paths: List[str] = [
            path for path, ifaces in objects.items()
            if path.startswith(prefix)
        ]

        if not dev_paths:
            print("[ble] no paired devices found")
            return

        # Get Adapter1 interface for RemoveDevice
        adapter_path = f"/org/bluez/{adapter}"
        adp_intro = await bus.introspect(BLUEZ_SERVICE, adapter_path)
        adp_proxy = bus.get_proxy_object(BLUEZ_SERVICE, adapter_path, adp_intro)
        adapter_if = adp_proxy.get_interface(ADAPTER_IFACE)

        removed = 0
        for path in dev_paths:
            try:
                # dbus-fast: RemoveDevice -> call_remove_device(object_path)
                await adapter_if.call_remove_device(path)
                print(f"[ble] removed paired device {path}")
                removed += 1
            except Exception as e:
                print(f"[ble] failed to remove {path}: {e}")

        if removed == 0:
            print("[ble] no devices removed (errors above)")
    finally:
        # dbus-fast .disconnect() is sync
        bus.disconnect()

# Optional: allow `python -m pihub.macros.ble` to run it once (hci0)
if __name__ == "__main__":
    asyncio.run(unpair_all("hci0"))