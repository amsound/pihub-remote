# pihub/pyatv/gestures.py
import asyncio
import time
import contextlib
from typing import Callable, Optional, Dict

class GestureResolver:
    """
    Minimal, polished-feel mapper:
      - Non-arrow keys: tap on UP (classic “press & release”)
      - Arrow keys: synthesize repeating taps while held (DOWN..UP)
      - Optional: hold detection (disabled by default for now)
      - Optional: context-aware swap for L/R during playback (placeholder)
    """

    def __init__(
        self,
        atv_service,                                 # AppleTvService (must have tap() and hold())
        *,
        repeat_keys=frozenset({"up", "down", "left", "right"}),
        repeat_ms: int = 180,                        # repeat cadence (ms) for arrows
        hold_ms: int = 600,                          # long-press threshold (ms)
        enable_hold: bool = False,                   # leave off for now
        get_context: Optional[Callable[[], str]] = None,  # returns e.g. "playing" or "menu"
    ):
        self.svc = atv_service
        self.repeat_keys = set(k.lower() for k in repeat_keys)
        self.repeat_ms = repeat_ms
        self.hold_ms = hold_ms
        self.enable_hold = enable_hold
        self.get_context = get_context

        # per-key state
        self._state: Dict[str, dict] = {}

    # ---- public entry point from dispatcher ----
    async def on_edge(self, key: str, edge: str):
        key = key.lower()
        edge = edge.lower()
        if edge == "down":
            await self._on_down(key)
        elif edge == "up":
            await self._on_up(key)
        # (we ignore 'repeat' since evdev repeats are filtered; we synthesize)

    # ---- internals ----
    async def _on_down(self, key: str):
        st = self._state.setdefault(key, {"down_ts": 0.0, "task": None, "hold_sent": False})
        st["down_ts"] = time.monotonic()
        st["hold_sent"] = False

        # Context hook (placeholder)
        ctx = None
        if self.get_context:
            try:
                ctx = self.get_context()
            except Exception:
                ctx = None

        # If arrow keys: start synthetic repeat task
        if key in self.repeat_keys:
            # Optional future: if ctx == "playing" and key in {"left","right"}:
            #     await self.svc.hold(key) ; st["hold_sent"]=True ; return
            if st["task"]:
                st["task"].cancel()
            st["task"] = asyncio.create_task(self._repeat_tapper(key), name=f"atv_repeat_{key}")
            # send first tap immediately for snappy UX
            await self._safe_tap(key)
            return

        # Non-arrow: we’ll decide tap vs hold on UP (classic behavior)

    async def _on_up(self, key: str):
        st = self._state.get(key)
        if not st:
            return

        # stop any repeat task
        t = st.get("task")
        if t:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
            st["task"] = None

        # if we already sent a hold, nothing to do
        if st.get("hold_sent"):
            return

        # decide tap vs hold (hold disabled for now)
        press_ms = int((time.monotonic() - st.get("down_ts", 0.0)) * 1000)
        if self.enable_hold and press_ms >= self.hold_ms:
            await self._safe_hold(key)
        else:
            # Plain tap for non-arrows; for arrows we tapped continuously while down
            if key not in self.repeat_keys:
                await self._safe_tap(key)

    async def _repeat_tapper(self, key: str):
        try:
            while True:
                await asyncio.sleep(self.repeat_ms / 1000.0)
                await self._safe_tap(key)
        except asyncio.CancelledError:
            pass

    async def _safe_tap(self, key: str):
        try:
            await self.svc.tap(key)
        except Exception as e:
            print(f"[atv] tap({key}) error: {e}")

    async def _safe_hold(self, key: str):
        try:
            await self.svc.hold(key)
            st = self._state.get(key)
            if st:
                st["hold_sent"] = True
        except Exception as e:
            print(f"[atv] hold({key}) error: {e}")
