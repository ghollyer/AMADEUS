"""Unified AMADEUS deployment config (``amadeus_config.json``).

One JSON file holds every IP, port, path and mode for *both* machines, plus
the instrument's calibration (``instrument`` section). The same
file is copied to the microscope PC and the control laptop; each process reads
the section it needs. Written by the setup wizard (``python -m setup_wizard``),
read by every launcher.

Lookup order (first hit wins):
    1. an explicit path (``--amadeus-config``)
    2. the ``AMADEUS_CONFIG`` environment variable
    3. ``<repo root>/amadeus_config.json``
    4. built-in defaults (all-localhost)

Precedence for every value is: CLI flag > config file > default.

Stdlib only, Python 3.10-compatible (runs in the vjem310 env).
"""

import copy
import datetime
import ipaddress
import json
import os
import socket
from pathlib import Path

from common.protocol import DEFAULT_PORTS

CONFIG_FILENAME = "amadeus_config.json"
ENV_VAR = "AMADEUS_CONFIG"
REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = 4

HW_MODES = ("online", "offline", "sim")
CONNECTION_MODES = ("direct", "ssh")

DEFAULTS = {
    "version": SCHEMA_VERSION,
    "microscope_pc": {
        "ip": "127.0.0.1",
        "repo_path": "",
        "vjem310_python": "",
        # Address the instrument-side servers bind to. "*" = all interfaces.
        "listen_ip": "*",
        # Windows adapter facing the laptop (used by the network doctor).
        "adapter_alias": "",
    },
    "control_pc": {
        "ip": "127.0.0.1",
        "data_dir": "amadeus_data/session",
    },
    "ports": dict(DEFAULT_PORTS),
    "connection": {
        "mode": "direct",
        "ssh_local_ports": {"relay": 29875, "stream": 29877},
    },
    "relay": {"timeout_ms": 30000},
    # Instrument calibration and wiring (see drivers/calibration.py for the
    # keys): label, roles, lens/deflector roles, focus DAC<->nm per mode,
    # stage settings. Empty = the drivers' defaults. Travels with the profile.
    "instrument": {},
    # allow_scripting: register the MCP escape hatches (execute_dm_script,
    # execute_tem3_call). false = the agent only gets the purpose-built tools.
    # web_port: amadeus-web's HTTP port (always bound to 127.0.0.1).
    "client": {"timeout_s": 600, "allow_scripting": True, "web_port": 8765},
    "experimental": {"firewall_autofix": False},
    # The drivers this microscope runs: {name: {"kind", "slots", "host", ...}}.
    # Replaced as a whole by the file's own section (never merged with this
    # default). See effective_drivers() and docs/ADDING_HARDWARE.md. Default:
    # JEOL (offline) holding column + stage, Digital Micrograph the rest.
    "drivers": {
        "hw": {"kind": "jeol", "offline": True, "host": "in_process",
               "slots": ["column", "stage"]},
        "dm": {"kind": "dm", "slots": ["scan", "camera", "live", "spectrometer", "files"]},
    },
    # Devices that can't be in use together (e.g. two retractable cameras):
    # [["dm/camera/0", "dm/camera/1"], ...]. Switching between members of a
    # group needs the operator's confirmation (relay/policy.py).
    "exclusive": [],
    # Relay-side safety rules (relay/policy.py).
    #   stage_limits: {"x_m": [min, max], ..., "alpha_deg": [min, max]} (hard)
    #   confirm: commands that need the operator's confirmation
    #   confirm_vendor_writes: raw vendor calls (tem3_call, vendor_call) that
    #     aren't reads need the operator's confirmation
    "policy": {"stage_limits": {}, "confirm": [], "confirm_vendor_writes": True},
}

DRIVER_HOSTS = ("in_process", "process")
DRIVER_FIELDS = ("kind", "host", "slots", "python", "offline", "stream", "class",
                 "options", "calibration", "restart")


class ConfigError(ValueError):
    """Raised when a config file is present but invalid."""


# Sections a file replaces as a whole instead of merging into the defaults.
_REPLACED_SECTIONS = ("drivers",)


def _merge(base: dict, override: dict, top: bool = True) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if top and key in _REPLACED_SECTIONS:
            out[key] = copy.deepcopy(value)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value, top=False)
        else:
            out[key] = value
    return out


