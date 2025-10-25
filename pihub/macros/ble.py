# pihub/macros/ble.py
from __future__ import annotations
import asyncio
from typing import List

from dbus_fast.aio import MessageBus
from dbus_fast import BusType

BLUEZ_SERVICE = "org.bluez"
OBJ_MANAGER = "org.freedesktop.DBus.ObjectManager"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"

async def unpair_all(adapter: str = "hci0") -> None:
    """
    Remove all bonded devices under /org/bluez/<adapter>/dev_* using BlueZ Adapter1.RemoveDevice.
    Only removes actual Device1 nodes (not their GATT children).
    """
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        # ObjectManager on "/"
        root_introspect = await bus.introspect(BLUEZ_SERVICE, "/")
        root_proxy = bus.get_proxy_object(BLUEZ_SERVICE, "/", root_introspect)
        om = root_proxy.get_interface(OBJ_MANAGER)

        # dbus-fast: call_get_managed_objects()
        objects = await om.call_get_managed_objects()

        # Gather ONLY Device1 nodes for this adapter (skip service/char/desc children)
        prefix = f"/org/bluez/{adapter}/dev_"
        dev_paths: List[str] = [
            path for path, ifaces in objects.items()
            if path.startswith(prefix) and DEVICE_IFACE in ifaces
        ]

        if not dev_paths:
            print("[macros] no paired bluetooth devices found")
            return

        # Prepare adapter proxy once
        adapter_path = f"/org/bluez/{adapter}"
        adp_intro = await bus.introspect(BLUEZ_SERVICE, adapter_path)
        adp_proxy = bus.get_proxy_object(BLUEZ_SERVICE, adapter_path, adp_intro)
        adapter_if = adp_proxy.get_interface(ADAPTER_IFACE)

        removed = 0
        for path in dev_paths:
            try:
                # If still connected, request a disconnect first to speed things up
                dev_intro = await bus.introspect(BLUEZ_SERVICE, path)
                dev_proxy = bus.get_proxy_object(BLUEZ_SERVICE, path, dev_intro)
                dev = dev_proxy.get_interface(DEVICE_IFACE)
                try:
                    props = await dev_proxy.get_interface("org.freedesktop.DBus.Properties").call_get_all(DEVICE_IFACE)
                    if bool(props.get("Connected", False)):
                        with asyncio.timeout(2.0):
                            await dev.call_disconnect()
                except Exception:
                    pass  # best effort

                # Remove the device (this clears the bond and will trigger disconnect if still up)
                await adapter_if.call_remove_device(path)
                print(f"[macros] removed paired bluetooth device {path}")
                removed += 1
            except Exception as e:
                print(f"[macros] failed to remove bluetooth device {path}: {e}")

        if removed == 0:
            print("[macros] no bluetooth devices removed (errors above)")
    finally:
        # dbus-fast disconnect() is synchronous
        bus.disconnect()

if __name__ == "__main__":
    asyncio.run(unpair_all("hci0"))