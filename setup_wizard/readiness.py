"""Readiness checklist for the microscope PC, shown by the launcher.

After a fresh install or a profile restore, it answers "is anything still
missing?". Every item AMADEUS can't fix itself (the adapter's static IP, DM's
Python packages, running the DM script) appears as a named step with the exact
instruction.

``readiness()`` is a pure function of its inputs; ``gather()`` collects them on
the real machine. Stdlib only, Python 3.10-compatible.
"""

import json
import os
import sys
from pathlib import Path

from common import config as amadeus_config
from common import netutil

OK, TODO, WARN, INFO = "OK", "TODO", "WARN", "INFO"

# python -m drivers.smoke --driver <name> writes logs/driver_smoke_<name>.json
DRIVER_SMOKE_DIR = amadeus_config.REPO_ROOT / "logs"
# Driver kinds that need no on-instrument check (simulators, separate processes
# with their own checks).
NO_SMOKE_KINDS = ("sim", "template", "dm", "remote")


def driver_smoke_path(name: str) -> Path:
    return DRIVER_SMOKE_DIR / f"driver_smoke_{name}.json"


def drivers_needing_smoke(cfg: dict) -> dict:
    """``{name: spec}`` for drivers that talk to real hardware: anything but
    a simulator, an offline JEOL, or a process with its own checks."""
    out = {}
    for name, spec in amadeus_config.effective_drivers(cfg).items():
        if spec.get("kind") in NO_SMOKE_KINDS or spec.get("offline"):
            continue
        if not spec.get("slots"):
            continue
        out[name] = spec
    return out


def persisted_amadeus_home() -> "str | None":
    """AMADEUS_HOME as new processes (DM) will see it. ``setx`` writes the
    registry and doesn't change already-running processes, so on Windows read
    HKCU\\Environment rather than this process's environment."""
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                return winreg.QueryValueEx(key, "AMADEUS_HOME")[0]
        except OSError:
            return None
    return os.environ.get("AMADEUS_HOME")


def read_smoke_result(path: Path) -> "dict | None":
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _zmq_available() -> bool:
    try:
        import zmq  # noqa: F401
        return True
    except ImportError:
        return False


def gather(cfg: dict, health_states: "dict | None" = None) -> dict:
    """Collect the facts ``readiness`` needs from this machine."""
    return {
        "local_ips": [ip for _, ip in netutil.local_ipv4_addresses()],
        "zmq": _zmq_available(),
        "amadeus_home": persisted_amadeus_home(),
        "driver_smokes": {name: read_smoke_result(driver_smoke_path(name))
                          for name in drivers_needing_smoke(cfg)},
        "python": sys.executable,
        "health": health_states or {},
        "windows": sys.platform == "win32",
    }


def _dm_finds_checkout(repo: str, amadeus_home: "str | None") -> bool:
    """Mirror scripts/start_amadeus_dm.py's search when DM gives no __file__."""
    norm = lambda p: os.path.normcase(os.path.abspath(p)) if p else ""  # noqa: E731
    home = os.path.expanduser("~")
    fallbacks = [r"C:\AMADEUS", os.path.join(home, "AMADEUS"),
                 os.path.join(home, "Documents", "AMADEUS")]
    return norm(amadeus_home) == norm(repo) or norm(repo) in {norm(p) for p in fallbacks}


