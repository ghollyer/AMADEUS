"""Does online PyJEM's stage setter block, and does it hold the GIL?

Run in the vjem310 env on the microscope PC, with the AMADEUS server STOPPED
(the launcher's Stop) so nothing else drives the stage:

    python scripts/pyjem_stage_probe.py --yes            # 200 nm X out and back
    python scripts/pyjem_stage_probe.py --yes --nm 1000 --axis y

It MOVES THE STAGE by --nm along one axis and back again (relative moves),
so park on an area where that's harmless.

What it measures, for the in-thread call and then through AMADEUS's motion
process (drivers/jeol/stage_motion.py):
  * call_s      how long the setter call took to return
  * max_gap_ms  the longest stall of a 5 ms ticker thread in this process.
                A gap close to call_s means the call held the GIL, freezing
                every thread (the relay's event loop included). A gap of a few
                ms means it didn't.
Writes logs/pyjem_stage_probe_result.json.
"""

import argparse
import json
import os
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


class Ticker:
    def __init__(self, period_s=0.005):
        self.period_s = period_s
        self.max_gap = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        last = time.perf_counter()
        while not self._stop.is_set():
            time.sleep(self.period_s)
            now = time.perf_counter()
            self.max_gap = max(self.max_gap, now - last)
            last = now

    def __enter__(self):
        self._thread.start()
        time.sleep(0.05)
        self.max_gap = 0.0
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(1)


def wait_rest(stage, timeout_s=60.0):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout_s:
        if not any(s == 1 for s in stage.GetStatus()):
            return time.perf_counter() - t0
        time.sleep(0.05)
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--yes", action="store_true", help="confirm the stage may move")
    parser.add_argument("--nm", type=float, default=200.0, help="move size (nm)")
    parser.add_argument("--axis", choices=["x", "y"], default="x")
    parser.add_argument("--offline", action="store_true", help="PyJEM offline simulator")
    args = parser.parse_args()
    if not args.yes:
        parser.error("this moves the stage; re-run with --yes")

    module = "PyJEM.offline.TEM3" if args.offline else "PyJEM.TEM3"
    import importlib
    stage = importlib.import_module(module).Stage3()
    setter = "SetXRel" if args.axis == "x" else "SetYRel"
    results = {"module": module, "axis": args.axis, "nm": args.nm,
               "python": sys.version.split()[0], "start_pos": list(stage.GetPos())}

    # 1) Direct call on this thread (how the old in-process backend did it)
    with Ticker() as ticker:
        t0 = time.perf_counter()
        getattr(stage, setter)(args.nm)
        call_s = time.perf_counter() - t0
        settle_after = wait_rest(stage)
    results["in_thread"] = {"call_s": round(call_s, 3),
                            "max_gap_ms": round(ticker.max_gap * 1000, 1),
                            "rest_after_return_s": settle_after}
    print(f"in-thread {setter}({args.nm:g}): returned after {call_s:.2f} s; "
          f"longest ticker stall {ticker.max_gap * 1000:.0f} ms")

    # 2) Through the motion process (the fix), moving back
    from drivers.jeol.stage_motion import ProcessMotion
    motion = ProcessMotion(module)
    try:
        with Ticker() as ticker:
            t0 = time.perf_counter()
            motion.submit([(setter, (-args.nm,))], "probe return")
            submit_s = time.perf_counter() - t0
            while not motion.done():
                time.sleep(0.02)
            done_s = time.perf_counter() - t0
            wait_rest(stage)
        results["motion_process"] = {"submit_s": round(submit_s, 4),
                                     "done_s": round(done_s, 3),
                                     "max_gap_ms": round(ticker.max_gap * 1000, 1),
                                     "error": motion.take_error()}
        print(f"motion process {setter}({-args.nm:g}): submit {submit_s * 1000:.1f} ms, "
              f"done after {done_s:.2f} s; longest ticker stall "
              f"{ticker.max_gap * 1000:.0f} ms")
    finally:
        motion.close()
    results["end_pos"] = list(stage.GetPos())

    held = results["in_thread"]["max_gap_ms"] > 0.5 * results["in_thread"]["call_s"] * 1000 \
        and results["in_thread"]["call_s"] > 0.2
    results["verdict"] = ("setter blocks AND holds the GIL" if held else
                          "setter blocks but releases the GIL" if call_s > 0.2 else
                          "setter returns immediately")
    print("verdict:", results["verdict"])
    os.makedirs(os.path.join(REPO, "logs"), exist_ok=True)
    out = os.path.join(REPO, "logs", "pyjem_stage_probe_result.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print("wrote", out)


if __name__ == "__main__":
    main()
