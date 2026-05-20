"""DM helpers for EDS acquisition.

By default, EDS is treated as ``Signal 1`` in the SI framework, so both point
and map acquisitions are driven through the generic spectroscopy helpers:

  - point spectrum → ``Multi-Point`` with a single point
  - EDS map        → ``2D Array``

The SI helpers handle the survey-image grab + mode setup + field/signal
configuration (see ``spectroscopy.py``).
"""

from dm_server.helpers import spectroscopy as _si


def eds_acquire_point(params: dict) -> dict:
    """Acquire a single-point EDS spectrum at the current beam position.

    Parameters
    ----------
    params["exposure_s"] : float, default 30.0
        Per-pixel live-time (mapped to the "Pixel Time" SI field).
    params["signal"] : int, default 1
        Signal channel index. Defaults to 1.
    """
    exposure_s = float(params.get("exposure_s", 30.0))
    signal     = int(params.get("signal", 1))

    _si.si_configure({
        "mode":         "Multi-Point",
        "pixel_time_s": exposure_s,
        "signal":       signal,
        "points":       1,
    })
    start = _si.si_start({})
    if not start.get("started"):
        return {
            "acquired":       False,
            "mode":           "Multi-Point",
            "exposure_s":     exposure_s,
            "warning":        start.get("warning"),
            "valves_open":    start.get("valves_open"),
            "operation_mode": start.get("operation_mode"),
        }
    return {"acquired": True, "mode": "Multi-Point", "exposure_s": exposure_s}


def eds_acquire_map(params: dict) -> dict:
    """Acquire an EDS map (2D-Array spectrum image) over the survey ROI.

    Parameters
    ----------
    params["x_pixels"] : int, default 64
    params["y_pixels"] : int, default 64
    params["exposure_s"] : float, default 0.05
        Per-pixel live-time (mapped to the "Pixel Time" SI field).
    params["signal"] : int, default 1
    """
    x_px  = int(params.get("x_pixels", 64))
    y_px  = int(params.get("y_pixels", 64))
    exp_s = float(params.get("exposure_s", 0.05))
    signal = int(params.get("signal", 1))

    _si.si_configure({
        "mode":         "2D Array",
        "pixel_time_s": exp_s,
        "signal":       signal,
        "x_samples":    x_px,
        "y_samples":    y_px,
    })
    start = _si.si_start({})
    if not start.get("started"):
        return {
            "acquired":       False,
            "mode":           "2D Array",
            "x_pixels":       x_px,
            "y_pixels":       y_px,
            "exposure_s":     exp_s,
            "warning":        start.get("warning"),
            "valves_open":    start.get("valves_open"),
            "operation_mode": start.get("operation_mode"),
        }
    return {
        "acquired":   True,
        "mode":       "2D Array",
        "x_pixels":   x_px,
        "y_pixels":   y_px,
        "exposure_s": exp_s,
    }
