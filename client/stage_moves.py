"""Stage-move options shared by the MCP stage tools and the console.

``move_stage`` takes per-move settings, each restored after the move
(``drivers/jeol/driver.py``):
  * ``speed``: slow / normal / fast (PyJEM ``SetSpeedMode``).
  * ``drive``: motor (coarse, +-100 um) / piezo (fine X/Y only, +-10 um;
    ``SelDrvMode``).
  * ``approach``: backlash-compensated X/Y move (overshoot, then approach
    from the calibrated side).
  * ``wait`` / ``timeout_s``: how long the relay follows the move before
    replying ``settled: false, moving: true``.
"""

from __future__ import annotations

SPEEDS = ("slow", "normal", "fast")
DRIVES = ("motor", "piezo")
PIEZO_AXES = ("x_m", "y_m")
# Client timeout = stage wait + this margin, so a slow move comes back as a
# structured reply (the relay answers at 90 % of it), never a dead socket.
CLIENT_MARGIN_S = 15.0


def move_options(params: dict, speed: str | None = None, drive: str | None = None,
                 approach: bool | None = None, wait: bool = True,
                 timeout_s: float = 20.0) -> int:
    """Add the move settings to *params* (in place) and return the client
    timeout in ms. ``None`` leaves a setting at the instrument's default."""
    if speed is not None:
        if speed not in SPEEDS:
            raise ValueError(f"speed must be one of {SPEEDS}, got {speed!r}")
        params["speed"] = speed
    if drive is not None:
        if drive not in DRIVES:
            raise ValueError(f"drive must be one of {DRIVES}, got {drive!r}")
        if drive == "piezo":
            other = [k for k in params if k.endswith(("_m", "_deg")) and k not in PIEZO_AXES]
            if other:
                raise ValueError(f"the piezo drive moves X/Y only (got {', '.join(other)})")
        params["drive"] = drive
    if approach is not None and params.get("drive") != "piezo":   # piezo has no backlash step
        params["approach"] = bool(approach)
    params["wait"] = bool(wait)
    timeout_s = max(1.0, float(timeout_s))
    params["timeout_s"] = timeout_s
    return int((timeout_s + CLIENT_MARGIN_S) * 1000)


DEFAULT_LABEL = "default"


def console_move_options(params: dict, speed_label: str, drive: str,
                         approach_checked: bool, wait_text: str) -> int:
    """The console's Stage-tab widgets -> :func:`move_options`.

    "default" speed and an unticked approach box send nothing, which leaves
    the instrument's own setting. A blank or invalid wait time means 20 s.
    Returns the client timeout in ms."""
    try:
        wait_s = float(wait_text)
    except (TypeError, ValueError):
        wait_s = 20.0
    if wait_s <= 0:
        wait_s = 20.0
    return move_options(params,
                        speed=None if speed_label in ("", DEFAULT_LABEL) else speed_label,
                        drive=None if drive == "motor" else drive,
                        approach=True if approach_checked else None,
                        timeout_s=wait_s)


def describe_status(status: dict) -> dict:
    """get_stage_status -> short strings for the console readouts."""
    speed = status.get("speed") or {}
    piezo = status.get("piezo_nm")
    busy = (status.get("motion") or {}).get("busy")
    moving = "yes" if status.get("moving") else "no"
    if busy:
        moving = f"yes ({busy.get('running_s', 0):.0f} s)"
    return {
        "moving": moving,
        "drive": status.get("drive", "?"),
        "speed": "/".join(speed.get(k, "?") for k in ("xy", "z", "tilt")) if speed else "?",
        "piezo": f"{piezo[0]:.0f}, {piezo[1]:.0f}" if piezo else "?",
        "limit": "LIMIT" if status.get("limit_error") else "ok",
    }
