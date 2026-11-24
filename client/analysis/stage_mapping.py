"""Image pixel -> stage position, using the instrument's measured Jacobian.

The stage axes on a real column are not aligned with the scan (or camera)
axes. On the F200 in STEM, a pure +Y stage move shifts features along a ~59°
diagonal, and X and Y have different gains. The old conversion
added the pixel offset straight onto the stage position, so targets landed
0.3-2 um off.

Model: moving the stage by ``d = (dx, dy)`` shifts image features by
``J @ d``. Image axes are x to the right and y down, and both vectors are in
the same length unit. ``J`` lives in the config's ``instrument`` section under
``stage.image_mapping.<MODE>.stage_to_image`` (see
``drivers/calibration.py``). To bring the feature at image offset ``o``
(from the image centre) to the centre, its features must shift by ``-o``, so
``J @ d = -o`` and ``d = -J^-1 @ o``.

Measuring J: take an image, move the stage by a known pure-X step, image,
cross-correlate to get the feature shift; that's J's first column divided by
the step. Repeat for Y. Approach both from the same direction, or backlash
pollutes the result.
"""

from __future__ import annotations

import math

import numpy as np

IDENTITY = [[1.0, 0.0], [0.0, 1.0]]


def image_mode(tags: dict) -> str | None:
    """"STEM" or "TEM" for a stored image, from its sidecar tags."""
    mode = tags.get("imaging_mode")
    if mode in ("STEM", "TEM"):
        return mode
    if "signal_index" in tags or "dwell_us" in tags:    # take_stem_image
        return "STEM"
    if "exposure_s" in tags:                             # take_camera_image
        return "TEM"
    return None


def mapping_for(instrument: dict | None, mode: str | None) -> dict | None:
    """The calibrated mapping entry for *mode*, or None if there isn't one."""
    if not instrument or not mode:
        return None
    entry = ((instrument.get("stage") or {}).get("image_mapping") or {}).get(mode)
    if not entry or entry.get("stage_to_image") is None:
        return None
    return entry


def validate_matrix(matrix) -> np.ndarray:
    j = np.asarray(matrix, dtype=float)
    if j.shape != (2, 2) or not np.all(np.isfinite(j)):
        raise ValueError(f"stage_to_image must be a finite 2x2 matrix, got {matrix!r}")
    if abs(np.linalg.det(j)) < 1e-6:
        raise ValueError(f"stage_to_image {matrix!r} is singular")
    return j


def stage_offset_for_image_offset(offset_m: tuple[float, float], matrix) -> tuple[float, float]:
    """Stage move (m) that brings the feature at image offset *offset_m*
    (m, x right / y down, from the image centre) to the image centre."""
    j = validate_matrix(matrix)
    d = -np.linalg.solve(j, np.asarray(offset_m, dtype=float))
    return float(d[0]), float(d[1])


def rotation_mismatch_deg(entry: dict, rotation_deg: float | None) -> float:
    """How far an image's scan rotation is from the calibration's."""
    if rotation_deg is None:
        return 0.0
    cal = float(entry.get("scan_rotation_deg", 0.0) or 0.0)
    diff = (float(rotation_deg) - cal + 180.0) % 360.0 - 180.0
    return abs(diff) if math.isfinite(diff) else 0.0


def pixel_to_stage(sidecar, px: float, py: float, instrument: dict | None) -> dict:
    """Stage position (m) that centres pixel (px, py) of a stored image.

    *sidecar* is an ``ImageSidecar`` (stage position at acquisition, spatial
    calibration, tags). Falls back to the image axes as stage axes, with a
    warning, when the instrument has no mapping for the image's mode."""
    from client.analysis.units import nm_per_px

    if sidecar.stage_position is None:
        raise ValueError("Image has no associated stage position.")
    rows, cols = sidecar.shape[:2]
    cal = sidecar.calibration
    cal_x = nm_per_px(cal.x_scale, cal.units) * 1e-9   # m/px; raises if uncalibrated
    cal_y = nm_per_px(cal.y_scale, cal.units) * 1e-9
    offset = ((px - cols / 2) * cal_x, (py - rows / 2) * cal_y)   # image coords, y down
    warnings = []
    if sidecar.tags.get("stage_position_source") == "cached":
        warnings.append("the stage position stored with this image is the client's "
                        "last known position, not a fresh read at acquisition")
    mode = image_mode(sidecar.tags)
    entry = mapping_for(instrument, mode)
    if entry is not None:
        matrix = entry["stage_to_image"]
        dx, dy = stage_offset_for_image_offset(offset, matrix)
        mismatch = rotation_mismatch_deg(entry, sidecar.tags.get("rotation"))
        if mismatch > 0.5:
            warnings.append(f"image scan rotation differs from the calibration's by "
                            f"{mismatch:.1f} deg; the mapping does not account for it")
        mapping = {"mode": mode, "stage_to_image": matrix, "calibrated": True,
                   "measured": entry.get("measured")}
    else:
        # Legacy: assume stage axes == image axes (generally wrong).
        dx, dy = offset
        mapping = {"mode": mode, "stage_to_image": IDENTITY, "calibrated": False}
        warnings.append(
            f"no measured stage->image mapping for mode {mode!r} in the instrument "
            f"config (instrument.stage.image_mapping); using the image axes as "
            f"stage axes, which can be off by microns")
    stage = sidecar.stage_position
    result = {"x_m": stage.x_m + dx, "y_m": stage.y_m + dy, "z_m": stage.z_m,
              "dx_m": dx, "dy_m": dy, "mapping": mapping}
    if warnings:
        result["warnings"] = warnings
    return result
