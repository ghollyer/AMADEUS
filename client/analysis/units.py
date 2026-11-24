"""Spatial calibration → nanometres, honouring the units DM reported.

Image sidecars keep the calibration exactly as DM gave it (units policy:
never invent units). DM calibrates the F200 OneView in µm, STEM images may
be in nm, and uncalibrated images have units "" — so anything that needs a
physical length must convert explicitly, and must refuse an uncalibrated
image rather than treat pixels as nanometres.
"""

from __future__ import annotations

_NM_PER_UNIT = {
    "pm": 1e-3,
    "å": 0.1, "a": 0.1, "angstrom": 0.1, "ångström": 0.1,
    "nm": 1.0,
    "µm": 1e3,          # U+00B5 micro sign (what DM uses)
    "μm": 1e3,          # U+03BC Greek mu
    "um": 1e3, "micron": 1e3,
    "mm": 1e6,
    "m": 1e9,
    # Sidecars written before the 2026-11-07 bytes fix store the
    # repr of DM's cp1252 bytes for "µm".
    "b'\\xb5m'": 1e3,
}

_UNCALIBRATED = {"", "px", "pixel", "pixels"}


class UncalibratedImageError(ValueError):
    """The image has no physical spatial calibration."""


def nm_per_px(scale: float, units: str) -> float:
    """Return the pixel size in nm for a calibration ``(scale, units)``.

    Raises :class:`UncalibratedImageError` for pixel / empty units and
    ``ValueError`` for units that aren't real-space lengths (e.g. ``1/nm``).
    """
    key = (units or "").strip()
    if key not in _NM_PER_UNIT:
        key = key.lower()
    if key in _UNCALIBRATED:
        raise UncalibratedImageError(
            "image is not spatially calibrated (units "
            f"{units!r}); a physical length can't be derived from it")
    if key not in _NM_PER_UNIT:
        raise ValueError(f"unsupported spatial unit {units!r} (expected a length "
                         "such as nm, µm, Å)")
    if not scale or scale <= 0:
        raise UncalibratedImageError(f"image calibration scale is {scale!r}")
    return float(scale) * _NM_PER_UNIT[key]
