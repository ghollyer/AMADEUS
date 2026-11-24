"""Contract checks: does a driver follow the interfaces in ``drivers/base.py``?

Run it on every new driver, first against a fake of the vendor API, then on
the instrument (read-only by default):

    amadeus-driver-test --kind sim                      # the reference driver
    amadeus-driver-test --kind jeol --offline           # JEOL's PyJEM simulator
    amadeus-driver-test --kind mine --class mypkg.drv:MyDriver --writes --acquire

What it checks:

* ``describe`` and ``capabilities`` are well formed: every listed command
  belongs to a slot the driver says it fills, devices have ``<kind>/<n>`` ids,
  extras are namespaced, the support level matches the commands, and it is
  all JSON.
* Every implemented *read* returns JSON in the neutral shape: device values
  carry ``unit`` and ``range``, stage positions are metres (a value above
  1 cm is almost certainly nanometres), enumerations come from the lists in
  ``capabilities``, ``focus_nm`` only with ``calibrated: true``.
* An operation the driver leaves out gives the standard "not implemented"
  error, not a crash.
* ``--writes``: set/get round trips that write back the current value (focus,
  beam shift, stigmator) and a zero-length relative stage move. Nothing
  should move, but they do talk to the hardware.
* ``--acquire``: one small STEM frame and one short camera exposure, checked
  for shape, dtype, payload size and calibration. The beam is used.

Stdlib only, Python 3.10 (runs in vendor environments).
"""

from __future__ import annotations

import argparse
import json
import sys

from common.commands import COMMAND_SLOT, DEVICE_KIND_SLOT, SLOTS
from drivers.base import ImageResult, support_level
from drivers.dispatch import call

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"

READ_OPS = ("get_focus", "get_magnification", "get_beam_shift", "get_beam_tilt",
            "get_stigmation", "get_spot_size", "get_alpha", "get_aperture",
            "get_instrument_mode", "get_function_mode", "get_high_tension",
            "get_microscope_state", "get_stage_position", "get_stage_status",
            "get_stem_settings", "get_detectors", "get_beam_position", "list_cameras",
            "get_camera_info", "live_status", "si_get_settings", "list_files")


class Result:
    __slots__ = ("level", "name", "detail")

    def __init__(self, level: str, name: str, detail: str = ""):
        self.level, self.name, self.detail = level, name, detail

    def __repr__(self) -> str:
        return f"{self.level:4s} {self.name}" + (f": {self.detail}" if self.detail else "")

    def to_json(self) -> dict:
        return {"level": self.level, "name": self.name, "detail": self.detail}


def _jsonable(value) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


def _is_range(value) -> bool:
    return isinstance(value, list) and len(value) == 2 and all(
        isinstance(v, (int, float)) for v in value) and value[0] < value[1]


def _run(driver, cmd: str, params: "dict | None" = None):
    """(ok, data or error text) through the real dispatcher."""
    reply = call(driver, {"cmd": cmd, "params": params or {}, "request_id": "contract"})
    if isinstance(reply, tuple):
        return True, reply
    if reply.get("status") != "ok":
        return False, reply.get("error", "")
    return True, reply.get("data")


# ---------------------------------------------------------------------------
# Shape checks for read replies
# ---------------------------------------------------------------------------

