# pihub/.../macros/ble.py
from __future__ import annotations

from dbus_fast.aio import MessageBus
from dbus_fast import BusType

BLUEZ = "org.bluez"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"
ADAPTER_IFACE = "org.bluez.Adapter1"

async def unpair_all(adapter: str = "hci0") -> int:
    """
    Remove all paired devices from the given adapter via BlueZ D-Bus.
    Returns the number of devices removed.
    """
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        root = await bus.introspect(BLUEZ, "/")
        obj = bus.get_proxy_object(BLUEZ, "/", root)
        om = obj.get_interface(OM_IFACE)
        objects = await om.call_get_managed_objects()

        adapter_path = f"/org/bluez/{adapter}"
        ad_xml = await bus.introspect(BLUEZ, adapter_path)
        ad_obj = bus.get_proxy_object(BLUEZ, adapter_path, ad_xml)
        adapter_iface = ad_obj.get_interface(ADAPTER_IFACE)

        removed = 0
        for path, ifaces in list(objects.items()):
            dev = ifaces.get("org.bluez.Device1")
            if not dev:
                continue
            paired = dev.get("Paired")
            if hasattr(paired, "value"):  # unwrap Variant if needed
                paired = paired.value
            if paired:
                try:
                    await adapter_iface.call_remove_device(path)
                    removed += 1
                    print(f"[ble] removed paired device {path}")
                except Exception as e:
                    print(f"[ble] failed to remove {path}: {e!r}")
        return removed
    finally:
        # In your dbus-fast version, disconnect() is sync — don't await it.
        try:
            bus.disconnect()
        except Exception:
            pass