def migrate(data: dict) -> dict:
    """Upgrade an older file in place, one version at a time.

    v1 -> v2: the hardware bridge lost its own port (``ports.hw``), and the
    relay republishes DM's stream on ``ports.stream``.

    v2/v3 -> v4: ``drivers`` becomes the only description of the hardware.
    ``hw_server.mode`` / ``hw_server.host`` / ``hw_server.calibration`` and
    ``relay.hardware_backend`` are turned into the equivalent ``drivers``
    section (unless the file already had one) and removed, and the focus
    calibration's JEOL lens names (``"olf"``/``"olc"``) become lens roles."""
    version = data.get("version", 1)
    if version < 2:
        ports = data.get("ports")
        if isinstance(ports, dict):
            ports.pop("hw", None)
            if "dm_stream" in ports:
                ports["stream"] = ports.pop("dm_stream")
        ssh = data.get("connection", {}).get("ssh_local_ports")
        if isinstance(ssh, dict) and "dm_stream" in ssh:
            ssh["stream"] = ssh.pop("dm_stream")
        version = 2
    if version < 4:
        hw = data.pop("hw_server", None) or {}
        relay = data.get("relay") or {}
        backend = relay.pop("hardware_backend", "hw")
        if not data.get("drivers"):
            mode = hw.get("mode", "offline")
            hw_slots = ["column", "stage"] if backend != "dm" else []
            dm_slots = ["scan", "camera", "live", "spectrometer", "files"] + \
                (["column", "stage"] if backend == "dm" else [])
            hw_spec = {"kind": "sim" if mode == "sim" else "jeol",
                       "host": hw.get("host") or "in_process",
                       "offline": mode == "offline", "slots": hw_slots}
            if hw.get("calibration"):
                hw_spec["calibration"] = hw["calibration"]
            data["drivers"] = {"hw": hw_spec, "dm": {"kind": "dm", "slots": dm_slots}}
        by_mode = ((data.get("instrument") or {}).get("focus") or {}).get("by_mode") or {}
        for cal in by_mode.values():
            if isinstance(cal, dict) and cal.get("lens") in ("olf", "olc"):
                cal["lens"] = {"olf": "focus.fine", "olc": "focus.coarse"}[cal["lens"]]
        version = 4
    data["version"] = version
    return data


def find_config(explicit: "str | os.PathLike | None" = None) -> "Path | None":
    """Return the config path to use, or ``None`` if there isn't one.

    An explicit path or env var that points nowhere is an error. A silent
    fallback to defaults there would hide a typo."""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        return path
    env = os.environ.get(ENV_VAR)
    if env:
        path = Path(env).expanduser()
        if not path.is_file():
            raise ConfigError(f"{ENV_VAR} points to a missing file: {path}")
        return path
    path = REPO_ROOT / CONFIG_FILENAME
    return path if path.is_file() else None


def validate(cfg: dict) -> list:
    """Return a list of human-readable problems (empty = valid)."""
    problems = []
    for section in ("microscope_pc", "control_pc"):
        ip = cfg[section].get("ip", "")
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            if ip != "localhost":
                problems.append(f"{section}.ip is not an IP address: {ip!r}")
    listen = cfg["microscope_pc"].get("listen_ip", "*")
    if listen != "*":
        try:
            ipaddress.ip_address(listen)
        except ValueError:
            problems.append(f"microscope_pc.listen_ip must be '*' or an IP: {listen!r}")
    ports = dict(cfg["ports"])
    ports.update({f"ssh_{k}": v for k, v in cfg["connection"]["ssh_local_ports"].items()})
    for name, port in ports.items():
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            problems.append(f"port {name} must be an integer 1-65535: {port!r}")
    unknown = sorted(set(cfg["ports"]) - set(DEFAULT_PORTS))
    if unknown:
        problems.append(f"unknown ports {unknown} (expected {sorted(DEFAULT_PORTS)})")
    main_ports = [cfg["ports"][k] for k in DEFAULT_PORTS]
    if len(set(main_ports)) != len(main_ports):
        problems.append(f"ports must be distinct: {cfg['ports']}")
    if cfg["connection"]["mode"] not in CONNECTION_MODES:
        problems.append(f"connection.mode must be one of {CONNECTION_MODES}")
    port = cfg["client"].get("web_port", 8765)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        problems.append("client.web_port must be an integer port (1-65535)")
    if not isinstance(cfg["client"].get("allow_scripting", True), bool):
        problems.append("client.allow_scripting must be true or false")
    if not isinstance(cfg.get("instrument"), dict):
        problems.append("instrument must be a JSON object (the calibration)")
    problems += validate_drivers(cfg)
    return problems


