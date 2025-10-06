# pihub/pyatv/atv_service.py
# Lean integration using pyatv Companion protocol with manual key-repeat support

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable
from pyatv import scan, connect
from pyatv.const import Protocol
from pyatv import exceptions as atv_exceptions
from pyatv.interface import AppleTV as ATVInterface

_LOG = logging.getLogger(__name__)

@dataclass
class PyAtvCreds:
    address: str
    companion: str
    airplay: str = ""  # optional AirPlay credential, if needed

class AppleTVController:
    def __init__(self, address: str, companion_id: str, loop: asyncio.AbstractEventLoop = None):
        self.address = address
        self.companion_id = companion_id
        self.loop = loop or asyncio.get_event_loop()
        self._atv: ATVInterface | None = None
        self._lock = asyncio.Lock()
        self._repeat_tasks: dict[str, asyncio.Task] = {}

    async def connect(self) -> ATVInterface | None:
        if self._atv is None:
            _LOG.info("[pyatv] Scanning Companion protocol on %s", self.address)
            try:
                services = await scan(
                    self.loop,
                    hosts=[self.address],
                    protocol=Protocol.Companion
                )
                if not services:
                    _LOG.error("[pyatv] No Companion service found at %s", self.address)
                    return None
                conf = services[0]
                conf.set_credentials(Protocol.Companion, self.companion_id)
                self._atv = await connect(conf, loop=self.loop)
                _LOG.info("[pyatv] Connected to Apple TV at %s", self.address)
            except atv_exceptions.PairingError as e:
                _LOG.error("[pyatv] Pairing error: %s", e)
                self._atv = None
            except Exception as e:
                _LOG.error("[pyatv] Connection failed: %s", e)
                self._atv = None
        return self._atv

    async def _send(self, cmd: str, hold: bool = False):
        async with self._lock:
            atv = await self.connect()
            if atv is None:
                return
            remote = atv.remote_control
            try:
                if hold:
                    _LOG.debug("[pyatv] hold %s", cmd)
                    await remote.press(cmd)
                else:
                    _LOG.debug("[pyatv] tap %s", cmd)
                    method = getattr(remote, cmd, None)
                    if callable(method):
                        await method()
                    else:
                        await remote.press(cmd)
            except Exception as e:
                _LOG.error("[pyatv] Command %s failed: %s", cmd, e)

    def tap(self, key: str):
        """Non-blocking single tap."""
        asyncio.run_coroutine_threadsafe(self._send(key, hold=False), self.loop)

    def hold(self, key: str, ms: int | None = None):
        """Non-blocking press-and-hold for a fixed duration."""
        async def _do_hold():
            await self._send(key, hold=True)
            if ms:
                await asyncio.sleep(ms / 1000)
        asyncio.run_coroutine_threadsafe(_do_hold(), self.loop)

    def start_repeat(self, key: str, interval_ms: int):
        """Begin repeating taps at given interval until stopped."""
        # Cancel existing
        self.stop_repeat(key)
        async def _repeater():
            _LOG.debug("[pyatv] start repeating %s every %dms", key, interval_ms)
            try:
                while True:
                    await self._send(key, hold=False)
                    await asyncio.sleep(interval_ms / 1000)
            except asyncio.CancelledError:
                _LOG.debug("[pyatv] stopped repeating %s", key)
                return
        task = self.loop.create_task(_repeater())
        self._repeat_tasks[key] = task

    def stop_repeat(self, key: str):
        """Stop any ongoing repeat for this key."""
        task = self._repeat_tasks.pop(key, None)
        if task:
            task.cancel()

    def double(self, key: str):
        """Non-blocking double-tap."""
        async def _do_double():
            await self._send(key, hold=False)
            await asyncio.sleep(0.1)
            await self._send(key, hold=False)
        asyncio.run_coroutine_threadsafe(_do_double(), self.loop)

    async def close(self):
        # Cancel all repeat tasks
        for task in self._repeat_tasks.values():
            task.cancel()
        self._repeat_tasks.clear()
        if self._atv:
            await self._atv.close()
            self._atv = None

class AppleTvService:
    def __init__(self, creds: PyAtvCreds, on_state: Callable[[dict], None] = None):
        self.creds = creds
        self.on_state = on_state
        self._controller: AppleTVController | None = None

    async def start(self):
        self._controller = AppleTVController(
            address=self.creds.address,
            companion_id=self.creds.companion
        )
        await self._controller.connect()
        _LOG.info("[pyatv] Service started")

    async def stop(self):
        if self._controller:
            await self._controller.close()
            _LOG.info("[pyatv] Service stopped")

    def tap(self, key: str):
        if self._controller:
            self._controller.tap(key)

    def hold(self, key: str, ms: int | None = None):
        if self._controller:
            self._controller.hold(key, ms)

    def start_repeat(self, key: str, interval_ms: int):
        if self._controller:
            self._controller.start_repeat(key, interval_ms)

    def stop_repeat(self, key: str):
        if self._controller:
            self._controller.stop_repeat(key)

    def double(self, key: str):
        if self._controller:
            self._controller.double(key)
