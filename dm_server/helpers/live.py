"""DM helpers for continuous *live scan* — STEM (DigiScan) or camera.

Unlike every other helper module, this one is **not** stateless: a live scan is
an ongoing DM acquisition that must be started, sampled many times, and later
stopped, so the running acquisition handle has to survive between commands. The
module keeps exactly that — the active session — in ``_SESSION`` and nothing
else. (The architectural "stateless DM server" rule is about request *memory*;
the live acquisition is inherent instrument state, the same way DM itself holds
the running scan.)

How it works
------------
DM's own acquisition engine runs the continuous scan in the background, so the
ZMQ server thread never blocks on it. The server loop merely calls
:func:`grab_frame` between client requests to snapshot the current frame buffer
and publishes it on the PUB socket. This keeps the DM server single-threaded —
no DM-Script is ever called from a background thread.

* **STEM**: ``DSStartAcquisition(paramID, 1, 0)`` (continuous, asynchronous);
  ``DSGetLastAcquiredImage`` returns the live buffer DM updates in place, so
  after one ``ShowImage`` at start, each :func:`grab_frame` just re-reads the
  front image's pixels in Python — no per-frame DM-Script.
* **Camera** (GMS 3.4+): ``CameraStartContinuousAcquisition`` then per frame
  ``CameraGetFrameInContinuousMode(camID, dst, max_wait)`` into the shown
  destination image (``Scripting_Camera.md`` Example 1).
"""

import numpy as np
import DigitalMicrograph as DM

from dm_server.helpers._dm_util import (
    exec_script, safe_exec_script, read_float, read_string, dm_text,
)

_TG = "GetPersistentTagGroup()"
_LIVE_NAME = "AMADEUS Live"

# Maps processing string -> DM-Script expression (mirrors camera.py).
_PROC_MAP = {
    "unprocessed":     "CameraGetUnprocessedEnum()",
    "dark_subtracted": "CameraGetDarkSubtractedEnum()",
    "gain_normalised": "CameraGetGainNormalizedEnum()",
}

# The single active live session, or None. Keys:
#   detector      : "stem" | "camera"
#   frame_index   : int, incremented on every grabbed frame
#   interval_ms   : int, how often the server loop samples/publishes
#   calibration   : {"x_scale", "y_scale", "units"}  (read once at start)
#   name          : str, DM image name of the live frame
#   param_id      : int  (STEM only — DSCreateParameters handle to delete)
#   signal_index  : int  (STEM only)
#   cam_id        : int  (camera only)
#   max_wait      : float (camera only — per-frame wait for GetFrame)
_SESSION: dict | None = None


# ---------------------------------------------------------------------------
# Queried by the server loop
# ---------------------------------------------------------------------------
def is_active() -> bool:
    return _SESSION is not None


def poll_interval_ms() -> int:
    """Inter-frame cadence while live; a relaxed REP poll otherwise."""
    if _SESSION is None:
        return 250
    return int(_SESSION.get("interval_ms", 100))


# ---------------------------------------------------------------------------
# Start / stop
# ---------------------------------------------------------------------------
def live_start(params: dict) -> dict:
    """Start a continuous live scan. ``params['detector']`` = 'stem' | 'camera'.

    STEM params : width, height, dwell_us, rotation, signal_index
    Camera params: exposure_s, binning, processing, camera_id
    Common      : interval_ms (server sampling cadence, default 100)
    """
    detector = params.get("detector", "stem")
    # Switching detectors (or restarting) tears down any existing session first;
    # STEM and camera are mutually exclusive on the column.
    if _SESSION is not None:
        _teardown()

    if detector == "stem":
        return _start_stem(params)
    if detector == "camera":
        return _start_camera(params)
    raise ValueError(f"Unknown detector {detector!r} (expected 'stem' or 'camera')")


def live_stop(params: dict) -> dict:
    """Stop the active live scan. Safe to call when nothing is running."""
    was = None if _SESSION is None else _SESSION.get("detector")
    _teardown()
    return {"stopped": was is not None, "detector": was}


