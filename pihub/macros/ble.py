# pihub/macros/ble.py
import asyncio
from dbus_fast.aio import MessageBus
from dbus_fast import BusType

BLUEZ_SERVICE = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
OBJ_MANAGER = "org.freedesktop.DBus.ObjectManager"


async def unpair_all(adapter="hci0"):
    """
    Remove all bonded devices from the given adapter.
    Mirrors `bluetoothctl remove *`.
    """
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    obj = await bus.introspect(BLUEZ_SERVICE, "/")
    mgr = bus.get_proxy_object(BLUEZ_SERVICE, "/", obj).get_interface(OBJ_MANAGER)

    objects = await mgr.GetManagedObjects()
    removed = False

    for path, ifaces in objects.items():
        if DEVICE_IFACE in ifaces and path.startswith(f"/org/bluez/{adapter}/dev_"):
            try:
                dev_obj = await bus.introspect(BLUEZ_SERVICE, path)
                dev_iface = bus.get_proxy_object(BLUEZ_SERVICE, path, dev_obj).get_interface(DEVICE_IFACE)
                await dev_iface.Remove()
                print(f"[ble] removed paired device {path}")
                removed = True
            except Exception as e:
                print(f"[ble] failed to remove {path}: {e}")

    # no await here — dbus-fast disconnect is sync
    bus.disconnect()

    if not removed:
        print("[ble] no paired devices found")