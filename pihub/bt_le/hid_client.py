import asyncio

KEEPALIVE_MS = 20  # Time between repeated key reports

class HIDClient:
    def __init__(self, hid_service):
        self.hid = hid_service

        # keyboard state
        self._kb_usage: int | None = None
        self._kb_mods: int = 0
        self._kb_task: asyncio.Task | None = None

        # consumer state
        self._cc_usage: int | None = None
        self._cc_task: asyncio.Task | None = None

        # warn only once per run if host hasn't enabled notifications (CCCD off)
        self._warned_keyboard = False
        self._warned_consumer = False

    # ---------------- Keyboard (Report ID 1) ----------------
    def _kb_payload(self, keys=(), modifiers=0, reserved=0) -> bytes:
        k = list(keys)[:6] + [0] * (6 - len(keys))
        return bytes([modifiers, reserved] + k)

    def _kb_apply(self) -> None:
        keys = [self._kb_usage] if self._kb_usage else []
        # Warn once only if HID says "not subscribed" (using the service helper)
        if not self._warned_keyboard:
            try:
                if hasattr(self.hid, "_is_subscribed") and not self.hid._is_subscribed(self.hid.input_keyboard):
                    summary = ""
                    try:
                        summary = f" → {self.hid._cccd_snapshot()}"  # optional
                    except Exception:
                        pass
                    print(f"[hid] NOTE: keyboard not notifying (CCCD disabled){summary}")
                    self._warned_keyboard = True
            except Exception:
                pass
        self.hid.send_keyboard(self._kb_payload(keys, self._kb_mods))

    async def _repeat_keyboard(self):
        try:
            while self._kb_usage is not None:
                self._kb_apply()
                await asyncio.sleep(KEEPALIVE_MS / 1000)
        except asyncio.CancelledError:
            pass

    def key_down(self, usage: int, modifiers: int = 0):
        self._kb_usage = usage
        self._kb_mods = modifiers
        self._kb_apply()

        if self._kb_task:
            self._kb_task.cancel()
        self._kb_task = asyncio.create_task(self._repeat_keyboard())

    def key_up(self):
        if self._kb_task:
            self._kb_task.cancel()
            self._kb_task = None

        self._kb_usage = None
        self._kb_apply()

    # ---------------- Consumer (Report ID 2) ----------------
    def _cc_payload(self, usage: int) -> bytes:
        return usage.to_bytes(2, "little")

    def _cc_apply(self):
        # Warn once only if HID says "not subscribed"
        if not self._warned_consumer:
            try:
                if hasattr(self.hid, "_is_subscribed") and not self.hid._is_subscribed(self.hid.input_consumer):
                    summary = ""
                    try:
                        summary = f" → {self.hid._cccd_snapshot()}"  # optional
                    except Exception:
                        pass
                    print(f"[hid] NOTE: consumer not notifying (CCCD disabled){summary}")
                    self._warned_consumer = True
            except Exception:
                pass

        if self._cc_usage:
            self.hid.send_consumer(self._cc_payload(self._cc_usage))
        else:
            # explicit neutral on release
            self.hid.send_consumer(self._cc_payload(0))

    async def _repeat_consumer(self):
        try:
            while self._cc_usage is not None:
                self._cc_apply()
                await asyncio.sleep(KEEPALIVE_MS / 1000)
        except asyncio.CancelledError:
            pass

    def consumer_down(self, usage: int):
        self._cc_usage = usage
        self._cc_apply()

        if self._cc_task:
            self._cc_task.cancel()
        self._cc_task = asyncio.create_task(self._repeat_consumer())

    def consumer_up(self):
        if self._cc_task:
            self._cc_task.cancel()
            self._cc_task = None

        self._cc_usage = None
        self._cc_apply()
        
        
    async def consumer_tap(self, usage: int, hold_ms: int = 60):
        """Press and release a consumer control key with a hold duration."""
        self.consumer_down(usage)
        await asyncio.sleep(hold_ms / 1000)
        self.consumer_up()