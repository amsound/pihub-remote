# File: mqtt_stats_pi.py
from __future__ import annotations

import os
import shutil
import socket
import subprocess
from typing import Dict, Any, List


def _read_first(path: str) -> str | None:
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except Exception:
        return None


def _hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "unknown"


def _primary_ip() -> str | None:
    # UDP trick: no packets sent, just chooses an outbound interface
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def _cpu_temp_c() -> float | None:
    raw = _read_first("/sys/class/thermal/thermal_zone0/temp")
    if raw and raw.isdigit():
        try:
            val = int(raw)
            return round(val / 1000.0, 1) if val > 1000 else float(val)
        except Exception:
            pass
    if shutil.which("vcgencmd"):
        try:
            out = subprocess.check_output(["vcgencmd", "measure_temp"], text=True)
            # e.g. temp=43.0'C
            parts = out.strip().replace("'C", "").split("=")
            if len(parts) == 2:
                return round(float(parts[1]), 1)
        except Exception:
            pass
    return None


def _load_pct() -> float | None:
    try:
        la1 = os.getloadavg()[0]
        cores = os.cpu_count() or 1
        pct = (la1 / float(cores)) * 100.0
        return round(pct, 1)
    except Exception:
        return None


def _mem_used_pct() -> float | None:
    try:
        meminfo = {}
        with open("/proc/meminfo", "r") as f:
            for line in f:
                k, v = line.split(":", 1)
                meminfo[k] = int(v.strip().split()[0])  # kB
        total = meminfo.get("MemTotal")
        avail = meminfo.get("MemAvailable")
        if total and avail is not None:
            used = total - avail
            return round(100.0 * used / total, 1)
    except Exception:
        pass
    return None


def _disk_used_pct(path: str = "/") -> float | None:
    try:
        total, used, free = shutil.disk_usage(path)
        return round(100.0 * used / total, 1)
    except Exception:
        return None


def _uptime_s() -> int | None:
    try:
        raw = _read_first("/proc/uptime")
        if raw:
            return int(float(raw.split()[0]))
    except Exception:
        pass
    return None


def _bt_connected() -> List[str] | None:
    # Requires bluetoothctl; returns list of MACs or [] if none
    if not shutil.which("bluetoothctl"):
        return None
    try:
        out = subprocess.check_output(["bluetoothctl", "devices", "Connected"], text=True)
        macs: List[str] = []
        for line in out.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[0] == "Device":
                macs.append(parts[1])
        return macs
    except Exception:
        return None


def _pi_undervolt_flags() -> tuple[bool | None, bool | None]:
    # Parse vcgencmd get_throttled (bit0=current UV, bit16=UV has occurred)
    if not shutil.which("vcgencmd"):
        return (None, None)
    try:
        out = subprocess.check_output(["vcgencmd", "get_throttled"], text=True)
        # e.g. throttled=0x50005
        hexpart = out.strip().split("=", 1)[-1]
        val = int(hexpart, 16)
        uv_now = bool(val & 0x1)
        uv_ever = bool(val & 0x10000)
        return (uv_now, uv_ever)
    except Exception:
        return (None, None)


def get_stats() -> Dict[str, Any]:
    """Return a dict of Pi stats to embed in status JSON under `attr`.
    Safe on non-Pi Linux; fields are None if not available.
    """
    bt = _bt_connected()
    uv_now, uv_ever = _pi_undervolt_flags()
    return {
        "host": _hostname(),
        "ip_addr": _primary_ip(),
        "uptime_s": _uptime_s(),
        "last_activity_cmd": None,     # filled by runner if desired
        "last_ha_service": None,       # filled by runner if desired
        "bt_connected_count": (len(bt) if bt is not None else None),
        "bt_connected_macs": (", ".join(bt) if bt else "-"),
        "cpu_load_pct": _load_pct(),
        "cpu_temp_c": _cpu_temp_c(),
        "disk_used_pct": _disk_used_pct("/"),
        "mem_used_pct": _mem_used_pct(),
        "pi_undervolt": uv_now,
        "pi_undervolt_ever": uv_ever,
    }