def _shape_problems(cmd: str, data, caps: dict) -> list:
    p = []
    if not isinstance(data, dict):
        return [f"reply is {type(data).__name__}, not a JSON object"]
    if not _jsonable(data):
        return ["reply is not JSON-serialisable"]
    if "range" in data and "unit" not in data:
        p.append("has 'range' but no 'unit'")
    if cmd == "get_focus":
        for key in ("focus_dac", "unit", "range", "calibrated"):
            if key not in data:
                p.append(f"missing {key!r}")
        if "range" in data and not _is_range(data["range"]):
            p.append(f"range {data['range']!r} is not [min, max]")
        if data.get("focus_nm") is not None and data.get("calibrated") is not True:
            p.append("focus_nm without calibrated: true (never invent physical units)")
        for role, entry in (data.get("lenses") or {}).items():
            if not (isinstance(entry, dict) and "value" in entry and "unit" in entry
                    and _is_range(entry.get("range"))):
                p.append(f"lenses[{role!r}] needs value, unit and range")
    elif cmd == "get_stage_position":
        for key in ("x_m", "y_m", "z_m", "alpha_deg", "beta_deg"):
            if not isinstance(data.get(key), (int, float)):
                p.append(f"{key} missing or not a number")
        for key in ("x_m", "y_m", "z_m"):
            if isinstance(data.get(key), (int, float)) and abs(data[key]) > 0.01:
                p.append(f"{key}={data[key]} is over 1 cm: nanometres sent as metres?")
    elif cmd == "get_stage_status":
        if not isinstance(data.get("moving"), bool):
            p.append("'moving' must be a boolean")
    elif cmd in ("get_beam_shift", "get_beam_tilt"):
        for key in ("x", "y", "unit", "range"):
            if key not in data:
                p.append(f"missing {key!r}")
    elif cmd == "get_stigmation":
        stigs = data.get("stigmators")
        if not isinstance(stigs, dict) or not stigs:
            p.append("missing 'stigmators' {name: {x, y}}")
        else:
            listed = caps.get("stigmators")
            for name in stigs:
                if listed and name not in listed:
                    p.append(f"stigmator {name!r} not in capabilities 'stigmators'")
        if "unit" not in data:
            p.append("missing 'unit'")
    elif cmd == "get_function_mode":
        fm, mode = data.get("function_mode"), data.get("mode")
        listed = (caps.get("function_modes") or {}).get(mode)
        if not isinstance(fm, str):
            p.append("function_mode must be a name (string)")
        elif listed and fm not in listed:
            p.append(f"function mode {fm!r} not in capabilities function_modes[{mode!r}]")
    elif cmd == "get_instrument_mode":
        if data.get("mode") not in ("TEM", "STEM"):
            p.append(f"mode {data.get('mode')!r} is not TEM or STEM")
    elif cmd == "get_magnification":
        if data.get("magnification") is None and data.get("mag_index") is None:
            p.append("neither magnification nor mag_index")
    elif cmd == "get_high_tension":
        if not isinstance(data.get("ht_v"), (int, float)):
            p.append("ht_v must be volts (a number)")
    elif cmd == "list_cameras":
        if not isinstance(data.get("cameras"), list):
            p.append("missing 'cameras' list")
    elif cmd == "get_detectors":
        if not isinstance(data.get("detectors"), list):
            p.append("missing 'detectors' list")
    return p


def _check_capabilities(driver, out: list) -> dict:
    try:
        desc = driver.describe()
        if isinstance(desc, dict) and desc.get("kind") and _jsonable(desc):
            out.append(Result(PASS, "describe", f"kind {desc['kind']}"))
        else:
            out.append(Result(FAIL, "describe", f"needs a JSON dict with 'kind': {desc!r}"))
    except Exception as exc:
        out.append(Result(FAIL, "describe", f"{type(exc).__name__}: {exc}"))
    try:
        caps = driver.capabilities()
    except Exception as exc:
        out.append(Result(FAIL, "capabilities", f"{type(exc).__name__}: {exc}"))
        return {}
    problems = []
    if not _jsonable(caps):
        problems.append("not JSON-serialisable")
    slots = caps.get("slots") or []
    for slot in slots:
        if slot not in SLOTS:
            problems.append(f"unknown slot {slot!r}")
    for cmd in caps.get("commands") or []:
        slot = COMMAND_SLOT.get(cmd)
        if slot is None:
            problems.append(f"command {cmd!r} is not a slot operation (make it an extra)")
        elif slot not in slots:
            problems.append(f"command {cmd!r} belongs to slot {slot!r}, not listed in slots")
    for extra in caps.get("extras") or []:
        if not str(extra).startswith(f"{caps.get('kind')}."):
            problems.append(f"extra {extra!r} is not namespaced '<kind>.<name>'")
    for dev in caps.get("devices") or []:
        kind, _, local = str(dev.get("id", "")).partition("/")
        if kind not in DEVICE_KIND_SLOT or not local:
            problems.append(f"device id {dev.get('id')!r} is not '<detector|camera|"
                            f"spectrometer>/<n>'")
        if dev.get("slot") not in SLOTS:
            problems.append(f"device {dev.get('id')!r} has slot {dev.get('slot')!r}")
    if caps.get("level") != support_level(caps.get("commands") or []):
        problems.append(f"level {caps.get('level')} != computed "
                        f"{support_level(caps.get('commands') or [])}")
    if caps.get("devices_error"):
        out.append(Result(WARN, "capabilities devices", caps["devices_error"]))
    if problems:
        out.append(Result(FAIL, "capabilities", "; ".join(problems)))
    else:
        out.append(Result(PASS, "capabilities",
                          f"level {caps.get('level')}, slots {', '.join(slots) or 'none'}, "
                          f"{len(caps.get('commands') or [])} commands"))
    return caps