def validate_drivers(cfg: dict) -> list:
    """Problems in the v3 ``drivers`` / ``exclusive`` / ``policy`` sections."""
    from common.commands import SINGLE_DRIVER_SLOTS, SLOTS
    problems = []
    drivers = cfg.get("drivers")
    if not isinstance(drivers, dict):
        return ["drivers must be a JSON object {name: {...}}"]
    holders: dict = {}
    for name, spec in drivers.items():
        where = f"drivers.{name}"
        if not isinstance(spec, dict):
            problems.append(f"{where} must be a JSON object")
            continue
        if "/" in name or ":" in name or "." in name or not name:
            problems.append(f"{where}: a driver name can't contain '/', ':' or '.'")
        unknown = sorted(set(spec) - set(DRIVER_FIELDS))
        if unknown:
            problems.append(f"{where}: unknown fields {unknown}")
        kind = spec.get("kind")
        if not isinstance(kind, str) or not kind:
            problems.append(f"{where}.kind is required (jeol, sim, dm, remote, ...)")
        host = spec.get("host", "in_process")
        if kind == "remote" or (kind == "dm" and "host" in spec):
            # A dm driver may leave host out: it then uses ports.dm.
            if not (isinstance(host, str) and host.startswith(("tcp://", "ipc://"))):
                problems.append(f"{where}.host must be the {kind} process address "
                                f"(tcp://...)")
        elif host not in DRIVER_HOSTS and not str(host).startswith(("tcp://", "ipc://")):
            problems.append(f"{where}.host must be in_process, process or an address")
        slots = spec.get("slots", [])
        if not isinstance(slots, list) or any(sl not in SLOTS for sl in slots):
            problems.append(f"{where}.slots must be a list of {list(SLOTS)}")
            continue
        for slot in slots:
            holders.setdefault(slot, []).append(name)
    for slot in SINGLE_DRIVER_SLOTS:
        if len(holders.get(slot, [])) > 1:
            problems.append(f"slot {slot!r} is held by {holders[slot]}; one driver only")
    groups = cfg.get("exclusive", [])
    if not isinstance(groups, list) or any(
            not isinstance(g, list) or len(g) < 2 or not all(isinstance(d, str) for d in g)
            for g in groups):
        problems.append("exclusive must be a list of device-id lists, each with 2+ ids")
    policy = cfg.get("policy", {})
    if not isinstance(policy, dict):
        problems.append("policy must be a JSON object")
    else:
        for axis, lim in (policy.get("stage_limits") or {}).items():
            if axis not in ("x_m", "y_m", "z_m", "alpha_deg", "beta_deg") or not (
                    isinstance(lim, list) and len(lim) == 2 and lim[0] < lim[1]):
                problems.append(f"policy.stage_limits.{axis} must be [min, max] on "
                                f"x_m/y_m/z_m/alpha_deg/beta_deg")
        if not isinstance(policy.get("confirm", []), list):
            problems.append("policy.confirm must be a list of command names")
        if not isinstance(policy.get("confirm_vendor_writes", True), bool):
            problems.append("policy.confirm_vendor_writes must be true or false")
    return problems


# ---------------------------------------------------------------------------
# Drivers
# ---------------------------------------------------------------------------

