# pihub/ha_mqtt/mqtt_cmd.py
import asyncio, contextlib, json, os, subprocess, time
from dataclasses import dataclass
from aiomqtt import Client, Will, MqttError

# ---- config helpers ---------------------------------------------------------
CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config")
ROOM_YAML  = os.path.join(CONFIG_DIR, "room.yaml")

def _load_room():
    import yaml
    with open(ROOM_YAML, "r", encoding="utf-8") as f:
        y = yaml.safe_load(f) or {}
    m = y.get("mqtt") or {}
    prefix = (m.get("prefix_bridge") or "").rstrip("/")
    if not prefix:
        raise RuntimeError("room.yaml: mqtt.prefix_bridge is required")
    return {
        "host": m.get("host") or "localhost",
        "port": int(m.get("port") or 1883),
        "user": m.get("username"),
        "password": m.get("password"),
        "prefix": prefix,
    }

ALLOW_REBOOT = False  # leave False unless you really want remote reboot

@dataclass
class Cfg:
    host: str
    port: int
    user: str | None
    password: str | None
    prefix: str

# ---- tiny exec helper -------------------------------------------------------
async def run_cmd(cmd: list[str], timeout: float = 8.0) -> tuple[int, str, str]:
    try:
        p = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            out, err = await asyncio.wait_for(p.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                p.kill()
            return 124, "", f"timeout after {timeout}s"
        return p.returncode, out.decode("utf-8","replace"), err.decode("utf-8","replace")
    except FileNotFoundError as e:
        return 127, "", str(e)
    except Exception as e:
        return 1, "", str(e)

# ---- command router ---------------------------------------------------------
async def handle_command(topic_suffix: str, payload: bytes) -> dict:
    text = (payload or b"").decode("utf-8", "replace").strip()
    parts = topic_suffix.split("/", 1)
    now = int(time.time())

    resp = {"status":"error","cmd":topic_suffix,"code":-1,"stdout":"","stderr":"","ts":now}

    # sys/bluetooth {status|restart}
    if len(parts) == 2 and parts[0] == "sys" and parts[1] == "bluetooth":
        if text.lower() == "status":
            print("[cmd] bluetooth status")
            code,out,err = await run_cmd(["systemctl","status","bluetooth","--no-pager"])
            resp.update(status="ok" if code==0 else "error", code=code,
                        stdout=out[-4000:], stderr=err[-2000:])
            return resp
        if text.lower() == "restart":
            print("[cmd] bluetooth restart")
            code,out,err = await run_cmd(["sudo","systemctl","restart","bluetooth"])
            resp.update(status="ok" if code==0 else "error", code=code,
                        stdout=out[-1000:], stderr=err[-1000:])
            return resp
        resp.update(stderr=f"unknown payload for sys/bluetooth: {text!r}")
        return resp

    # sys/reboot now (guarded)
    if topic_suffix == "sys/reboot":
        if not ALLOW_REBOOT:
            resp.update(stderr="reboot disabled (ALLOW_REBOOT=False)")
            return resp
        if text.lower() not in ("now","true","1","yes"):
            resp.update(stderr="refusing: send payload 'now'")
            return resp
        print("[cmd] system reboot")
        resp.update(status="ok", code=0, stdout="rebooting")
        asyncio.create_task(run_cmd(["sudo","reboot","now"]))
        return resp

    resp.update(stderr=f"unknown command: {topic_suffix}")
    return resp

# ---- main loop --------------------------------------------------------------
async def main():
    room = _load_room()
    cfg = Cfg(room["host"], room["port"], room["user"], room["password"], room["prefix"])

    topic_cmd   = f"{cfg.prefix}/cmd/#"
    topic_resp  = f"{cfg.prefix}/cmd/resp"
    topic_health= f"{cfg.prefix}/cmd/health"

    client = Client(
        hostname=cfg.host,
        port=cfg.port,
        username=cfg.user,
        password=cfg.password,
        will=Will(topic_health, b"offline", 1, True),
        keepalive=15,
    )

    async with client as c:
        print("[cmd] connected")
        await c.publish(topic_health, b"online", qos=1, retain=True)
        await c.subscribe(topic_cmd)
        print(f"[cmd] subscribed → {topic_cmd}")

        messages_mgr = getattr(c, "messages", None)

        async def _handle(m):
            if m.retain:
                return  # ignore retained noise
            topic = str(m.topic)

            # Skip our own response topic to avoid echo-loops
            if topic == topic_resp or topic.startswith(topic_resp + "/"):
                return

            # Extract suffix after "<prefix>/cmd/"
            base = cfg.prefix + "/cmd/"
            if topic.startswith(base):
                suffix = topic[len(base):]  # <-- correct slice (+5 not +6)
            else:
                suffix = ""

            print(f"[cmd] received: {suffix or '(root)'} | {len(m.payload or b'')} bytes")
            resp = await handle_command(suffix, m.payload or b"")
            try:
                await c.publish(topic_resp, json.dumps(resp, separators=(",",":")), qos=1, retain=False)
            except Exception as e:
                print(f"[cmd] publish resp error: {e}")

        if callable(messages_mgr):
            async with client.messages() as messages:
                async for m in messages:
                    await _handle(m)
        else:
            async for m in messages_mgr:
                await _handle(m)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass