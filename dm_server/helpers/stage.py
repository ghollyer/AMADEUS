"""DM helpers for stage control — the FALLBACK hardware path.

Stage control normally goes through a vendor driver (``drivers/``, JEOL on the
F200); the relay routes these commands here only when the config gives the
DM driver the ``stage`` slot. Keep working; add new features to the drivers.

Stage values are returned through persistent tags and converted to SI units on
the Python side.
"""

from dm_server.helpers._dm_util import exec_script, read_float

_TG = "GetPersistentTagGroup()"


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------

def _read_position() -> dict:
    return {
        "x_m":       read_float("stage:x"),
        "y_m":       read_float("stage:y"),
        "z_m":       read_float("stage:z"),
        "alpha_deg": read_float("stage:alpha"),
        "beta_deg":  read_float("stage:beta"),
    }


_FETCH_SCRIPT = f"""\
    number x, y, z, a, b
    EMGetStagePositions(31, x, y, z, a, b)
    TagGroup tg = {_TG}
    TagGroupSetTagAsFloat(tg, "__amadeus__:stage:x",     x / 1e6)
    TagGroupSetTagAsFloat(tg, "__amadeus__:stage:y",     y / 1e6)
    TagGroupSetTagAsFloat(tg, "__amadeus__:stage:z",     z / 1e6)
    TagGroupSetTagAsFloat(tg, "__amadeus__:stage:alpha", a)
    TagGroupSetTagAsFloat(tg, "__amadeus__:stage:beta",  b)
"""


# --------------------------------------------------------------------------
# Public helpers
# --------------------------------------------------------------------------

def get_stage_position(params: dict) -> dict:
    """Return the current stage position for all axes.

    Returns
    -------
    {"x_m": float, "y_m": float, "z_m": float, "alpha_deg": float, "beta_deg": float}
    Units: x/y/z in metres, alpha/beta in degrees.
    """
    exec_script(_FETCH_SCRIPT)
    return _read_position()


def move_stage(params: dict) -> dict:
    """Move the stage to an absolute or relative position.

    Parameters
    ----------
    params["mode"] : "absolute" | "relative"
    params["x_m"], params["y_m"], params["z_m"] : float, optional (metres)
    params["alpha_deg"], params["beta_deg"] : float, optional (degrees)
    params["speed"] : float [0, 1], optional — uses WithSpeed variant

    Returns current stage position after move.
    """
    mode = params.get("mode", "absolute")

    axis_map = {
        "x_m":       1,
        "y_m":       2,
        "z_m":       4,
        "alpha_deg": 8,
        "beta_deg":  16,
    }

    if mode == "relative":
        exec_script(_FETCH_SCRIPT)
        current = _read_position()
        target = {
            "x_m":       current["x_m"]       + float(params.get("x_m",       0.0)),
            "y_m":       current["y_m"]       + float(params.get("y_m",       0.0)),
            "z_m":       current["z_m"]       + float(params.get("z_m",       0.0)),
            "alpha_deg": current["alpha_deg"] + float(params.get("alpha_deg", 0.0)),
            "beta_deg":  current["beta_deg"]  + float(params.get("beta_deg",  0.0)),
        }
        provided = {k for k in axis_map if k in params}
    else:
        target = {
            "x_m":       float(params.get("x_m",       0.0)),
            "y_m":       float(params.get("y_m",       0.0)),
            "z_m":       float(params.get("z_m",       0.0)),
            "alpha_deg": float(params.get("alpha_deg", 0.0)),
            "beta_deg":  float(params.get("beta_deg",  0.0)),
        }
        provided = {k for k in axis_map if k in params}

    flags = 0
    for key, flag in axis_map.items():
        if key in provided:
            flags |= flag

    if flags == 0:
        raise ValueError("move_stage: no axes specified in params")

    x = target["x_m"] * 1e6     # m → µm (EMSetStagePositions expects µm)
    y = target["y_m"] * 1e6
    z = target["z_m"] * 1e6
    a = target["alpha_deg"]
    b = target["beta_deg"]
    speed = params.get("speed")

    if speed is not None:
        script = f"EMSetStagePositionsWithSpeed({flags}, {x}, {y}, {z}, {a}, {b}, {float(speed)})\nEMWaitUntilReady()"
    else:
        script = f"EMSetStagePositions({flags}, {x}, {y}, {z}, {a}, {b})\nEMWaitUntilReady()"

    exec_script(script)
    exec_script(_FETCH_SCRIPT)
    return _read_position()