def _check_image(name: str, result, out: list) -> None:
    ok, data = result
    if not ok:
        out.append(Result(FAIL, name, str(data).splitlines()[0][:300]))
        return
    if not isinstance(data, tuple):
        out.append(Result(FAIL, name, "returned JSON, not an image (use ImageResult)"))
        return
    meta, payload = data
    im = meta.get("image_meta", {})
    cal = im.get("calibration") or {}
    problems = []
    if not (isinstance(im.get("shape"), list) and len(im["shape"]) >= 2):
        problems.append(f"shape {im.get('shape')!r}")
    if "units" not in cal:
        problems.append("image_meta.calibration has no 'units' ('' = uncalibrated)")
    if problems:
        out.append(Result(FAIL, name, "; ".join(problems)))
    else:
        out.append(Result(PASS, name, f"{im['shape']} {im.get('dtype')}, "
                                      f"{len(payload)} bytes, units {cal.get('units')!r}"))


def _finish_move(driver, data) -> "tuple[bool, object]":
    """Follow a _continue reply the way the relay does."""
    import time
    deadline = time.monotonic() + 30
    while isinstance(data, dict) and "_continue" in data:
        cont = data["_continue"]
        time.sleep(float(cont.get("interval_s", 0.1)))
        params = dict(cont.get("params") or {})
        if time.monotonic() > deadline:
            params["finalize"] = True
        ok, data = _run(driver, cont["cmd"], params)
        if not ok:
            return False, data
    return True, data


def check_driver(driver, writes: bool = False, acquire: bool = False) -> list:
    """Run the contract checks on a built driver. Returns ``[Result]``."""
    out: list = []
    caps = _check_capabilities(driver, out)
    commands = set(caps.get("commands") or [])

    for cmd in READ_OPS:
        if cmd not in commands:
            continue
        ok, data = _run(driver, cmd)
        if not ok:
            first = str(data).splitlines()[0][:300] if data else ""
            known = (caps.get("unreliable_reads") or {}).get(cmd)
            if known:
                out.append(Result(WARN, cmd, f"{first} (known: {known})"))
            else:
                out.append(Result(FAIL, cmd, first))
            continue
        problems = _shape_problems(cmd, data, caps)
        out.append(Result(FAIL if problems else PASS, cmd,
                          "; ".join(problems) if problems else ""))

    missing = sorted(set(COMMAND_SLOT) - commands)
    if missing:
        ok, err = _run(driver, missing[0])
        if ok or "not implemented" not in str(err).lower() and "unknown command" \
                not in str(err).lower():
            out.append(Result(FAIL, f"{missing[0]} (not implemented)",
                              f"expected the standard error, got {str(err)[:200]!r}"))
        else:
            out.append(Result(PASS, "unimplemented operations",
                              f"{len(missing)} left out, standard error"))

    if writes:
        _check_writes(driver, commands, out)
    else:
        out.append(Result(SKIP, "write round trips", "pass --writes"))
    if acquire:
        if "stem_acquire" in commands:
            mode = _run(driver, "get_instrument_mode") if "get_instrument_mode" in commands \
                else (False, None)
            if mode[0] and isinstance(mode[1], dict) and mode[1].get("mode") == "TEM":
                out.append(Result(SKIP, "stem_acquire", "the column is in TEM mode; switch "
                                                        "to STEM to check it (not done here)"))
            else:
                _check_image("stem_acquire", _run(driver, "stem_acquire",
                                                  {"width": 32, "height": 32,
                                                   "dwell_us": 1.0}), out)
        if "camera_acquire" in commands:
            _check_image("camera_acquire", _run(driver, "camera_acquire",
                                                {"exposure_s": 0.01}), out)
    else:
        out.append(Result(SKIP, "acquisitions", "pass --acquire (uses the beam)"))
    return out