def effective_drivers(cfg: dict, hw_mode: "str | None" = None,
                      dm_address: "str | None" = None) -> dict:
    """The drivers the relay runs: ``{name: spec}`` with every field filled.

    A ``dm`` driver without a ``host``/``stream`` gets DM's ports from
    ``ports.dm`` / ``ports.dm_stream``. A ``jeol`` driver in its own process
    defaults to the vjem310 interpreter. *hw_mode* (``online``/``offline``/
    ``sim``, from ``--online``/``--offline``/``--sim``) re-points the driver
    holding the column; *dm_address* (``--dm``) re-points the DM driver."""
    drivers = copy.deepcopy(cfg.get("drivers") or {})
    for spec in drivers.values():
        spec.setdefault("host", "in_process")
        spec.setdefault("slots", [])
        spec.setdefault("offline", False)
        spec.setdefault("options", {})
        kind = spec.get("kind")
        if kind == "dm":
            if dm_address:
                spec["host"] = dm_address
            elif spec["host"] == "in_process":
                spec["host"] = f"tcp://127.0.0.1:{cfg['ports']['dm']}"
            spec.setdefault("stream", f"tcp://127.0.0.1:{cfg['ports']['dm_stream']}")
        if hw_mode and kind in ("jeol", "sim") and "column" in spec["slots"]:
            spec["kind"] = "sim" if hw_mode == "sim" else "jeol"
            spec["offline"] = hw_mode == "offline"
        if spec.get("kind") == "jeol" and spec["host"] == "process" and not spec.get("python"):
            spec["python"] = cfg["microscope_pc"].get("vjem310_python") or ""
    return drivers


def hardware_driver(cfg: dict) -> "tuple[str | None, dict]":
    """``(name, spec)`` of the driver holding the column (``(None, {})``)."""
    for name, spec in (cfg.get("drivers") or {}).items():
        if "column" in (spec.get("slots") or []):
            return name, spec
    return None, {}


def hardware_mode(cfg: dict) -> str:
    """``online`` / ``offline`` / ``sim`` / the kind, for the column driver."""
    _, spec = hardware_driver(cfg)
    kind = spec.get("kind")
    if kind == "jeol":
        return "offline" if spec.get("offline") else "online"
    return kind or "none"


def is_online(cfg: dict) -> bool:
    """True if any driver talks to real hardware (not a simulator or offline)."""
    return any(spec.get("kind") not in ("sim", "template", "dm", "remote")
               and not spec.get("offline")
               for spec in (cfg.get("drivers") or {}).values())


def set_hardware(cfg: dict, mode: "str | None" = None, host: "str | None" = None,
                 via_dm: "bool | None" = None) -> None:
    """The setup wizard's simple controls, applied to the ``drivers`` section:

    *mode* online/offline/sim for the JEOL/sim driver; *host* in_process or
    process for it; *via_dm* True moves column + stage to the DM driver (the
    legacy Gatan EM* fallback), False gives them back."""
    drivers = cfg.setdefault("drivers", {})
    hw_name = next((n for n, s in drivers.items() if s.get("kind") in ("jeol", "sim")), None)
    dm_name = next((n for n, s in drivers.items() if s.get("kind") == "dm"), None)
    if hw_name is not None:
        spec = drivers[hw_name]
        if mode is not None:
            spec["kind"] = "sim" if mode == "sim" else "jeol"
            spec["offline"] = mode == "offline"
        if host is not None:
            spec["host"] = host
    if via_dm is not None and dm_name is not None:
        to, frm = (dm_name, hw_name) if via_dm else (hw_name, dm_name)
        if to is not None:
            for slot in ("column", "stage"):
                if frm is not None and slot in drivers[frm].get("slots", []):
                    drivers[frm]["slots"].remove(slot)
                if slot not in drivers[to].setdefault("slots", []):
                    drivers[to]["slots"].append(slot)


def describe_drivers(cfg: dict) -> "list[str]":
    """One line per driver, for the launcher and amadeus-check."""
    lines = []
    for name, spec in effective_drivers(cfg).items():
        kind = spec["kind"]
        if kind == "jeol":
            kind += " (offline)" if spec.get("offline") else " (online)"
        slots = ", ".join(spec["slots"]) or "no slots"
        lines.append(f"{name}: {kind}, {spec['host']}: {slots}")
    return lines


def load(explicit: "str | os.PathLike | None" = None) -> dict:
    """Load the config merged over defaults. Raises ``ConfigError`` if invalid.

    The returned dict carries ``_path`` (str or None) so callers can report
    where settings came from."""
    path = find_config(explicit)
    data = {}
    if path is not None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"{path} must contain a JSON object")
        data = migrate(data)
    cfg = _merge(DEFAULTS, data)
    problems = validate(cfg)
    if problems:
        where = path or "defaults"
        raise ConfigError(f"invalid config ({where}):\n  - " + "\n  - ".join(problems))
    cfg["_path"] = str(path) if path else None
    return cfg


