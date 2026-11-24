"""Connection checks run from the control laptop (``amadeus-check``).

Reads ``amadeus_config.json`` and checks the path step by step, so a failure
says *which* layer broke:

    1. config sanity: is the target IP really the microscope PC, not this laptop?
    2. TCP reachability of the relay and live-stream ports
    3. relay -> backend health (``backend_status``)
    4. read-only instrument queries (stage, focus, magnification, live status)

``--image`` adds the synthetic DM image round trip (creates an 8x6 test image
in DM, saves it on the PC, fetches it back and checks every pixel). It moves
no hardware.

Exit code 0 only if every check passed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from urllib.parse import urlparse
from uuid import uuid4

from common import config as amadeus_config
from common import netutil

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"


class Report:
    def __init__(self, out=print):
        self.out = out
        self.failures = 0

    def line(self, status: str, label: str, detail: str = "") -> None:
        if status == FAIL:
            self.failures += 1
        self.out(f"[{status}] {label}" + (f": {detail}" if detail else ""))


def _host_port(address: str) -> tuple[str, int]:
    parsed = urlparse(address)
    return parsed.hostname or "", parsed.port or 0


def check_config(cfg: dict, report: Report) -> None:
    pc_ip = cfg["microscope_pc"]["ip"]
    report.line(PASS, "config", cfg["_path"] or "built-in defaults (no amadeus_config.json)")
    if cfg["connection"]["mode"] == "ssh":
        report.line(WARN, "connection mode",
                    "ssh: start the tunnel first -> " + amadeus_config.ssh_command(cfg))
        return
    if pc_ip not in ("127.0.0.1", "localhost") and netutil.is_local_address(pc_ip):
        report.line(FAIL, "microscope PC IP",
                    f"{pc_ip} belongs to THIS computer. Put the microscope PC's "
                    "address in microscope_pc.ip (run `ipconfig` on the PC).")
    if netutil.is_link_local(pc_ip):
        report.line(WARN, "microscope PC IP",
                    f"{pc_ip} is link-local (169.254.x.x): Windows chose it because "
                    "DHCP failed, and it can change after a reboot. Set a static IP "
                    "on both adapters, e.g. PC 10.77.77.1 / laptop 10.77.77.2 "
                    "(docs/ETHERNET_SETUP.md, steps 2-3).")
    local = [ip for _, ip in netutil.local_ipv4_addresses()]
    if pc_ip not in ("127.0.0.1", "localhost") and local \
            and not any(netutil.same_subnet(ip, pc_ip) for ip in local):
        report.line(WARN, "subnet",
                    f"no local adapter is on the same subnet as {pc_ip} "
                    f"(local: {', '.join(local)}). Check the Ethernet adapter settings.")
    route = netutil.check_route(pc_ip, cfg["control_pc"]["ip"])
    if route:
        report.line(WARN, "route", route)


def check_ports(cfg: dict, report: Report) -> bool:
    ok_all = True
    for label, address in (("relay", amadeus_config.relay_address(cfg)),
                           ("live stream", amadeus_config.stream_address(cfg))):
        host, port = _host_port(address)
        ok, msg = netutil.tcp_probe(host, port)
        # Both ports belong to the AMADEUS server on the PC: if one is open
        # and the other isn't, it's a firewall rule covering only one port.
        report.line(PASS if ok else FAIL, f"TCP {label} {host}:{port}", msg)
        ok_all = ok_all and ok
    if not ok_all:
        report.line(WARN, "hint",
                    "On the microscope PC, open the AMADEUS Launcher and press "
                    "'Network doctor'. It lists listeners and firewall block rules.")
    return ok_all


async def check_relay(cfg: dict, report: Report, address: str | None = None) -> None:
    from client.transport.zmq_client import ZMQClient
    address = address or amadeus_config.relay_address(cfg)
    async with ZMQClient(address, timeout_ms=10_000) as client:
        try:
            status = await client.send_command("backend_status")
        except Exception as exc:
            report.line(FAIL, "relay backend_status", str(exc))
            return
        backends = status.get("backends", {})
        for name, info in sorted(backends.items()):
            ok = bool(info.get("ok"))
            kind = info.get("kind") or name
            detail = f"{kind} via {info.get('address', '')}"
            if info.get("slots"):
                detail += f": {', '.join(info['slots'])}"
            if info.get("host") == "process":
                detail += f" (own process, pid {info.get('pid')}, " \
                          f"{info.get('restarts', 0)} restart(s))"
            if not ok:
                detail += f" ({info.get('error', 'not responding')})"
                detail += {"dm": ". Is scripts/start_amadeus_dm.py running inside DM?",
                           "jeol": ". Check the hardware mode in the setup, and the "
                                   "AMADEUS Launcher log on the microscope PC.",
                           "sim": ". See the AMADEUS Launcher log on the microscope PC.",
                           }.get(kind, "")
            report.line(PASS if ok else FAIL, f"driver {name}", detail.strip())
        if not backends:
            report.line(WARN, "relay backend_status", json.dumps(status))

        healthy = {n for n, i in backends.items() if i.get("ok")}
        # Reads go to whichever driver holds the slot. Skip the reads whose
        # driver is already known down, or that this microscope doesn't have.
        routing = status.get("routing") or {}

        def holder(slot: str) -> "str | None":
            names = routing.get(slot)
            return names[0] if names else None
        reads = [("get_stage_position", holder("stage")),
                 ("get_focus", holder("column")),
                 ("get_magnification", holder("column")),
                 ("live_status", holder("live"))]
        for cmd, backend in reads:
            if backend is None:
                report.line(WARN, cmd, "skipped (no driver on this microscope provides it)")
                continue
            if backend not in healthy:
                report.line(WARN, cmd, f"skipped (driver {backend} is down)")
                continue
            try:
                data = await client.send_command(cmd)
                report.line(PASS, cmd, json.dumps(data)[:160])
            except Exception as exc:
                report.line(FAIL, cmd, str(exc))


IMAGE_SCRIPT = '''image testImage := RealImage("AMADEUS connection test", 4, 8, 6)
testImage = icol + 8 * irow
ShowImage(testImage)
SaveAsGatan(testImage, "{path}")
Result("AMADEUS synthetic image created\\n")
'''


async def check_image(cfg: dict, report: Report) -> None:
    """Image round trip. With Digital Micrograph: a synthetic DM image (one
    small test file written on the PC, no hardware moved). Without DM: one
    short acquisition from the driver holding the camera (or scan) slot."""
    import numpy as np
    from client.transport.zmq_client import ZMQClient

    async with ZMQClient(amadeus_config.relay_address(cfg), timeout_ms=15_000) as client:
        try:
            status = await client.send_command("backend_status")
        except Exception as exc:
            report.line(FAIL, "image round trip", f"backend_status failed: {exc}")
            return
    kinds = {n: i.get("kind") for n, i in (status.get("backends") or {}).items()}
    if "dm" not in kinds.values():
        await _check_driver_image(cfg, report, status.get("routing") or {})
        return

    repo = cfg["microscope_pc"].get("repo_path") or "C:/AMADEUS"
    remote_dir = repo.replace("\\", "/").rstrip("/") + "/test-data"
    # GMS 3.x SaveAsGatan writes .dm4 and appends the extension when the name
    # has another one (".dm3" became ".dm3.dm4"), so ask for .dm4.
    remote_path = f"{remote_dir}/synthetic-{uuid4().hex}.dm4"
    script = IMAGE_SCRIPT.format(path=remote_path)
    async with ZMQClient(amadeus_config.relay_address(cfg), timeout_ms=15_000) as client:
        try:
            await client.send_command("dm.execute_script", {"script": script})
            try:
                _, array = await client.send_command_image("load_file", {"path": remote_path})
            except RuntimeError as exc:
                if "not found" not in str(exc).lower():
                    raise
                remote_path += ".dm4"
                _, array = await client.send_command_image("load_file",
                                                           {"path": remote_path})
        except Exception as exc:
            report.line(FAIL, "synthetic image round trip",
                        f"{exc}. Make sure {remote_dir} exists on the microscope PC "
                        "(the launcher creates it).")
            return
    expected = np.arange(48, dtype=np.float32).reshape(6, 8)
    if array.dtype == expected.dtype and array.shape == expected.shape \
            and np.array_equal(array, expected):
        report.line(PASS, "synthetic image round trip",
                    f"all 48 pixels match ({remote_path})")
    else:
        report.line(FAIL, "synthetic image round trip",
                    f"got {array.shape} {array.dtype}, expected (6, 8) float32 gradient")


async def _check_driver_image(cfg: dict, report: Report, routing: dict) -> None:
    import numpy as np
    from client.transport.zmq_client import ZMQClient
    if routing.get("camera"):
        cmd, params, where = "camera_acquire", {"exposure_s": 0.05}, routing["camera"][0]
    elif routing.get("scan"):
        cmd, params, where = "stem_acquire", {"width": 64, "height": 64, "dwell_us": 1.0}, \
            routing["scan"][0]
    else:
        report.line(WARN, "image round trip", "skipped (no camera or scan driver)")
        return
    async with ZMQClient(amadeus_config.relay_address(cfg), timeout_ms=15_000) as client:
        try:
            _, array = await client.send_command_image(cmd, params)
        except Exception as exc:
            report.line(FAIL, "image round trip", f"{cmd} on driver {where}: {exc}")
            return
    if array.ndim == 2 and array.size and np.isfinite(array).all():
        report.line(PASS, "image round trip",
                    f"{cmd} on driver {where}: {array.shape} {array.dtype}")
    else:
        report.line(FAIL, "image round trip", f"{cmd} gave {array.shape} {array.dtype}")


def run_checks(cfg: dict, image: bool = False, out=print) -> int:
    report = Report(out)
    check_config(cfg, report)
    if check_ports(cfg, report):
        asyncio.run(check_relay(cfg, report))
        if image:
            asyncio.run(check_image(cfg, report))
    out("")
    out("All checks passed." if report.failures == 0
        else f"{report.failures} check(s) failed.")
    return 0 if report.failures == 0 else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Check the AMADEUS connection from "
                                                 "this computer to the microscope PC")
    amadeus_config.add_config_argument(parser)
    parser.add_argument("--image", action="store_true",
                        help="also run the synthetic DM image round trip")
    args = parser.parse_args()
    try:
        cfg = amadeus_config.load(args.amadeus_config)
    except amadeus_config.ConfigError as exc:
        parser.error(str(exc))
    sys.exit(run_checks(cfg, image=args.image))


if __name__ == "__main__":
    main()
