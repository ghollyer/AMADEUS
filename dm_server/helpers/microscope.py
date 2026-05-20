"""DM helpers for microscope control.

Values are passed through persistent tags so the Python side can read results
from script calls. Focus conversion uses a local raw-to-nm calibration because
calibrated focus APIs are not reliably configured across instruments.
"""

from dm_server.helpers._dm_util import exec_script, safe_exec_script, read_float, read_string

_TG = "GetPersistentTagGroup()"   # shorthand used in script strings

# Focus calibration is intentionally local configuration. Recalibrate these
# constants for each microscope and accelerating voltage before relying on
# focus_nm values.
#
# The relationship is affine, not multiplicative: the raw lens value at zero
# displayed defocus is nonzero. Store both a slope and an offset:
#
#     focus_nm = (raw - focus_raw_at_zero) / focus_raw_per_nm
#     raw      = focus_nm * focus_raw_per_nm + focus_raw_at_zero
#
# How to recalibrate (must be redone per microscope and per HT):
#   1. Park the microscope at standard / zero displayed defocus, call
#      get_focus(), and record raw → focus_raw_at_zero.
#   2. Set defocus to two known values D1, D2 (nm) on the microscope console,
#      call get_focus() at each, recording raw1, raw2.
#   3. Slope: focus_raw_per_nm = (raw2 - raw1) / (D2 - D1)
#      (Cross-check the offset: focus_raw_at_zero ≈ raw1 - D1 * slope.)
DEFAULT_FOCUS_CALIBRATION = {
    "label":        "configured microscope",
    "default_mode": "IMAGING",
    "by_mode": {
        # TEM imaging — raws are ~1.55 M.
        "IMAGING":  {
            "focus_raw_per_nm":  0.7505,
            "focus_raw_at_zero": 1548890.0,
        },
        # STEM scanning — raws are ~33 k.
        "SCANNING": {
            "focus_raw_per_nm":  0.71429,
            "focus_raw_at_zero": 32797.0,
        },
    },
}

# Active calibration. Reassign this for each deployment.
MICROSCOPE_CALIBRATION = DEFAULT_FOCUS_CALIBRATION


def _focus_cal_for_mode(op_mode: str) -> dict:
    """Pick the per-mode focus calibration sub-dict, tolerant of trailing
    whitespace and case.  Falls back to the calibration's ``default_mode``
    when the supplied mode is empty or unrecognised."""
    by_mode = MICROSCOPE_CALIBRATION.get("by_mode", {})
    norm = (op_mode or "").strip().upper()
    for k, v in by_mode.items():
        if norm.startswith(k.upper()):
            return v
    default = MICROSCOPE_CALIBRATION.get("default_mode", "IMAGING")
    return by_mode.get(default, next(iter(by_mode.values())))


def _focus_raw_per_nm(op_mode: str) -> float:
    return _focus_cal_for_mode(op_mode)["focus_raw_per_nm"]


def _focus_raw_at_zero(op_mode: str) -> float:
    return _focus_cal_for_mode(op_mode)["focus_raw_at_zero"]


def _microscope_label() -> str:
    return MICROSCOPE_CALIBRATION.get("label", "")


def _raw_to_nm(raw: float, op_mode: str) -> float:
    cal = _focus_cal_for_mode(op_mode)
    return (raw - cal["focus_raw_at_zero"]) / cal["focus_raw_per_nm"]


def _nm_to_raw(nm: float, op_mode: str) -> float:
    cal = _focus_cal_for_mode(op_mode)
    return nm * cal["focus_raw_per_nm"] + cal["focus_raw_at_zero"]


def _read_operation_mode() -> str:
    """One-off probe of ``EMGetOperationMode()``.  Returns the mode string
    ("IMAGING", "SCANNING", "DIFFRACTION", …) with whitespace stripped."""
    safe_exec_script(
        f'TagGroupSetTagAsString({_TG}, "__amadeus__:opmode", EMGetOperationMode())',
        expected_tags=["opmode"],
    )
    return read_string("opmode").strip()


# --------------------------------------------------------------------------
# Focus
# --------------------------------------------------------------------------

