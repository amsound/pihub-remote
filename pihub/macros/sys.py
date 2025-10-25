# pihub/macros/sys.py
import asyncio

async def restart_pihub():
    print("[macros] restarting pihub systemd service…")
    proc = await asyncio.create_subprocess_exec(
        "sudo", "systemctl", "restart", "pihub.service"
    )
    await proc.wait()

async def reboot_pi():
    print("[macros] rebooting pi…")
    proc = await asyncio.create_subprocess_exec(
        "sudo", "reboot"
    )
    await proc.wait()
