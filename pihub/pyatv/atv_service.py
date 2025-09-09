# pihub/pyatv/atv_service.py
import asyncio, json, contextlib
from dataclasses import dataclass
from typing import Callable, Optional

from pyatv import scan, connect, exceptions
from pyatv.const import PowerState, MediaType, DeviceState, Protocol

@dataclass
class PyAtvCreds:
    address: Optional[str] = None          # e.g. "192.168.70.44"
    identifier: Optional[str] = None       # e.g. "FA:1A:26:07:F0:A7"
    companion: str | None = None
    airplay: str | None = None

class AppleTvService:
    def __init__(self, creds: PyAtvCreds, on_state: Callable[[dict], None]):
        self.creds = creds
        self.on_state = on_state  # sync callback (app schedules MQTT)
        self.atv = None
        self.push_updater = None
        self._task = None
        self._stop = asyncio.Event()
        self._poll_task = None
        self._last_state: dict | None = None
        self._last_app_shell_at: float = 0.0   # rate-limit shell fallback
        self._shell_id: str | None = None
        
    async def _poll_loop(self):
        # Periodic state publish as a safety net alongside push updates
        try:
            while not self._stop.is_set():
                await self._emit_state(settle_ms=0)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass
        
    # --- add inside class AppleTvService ---
    def _dump_feature_states(self):
        try:
            from pyatv.interface import FeatureName
            feat = self.atv.features
            names = [
                FeatureName.Select, FeatureName.Up, FeatureName.Down,
                FeatureName.Left, FeatureName.Right, FeatureName.Menu,
                FeatureName.Home, FeatureName.PlayPause,
            ]
            states = {n.name: feat.get_feature(n).state.name for n in names}
            print(f"[pyatv] features: {states}")
        except Exception as e:
            print(f"[pyatv] feature dump failed: {e}")

    # ---- public control API ----
    async def tap(self, key: str):
        d = self.atv
        if not d:
            return
        from pyatv.interface import FeatureName, FeatureState
        rc = d.remote_control
        feat = d.features

        key = (key or "").lower()
        mapping = {
            "select": FeatureName.Select,
            "ok":     FeatureName.Select,
            "menu":   FeatureName.Menu,
            "home":   FeatureName.Home,
            "up":     FeatureName.Up,
            "down":   FeatureName.Down,
            "left":   FeatureName.Left,
            "right":  FeatureName.Right,
            "play":   FeatureName.PlayPause,
            "pause":  FeatureName.PlayPause,
            "play_pause": FeatureName.PlayPause,
            "power":  None,  # handled via power interface
        }

        if key == "power":
            await d.power.turn_on()
            asyncio.create_task(self._emit_state(settle_ms=250))
            return

        fn = mapping.get(key)
        if not fn:
            print(f"[atv] tap({key}) unknown key")
            return

        state = feat.get_feature(fn).state
        if state != FeatureState.Available:
            print(f"[atv] tap({key}) not supported (feature={fn.name} state={state.name})")
            with contextlib.suppress(Exception):
                self._dump_feature_states()
            return

        try:
            if   fn is FeatureName.Select:      await rc.select()
            elif fn is FeatureName.Menu:        await rc.menu()
            elif fn is FeatureName.Home:        await rc.home()
            elif fn is FeatureName.Up:          await rc.up()
            elif fn is FeatureName.Down:        await rc.down()
            elif fn is FeatureName.Left:        await rc.left()
            elif fn is FeatureName.Right:       await rc.right()
            elif fn is FeatureName.PlayPause:   await rc.play_pause()
        except Exception as e:
            print(f"[atv] tap({key}) error: {e}")
            return

        # publish updated state after action
        asyncio.create_task(self._emit_state(settle_ms=250))

    async def hold(self, key: str, ms: int | None = None):
        d = self.atv
        if not d:
            return
        from pyatv.interface import FeatureName, FeatureState
        rc = d.remote_control
        feat = d.features
        key = (key or "").lower()

        if key == "power":
            await d.power.turn_off()
            asyncio.create_task(self._emit_state(settle_ms=250))
            return

        mapping = {
            "select": FeatureName.Select,
            "ok":     FeatureName.Select,
            "menu":   FeatureName.Menu,
            "home":   FeatureName.Home,
            "left":   FeatureName.Left,
            "right":  FeatureName.Right,
        }
        fn = mapping.get(key)
        if not fn:
            print(f"[atv] hold({key}) unknown/unsupported hold key")
            return

        state = feat.get_feature(fn).state
        if state != FeatureState.Available:
            print(f"[atv] hold({key}) not supported (feature={fn.name} state={state.name})")
            with contextlib.suppress(Exception):
                self._dump_feature_states()
            return

        try:
            if   fn is FeatureName.Select:  await rc.select(hold=True)
            elif fn is FeatureName.Menu:    await rc.menu(hold=True)
            elif fn is FeatureName.Home:    await rc.home(hold=True)
            elif fn is FeatureName.Left:    await rc.left(hold=True)
            elif fn is FeatureName.Right:   await rc.right(hold=True)
        except Exception as e:
            print(f"[atv] hold({key}) error: {e}")
            return

        # publish updated state after action
        asyncio.create_task(self._emit_state(settle_ms=250))

    async def double(self, key: str):
        # optional: implement if you need double-tap semantics later
        print(f"[atv] double({key}) not implemented")

    # ---- lifecycle ----
    async def start(self):
        self._task = asyncio.create_task(self._runner(), name="pyatv_runner")

    async def stop(self):
        self._stop.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._disconnect()

    # ---- internals ----
    async def _runner(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._connect()
                backoff = 1.0
                await self._stop.wait()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[pyatv] runner error: {e}; retry {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
    
    async def _connect(self):
        loop = asyncio.get_running_loop()
    
        # Prefer matching by identifier if provided (exact same one you used with atvremote)
        scan_kwargs = {}
        if self.creds.address:
            scan_kwargs["hosts"] = [self.creds.address]
        # pyatv supports "identifiers" list
        if self.creds.identifier:
            scan_kwargs["identifiers"] = [self.creds.identifier]
    
        results = await scan(loop=loop, **scan_kwargs)
        if not results:
            raise RuntimeError("Apple TV not found")
    
        # Prefer an entry with Companion protocol
        def has_proto(conf, proto):
            try:
                return any(s.protocol == proto for s in conf.services)
            except Exception:
                return False
    
        # If identifier is set, pick the one that actually matches; else first companion; else first
        chosen = None
        if self.creds.identifier:
            for c in results:
                try:
                    if getattr(c, "identifier", None) == self.creds.identifier:
                        chosen = c
                        break
                except Exception:
                    pass
        if not chosen:
            chosen = next((c for c in results if has_proto(c, Protocol.Companion)), results[0])
    
        conf = chosen
    
        # Set credentials with Protocol enums (not strings)
        if self.creds.companion:
            conf.set_credentials(Protocol.Companion, self.creds.companion)
        if self.creds.airplay:
            conf.set_credentials(Protocol.AirPlay, self.creds.airplay)
    
        self.atv = await connect(conf, loop=loop)
        
        # Prefer a stable unique id for CLI fallbacks
        try:
            # pyatv ≥ 0.14
            self._shell_id = getattr(self.atv.device_info, "unique_id", None) or None
        except Exception:
            self._shell_id = None
        # fallback to scan config identifier if needed
        if not self._shell_id:
            try:
                self._shell_id = getattr(conf, "identifier", None) or None
            except Exception:
                pass
    
        # Log exactly what we connected to
        try:
            protos = [getattr(s.protocol, "name", s.protocol) for s in conf.services]
            print(f"[pyatv] connected to {getattr(conf,'address',None)} id={getattr(conf,'identifier',None)}; services={protos}")
        except Exception:
            pass
    
        # Start push updates
        self.push_updater = self.atv.push_updater
        self.push_updater.listener = _PushListener(self._emit_state)
        self.push_updater.start()
    
        # Immediate probes so we *see* what the box returns now
        try:
            p = await self.atv.power.power_state()
            print(f"[pyatv] probe power_state={p!r}")
        except Exception as e:
            print(f"[pyatv] probe power_state error: {e}")
    
        try:
            cur = await self.atv.apps.current_app()
            if cur:
                print(f"[pyatv] probe current_app name={getattr(cur,'name',None)!r} id={getattr(cur,'identifier',None)!r}")
            else:
                print("[pyatv] probe current_app = None")
        except Exception as e:
            print(f"[pyatv] probe current_app error: {e}")
    
        await self._emit_state(settle_ms=150)
    
        # One-shot feature dump for diagnostics
        with contextlib.suppress(Exception):
            self._dump_feature_states()
    
        # Start poll loop (idempotent)
        if not self._poll_task:
            self._poll_task = asyncio.create_task(self._poll_loop(), name="pyatv_poll")

    async def _disconnect(self):
        if self.push_updater:
            with contextlib.suppress(Exception):
                self.push_updater.stop()
        if self.atv:
            with contextlib.suppress(Exception):
                await self.atv.close()
        self.atv = None
        self.push_updater = None
        
        if self._poll_task:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None
    
    async def _try_shell_current_app(self) -> str | None:
        """Best-effort app name via `atvremote … app`. Prefer --id; fallback to --address."""
        now = asyncio.get_running_loop().time()
        if now - self._last_app_shell_at < 3.0:
            return None
        self._last_app_shell_at = now
    
        args = None
        if self._shell_id:
            args = ["atvremote", "--id", self._shell_id, "app"]
        else:
            addr = (self.creds.address or "").strip()
            if addr:
                args = ["atvremote", "--address", addr, "app"]
        if not args:
            return None
    
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=1.5)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                return None
    
            line = (stdout or b"").decode(errors="ignore").strip()
            # Expected: "App: Plex (com.plexapp.plex)"
            if line.startswith("App: "):
                rest = line[5:].strip()
                name = rest.split("(", 1)[0].strip() if "(" in rest else rest
                return name or None
        except Exception:
            pass
        return None
    
    async def _emit_state(self, *, settle_ms: int = 0):
        if settle_ms:
            await asyncio.sleep(settle_ms / 1000.0)
    
        d = self.atv
        if not d:
            state = {"power": "unknown", "play_state": "unknown", "app": None, "title": None}
            if state != self._last_state:
                print(f"[pyatv→mqtt] {state}")
                self.on_state(state)
                self._last_state = state
            return
    
        # ---- Power (property) ----
        power = "unknown"
        try:
            pwr = d.power.power_state  # property, no await
            if pwr == PowerState.On:
                power = "on"
            elif pwr == PowerState.Off:
                power = "off"
        except Exception as e:
            # Keep quiet unless you want logs:
            # print(f"[pyatv] power_state error: {e}")
            pass
    
        # ---- Playback snapshot ----
        play_state = "unknown"
        title = None
        app_name = None
        try:
            playing = await d.metadata.playing()
            if playing is not None:
                st = getattr(playing, "device_state", None)
                if   st == DeviceState.Playing: play_state = "playing"
                elif st == DeviceState.Paused:  play_state = "paused"
                elif st == DeviceState.Idle:    play_state = "menu"
                elif st == DeviceState.Stopped: play_state = "stopped"
    
                title = getattr(playing, "title", None)
    
                # primary: Companion-provided app on the Playing object
                app_obj = getattr(playing, "app", None)
                if app_obj:
                    app_name = getattr(app_obj, "name", None) or getattr(app_obj, "identifier", None)
        except Exception:
            pass
    
        # Heuristic: if UI is responsive but power unknown, treat as on
        if power == "unknown" and play_state in ("playing", "paused", "menu"):
            power = "on"
    
        # ---- Shell fallback for app name (only if still None) ----
        if app_name is None:
            app_name = await self._try_shell_current_app()
    
        state = {"power": power, "play_state": play_state, "app": app_name, "title": title}
    
        # publish only on change to stop scroll spam
        if state != self._last_state:
            print(f"[pyatv→mqtt] {state}")
            self.on_state(state)
            self._last_state = state
                    
class _PushListener:
    def __init__(self, emit):
        self.emit = emit

    def connection_lost(self, _exc):
        # On disconnect, publish whatever we can right away
        asyncio.create_task(self.emit(settle_ms=0))

    def playstatus_update(self, _updater, _playing):
        # small settle so play/pause reports correctly
        asyncio.create_task(self.emit(settle_ms=150))

    # New: react to power state push updates too
    def powerstate_update(self, _updater, _power_state):
        asyncio.create_task(self.emit(settle_ms=0))