def get_focus(params: dict) -> dict:
    """Return the current defocus value.

    Reads ``EMGetFocus()`` and ``EMGetOperationMode()`` in the same script
    round-trip so the conversion uses the calibration appropriate to the
    current operation mode (TEM = IMAGING, STEM = SCANNING).  See the
    calibration block at the top of this module — the two modes expose
    raw values that differ by ~50× because they drive different lens
    coils.

    Returns
    -------
    {"focus_nm": float, "focus_raw": float, "operation_mode": str}
    """
    safe_exec_script(f"""\
        EMUpdateCalibrationState()
        number f = EMGetFocus()
        string opmode = EMGetOperationMode()
        TagGroup tg = {_TG}
        TagGroupSetTagAsFloat(tg,  "__amadeus__:focus",        f)
        TagGroupSetTagAsString(tg, "__amadeus__:focus:opmode", opmode)
    """, expected_tags=["focus", "focus:opmode"])
    raw    = read_float("focus")
    opmode = read_string("focus:opmode").strip()
    return {
        "focus_nm":       _raw_to_nm(raw, opmode),
        "focus_raw":      raw,
        "operation_mode": opmode,
    }


def set_focus(params: dict) -> dict:
    """Set absolute defocus.

    params["focus_nm"]: float (nanometres) — converted to raw units via the
    microscope-specific calibration for the *current* operation mode (probed
    via a quick ``EMGetOperationMode()`` call before the set).  The
    ``EMSetFocus`` call is wrapped in ``safe_exec_script`` so an "outside
    permitted range" failure surfaces the actual raw value being sent.
    """
    opmode = _read_operation_mode()
    raw = _nm_to_raw(float(params["focus_nm"]), opmode)
    safe_exec_script(f"EMSetFocus({raw})")
    return {"focus_raw": raw, "operation_mode": opmode}


def change_focus(params: dict) -> dict:
    """Apply a relative focus change.

    Uses ``EMChangeFocus(delta)`` directly rather than read-modify-writing
    through ``EMSetFocus``.  This avoids issuing an absolute focus command
    whose value depends on a stale ``EMGetFocus()`` reading, and is also
    the relative-change API exposed by the microscope environment.

    params["delta_nm"]: float (nanometres)
    """
    # Probe the operation mode first so the slope used for the delta
    # matches the lens system that's actually driving the column.
    opmode = _read_operation_mode()
    # Relative delta — only the slope applies; the raw-at-zero offset
    # cancels for a change and must not be added in.
    delta_raw = float(params["delta_nm"]) * _focus_raw_per_nm(opmode)
    safe_exec_script(f"""\
        EMChangeFocus({delta_raw})
        EMUpdateCalibrationState()
        number f = EMGetFocus()
        TagGroupSetTagAsFloat({_TG}, "__amadeus__:focus", f)
    """, expected_tags=["focus"])
    raw = read_float("focus")
    return {
        "focus_nm":       _raw_to_nm(raw, opmode),
        "focus_raw":      raw,
        "operation_mode": opmode,
    }


def autofocus(params: dict) -> dict:
    """Trigger the DM autofocus routine.

    The exact autofocus function name varies by instrument/plugin.
    Common options: AutoFocus(), EMDoAutoFocus().
    Adjust the script line below for your instrument.

    Returns: {"focus_nm": float}
    """
    exec_script(f"""\
        // Uncomment the autofocus call for your instrument:
        // AutoFocus()
        // EMDoAutoFocus()
        number f = EMGetFocus()
        TagGroupSetTagAsFloat({_TG}, "__amadeus__:focus", f)
    """)
    return {"focus_nm": _raw_to_nm(read_float("focus"))}


# --------------------------------------------------------------------------
# Magnification
# --------------------------------------------------------------------------

def get_magnification(params: dict) -> dict:
    """Return current magnification and mag table index.

    Returns: {"magnification": float, "mag_index": int}
    """
    exec_script(f"""\
        EMUpdateCalibrationState()
        number mag = EMGetMagnification()
        number idx = EMGetMagIndex()
        TagGroupSetTagAsFloat({_TG}, "__amadeus__:mag",     mag)
        TagGroupSetTagAsFloat({_TG}, "__amadeus__:mag_idx", idx)
    """)
    return {
        "magnification": read_float("mag"),
        "mag_index":     int(read_float("mag_idx")),
    }