def load_defaults() -> dict:
    """The built-in defaults, as ``load`` would return them with no file."""
    cfg = copy.deepcopy(DEFAULTS)
    cfg["_path"] = None
    return cfg


def save(cfg: dict, path: "str | os.PathLike | None" = None) -> Path:
    """Validate and write ``cfg``. Defaults to ``<repo>/amadeus_config.json``."""
    clean = public(cfg)
    clean = _merge(DEFAULTS, migrate(clean))
    problems = validate(clean)
    if problems:
        raise ConfigError("refusing to save invalid config:\n  - " + "\n  - ".join(problems))
    target = Path(path) if path else REPO_ROOT / CONFIG_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=2)
        f.write("\n")
    os.replace(tmp, target)
    return target


# ---------------------------------------------------------------------------
# Derived addresses
# ---------------------------------------------------------------------------

def relay_address(cfg: dict) -> str:
    """Where the control laptop connects for commands."""
    if cfg["connection"]["mode"] == "ssh":
        return f"tcp://127.0.0.1:{cfg['connection']['ssh_local_ports']['relay']}"
    return f"tcp://{cfg['microscope_pc']['ip']}:{cfg['ports']['relay']}"


def stream_address(cfg: dict) -> str:
    """Where the control laptop subscribes for live frames (the relay's
    republished stream)."""
    if cfg["connection"]["mode"] == "ssh":
        return f"tcp://127.0.0.1:{cfg['connection']['ssh_local_ports']['stream']}"
    return f"tcp://{cfg['microscope_pc']['ip']}:{cfg['ports']['stream']}"


def _public_bind_ip(cfg: dict) -> str:
    ip = cfg["microscope_pc"].get("listen_ip", "*")
    if cfg["connection"]["mode"] == "ssh" and ip == "*":
        ip = "127.0.0.1"   # with SSH forwarding nothing needs to be exposed
    return ip


def relay_listen(cfg: dict) -> str:
    """Relay front-door bind address on the microscope PC."""
    return f"tcp://{_public_bind_ip(cfg)}:{cfg['ports']['relay']}"


def relay_stream_listen(cfg: dict) -> str:
    """Where the relay republishes DM's live frames for the laptop."""
    return f"tcp://{_public_bind_ip(cfg)}:{cfg['ports']['stream']}"


def public(cfg: dict) -> dict:
    """*cfg* without private ``_``-prefixed keys (safe to save or share)."""
    return {k: v for k, v in cfg.items() if not k.startswith("_")}


def data_dir(cfg: dict) -> Path:
    """Client data dir, resolved (relative paths are relative to the repo)."""
    path = Path(cfg["control_pc"]["data_dir"]).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


def ssh_command(cfg: dict, user: str = "WINDOWS_USER") -> str:
    """The laptop-side SSH port-forward command for ``connection.mode == 'ssh'``."""
    local = cfg["connection"]["ssh_local_ports"]
    ports = cfg["ports"]
    return (
        "ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 "
        f"-L 127.0.0.1:{local['relay']}:127.0.0.1:{ports['relay']} "
        f"-L 127.0.0.1:{local['stream']}:127.0.0.1:{ports['stream']} "
        f"{user}@{cfg['microscope_pc']['ip']}"
    )


def load_or_none(explicit=None) -> "dict | None":
    """Like ``load`` but returns ``None`` when no config file exists (and
    re-raises genuine errors). Entry points use this so the old CLI-only flow
    keeps working unchanged."""
    if explicit is None and not os.environ.get(ENV_VAR) \
            and not (REPO_ROOT / CONFIG_FILENAME).is_file():
        return None
    return load(explicit)


# ---------------------------------------------------------------------------
# Instrument profile: everything needed to reinstall AMADEUS on this
# microscope, minus machine-local paths (which are re-detected on restore).
# ---------------------------------------------------------------------------