def _check_writes(driver, commands: set, out: list) -> None:
    def roundtrip(name, get_cmd, set_cmd, make_params, same):
        if not {get_cmd, set_cmd} <= commands:
            return
        ok, before = _run(driver, get_cmd)
        if not ok:
            out.append(Result(FAIL, name, f"{get_cmd}: {before}"))
            return
        ok, reply = _run(driver, set_cmd, make_params(before))
        if not ok:
            out.append(Result(FAIL, name, f"{set_cmd}: {str(reply)[:300]}"))
            return
        ok, after = _run(driver, get_cmd)
        out.append(Result(PASS if ok and same(before, after) else FAIL, name,
                          "" if ok and same(before, after) else
                          f"read back {after!r} after writing {before!r}"))

    roundtrip("set_focus round trip", "get_focus", "set_focus",
              lambda f: {"focus_dac": f["focus_dac"]},
              lambda a, b: a["focus_dac"] == b["focus_dac"])
    roundtrip("set_beam_shift round trip", "get_beam_shift", "set_beam_shift",
              lambda d: {"x": d["x"], "y": d["y"]},
              lambda a, b: (a["x"], a["y"]) == (b["x"], b["y"]))
    roundtrip("set_stigmation round trip", "get_stigmation", "set_stigmation",
              lambda d: {"stigmator": next(iter(d["stigmators"])),
                         **d["stigmators"][next(iter(d["stigmators"]))]},
              lambda a, b: a["stigmators"] == b["stigmators"])
    if "move_stage" in commands:
        ok, data = _run(driver, "move_stage", {"mode": "relative", "x_m": 0.0,
                                                "timeout_s": 20})
        if ok:
            ok, data = _finish_move(driver, data)
        good = ok and isinstance(data, dict) and "x_m" in data and "settled" in data
        out.append(Result(PASS if good else FAIL, "move_stage (relative 0)",
                          "" if good else str(data)[:300]))


def failures(results: list) -> int:
    return sum(1 for r in results if r.level == FAIL)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Check a driver against the AMADEUS "
                                                 "driver interfaces")
    parser.add_argument("--kind", required=True, help="driver kind (jeol, sim, ...)")
    parser.add_argument("--class", dest="cls", default=None,
                        help="explicit driver class 'package.module:ClassName'")
    parser.add_argument("--offline", action="store_true", help="JEOL: PyJEM's simulator")
    parser.add_argument("--option", action="append", default=[], metavar="KEY=VALUE",
                        help="driver option (repeatable; values are JSON if they parse)")
    parser.add_argument("--writes", action="store_true",
                        help="also write back current values (nothing should move)")
    parser.add_argument("--acquire", action="store_true",
                        help="also take one small frame per acquisition slot")
    parser.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args(argv)
    from drivers.registry import build_driver
    options = {}
    for item in args.option:
        key, _, value = item.partition("=")
        try:
            options[key] = json.loads(value)
        except ValueError:
            options[key] = value
    settings = {"offline": args.offline, "options": options}
    if args.cls:
        settings["class"] = args.cls
    try:
        driver = build_driver(args.kind, settings)
    except Exception as exc:
        print(f"FAIL build: {type(exc).__name__}: {exc}")
        return 2
    try:
        results = check_driver(driver, writes=args.writes, acquire=args.acquire)
    finally:
        driver.close()
    if args.json:
        print(json.dumps([r.to_json() for r in results], indent=2))
    else:
        for r in results:
            print(r)
        n = failures(results)
        print(f"\n{n} failure(s)" if n else "\nall contract checks passed")
    return 1 if failures(results) else 0


if __name__ == "__main__":
    sys.exit(main())
