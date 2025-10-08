# pihub/pyatv/atv_service.py
from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from dataclasses import dataclass
from typing import Optional

from pyatv import scan, connect, exceptions as atv_exceptions
from pyatv.const import Protocol, DeviceState
try:
    from pyatv.const import InputAction
    HAS_INPUT_ACTION = True
except Exception:  # pragma: no cover
    HAS_INPUT_ACTION = False
    class InputAction:  # minimal shim
        SingleTap = 0
        DoubleTap = 1
        Hold = 2

# Optional enum; some builds don’t expose Key
try:
    from pyatv.const import Key
    HAS_KEY_ENUM = True
except Exception:  # pragma: no cover
    HAS_KEY_ENUM = False
    Key = None  # type: ignore[assignment]

from pyatv.interface import AppleTV as ATVInterface, PushListener

_LOG = logging.getLogger("pihub.atv")

# value used by press()/release() across builds
_KEYVAL = {
    "left":   (Key.Left   if HAS_KEY_ENUM else "left"),
    "right":  (Key.Right  if HAS_KEY_ENUM else "right"),
    "up":     (Key.Up     if HAS_KEY_ENUM else "up"),
    "down":   (Key.Down   if HAS_KEY_ENUM else "down"),
    "select": (Key.Select if HAS_KEY_ENUM else "select"),
    "menu":   (Key.Menu   if HAS_KEY_ENUM else "menu"),
    "home":   (Key.Home   if HAS_KEY_ENUM else "home"),
}

# Buttons that accept InputAction (tap/double/hold)
_ACTION_KEYS = {"up", "down", "left", "right", "select", "menu", "home"}
# Transport-like methods: tap-only (no action=)
_TAP_ONLY_KEYS = {
    "play", "pause", "play_pause", "stop",
    "next", "previous",
    "skip_forward", "skip_backward", "channel_up", "channel_down",
}
_ALLOWED = _ACTION_KEYS | _TAP_ONLY_KEYS


# ---------- helper (CHUNK B): put at module level, above the class ----------
async def _call_method(rc, name: str, val=None) -> None:
    """Call rc.<name>([val]) and adapt to builds where release() takes no arg.
    Why: some pyatv versions have release(), others release(key)."""
    fn = getattr(rc, name, None)
    if not callable(fn):
        return
    try:
        sig = inspect.signature(fn)
        # Bound method (no 'self'); count required positional-only/positional-or-keyword params
        required = [
            p for p in sig.parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.default is p.empty
        ]
        if len(required) == 0:
            res = fn()
        elif len(required) >= 1 and val is not None:
            res = fn(val)
        else:
            res = fn()
        if asyncio.iscoroutine(res):
            await res
    except TypeError:
        # Fallback: safest is call without arg
        res = fn()
        if asyncio.iscoroutine(res):
            await res
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PyAtvCreds:
    address: str
    companion: str
    mrp: Optional[str] = None       # optional; harmless if unused on modern tvOS
    airplay: Optional[str] = None   # AirPlay cred (enables MRP-over-AirPlay)


class _PihubPushListener(PushListener):
    def __init__(self, svc: "AppleTvService") -> None:
        self._svc = svc

    def playstatus_update(self, updater, playstatus) -> None:  # type: ignore[override]
        try:
            prev = self._svc._device_state
            state = getattr(playstatus, "device_state", None)
            self._svc._device_state = state
            if state != prev:
                is_playback = state in (DeviceState.Playing, DeviceState.Paused)
                _LOG.info("ATV state=%s playback_active=%s", state, "true" if is_playback else "false")
            self._svc._emit({"device_state": str(state)})
        except Exception:
            _LOG.exception("[pyatv] push playstatus_update error")

    # Some builds require this abstract; it’s ok to no-op
    def playstatus_error(self, updater, exception) -> None:  # type: ignore[override]
        _LOG.warning("[pyatv] push playstatus_error: %s", exception)

    def connection_lost(self, exception) -> None:  # type: ignore[override]
        self._svc._device_state = None
        if exception:
            _LOG.warning("[pyatv] push connection_lost: %s", exception)