def live_status(params: dict) -> dict:
    """Report whether a live scan is running and its parameters."""
    if _SESSION is None:
        return {"active": False, "detector": None, "frame_index": 0}
    return {
        "active":       True,
        "detector":     _SESSION["detector"],
        "frame_index":  _SESSION["frame_index"],
        "interval_ms":  _SESSION.get("interval_ms"),
        "signal_index": _SESSION.get("signal_index"),
        "camera_id":    _SESSION.get("cam_id"),
        "calibration":  _SESSION.get("calibration"),
    }


# ---------------------------------------------------------------------------
# Per-frame grab — called by the server loop, never registered as a command
# ---------------------------------------------------------------------------
def grab_frame():
    """Return ``(meta, np.ndarray)`` for the current live frame, or ``None``.

    ``None`` means "no new frame this tick" (camera timeout) or a transient
    read error — the server loop simply skips publishing and tries again.
    """
    if _SESSION is None:
        return None
    try:
        if _SESSION["detector"] == "camera":
            if not _camera_pull_frame():
                return None  # timed out waiting for a frame
        # STEM: the live buffer updates in place; nothing to pull.
        img = DM.FindFrontImage()
        arr = np.array(img.GetNumArray()).astype(np.float32)
    except Exception:
        return None

    _SESSION["frame_index"] += 1
    meta = {
        "detector":     _SESSION["detector"],
        "frame_index":  _SESSION["frame_index"],
        "signal_index": _SESSION.get("signal_index"),
        "camera_id":    _SESSION.get("cam_id"),
        "image_meta": {
            "name":        _SESSION.get("name", _LIVE_NAME),
            "calibration": _SESSION.get("calibration",
                                        {"x_scale": 1.0, "y_scale": 1.0, "units": "px"}),
        },
    }
    return meta, arr


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _start_stem(params: dict) -> dict:
    global _SESSION
    width        = int(params.get("width",        512))
    height       = int(params.get("height",       512))
    dwell_us     = float(params.get("dwell_us",   2.0))
    rotation     = float(params.get("rotation",   0.0))
    signal_index = int(params.get("signal_index", 0))
    interval_ms  = int(params.get("interval_ms",  100))

    # Continuous (continuous=1, synchronous=0) — DM scans in the background and
    # keeps the live image updated. We hold paramID for a clean stop later.
    script = f"""\
        number paramID = DSCreateParameters({width}, {height}, {rotation}, {dwell_us}, 0)
        DSSetParametersSignal(paramID, {signal_index}, 4, 1, 0)
        DSStartAcquisition(paramID, 1, 0)
        image img := DSGetLastAcquiredImage({signal_index})
        SetName(img, "{_LIVE_NAME}")
        ShowImage(img)
        TagGroupSetTagAsFloat({_TG}, "__amadeus__:live:param_id", paramID)
    """
    safe_exec_script(script, expected_tags=["live:param_id"])

    param_id = int(read_float("live:param_id"))
    calibration = _read_front_calibration()

    _SESSION = {
        "detector":     "stem",
        "frame_index":  0,
        "interval_ms":  interval_ms,
        "calibration":  calibration,
        "name":         _LIVE_NAME,
        "param_id":     param_id,
        "signal_index": signal_index,
    }
    return {"active": True, "detector": "stem", "signal_index": signal_index,
            "interval_ms": interval_ms, "calibration": calibration}


