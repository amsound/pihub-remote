# pihub/bt_le/hid_client.py
from __future__ import annotations

import asyncio
import contextlib
from typing import Optional, Set, Iterable


# ---- Tunables --------------------------------------------------------------

# Siri-like Consumer hold repeat cadence. 120–150 ms works well with 15ms/lat=4 links.
CONSUMER_REPEAT_MS: int = 150

# Tap timings (kept for convenience)
CONSUMER_TAP_MS: int = 40
KEYBOARD_TAP_MS: int = 40


class HidDevice:
    """BLE GATT backend contract (implemented elsewhere)."""
    def send_keyboard(self, payload: bytes) -> None: ...
    def send_consumer(self, payload: bytes) -> None: ...


class HIDClient:
    """Edge-driven HID:
       - Consumer (0x0C): steady reassert (DOWN) while held; UP on release.
       - Keyboard (0x07): edges only; host handles typematic.
    """

    def __init__(self, dev: HidDevice, max_keys: int = 6) -> None:
        self.dev = dev
        self.max_keys = max_keys

        # Keyboard state
        self._kb_mods: int = 0
        self._kb_keys: Set[int] = set()
        self._last_kb: Optional[bytes] = None

        # Consumer state
        self._cc_usage: int = 0
        self._last_cc: Optional[bytes] = None
        self._cc_repeat_task: Optional[asyncio.Task] = None

    # ---------------- Keyboard (edge-only; host repeats) --------------------

    async def key_down(self, code: int, modifiers: int = 0) -> None:
        if len(self._kb_keys) < self.max_keys:
            self._kb_keys.add(int(code))
        self._kb_mods = modifiers & 0xFF
        self._kb_send()

    async def key_up(self, code: Optional[int] = None) -> None:
        """Release a specific key, or all if code=None (matches your dispatcher)."""
        if code is None:
            if self._kb_keys:
                self._kb_keys.clear()
                self._kb_send()
        else:
            self._kb_keys.discard(int(code))
            self._kb_send()

    async def key_tap(self, code: int, modifiers: int = 0, hold_ms: int = KEYBOARD_TAP_MS) -> None:
        await self.key_down(code, modifiers)
        await asyncio.sleep(max(0, hold_ms) / 1000)
        await self.key_up(code)

    # ---------------- Consumer (Siri-like steady repeat while held) ---------

    async def consumer_down(self, usage: int) -> None:
        """Send Consumer 'down' and start a steady repeat until released."""
        self._cc_usage = int(usage) & 0xFFFF
        self._cc_send()
        await self._start_cc_repeat()

    async def consumer_up(self) -> None:
        """Send Consumer 'up' and stop repeat."""
        await self._stop_cc_repeat()
        self._cc_usage = 0
        self._cc_send()

    async def consumer_tap(self, usage: int, hold_ms: int = CONSUMER_TAP_MS) -> None:
        await self.consumer_down(usage)
        await asyncio.sleep(max(0, hold_ms) / 1000)
        await self.consumer_up()

    # ---------------- Builders & senders ------------------------------------

    def _kb_build(self) -> bytes:
        mods = self._kb_mods
        keys: Iterable[int] = list(self._kb_keys)[: self.max_keys]
        buf = bytearray(2 + self.max_keys)
        buf[0] = mods
        buf[1] = 0x00
        for i, code in enumerate(keys):
            buf[2 + i] = code & 0xFF
        return bytes(buf)

    def _kb_send(self) -> None:
        payload = self._kb_build()
        if payload == self._last_kb:
            return
        self._last_kb = payload
        try:
            self.dev.send_keyboard(payload)
        except Exception:
            # hot path: stay silent
            pass

    def _cc_build(self) -> bytes:
        u = self._cc_usage
        return bytes((u & 0xFF, (u >> 8) & 0xFF))

    def _cc_send(self) -> None:
        payload = self._cc_build()
        if payload == self._last_cc:
            return
        self._last_cc = payload
        try:
            self.dev.send_consumer(payload)
        except Exception:
            pass

    # ---------------- Repeat loop (Consumer only) ---------------------------

    async def _start_cc_repeat(self) -> None:
        await self._stop_cc_repeat()
        interval = max(60, CONSUMER_REPEAT_MS) / 1000.0

        async def loop():
            import time
            try:
                # Delay first tick so we don't duplicate the initial DOWN
                next_at = time.monotonic() + interval
                while self._cc_usage:
                    now = time.monotonic()
                    delay = next_at - now
                    if delay > 0:
                        await asyncio.sleep(delay)
                    # Force a notify each tick with the same DOWN payload
                    payload = bytes((self._cc_usage & 0xFF, (self._cc_usage >> 8) & 0xFF))
                    try:
                        self.dev.send_consumer(payload)
                    except Exception:
                        pass
                    next_at += interval
            except asyncio.CancelledError:
                pass

        self._cc_repeat_task = asyncio.create_task(loop(), name="hid:cc_repeat")

    async def _stop_cc_repeat(self) -> None:
        task = self._cc_repeat_task
        if task:
            self._cc_repeat_task = None
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # ---------------- Cleanup ------------------------------------------------

    async def close(self) -> None:
        await self._stop_cc_repeat()
        self._kb_keys.clear()
        self._kb_mods = 0
        self._last_kb = None
        self._cc_usage = 0
        self._last_cc = None