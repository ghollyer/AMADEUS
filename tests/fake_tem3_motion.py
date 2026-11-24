"""A stand-in ``TEM3`` module for the stage-motion child process in tests.

``ProcessMotion`` imports a module by name in a *spawned* process and
calls ``Stage3()`` on it, so the in-memory PyJEM fake in
``test_hw_backends.py`` can't reach it. Each setter here blocks for
``DELAY_S``, as online PyJEM's do until the stage settles, and appends the
call to a log file named by ``AMADEUS_FAKE_STAGE_LOG`` so the parent can see
what ran.
"""

import os
import time

DELAY_S = float(os.environ.get("AMADEUS_FAKE_STAGE_DELAY", "0.6"))


def _log(entry: str) -> None:
    path = os.environ.get("AMADEUS_FAKE_STAGE_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(entry + "\n")


class Stage3:
    def _drive(self, name, value):
        time.sleep(DELAY_S)
        _log(f"{name} {float(value):g}")

    def SetX(self, v): self._drive("SetX", v)
    def SetY(self, v): self._drive("SetY", v)
    def SetZ(self, v): self._drive("SetZ", v)
    def SetXRel(self, v): self._drive("SetXRel", v)
    def SetYRel(self, v): self._drive("SetYRel", v)

    def SetTiltXAngle(self, v):
        raise RuntimeError("tilt not supported by this fake")