def _start_camera(params: dict) -> dict:
    global _SESSION
    exposure_s  = float(params.get("exposure_s", 0.1))
    binning     = int(params.get("binning", 1))
    proc_key    = params.get("processing", "gain_normalised")
    interval_ms = int(params.get("interval_ms", 100))
    max_wait    = float(params.get("max_wait_s", 0.05))

    if proc_key not in _PROC_MAP:
        raise ValueError(
            f"Unknown processing {proc_key!r}. Choose from: {list(_PROC_MAP)}")
    proc_expr = _PROC_MAP[proc_key]
    cam_expr = (str(int(params["camera_id"])) if "camera_id" in params
                else "CameraGetActiveCameraID()")

    # Create a correctly-sized destination once, show it, then start streaming.
    # CameraGetFrameInContinuousMode (called each grab) refreshes it in place.
    # Neither CameraCreateImageForAcquire nor the continuous frames calibrate
    # the destination (verified on the F200: scale 1, no units, even after
    # frames), so take one reference frame with CameraAcquire — which DM does
    # calibrate — and copy its calibration across before streaming.
    script = f"""\
        number cam_id = {cam_expr}
        number proc   = {proc_expr}
        if ( DSIsAcquisitionActive() )
        {{
            DSStopAcquisition(-1)
        }}
        number _wait_n = 0
        while ( DSIsAcquisitionActive() && _wait_n < 50 )
        {{
            Sleep(0.1)
            _wait_n = _wait_n + 1
        }}
        CameraPrepareForAcquire(cam_id)
        image dst := CameraCreateImageForAcquire(cam_id, {binning}, {binning}, proc)
        image _cal_ref := CameraAcquire(cam_id, {exposure_s}, {binning}, {binning}, proc)
        ImageCopyCalibrationFrom(dst, _cal_ref)
        SetName(dst, "{_LIVE_NAME}")
        dst.ShowImage()
        CameraStartContinuousAcquisition(cam_id, {exposure_s}, {binning}, {binning}, proc)
        number origin_x, scale_x, origin_y, scale_y
        string units_x
        dst.ImageGetDimensionCalibration(0, origin_x, scale_x, units_x, 1)
        dst.ImageGetDimensionCalibration(1, origin_y, scale_y, units_x, 1)
        TagGroupSetTagAsFloat({_TG},  "__amadeus__:live:cam_id", cam_id)
        TagGroupSetTagAsFloat({_TG},  "__amadeus__:live:scx",    scale_x)
        TagGroupSetTagAsFloat({_TG},  "__amadeus__:live:scy",    scale_y)
        TagGroupSetTagAsString({_TG}, "__amadeus__:live:units",  units_x)
    """
    safe_exec_script(script, expected_tags=["live:cam_id"])

    cam_id = int(read_float("live:cam_id"))
    try:
        calibration = {"x_scale": read_float("live:scx"),
                       "y_scale": read_float("live:scy"),
                       "units":   read_string("live:units")}
    except RuntimeError:
        calibration = {"x_scale": 1.0, "y_scale": 1.0, "units": "px"}

    _SESSION = {
        "detector":    "camera",
        "frame_index": 0,
        "interval_ms": interval_ms,
        "calibration": calibration,
        "name":        _LIVE_NAME,
        "cam_id":      cam_id,
        "max_wait":    max_wait,
    }
    return {"active": True, "detector": "camera", "camera_id": cam_id,
            "interval_ms": interval_ms, "calibration": calibration}


def _camera_pull_frame() -> bool:
    """Pull one frame into the live image. Returns False on timeout."""
    cam_id   = _SESSION["cam_id"]
    max_wait = _SESSION.get("max_wait", 0.05)
    script = f"""\
        image dst := GetFrontImage()
        number got = CameraGetFrameInContinuousMode({cam_id}, dst, {max_wait})
        TagGroupSetTagAsFloat({_TG}, "__amadeus__:live:got", got)
    """
    safe_exec_script(script, expected_tags=["live:got"])
    return bool(read_float("live:got"))


def _read_front_calibration() -> dict:
    """Read the front image's x/y calibration (best effort)."""
    try:
        img = DM.FindFrontImage()
        _, x_scale, x_units = img.GetDimensionCalibration(0, 0)
        _, y_scale, _       = img.GetDimensionCalibration(1, 0)
        return {"x_scale": x_scale, "y_scale": y_scale, "units": dm_text(x_units)}
    except Exception:
        return {"x_scale": 1.0, "y_scale": 1.0, "units": "px"}


def _teardown() -> None:
    """Stop whatever is running and clear the session. Never raises."""
    global _SESSION
    if _SESSION is None:
        return
    try:
        if _SESSION["detector"] == "stem":
            param_id = _SESSION.get("param_id", -1)
            exec_script(
                "if ( DSIsAcquisitionActive() ) DSStopAcquisition(-1)\n"
                "number _n = 0\n"
                "while ( DSIsAcquisitionActive() && _n < 50 )\n"
                "{\n"
                "    Sleep(0.1)\n"
                "    _n = _n + 1\n"
                "}\n"
                f"DSDeleteParameters({param_id})\n"
            )
        else:
            exec_script(f"CameraStopContinuousAcquisition({_SESSION['cam_id']})")
    except Exception:
        # A failed stop must not wedge the server; drop the session regardless.
        pass
    finally:
        _SESSION = None
