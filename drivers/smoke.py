"""Read-only first check of a configured driver on the real instrument.

Run it on the microscope PC, in the driver's own Python, before letting the
driver move anything (for JEOL this is the vjem310 interpreter):

    "<driver python>" -m drivers.smoke --driver hw
    python -m drivers.smoke --driver hw --amadeus-config C:\\AMADEUS\\amadeus_config.json

It builds the driver exactly as the relay would (from ``amadeus_config.json``),
runs the contract's read checks (``drivers/contract.py``: replies in the
neutral shape, stage in metres, enumerations listed), and prints every value
so they can be compared with the vendor's own console. Nothing is written to
the instrument. The result goes to ``logs/driver_smoke_<name>.json``, which
the launcher's Readiness list requires before an online driver is trusted.

Stop the AMADEUS server first if the vendor API allows only one client.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

from common import config as amadeus_config

LOG_DIR = os.path.join(str(amadeus_config.REPO_ROOT), "logs")


def result_path(name: str) -> str:
    return os.path.join(LOG_DIR, f"driver_smoke_{name}.json")


def record(name: str, spec: dict, passed: bool, results: list) -> str:
    path = result_path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"driver": name, "kind": spec.get("kind"), "passed": passed,
                   "offline": bool(spec.get("offline")), "python": sys.executable,
                   "repo": str(amadeus_config.REPO_ROOT),
                   "time": datetime.datetime.now().isoformat(timespec="seconds"),
                   "results": [r.to_json() for r in results]}, f, indent=2)
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only check of a configured driver")
    amadeus_config.add_config_argument(parser)
    parser.add_argument("--driver", required=True, help="driver name in the config")
    args = parser.parse_args(argv)
    try:
        cfg = amadeus_config.load(args.amadeus_config)
    except amadeus_config.ConfigError as exc:
        parser.error(str(exc))
    drivers = amadeus_config.effective_drivers(cfg)
    if args.driver not in drivers:
        parser.error(f"no driver {args.driver!r} in the config (have: {', '.join(drivers)})")
    spec = drivers[args.driver]
    if spec.get("kind") in ("dm", "remote"):
        print(f"{args.driver} is a {spec['kind']} driver: a separate process with its own "
              f"checks (for DM: amadeus-check). Nothing to do here.")
        return 0
    print(f"Python : {sys.version.splitlines()[0]} ({sys.executable})")
    print(f"Driver : {args.driver} ({spec['kind']}"
          f"{', offline' if spec.get('offline') else ''}), slots {', '.join(spec['slots'])}")

    from drivers import contract
    from drivers.dispatch import call
    from drivers.registry import build_driver
    settings = {k: spec[k] for k in ("offline", "calibration", "options", "class")
                if spec.get(k) is not None}
    settings["instrument"] = cfg.get("instrument") or {}
    try:
        driver = build_driver(spec["kind"], settings)
    except Exception as exc:
        print(f"FAIL   : can't build the driver: {type(exc).__name__}: {exc}")
        record(args.driver, spec, False, [contract.Result(contract.FAIL, "build", str(exc))])
        return 1
    try:
        results = contract.check_driver(driver, writes=False, acquire=False)
        print()
        for r in results:
            print(r)
        print("\nValues to compare with the vendor console:")
        for cmd in ("get_stage_position", "get_focus", "get_magnification",
                    "get_high_tension", "get_instrument_mode"):
            if cmd in driver.operations():
                reply = call(driver, {"cmd": cmd, "params": {}})
                data = reply.get("data") if isinstance(reply, dict) else None
                print(f"  {cmd}: {json.dumps(data)[:240]}")
    finally:
        driver.close()
    passed = contract.failures(results) == 0
    path = record(args.driver, spec, passed, results)
    print(f"\n{'PASSED' if passed else 'FAILED'}; recorded in {path}")
    if passed and not spec.get("offline"):
        print("Check the stage readout above against the vendor console (in metres here) "
              "before trusting closed-loop moves.")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