PROFILE_KEY = "amadeus_profile"
PROFILE_VERSION = 1
PROFILE_SUFFIX = ".amadeus.json"
# (section, key) pairs that only make sense on the machine that wrote them.
MACHINE_LOCAL = (("microscope_pc", "repo_path"), ("microscope_pc", "vjem310_python"),
                 ("control_pc", "data_dir"))
# Per-driver fields that are machine-local too (stripped on export).
MACHINE_LOCAL_DRIVER_FIELDS = ("python",)


def amadeus_version() -> str:
    """The AMADEUS version from pyproject.toml (+ git commit when available)."""
    version = "unknown"
    try:
        text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.strip().startswith("version"):
                version = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    except OSError:
        pass
    head = REPO_ROOT / ".git" / "HEAD"
    try:
        ref = head.read_text(encoding="utf-8").strip()
        if ref.startswith("ref:"):
            ref = (REPO_ROOT / ".git" / ref[5:].strip()).read_text(encoding="utf-8").strip()
        version += f"+{ref[:8]}"
    except OSError:
        pass
    return version


def export_profile(cfg: dict, path: "str | os.PathLike", note: str = "") -> Path:
    """Write an instrument profile: connection details, modes, ports and the
    instrument calibration, without machine-local paths.

    A driver's separate calibration file (``drivers.<name>.calibration``) is
    folded into the ``instrument`` section, so the profile is self-contained."""
    data = copy.deepcopy(public(cfg))
    for spec in (data.get("drivers") or {}).values():
        cal_path = spec.pop("calibration", None)
        if not cal_path:
            continue
        try:
            with open(cal_path, "r", encoding="utf-8") as f:
                cal = json.load(f)
        except (OSError, ValueError) as exc:
            raise ConfigError(f"can't read the calibration file {cal_path}: {exc}") from exc
        data["instrument"] = _merge(data.get("instrument") or {}, cal, top=False)
    for section, key in MACHINE_LOCAL:
        data[section].pop(key, None)
    for spec in (data.get("drivers") or {}).values():
        for key in MACHINE_LOCAL_DRIVER_FIELDS:
            spec.pop(key, None)
    problems = validate(_merge(DEFAULTS, data))
    if problems:
        raise ConfigError("refusing to export an invalid config:\n  - " + "\n  - ".join(problems))
    doc = {
        PROFILE_KEY: PROFILE_VERSION,
        "exported": datetime.datetime.now().isoformat(timespec="seconds"),
        "exported_from": socket.gethostname(),
        "note": note,
        "provenance": {
            "amadeus_version": amadeus_version(),
            "drivers": {name: spec["kind"] for name, spec in effective_drivers(
                _merge(DEFAULTS, data)).items()},
        },
        "config": data,
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    return target


def load_profile(path: "str | os.PathLike") -> "tuple[dict, dict]":
    """Read a profile (or a plain amadeus_config.json). Returns
    ``(config, info)``. *config* is merged over the defaults and validated;
    *info* holds ``exported``, ``exported_from`` and ``note`` when present."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"can't read profile {path}: {exc}") from exc
    if not isinstance(doc, dict):
        raise ConfigError(f"{path} is not an AMADEUS profile")
    if PROFILE_KEY in doc:
        data = doc.get("config") or {}
        info = {k: doc.get(k) for k in ("exported", "exported_from", "note", "provenance")}
    else:
        data, info = doc, {}
    cfg = _merge(DEFAULTS, migrate(copy.deepcopy(data)))
    problems = validate(cfg)
    if problems:
        raise ConfigError(f"invalid profile {path}:\n  - " + "\n  - ".join(problems))
    info["warnings"] = profile_warnings(cfg, info)
    return cfg, info


# Calibrations older than this are flagged on restore.
CALIBRATION_STALE_DAYS = 180


def profile_warnings(cfg: dict, info: dict) -> "list[str]":
    """Things to check before trusting a restored profile.

    * a different AMADEUS version than the one that exported it;
    * focus calibrations or stage mappings with no record of when they were
      measured, or measured more than CALIBRATION_STALE_DAYS ago. A gun
      re-alignment or an HT change silently invalidates them."""
    warnings = []
    prov = info.get("provenance") or {}
    exported_with = prov.get("amadeus_version")
    here = amadeus_version()
    if exported_with and exported_with.split("+")[0] != here.split("+")[0]:
        warnings.append(f"exported with AMADEUS {exported_with}; this install is {here}. "
                        f"Check the release notes for config changes.")
    inst = cfg.get("instrument") or {}
    today = datetime.date.today()

    def check(label: str, entry: dict) -> None:
        measured = (entry or {}).get("measured")
        if not measured:
            warnings.append(f"{label}: no record of when it was measured; re-check it "
                            f"before trusting it (add \"measured\": {{\"date\": ...}}).")
            return
        date_text = measured.get("date") if isinstance(measured, dict) else str(measured)
        try:
            when = datetime.date.fromisoformat(str(date_text)[:10])
        except ValueError:
            return
        age = (today - when).days
        if age > CALIBRATION_STALE_DAYS:
            warnings.append(f"{label} was measured {age} days ago ({when}); re-measure "
                            f"it if the column has been re-aligned since.")

    for mode, cal in (((inst.get("focus") or {}).get("by_mode")) or {}).items():
        if cal and cal.get("dac_per_nm") is not None:
            check(f"focus calibration ({mode})", cal)
    for mode, entry in ((((inst.get("stage") or {}).get("image_mapping")) or {}).items()):
        if entry and entry.get("stage_to_image"):
            check(f"stage-to-image mapping ({mode})", entry)
    return warnings


def restore_profile(profile_cfg: dict, repo_path: "str | os.PathLike",
                    vjem310_python: str = "", keep_online: bool = True) -> dict:
    """The profile's config with this machine's paths filled in.

    With ``keep_online=False``, a profile saved in online mode comes back in
    offline mode, so the first run on a fresh install can't move the column
    before ``scripts/pyjem_smoke.py`` has passed."""
    cfg = copy.deepcopy(public(profile_cfg))
    cfg["microscope_pc"]["repo_path"] = str(repo_path)
    cfg["microscope_pc"]["vjem310_python"] = vjem310_python or ""
    cfg["control_pc"].setdefault("data_dir", DEFAULTS["control_pc"]["data_dir"])
    if not keep_online:
        for spec in (cfg.get("drivers") or {}).values():
            if spec.get("kind") == "jeol" and not spec.get("offline"):
                spec["offline"] = True
    return cfg


def instrument_summary(cfg: dict) -> str:
    """One line describing the instrument calibration in *cfg*."""
    inst = cfg.get("instrument") or {}
    label = inst.get("label") or "unnamed instrument"
    modes = (inst.get("focus") or {}).get("by_mode") or {}
    calibrated = [m for m, c in sorted(modes.items())
                  if c and c.get("dac_per_nm") is not None and c.get("dac_at_zero") is not None]
    focus = ("focus calibrated for " + ", ".join(calibrated)) if calibrated \
        else "focus uncalibrated (DAC only)"
    mapped = sorted(m for m, e in (((inst.get("stage") or {}).get("image_mapping")) or {}).items()
                    if e and e.get("stage_to_image"))
    stage = f"; stage->image mapping for {', '.join(mapped)}" if mapped else ""
    extra = " + calibration file" if any(
        s.get("calibration") for s in (cfg.get("drivers") or {}).values()) else ""
    return f"{label}: {focus}{stage}{extra}"


def add_config_argument(parser) -> None:
    """Add the standard ``--amadeus-config`` flag to an argparse parser."""
    parser.add_argument(
        "--amadeus-config", default=None,
        help=f"unified {CONFIG_FILENAME} (default: {ENV_VAR} env var, then "
             f"<repo>/{CONFIG_FILENAME})")


def client_defaults(parser, args) -> dict:
    """Defaults for the laptop-side GUIs/tools: ``address``, ``data_dir``,
    ``timeout_s``. Comes from the config if one exists, else the historical
    localhost defaults. Exits through ``parser.error`` on an invalid config."""
    try:
        cfg = load_or_none(getattr(args, "amadeus_config", None))
    except ConfigError as exc:
        parser.error(str(exc))
    if cfg is None:
        return {"address": "tcp://localhost:9875", "data_dir": Path("amadeus_data"),
                "timeout_s": 600, "config_path": None}
    return {"address": relay_address(cfg), "data_dir": data_dir(cfg),
            "timeout_s": cfg["client"]["timeout_s"], "config_path": cfg["_path"]}
