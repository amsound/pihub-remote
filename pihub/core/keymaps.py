import asyncio, os, time, yaml
from dataclasses import dataclass

@dataclass(frozen=True)
class Keymaps:
    keyboard: dict[str, int]
    consumer: dict[str, int]

def _load_yaml(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data

def load_keymaps(path: str) -> Keymaps:
    data = _load_yaml(path)
    kb = data.get("keyboard", {}) or {}
    cc = data.get("consumer_usages", {}) or {}
    # normalize keys to strings, values to ints
    kb = {str(k): int(v) for k, v in kb.items()}
    cc = {str(k): int(v) for k, v in cc.items()}
    return Keymaps(kb, cc)

async def watch_keymaps(path: str, on_reload, *, poll=0.5):
    """Call on_reload(Keymaps) when file mtime changes. Never throws."""
    last_mtime = 0.0
    km = None
    while True:
        try:
            m = os.path.getmtime(path)
            if m != last_mtime:
                last_mtime = m
                km = load_keymaps(path)
                on_reload(km)
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[keymaps] reload error: {e}")
        await asyncio.sleep(poll)
