#!/usr/bin/env python3
from __future__ import annotations
import asyncio
import contextlib
from dataclasses import dataclass
from typing import Callable, Optional, Dict

from evdev import InputDevice, ecodes

@dataclass
class RemoteConfig:
    path: str                     # /dev/input/by-id/...
    mapping: Dict[str, str]       # scancode (string) -> logical name
    grab: bool = True             # exclusive grab

def load_remote_config(path: str) -> RemoteConfig:
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    dev = data.get("device") or {}
    by_id = dev.get("by_id")
    if not by_id or not isinstance(by_id, str):
        raise ValueError("remote_keymap.yaml: device.by_id must be a non-empty string")

    raw_map = (data.get("mapping") or {})
    if not isinstance(raw_map, dict):
        raise ValueError("remote_keymap.yaml: mapping must be a dict")

    mapping: Dict[str, str] = {str(k): str(v) for k, v in raw_map.items()}
    grab = bool(data.get("grab", True))
    return RemoteConfig(path=by_id, mapping=mapping, grab=grab)

async def read_events_scancode(
    rcfg: RemoteConfig,
    on_button: Callable[[str, str], "asyncio.Future|None"],
    stop_event: Optional[asyncio.Event] = None,
    retry_backoff: float = 1.0,
    *,
    msc_only: bool = True,
    debug_unmapped: bool = False,
    debug_trace: bool = False,
    on_disconnect: Optional[Callable[[], None]] = None,
    on_reconnect: Optional[Callable[[], None]] = None,
    log: Optional[Callable[[str], None]] = None,
):
    """MSC-scan-only reader with robust reopen + jittered backoff + breadcrumb logs."""
    import asyncio, errno, random, contextlib
    from evdev import InputDevice, ecodes

    if stop_event is None:
        stop_event = asyncio.Event()
    if log is None:
        log = print

    backoff = max(0.2, float(retry_backoff))
    backoff_max = 10.0

    def jitter(s: float) -> float:
        return s * random.uniform(0.8, 1.2)

    was_connected = False  # ← breadcrumb state

    while not stop_event.is_set():
        dev = None
        try:
            dev = InputDevice(rcfg.path)

            grabbed = False
            if rcfg.grab:
                try:
                    dev.grab()
                    grabbed = True
                    if debug_trace:
                        log(f"[remote] grabbed {rcfg.path}")
                except PermissionError:
                    log("[remote] grab failed (permission). Add user to 'input' group or run with sudo.")
                    await asyncio.sleep(jitter(min(backoff_max, 15.0)))
                    continue
                except OSError as e:
                    if debug_trace:
                        log(f"[remote] grab failed: {e}")

            # on successful open (and optional grab)
            if on_reconnect:
                with contextlib.suppress(Exception):
                    on_reconnect()

            # ── breadcrumb: connected once ───────────────────────────────────────
            if not was_connected:
                log(f"[remote] input device connected ({rcfg.path})")
            was_connected = True
            # ────────────────────────────────────────────────────────────────────

            last_msc: str | None = None
            backoff = max(0.2, float(retry_backoff))  # reset after success

            async for ev in dev.async_read_loop():
                if stop_event.is_set():
                    break

                if ev.type == ecodes.EV_MSC and ev.code == ecodes.MSC_SCAN:
                    v = str(ev.value)
                    if debug_trace and v != last_msc:
                        log(f"[remote:trace] MSC_SCAN={v}")
                    last_msc = v
                    continue

                if ev.type != ecodes.EV_KEY:
                    if debug_trace:
                        log(f"[remote:trace] type={ev.type} code={ev.code} val={ev.value}")
                    continue

                if ev.value == 1:
                    edge = "down"
                elif ev.value == 0:
                    edge = "up"
                else:
                    if debug_trace:
                        log(f"[remote:trace] KEY repeat ignored (val=2)")
                    continue

                if msc_only and not last_msc:
                    if debug_trace:
                        log("[remote:trace] KEY without prior MSC_SCAN (ignored)")
                    continue

                sc = last_msc or str(ev.code)
                logical = rcfg.mapping.get(sc)
                if not logical:
                    if debug_unmapped:
                        log(f"[remote] unmapped scan '{sc}' (edge={edge})")
                    continue

                try:
                    res = on_button(logical, edge)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as e:
                    log(f"[remote] on_button error for {logical}/{edge}: {e}")

            # If we ever fall out of the loop, treat as disconnect
            raise OSError(errno.ENODEV, "device read loop ended")

        except FileNotFoundError:
            if on_disconnect:
                with contextlib.suppress(Exception):
                    on_disconnect()
            # ── breadcrumb: disconnected ────────────────────────────────────────
            if was_connected:
                log("[remote] input device disconnected; reopening…")
            was_connected = False
            # ────────────────────────────────────────────────────────────────────
            if debug_trace:
                log(f"[remote] {rcfg.path} not found; retrying…")
            await asyncio.sleep(jitter(backoff))
            backoff = min(backoff_max, backoff * 1.7)
            continue

        except OSError as e:
            if on_disconnect:
                with contextlib.suppress(Exception):
                    on_disconnect()
            # ── breadcrumb: disconnected ────────────────────────────────────────
            if was_connected:
                log("[remote] input device disconnected; reopening…")
            was_connected = False
            # ────────────────────────────────────────────────────────────────────
            if debug_trace:
                log(f"[remote] OSError: {e}; reopening after backoff…")
            await asyncio.sleep(jitter(backoff))
            backoff = min(backoff_max, backoff * 1.7)
            continue

        except Exception as e:
            log(f"[remote] unexpected error: {e}; reopening…")
            await asyncio.sleep(jitter(backoff))
            backoff = min(backoff_max, backoff * 1.7)
            continue

        finally:
            with contextlib.suppress(Exception):
                if dev:
                    dev.ungrab()
            with contextlib.suppress(Exception):
                if dev:
                    dev.close()