def set_magnification(params: dict) -> dict:
    """Set magnification by table index.  params["mag_index"]: int"""
    idx = int(params["mag_index"])
    exec_script(f"""\
        EMSetMagIndex({idx})
        EMWaitUntilReady()
        EMUpdateCalibrationState()
        number mag     = EMGetMagnification()
        number new_idx = EMGetMagIndex()
        TagGroupSetTagAsFloat({_TG}, "__amadeus__:mag",     mag)
        TagGroupSetTagAsFloat({_TG}, "__amadeus__:mag_idx", new_idx)
    """)
    return {
        "magnification": read_float("mag"),
        "mag_index":     int(read_float("mag_idx")),
    }


# --------------------------------------------------------------------------
# Beam shift / tilt
# --------------------------------------------------------------------------

def get_beam_shift(params: dict) -> dict:
    exec_script(f"""\
        number x, y
        EMGetBeamShift(x, y)
        TagGroupSetTagAsFloat({_TG}, "__amadeus__:bsx", x)
        TagGroupSetTagAsFloat({_TG}, "__amadeus__:bsy", y)
    """)
    return {"x": read_float("bsx"), "y": read_float("bsy")}


def set_beam_shift(params: dict) -> dict:
    x, y = float(params["x"]), float(params["y"])
    exec_script(f"EMSetBeamShift({x}, {y})")
    return {}


def get_beam_tilt(params: dict) -> dict:
    exec_script(f"""\
        number x, y
        EMGetBeamTilt(x, y)
        TagGroupSetTagAsFloat({_TG}, "__amadeus__:btx", x)
        TagGroupSetTagAsFloat({_TG}, "__amadeus__:bty", y)
    """)
    return {"x": read_float("btx"), "y": read_float("bty")}


def set_beam_tilt(params: dict) -> dict:
    x, y = float(params["x"]), float(params["y"])
    exec_script(f"EMSetBeamTilt({x}, {y})")
    return {}


# --------------------------------------------------------------------------
# Stigmation
# --------------------------------------------------------------------------

def get_stigmation(params: dict) -> dict:
    exec_script(f"""\
        number ox, oy, cx, cy
        EMGetObjectiveStigmation(ox, oy)
        EMGetCondenserStigmation(cx, cy)
        TagGroupSetTagAsFloat({_TG}, "__amadeus__:stig:ox", ox)
        TagGroupSetTagAsFloat({_TG}, "__amadeus__:stig:oy", oy)
        TagGroupSetTagAsFloat({_TG}, "__amadeus__:stig:cx", cx)
        TagGroupSetTagAsFloat({_TG}, "__amadeus__:stig:cy", cy)
    """)
    return {
        "obj_x":  read_float("stig:ox"),
        "obj_y":  read_float("stig:oy"),
        "cond_x": read_float("stig:cx"),
        "cond_y": read_float("stig:cy"),
    }


def set_stigmation(params: dict) -> dict:
    """params["mode"]: 0=objective, 1=condenser"""
    mode = int(params["mode"])
    x, y = float(params["x"]), float(params["y"])
    if mode == 0:
        exec_script(f"EMSetObjectiveStigmation({x}, {y})")
    else:
        exec_script(f"EMSetCondenserStigmation({x}, {y})")
    return {}


# --------------------------------------------------------------------------
# Full microscope state snapshot
# --------------------------------------------------------------------------

