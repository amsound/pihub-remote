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
    # keep your default behavior (MSC only). Flip to False temporarily if we discover the device
    # isn’t sending MSC for some reason.
    msc_only: bool = True,
    debug_unmapped: bool = False,
    debug_trace: bool = False,
):
    if stop_event is None:
        stop_event = asyncio.Event()

    async def _handle(dev: InputDevice):
        grabbed = False
        if rcfg.grab:
            try:
                dev.grab()
                grabbed = True
                print(f"[remote] grabbed {rcfg.path}")
            except PermissionError:
                print("[remote] grab failed (permission). Add user to 'input' group or run with sudo.")
            except OSError as e:
                print(f"[remote] grab failed: {e}")

        last_msc_scan: str | None = None

        try:
            async for ev in dev.async_read_loop():
                if stop_event.is_set():
                    break

                if ev.type == ecodes.EV_MSC and ev.code == ecodes.MSC_SCAN:
                    last_msc_scan = str(ev.value)
                    if debug_trace:
                        print(f"[remote:trace] MSC_SCAN={last_msc_scan}")
                    continue

                if ev.type != ecodes.EV_KEY:
                    # optionally trace everything
                    if debug_trace:
                        print(f"[remote:trace] type={ev.type} code={ev.code} val={ev.value}")
                    continue

                if ev.value == 1:
                    edge = "down"
                elif ev.value == 0:
                    edge = "up"
                else:
                    # ignore repeats
                    if debug_trace:
                        print(f"[remote:trace] KEY code={ev.code} val=2 (repeat) ignored")
                    continue

                # Decide which identifier to use
                if last_msc_scan is not None:
                    scancode_key = last_msc_scan
                    last_msc_scan = None
                elif msc_only:
                    # You asked for MSC only: if none was seen, this edge is ignored
                    if debug_unmapped:
                        print(f"[remote] no MSC for this KEY (code={ev.code}), skipping ({edge})")
                    continue
                else:
                    scancode_key = str(ev.code)

                logical = rcfg.mapping.get(scancode_key)
                if not logical:
                    if debug_unmapped:
                        print(f"[remote] UNMAPPED id={scancode_key} ({edge})")
                    continue

                # Relay (your app prints “press/release”)
                try:
                    res = on_button(logical, edge)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as e:
                    print(f"[remote] on_button error for {logical} {edge}: {e}")

        finally:
            if grabbed:
                try:
                    dev.ungrab()
                    print(f"[remote] ungrabbed {rcfg.path}")
                except Exception:
                    pass
            try:
                dev.close()
            except Exception:
                pass

    while not stop_event.is_set():
        try:
            dev = InputDevice(rcfg.path)
            # Print the resolved event node so we can spot interface changes (if01/if02 etc.)
            try:
                realp = dev.fn
            except Exception:
                realp = rcfg.path
            name = dev.name or "Unknown"
            print(f"[remote] using {rcfg.path} -> {realp} ({name})")
            await _handle(dev)

        except FileNotFoundError:
            print(f"[remote] device not found; retrying in {retry_backoff:.1f}s")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=retry_backoff)
                break
            except asyncio.TimeoutError:
                continue
        except OSError as e:
            print(f"[remote] device error: {e}; retrying in {retry_backoff:.1f}s")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=retry_backoff)
                break
            except asyncio.TimeoutError:
                continue
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[remote] unexpected error: {e}; retrying in {retry_backoff:.1f}s")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=retry_backoff)
                break
            except asyncio.TimeoutError:
                continue