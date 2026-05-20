"""DM helpers for file listing and loading from the microscope computer.

These helpers expose the file system on the microscope computer to the
control side, so previously-saved DM images (.dm3, .dm4) — including EDS
spectrum-image cubes — can be browsed and pulled across the ZMQ bridge.

DM exposes file opening directly in the Python API:

  DM.OpenImage(filename) -> image       (raises on failure)
  image.GetNumArray()                    -> numpy.ndarray (full N-D data)
  image.GetNumDimensions()
  image.GetDimensionCalibration(d, fmt)  -> (origin, scale, units)  fmt=1
  image.GetIntensityScale() / GetIntensityOrigin()
  image.GetName()

For a 2D image the array is 2D; for an SI cube the array is 3D and one of
the axes is calibrated in energy (eV / keV).

The directory listing intentionally uses ``os.listdir`` because the microscope
computer's file system is available to the Python interpreter.
"""

from __future__ import annotations

import os
from pathlib import PureWindowsPath

import numpy as np
import DigitalMicrograph as DM


_DEFAULT_PATTERNS = ("*.dm3", "*.dm4")


def _match_any(name: str, patterns: tuple[str, ...]) -> bool:
    """Case-insensitive glob-style match against a tuple of patterns."""
    import fnmatch
    lower = name.lower()
    return any(fnmatch.fnmatch(lower, p.lower()) for p in patterns)


def list_files(params: dict) -> dict:
    """List files in *directory* on the microscope computer.

    Parameters
    ----------
    params["directory"] : str
        Absolute directory path on the DM machine.
    params["patterns"] : list[str], optional
        Glob patterns to match (case-insensitive). Defaults to
        ["*.dm3", "*.dm4"].
    params["recursive"] : bool, default False
        Walk subdirectories.

    Returns
    -------
    {
        "directory": str,
        "files": [
            {"name": str, "path": str, "size_bytes": int, "mtime_iso": str},
            ...
        ]
    }
    """
    directory = params.get("directory")
    if not directory:
        raise ValueError("'directory' parameter is required")
    patterns = tuple(params.get("patterns") or _DEFAULT_PATTERNS)
    recursive = bool(params.get("recursive", False))

    if not os.path.isdir(directory):
        raise FileNotFoundError(f"Directory not found: {directory}")

    entries: list[dict] = []
    if recursive:
        walker = (
            (root, fn)
            for root, _dirs, files in os.walk(directory)
            for fn in files
        )
    else:
        walker = (
            (directory, fn)
            for fn in os.listdir(directory)
            if os.path.isfile(os.path.join(directory, fn))
        )

    from datetime import datetime
    for root, fn in walker:
        if not _match_any(fn, patterns):
            continue
        full = os.path.join(root, fn)
        try:
            st = os.stat(full)
        except OSError:
            continue
        entries.append({
            "name": fn,
            "path": full,
            "size_bytes": int(st.st_size),
            "mtime_iso": datetime.utcfromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        })

    entries.sort(key=lambda e: e["mtime_iso"], reverse=True)
    return {"directory": directory, "files": entries, "count": len(entries)}


def _collect_dim_calibrations(img) -> list[dict]:
    """Read calibration for every dimension of *img*."""
    n = int(img.GetNumDimensions())
    dims = []
    for i in range(n):
        origin, scale, units = img.GetDimensionCalibration(i, 1)
        dims.append({
            "index": i,
            "size": int(img.GetDimensionSize(i)),
            "origin": float(origin),
            "scale": float(scale),
            "units": str(units),
        })
    return dims


def _energy_axis_index(dims: list[dict]) -> int | None:
    """Return the index of the energy-calibrated dimension, or None."""
    for d in dims:
        u = d["units"].strip().lower()
        if u in ("ev", "kev"):
            return d["index"]
    return None


def load_dm_file(params: dict):
    """Open a .dm3/.dm4 file and return its array + metadata.

    Parameters
    ----------
    params["path"] : str
        Absolute file path on the DM machine.
    params["close_after"] : bool, default True
        Close the image document after extracting data so DM does not
        accumulate open windows from every fetch.

    Returns
    -------
    (dict, numpy.ndarray)
        Standard image protocol — the metadata dict includes
        ``image_meta`` with per-axis calibrations and intensity scale,
        plus a top-level ``axes`` field describing every dimension and
        the detected energy axis (if any).
    """
    path = params.get("path")
    if not path:
        raise ValueError("'path' parameter is required")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"File not found: {path}")
    close_after = bool(params.get("close_after", True))

    img = DM.OpenImage(path)
    try:
        name = str(img.GetName() or PureWindowsPath(path).name)
        arr = np.array(img.GetNumArray())
        dims = _collect_dim_calibrations(img)
        intensity_scale = float(img.GetIntensityScale())
        intensity_origin = float(img.GetIntensityOrigin())
        intensity_units = str(img.GetIntensityUnitString())
    finally:
        if close_after:
            try:
                doc = img.GetOrCreateImageDocument()
                doc.Close()
            except Exception:
                pass

    energy_axis = _energy_axis_index(dims)

    # Pick a "primary" x/y calibration for the ImageSidecar model on the
    # client side — first two spatial dims, or first two dims if there is
    # no energy axis.
    spatial = [d for d in dims if d["index"] != energy_axis]
    cal = {"x_scale": 1.0, "y_scale": 1.0, "units": "px"}
    if len(spatial) >= 1:
        cal["x_scale"] = spatial[0]["scale"]
        cal["units"] = spatial[0]["units"] or "px"
    if len(spatial) >= 2:
        cal["y_scale"] = spatial[1]["scale"]

    meta = {
        "source_path": path,
        "name": name,
        "ndim": len(dims),
        "axes": dims,
        "energy_axis_index": energy_axis,
        "intensity": {
            "scale":  intensity_scale,
            "origin": intensity_origin,
            "units":  intensity_units,
        },
        "image_meta": {
            "name": name,
            "calibration": cal,
        },
    }
    return meta, np.ascontiguousarray(arr)
