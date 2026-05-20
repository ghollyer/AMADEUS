"""DM helpers for STEM scanning."""

import numpy as np
import DigitalMicrograph as DM

from dm_server.helpers._dm_util import exec_script, safe_exec_script, read_float, read_string

_TG = "GetPersistentTagGroup()"


def stem_acquire(params: dict):
    """Acquire a single STEM frame using DigiScan.

    Parameters
    ----------
    params["mode"]         : "search" | "preview" | "capture"  (default "capture")
    params["width"]        : int, default 512
    params["height"]       : int, default 512
    params["dwell_us"]     : float, default 2.0
    params["rotation"]     : float, default 0.0
    params["signal_index"] : int, default 0

    Returns
    -------
    (dict, numpy.ndarray)
    """
    width        = int(params.get("width",        512))
    height       = int(params.get("height",       512))
    dwell_us     = float(params.get("dwell_us",   2.0))
    rotation     = float(params.get("rotation",   0.0))
    signal_index = int(params.get("signal_index", 0))
    mode         = params.get("mode", "capture")

    # DSCreateParameters: (width, height, rotation, pixelTime_us, lineSynchEnabled)
    # DSSetParametersSignal dataType: bytes — 4 = float32
    script = f"""\
        number paramID = DSCreateParameters({width}, {height}, {rotation}, {dwell_us}, 0)
        DSSetParametersSignal(paramID, {signal_index}, 4, 1, 0)
        DSStartAcquisition(paramID, 0, 1)
        image img := DSGetLastAcquiredImage({signal_index})
        ShowImage(img)
        TagGroupSetTagAsString({_TG}, "__amadeus__:ds:name", GetName(img))
        DSDeleteParameters(paramID)
    """
    safe_exec_script(script, expected_tags=["ds:name"])

    img = DM.FindFrontImage()
    arr = np.array(img.GetNumArray()).astype(np.float32)

    try:
        _, x_scale, x_units = img.GetDimensionCalibration(0, 0)
        _, y_scale, _       = img.GetDimensionCalibration(1, 0)
    except Exception:
        x_scale, y_scale, x_units = 1.0, 1.0, "px"

    try:
        name = read_string("ds:name")
    except RuntimeError:
        name = f"STEM_{mode}_{width}x{height}"

    meta = {
        "mode":         mode,
        "signal_index": signal_index,
        "image_meta": {
            "name": name,
            "calibration": {
                "x_scale": x_scale,
                "y_scale": y_scale,
                "units":   x_units,
            },
        },
    }
    return meta, arr


def get_stem_settings(params: dict) -> dict:
    """Return current DigiScan detector info and acquisition status."""
    script = f"""\
        TagGroup tg = {_TG}
        number n = DSCountSignals()
        TagGroupSetTagAsFloat(tg, "__amadeus__:ds:n_signals", n)
        number active = 0
        if (DSIsAcquisitionActive()) {{ active = 1 }}
        TagGroupSetTagAsFloat(tg, "__amadeus__:ds:active", active)
        number i
        for (i = 0; i < n; i++) {{
            TagGroupSetTagAsString(tg, "__amadeus__:ds:sig:" + i, DSGetSignalName(i))
        }}
    """
    safe_exec_script(script, expected_tags=["ds:n_signals", "ds:active"])

    n = int(read_float("ds:n_signals"))
    signals = []
    for i in range(n):
        try:
            name = read_string(f"ds:sig:{i}")
        except RuntimeError:
            name = f"Signal {i}"
        signals.append({"index": i, "name": name})

    return {
        "num_signals": n,
        "signals":     signals,
        "is_active":   bool(read_float("ds:active")),
    }


def set_stem_settings(params: dict) -> dict:
    """Settings are applied at acquisition time via stem_acquire params."""
    return {}


def get_detectors(params: dict) -> dict:
    """Return list of available DigiScan signals."""
    script = f"""\
        TagGroup tg = {_TG}
        number n = DSCountSignals()
        TagGroupSetTagAsFloat(tg, "__amadeus__:ds:n_signals", n)
        number i
        for (i = 0; i < n; i++) {{
            TagGroupSetTagAsString(tg, "__amadeus__:ds:sig:" + i, DSGetSignalName(i))
        }}
    """
    safe_exec_script(script, expected_tags=["ds:n_signals"])

    n = int(read_float("ds:n_signals"))
    detectors = []
    for i in range(n):
        try:
            name = read_string(f"ds:sig:{i}")
        except RuntimeError:
            name = f"Signal {i}"
        detectors.append({"index": i, "name": name})
    return {"detectors": detectors}


def set_detector(params: dict) -> dict:
    """Validate signal index and return its name."""
    idx = int(params["signal_index"])
    script = f"""\
        TagGroup tg = {_TG}
        number n = DSCountSignals()
        TagGroupSetTagAsFloat(tg, "__amadeus__:ds:n_signals", n)
        TagGroupSetTagAsString(tg, "__amadeus__:ds:sig:{idx}", DSGetSignalName({idx}))
    """
    safe_exec_script(script, expected_tags=["ds:n_signals"])
    n = int(read_float("ds:n_signals"))
    if idx < 0 or idx >= n:
        raise ValueError(f"signal_index {idx} out of range (0–{n-1})")
    try:
        name = read_string(f"ds:sig:{idx}")
    except RuntimeError:
        name = f"Signal {idx}"
    return {"signal_index": idx, "name": name}


def move_beam_to(params: dict) -> dict:
    """Move the STEM beam to a position in DS coordinates.

    params["x"], params["y"]: float (DS coordinate system)
    """
    x = float(params["x"])
    y = float(params["y"])
    exec_script(f"DSSetBeamDSPosition({x}, {y})")
    return {}


def get_beam_position(params: dict) -> dict:
    """Return the current STEM beam position in DS coordinates."""
    script = f"""\
        TagGroup tg = {_TG}
        number x, y
        DSGetBeamDSPosition(x, y)
        TagGroupSetTagAsFloat(tg, "__amadeus__:ds:bx", x)
        TagGroupSetTagAsFloat(tg, "__amadeus__:ds:by", y)
    """
    safe_exec_script(script, expected_tags=["ds:bx", "ds:by"])
    return {"x": read_float("ds:bx"), "y": read_float("ds:by")}