def readiness(cfg: dict, facts: dict, doctor_findings: "list | None" = None) -> list:
    """``[(level, title, detail), ...]``, most important first."""
    items = []
    pc = cfg["microscope_pc"]
    ip = pc["ip"]
    health = facts.get("health") or {}

    # 1. Network: the saved address must exist on this PC.
    if ip.startswith("127.") or ip == "localhost":
        items.append((INFO, f"PC address {ip} is loopback", "fine for a single-machine test"))
    elif ip in facts.get("local_ips", []):
        items.append((OK, f"this PC has the configured address {ip}", ""))
    else:
        adapter = f" ('{pc['adapter_alias']}')" if pc.get("adapter_alias") else ""
        items.append((TODO, f"no adapter on this PC has {ip}",
                      f"Set {ip} as a static IPv4 address on the laptop-facing Ethernet "
                      f"adapter{adapter}: Settings → Network & internet → Ethernet → IP "
                      "assignment → Edit → Manual. Or pick this PC's actual address in "
                      "Settings (the laptop then needs `amadeus-setup --pc <new ip>`)."))
    if netutil.is_link_local(ip):
        items.append((WARN, f"{ip} is a link-local (169.254.x.x) address",
                      "Windows picks these when DHCP fails and may change it after a "
                      "reboot. Set a static address instead."))

    # 2. This interpreter.
    if facts.get("zmq"):
        items.append((OK, "pyzmq available in this Python", facts.get("python", "")))
    else:
        items.append((TODO, "pyzmq missing in this Python",
                      f"Run: \"{facts.get('python', 'python')}\" -m pip install pyzmq"))

    # 3. DM can find the checkout.
    repo = pc.get("repo_path") or str(amadeus_config.REPO_ROOT)
    if health.get("dm") == "up":
        pass   # the DM server is running, so DM evidently found the checkout
    elif _dm_finds_checkout(repo, facts.get("amadeus_home")):
        items.append((OK, "DM can find the AMADEUS checkout", repo))
    else:
        items.append((TODO, "DM may not find the AMADEUS checkout",
                      "Open Settings and save (it sets AMADEUS_HOME), then restart DM once."))

    # 4. Services.
    server = health.get("server")
    if server == "up":
        items.append((OK, "AMADEUS server running", ""))
        if health.get("hw") == "up":
            items.append((OK, f"hardware driver up ({amadeus_config.hardware_mode(cfg)})", ""))
        else:
            items.append((TODO, "hardware backend not available",
                          "See the server log below. Online mode needs the real PyJEM on "
                          "this PC; use Offline or Sim to test without it."))
    else:
        items.append((TODO, "AMADEUS server not running", "Press Start."))
    if health.get("dm") == "up":
        items.append((OK, "DM server answering", "so DM's Python has numpy and pyzmq"))
    else:
        items.append((TODO, "DM server not answering",
                      "In DM: File › Open scripts\\start_amadeus_dm.py, then Execute. If it "
                      "prints MISSING packages, install them into DM's Python and restart DM."))

    for name, state in sorted((health.get("drivers") or {}).items()):
        if state == "down" and server == "up":
            items.append((TODO, f"driver {name} not answering",
                          "See the server log and logs\\driver_<name>.log. "
                          "backend_status on the laptop shows its error."))

    # 5. Drivers on real hardware need a passing read-only smoke test on this
    #    install and interpreter before they drive anything.
    driver_smokes = facts.get("driver_smokes") or {}

    def same(result, python) -> bool:
        return bool(result) and os.path.normcase(str(result.get("python", ""))) == \
            os.path.normcase(str(python or facts.get("python", "")))

    for name, spec in drivers_needing_smoke(cfg).items():
        python = spec.get("python") or facts.get("python", "")
        result = driver_smokes.get(name)
        if result and result.get("passed") and same(result, python):
            items.append((OK, f"driver {name} ({spec['kind']}) check passed on this install",
                          f"python -m drivers.smoke --driver {name}, {result.get('time', '')}"))
        else:
            items.append((WARN, f"driver {name} ({spec['kind']}) not yet checked on this "
                                f"install",
                          f"Before it drives anything, run (read-only): \"{python or 'python'}\" "
                          f"-m drivers.smoke --driver {name}, and compare the values it "
                          f"prints with the vendor's console."))

    # 6. Instrument parameters.
    items.append((INFO, "instrument", amadeus_config.instrument_summary(cfg)))

    # 7. Firewall (from the last network doctor run).
    direct = cfg["connection"]["mode"] == "direct"
    if direct and facts.get("windows"):
        if doctor_findings is None:
            items.append((INFO, "firewall not checked yet",
                          "Press Network doctor to check listeners and firewall rules."))
        else:
            bad = [t for lvl, t, _, _ in doctor_findings if lvl in ("FAIL", "WARN")]
            if bad:
                items.append((TODO, "network doctor found problems", "; ".join(bad)))
            else:
                items.append((OK, "network doctor found no problems", ""))

    order = {TODO: 0, WARN: 1, OK: 2, INFO: 3}
    return sorted(items, key=lambda item: order[item[0]])
