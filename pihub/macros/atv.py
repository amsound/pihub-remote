# pihub/macros/atv.py
import asyncio

# Consumer usages (HID Usage Page 0x0C)
U_STOP      = 0x00B7
U_MENU      = 0x0040
U_AC_HOME   = 0x0223
U_POWER     = 0x0030

DEFAULT_IKD_MS = 400  # inter-key delay

async def atv_off(hid, ikd_ms: int = DEFAULT_IKD_MS):
    """OFF sequence:
       stop → ac_home → ac_home → menu → menu → power(3s)"""
    d = ikd_ms / 1000.0
    await hid.consumer_tap(U_STOP,    hold_ms=40); await asyncio.sleep(d)
    await hid.consumer_tap(U_AC_HOME, hold_ms=40); await asyncio.sleep(d)
    await hid.consumer_tap(U_AC_HOME, hold_ms=40); await asyncio.sleep(d)
    await hid.consumer_tap(U_MENU,    hold_ms=40); await asyncio.sleep(d)
    await hid.consumer_tap(U_MENU,    hold_ms=40); await asyncio.sleep(d)
    await hid.consumer_tap(U_POWER,   hold_ms=2000)

async def atv_on(hid, ikd_ms: int = DEFAULT_IKD_MS):
    """ON sequence:
       power → wait 3s → menu"""
    await hid.consumer_tap(U_POWER, hold_ms=40)
    await asyncio.sleep(3.0)
    await hid.consumer_tap(U_MENU, hold_ms=40)