class AppleTvService:
    """Lean pyatv remote service: InputAction gestures + press/release for scrub."""

    def __init__(
        self,
        creds: PyAtvCreds,
        on_state: Optional[Callable[[dict], None]] = None,
        *,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        self._loop = loop or asyncio.get_event_loop()
        self._creds = creds
        self._on_state = on_state

        self._atv: ATVInterface | None = None
        self._connected_evt = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stop_evt = asyncio.Event()

        self._device_state: DeviceState | None = None
        self._listener = _PihubPushListener(self)

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_evt.clear()
        self._task = asyncio.create_task(self._run(), name="AppleTvService")

    async def stop(self) -> None:
        self._stop_evt.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._disconnect()

    # ── gestures (InputAction) ─────────────────────────────────────────────
    async def tap(self, key: str) -> None:
        name = self._norm(key)
        if name:
            await self._send(name, "tap")

    async def hold(self, key: str, ms: int | None = None) -> None:
        # duration is ignored by Companion; semantic Hold is what matters
        name = self._norm(key)
        if name:
            await self._send(name, "hold")

    async def double(self, key: str) -> None:
        name = self._norm(key)
        if name:
            await self._send(name, "double")

    # ── low-level press/release for true “held” scrub ──────────────────────
    async def press(self, key: str) -> None:
        name = self._norm(key)
        if not name:
            return
        atv = await self._ensure()
        rc = atv.remote_control
        val = _KEYVAL.get(name, name)
        await _call_method(rc, "press", val)  # press(key) on all builds

    async def release(self, key: str) -> None:
        name = self._norm(key)
        if not name:
            return
        atv = await self._ensure()
        rc = atv.remote_control
        val = _KEYVAL.get(name, name)
        # Some builds expect release() with NO argument; helper adapts
        await _call_method(rc, "release", val)

    # ── playback helper ────────────────────────────────────────────────────
    def is_playback_active(self) -> bool:
        return self._device_state in (DeviceState.Playing, DeviceState.Paused)

    # ── internals ──────────────────────────────────────────────────────────
    def _norm(self, key: str | None) -> str | None:
        if not key:
            return None
        k = key.strip().lower()
        if k not in _ALLOWED:
            _LOG.warning("[pyatv] unsupported key: %r", key)
            return None
        return k

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop_evt.is_set():
            try:
                await self._connect_once()
                self._connected_evt.set()
                self._emit({"connected": True})
                await self._stop_evt.wait()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected_evt.clear()
                self._emit({"connected": False, "error": str(e)})
                _LOG.error("[pyatv] connection error: %s", e)
                await self._disconnect()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)
            else:
                break

    async def _connect_once(self) -> None:
        await self._disconnect()

        configs = await scan(loop=self._loop, hosts=[self._creds.address])
        if not configs:
            raise RuntimeError("No Apple TV found at address " + self._creds.address)

        # Prefer a config that includes Companion
        chosen = None
        for c in configs:
            protos = [s.protocol for s in c.services]
            _LOG.info("ATV services at %s: %s", self._creds.address, protos)
            if Protocol.Companion in protos:
                chosen = c
                break
        chosen = chosen or configs[0]

        # Set creds (present → set; absent → skip)
        if self._creds.companion:
            chosen.set_credentials(Protocol.Companion, self._creds.companion)
        if self._creds.mrp:
            chosen.set_credentials(Protocol.MRP, self._creds.mrp)
        if self._creds.airplay:
            chosen.set_credentials(Protocol.AirPlay, self._creds.airplay)

        _LOG.info("[pyatv] connecting to %s", self._creds.address)
        self._atv = await connect(chosen, loop=self._loop)

        # Start push updates; with AirPlay paired, MRP-over-AirPlay provides state
        try:
            self._atv.push_updater.listener = self._listener
            self._atv.push_updater.start()
            _LOG.info("push_updater started")
        except Exception as e:
            _LOG.info("push_updater unavailable: %s", e)

        _ = self._atv.remote_control  # probe

    async def _disconnect(self) -> None:
        if self._atv:
            with contextlib.suppress(Exception):
                try:
                    self._atv.push_updater.stop()
                except Exception:
                    pass
            with contextlib.suppress(Exception):
                await self._atv.close()
        self._atv = None
        self._connected_evt.clear()
        self._device_state = None

    async def _ensure(self) -> ATVInterface:
        if self._atv is None:
            try:
                await asyncio.wait_for(self._connected_evt.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                raise RuntimeError("AppleTV not connected")
        assert self._atv is not None
        return self._atv

    async def _send(self, name: str, action: str) -> None:
        """Dispatch a gesture to pyatv. Transport keys are tap-only."""
        try:
            atv = await self._ensure()
            rc = atv.remote_control
            method = getattr(rc, name, None)
            if not callable(method):
                _LOG.warning("[pyatv] method %s() not found", name)
                return
    
            # Transport: no action= supported (next/previous/stop/skip_* etc.)
            if name in _TAP_ONLY_KEYS or not HAS_INPUT_ACTION:
                res = method()
                if asyncio.iscoroutine(res):
                    await res
                return
    
            # Gesture-capable keys (up/down/left/right/select/menu/home)
            if action == "tap":
                # IMPORTANT: call without action kwarg for SingleTap on 0.16.1
                res = method()
            elif action == "double":
                res = method(action=InputAction.DoubleTap)
            elif action == "hold":
                res = method(action=InputAction.Hold)
            else:
                _LOG.warning("[pyatv] unknown action=%s for %s", action, name)
                return
    
            if asyncio.iscoroutine(res):
                await res
    
        except atv_exceptions.AuthenticationError:
            _LOG.error("[pyatv] authentication error (bad credentials)")
            self._emit({"connected": False, "auth_error": True})
            await self._disconnect()
        except Exception as e:
            _LOG.error("[pyatv] send(%s, action=%s) failed: %s", name, action, e)

        except atv_exceptions.AuthenticationError:
            _LOG.error("[pyatv] authentication error (bad credentials)")
            self._emit({"connected": False, "auth_error": True})
            await self._disconnect()
        except Exception as e:
            _LOG.error("[pyatv] send(%s, action=%s) failed: %s", name, action, e)

    def _emit(self, state: dict) -> None:
        if self._on_state:
            try:
                self._on_state(state)
            except Exception:
                _LOG.exception("[pyatv] on_state callback error")