def get_microscope_state(params: dict) -> dict:
    """Return a snapshot of all common microscope parameters."""
    exec_script(f"""\
        EMUpdateCalibrationState()
        number ht      = EMGetHighTension()
        number mag     = EMGetMagnification()
        number idx     = EMGetMagIndex()
        number f = EMGetFocus()
        number blanked = EMGetBeamBlanked()
        number valves  = EMGetColumnValvesOpen()
        number bsx, bsy, btx, bty, ox, oy, cx, cy
        EMGetBeamShift(bsx, bsy)
        EMGetBeamTilt(btx, bty)
        EMGetObjectiveStigmation(ox, oy)
        EMGetCondenserStigmation(cx, cy)
        // Operation mode picks the focus calibration on the Python side —
        // TEM and STEM expose lens raws that differ by ~50×.
        string opmode = EMGetOperationMode()
        // EMGetMicroscopeName() can be resolved at parse time, so missing
        // functions may abort before any try/catch can fire. Source
        // microscope_name from the active calibration dict instead.
        TagGroup tg = {_TG}
        TagGroupSetTagAsFloat(tg,  "__amadeus__:state:ht",      ht)
        TagGroupSetTagAsFloat(tg,  "__amadeus__:state:mag",     mag)
        TagGroupSetTagAsFloat(tg,  "__amadeus__:state:mag_idx", idx)
        TagGroupSetTagAsFloat(tg,  "__amadeus__:state:focus",   f)
        TagGroupSetTagAsFloat(tg,  "__amadeus__:state:blanked", blanked)
        TagGroupSetTagAsFloat(tg,  "__amadeus__:state:valves",  valves)
        TagGroupSetTagAsFloat(tg,  "__amadeus__:state:bsx",     bsx)
        TagGroupSetTagAsFloat(tg,  "__amadeus__:state:bsy",     bsy)
        TagGroupSetTagAsFloat(tg,  "__amadeus__:state:btx",     btx)
        TagGroupSetTagAsFloat(tg,  "__amadeus__:state:bty",     bty)
        TagGroupSetTagAsFloat(tg,  "__amadeus__:state:ox",      ox)
        TagGroupSetTagAsFloat(tg,  "__amadeus__:state:oy",      oy)
        TagGroupSetTagAsFloat(tg,  "__amadeus__:state:cx",      cx)
        TagGroupSetTagAsFloat(tg,  "__amadeus__:state:cy",      cy)
        TagGroupSetTagAsString(tg, "__amadeus__:state:opmode",  opmode)
    """)
    opmode = read_string("state:opmode").strip()
    return {
        "high_tension_kV":    read_float("state:ht") / 1000.0,
        "magnification":      read_float("state:mag"),
        "mag_index":          int(read_float("state:mag_idx")),
        "focus_nm":           _raw_to_nm(read_float("state:focus"), opmode),
        "beam_blanked":       bool(read_float("state:blanked")),
        "column_valves_open": bool(read_float("state:valves")),
        "beam_shift":         {"x": read_float("state:bsx"), "y": read_float("state:bsy")},
        "beam_tilt":          {"x": read_float("state:btx"), "y": read_float("state:bty")},
        "stigmation": {
            "obj_x":  read_float("state:ox"),
            "obj_y":  read_float("state:oy"),
            "cond_x": read_float("state:cx"),
            "cond_y": read_float("state:cy"),
        },
        "microscope_name": _microscope_label(),
        "operation_mode":  opmode,
    }


# --------------------------------------------------------------------------
# Blanking / valves / apertures / escape hatch
# --------------------------------------------------------------------------

def set_beam_blank(params: dict) -> dict:
    val = 1 if params.get("blanked") else 0
    exec_script(f"EMSetBeamBlanked({val})")
    return {}


def set_column_valves(params: dict) -> dict:
    val = 1 if params.get("open") else 0
    exec_script(f"EMSetColumnValvesOpen({val})")
    return {}


def get_aperture(params: dict) -> dict:
    raise NotImplementedError(
        "Aperture control API varies by microscope vendor. "
        "Check available EM* aperture functions on your instrument."
    )


def set_aperture(params: dict) -> dict:
    raise NotImplementedError(
        "Aperture control API varies by microscope vendor. "
        "Check available EM* aperture functions on your instrument."
    )


def execute_script(params: dict) -> dict:
    """Execute an arbitrary DM-Script string (escape hatch)."""
    script = params.get("script", "")
    if not script.strip():
        raise ValueError("execute_script: 'script' param is empty")
    exec_script(script)
    return {"executed": True}
