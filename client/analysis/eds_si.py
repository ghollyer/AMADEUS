"""EDS spectrum-image analysis — elemental maps + Cliff-Lorimer quant.

Operates on 3-D SI cubes (numpy arrays where one axis is calibrated in
eV or keV). The energy axis is identified explicitly by the caller from
the per-axis calibration captured at fetch time (DM files) or at
acquisition time (eds_acquire_map → SI cube).

Routines
--------
energy_axis_values(n_channels, origin, scale, units)
    Return the energy of every channel in keV, given the axis origin
    and per-channel dispersion read from the DM calibration.

extract_elemental_map(cube, energy_axis, energies_keV, line_keV, ...)
    Sum counts inside a window around the line, subtract a linear
    background fit to two side-bands, and return a 2-D map in counts.

cliff_lorimer_quantify(intensities, k_factors)
    Closed Cliff-Lorimer normalisation across a set of elements,
    returning weight fractions that sum to 1.

EDS_LINES
    Built-in table of major K-α / L-α / M-α x-ray line energies (keV)
    for elements commonly seen in TEM EDS work.  The table is not
    exhaustive — callers can pass explicit ``line_keV`` to the map
    extractor for elements not in the table.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


# ---------------------------------------------------------------------------
# Element line database (keV).
# Values are characteristic line energies from standard x-ray reference
# tables. Where two close lines (e.g. Kα1/Kα2) are unresolved at typical
# EDS resolution (~130 eV), the weighted average is used.
# ---------------------------------------------------------------------------

EDS_LINES: dict[str, dict[str, float]] = {
    # Light elements (K series only)
    "B":  {"Ka": 0.183},
    "C":  {"Ka": 0.277},
    "N":  {"Ka": 0.392},
    "O":  {"Ka": 0.525},
    "F":  {"Ka": 0.677},
    "Na": {"Ka": 1.041},
    "Mg": {"Ka": 1.254},
    "Al": {"Ka": 1.486},
    "Si": {"Ka": 1.740},
    "P":  {"Ka": 2.013},
    "S":  {"Ka": 2.307},
    "Cl": {"Ka": 2.622},
    "K":  {"Ka": 3.314},
    "Ca": {"Ka": 3.690},
    # 3d transition metals (K and L)
    "Ti": {"Ka": 4.508,  "La": 0.452},
    "V":  {"Ka": 4.949,  "La": 0.510},
    "Cr": {"Ka": 5.411,  "La": 0.571},
    "Mn": {"Ka": 5.894,  "La": 0.636},
    "Fe": {"Ka": 6.398,  "La": 0.704},
    "Co": {"Ka": 6.924,  "La": 0.775},
    "Ni": {"Ka": 7.471,  "La": 0.849},
    "Cu": {"Ka": 8.040,  "La": 0.928},
    "Zn": {"Ka": 8.630,  "La": 1.012},
    "Ga": {"Ka": 9.241,  "La": 1.098},
    "Ge": {"Ka": 9.874,  "La": 1.188},
    "As": {"Ka": 10.530, "La": 1.282},
    "Se": {"Ka": 11.207, "La": 1.379},
    "Br": {"Ka": 11.907, "La": 1.480},
    # 4d transition metals (L lines used in practice)
    "Y":  {"La": 1.922},
    "Zr": {"La": 2.042},
    "Nb": {"La": 2.166},
    "Mo": {"La": 2.293, "Ka": 17.443},
    "Ru": {"La": 2.558},
    "Rh": {"La": 2.696},
    "Pd": {"La": 2.838},
    "Ag": {"La": 2.984, "Ka": 22.103},
    "Cd": {"La": 3.133},
    "In": {"La": 3.286},
    "Sn": {"La": 3.443, "Ka": 25.270},
    "Sb": {"La": 3.604},
    "Te": {"La": 3.769},
    "I":  {"La": 3.937},
    # Heavy elements (M and L)
    "Ba": {"La": 4.466, "Ma": 0.972},
    "La": {"La": 4.650, "Ma": 0.833},
    "Ce": {"La": 4.839, "Ma": 0.883},
    "Nd": {"La": 5.229, "Ma": 0.978},
    "Sm": {"La": 5.633, "Ma": 1.080},
    "Gd": {"La": 6.056, "Ma": 1.185},
    "Hf": {"La": 7.898, "Ma": 1.644},
    "Ta": {"La": 8.145, "Ma": 1.709},
    "W":  {"La": 8.396, "Ma": 1.774},
    "Pt": {"La": 9.441, "Ma": 2.048},
    "Au": {"La": 9.711, "Ma": 2.122},
    "Pb": {"La": 10.549, "Ma": 2.342},
    "U":  {"La": 13.612, "Ma": 3.165},
}


# ---------------------------------------------------------------------------
# Default Cliff-Lorimer k-factors (relative to Si Kα). Approximate values
# from standard EDS tables — adequate for first-pass analysis on a TEM
# without standards but should be replaced with locally calibrated values
# for publication-grade quant.
# ---------------------------------------------------------------------------

K_FACTORS_VS_SI: dict[str, float] = {
    "C":  3.4,  "N":  2.5,  "O":  1.9,  "F":  1.5,
    "Na": 1.20, "Mg": 1.05, "Al": 1.00, "Si": 1.00,
    "P":  1.02, "S":  1.05, "Cl": 1.10, "K":  1.10, "Ca": 1.12,
    "Ti": 1.10, "V":  1.12, "Cr": 1.15, "Mn": 1.18, "Fe": 1.22,
    "Co": 1.26, "Ni": 1.30, "Cu": 1.35, "Zn": 1.42,
    "Ga": 1.50, "Ge": 1.58, "As": 1.66, "Se": 1.75, "Br": 1.85,
    "Mo": 2.10, "Ag": 2.35, "Sn": 2.60, "W":  3.50,
    "Pt": 4.10, "Au": 4.30, "Pb": 4.50, "U":  6.20,
}


# ---------------------------------------------------------------------------
# Axis helpers
# ---------------------------------------------------------------------------

def energy_axis_values(
    n_channels: int,
    origin: float,
    scale: float,
    units: str,
) -> np.ndarray:
    """Convert a per-axis origin/scale/units triple to a keV array.

    DM stores the calibration as ``value = (i - origin) * scale`` in the
    declared units. We normalise everything to keV here so downstream
    code can pick a single energy unit.

    Parameters
    ----------
    n_channels : int
    origin : float
        Channel index that corresponds to the calibrated zero.
    scale : float
        Per-channel dispersion in the declared *units*.
    units : str
        Either 'eV' or 'keV' (case-insensitive).
    """
    u = units.strip().lower()
    if u not in ("ev", "kev"):
        raise ValueError(f"energy axis units must be eV or keV, got {units!r}")
    idx = np.arange(n_channels, dtype=np.float64)
    raw = (idx - origin) * scale
    return raw / 1000.0 if u == "ev" else raw


def channel_range_for_window(
    energies_keV: np.ndarray, center_keV: float, half_width_keV: float
) -> tuple[int, int]:
    """Return inclusive channel index range covering [center±half_width]."""
    lo = center_keV - half_width_keV
    hi = center_keV + half_width_keV
    mask = (energies_keV >= lo) & (energies_keV <= hi)
    if not mask.any():
        raise ValueError(
            f"window [{lo:.3f}, {hi:.3f}] keV does not overlap the energy axis "
            f"[{energies_keV.min():.3f}, {energies_keV.max():.3f}] keV"
        )
    idx = np.where(mask)[0]
    return int(idx[0]), int(idx[-1])


# ---------------------------------------------------------------------------
# Elemental maps
# ---------------------------------------------------------------------------

@dataclass
class ElementalMapResult:
    element: str
    line: str
    line_keV: float
    window_keV: float
    map: np.ndarray            # 2-D, same in-plane shape as the cube
    total_counts: float
    background_counts: float


def extract_elemental_map(
    cube: np.ndarray,
    energy_axis: int,
    energies_keV: np.ndarray,
    line_keV: float,
    window_keV: float = 0.150,
    bg_offset_keV: float = 0.300,
    bg_width_keV: float = 0.150,
    *,
    element: str = "",
    line: str = "",
) -> ElementalMapResult:
    """Background-subtracted line integration for a single element.

    For every pixel, sum counts inside ``[line_keV ± window_keV]``,
    estimate a linear background from two side-bands at
    ``± bg_offset_keV`` (each ``bg_width_keV`` wide), and subtract the
    background under the peak.

    Parameters
    ----------
    cube : ndarray
        3-D SI data (with the energy axis indicated by *energy_axis*).
    energy_axis : int
        Index of the energy axis in *cube* (0, 1, or 2).
    energies_keV : ndarray
        1-D energy array, length equal to ``cube.shape[energy_axis]``.
    line_keV : float
        Centre of the line of interest.
    window_keV : float
        Half-width of the integration window.
    bg_offset_keV : float
        Distance from the line centre to the centre of each side-band.
    bg_width_keV : float
        Half-width of each side-band.
    element, line : str
        Bookkeeping labels written into the returned result.

    Returns
    -------
    ElementalMapResult
        Includes the 2-D background-subtracted counts map and total/
        background counts summed over the whole field.
    """
    if cube.ndim != 3:
        raise ValueError(f"cube must be 3-D, got shape {cube.shape}")
    if energy_axis not in (0, 1, 2):
        raise ValueError(f"energy_axis must be 0, 1 or 2, got {energy_axis}")
    if energies_keV.shape[0] != cube.shape[energy_axis]:
        raise ValueError(
            f"energy axis length mismatch: cube has {cube.shape[energy_axis]} "
            f"channels but energies_keV has {energies_keV.shape[0]}"
        )

    # Move the energy axis to the last position for simple slicing.
    work = np.moveaxis(cube, energy_axis, -1).astype(np.float64, copy=False)

    peak_lo, peak_hi = channel_range_for_window(energies_keV, line_keV, window_keV)
    n_peak = peak_hi - peak_lo + 1
    peak = work[..., peak_lo:peak_hi + 1].sum(axis=-1)

    # Background side-bands. If either side-band falls off the axis we
    # gracefully fall back to whichever side is available.
    bg_left = bg_right = None
    try:
        ll, lh = channel_range_for_window(energies_keV, line_keV - bg_offset_keV, bg_width_keV)
        bg_left = work[..., ll:lh + 1].mean(axis=-1)
    except ValueError:
        pass
    try:
        rl, rh = channel_range_for_window(energies_keV, line_keV + bg_offset_keV, bg_width_keV)
        bg_right = work[..., rl:rh + 1].mean(axis=-1)
    except ValueError:
        pass

    if bg_left is not None and bg_right is not None:
        bg_per_channel = 0.5 * (bg_left + bg_right)
    elif bg_left is not None:
        bg_per_channel = bg_left
    elif bg_right is not None:
        bg_per_channel = bg_right
    else:
        bg_per_channel = np.zeros_like(peak)

    bg_under_peak = bg_per_channel * n_peak
    net = peak - bg_under_peak
    net[net < 0] = 0.0

    return ElementalMapResult(
        element=element,
        line=line,
        line_keV=float(line_keV),
        window_keV=float(window_keV),
        map=net.astype(np.float32),
        total_counts=float(net.sum()),
        background_counts=float(bg_under_peak.sum()),
    )


def lookup_line_keV(element: str, line: str = "Ka") -> float:
    """Return the tabulated line energy for *element*/*line* (keV)."""
    try:
        return EDS_LINES[element][line]
    except KeyError as exc:
        raise KeyError(
            f"No tabulated {line} line for element {element!r}. "
            f"Pass an explicit line_keV instead."
        ) from exc


# ---------------------------------------------------------------------------
# Quantification
# ---------------------------------------------------------------------------

def cliff_lorimer_quantify(
    intensities: dict[str, float],
    k_factors: dict[str, float] | None = None,
) -> dict[str, float]:
    """Closed Cliff-Lorimer quant for a set of elements.

    Given net x-ray intensities ``I_i`` and k-factors ``k_i`` (relative
    to a common reference — Si Kα here), weight fractions follow

        C_i = (k_i * I_i) / Σ_j (k_j * I_j)

    so the result is intrinsically normalised to 1 across the supplied
    elements. This assumes all major elements present in the field are
    included — adding/removing an element re-normalises the rest.

    Parameters
    ----------
    intensities : dict[str, float]
        Net integrated counts per element.
    k_factors : dict[str, float], optional
        k-factor per element. Missing keys fall back to
        :data:`K_FACTORS_VS_SI` and ultimately 1.0.

    Returns
    -------
    dict[str, float]
        Weight fractions (0..1), summing to 1.0.
    """
    if not intensities:
        return {}

    k_factors = k_factors or {}
    products: dict[str, float] = {}
    for element, intensity in intensities.items():
        if intensity < 0:
            intensity = 0.0
        k = k_factors.get(element)
        if k is None:
            k = K_FACTORS_VS_SI.get(element, 1.0)
        products[element] = k * float(intensity)

    total = sum(products.values())
    if total <= 0:
        return {e: 0.0 for e in intensities}
    return {e: v / total for e, v in products.items()}
