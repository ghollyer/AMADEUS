"""FFT analysis — compute power spectrum and extract d-spacings."""

from __future__ import annotations

import numpy as np
from scipy import ndimage


def compute_power_spectrum(array: np.ndarray) -> np.ndarray:
    """Return the log-scaled, zero-centred power spectrum of *array*."""
    f = np.fft.fftshift(np.fft.fft2(array.astype(np.float64)))
    power = np.abs(f) ** 2
    return np.log1p(power).astype(np.float32)


def find_diffraction_peaks(
    power_spectrum: np.ndarray,
    min_distance_px: int = 5,
    threshold_rel: float = 0.3,
    max_peaks: int = 20,
) -> list[dict]:
    """Find local maxima in *power_spectrum*, excluding the central beam.

    Parameters
    ----------
    power_spectrum : ndarray
        Output of :func:`compute_power_spectrum`.
    min_distance_px : int
        Minimum separation between peaks in pixels.
    threshold_rel : float
        Peaks below ``threshold_rel * max_value`` are ignored.
    max_peaks : int
        Return at most this many peaks (strongest first).

    Returns
    -------
    list of {"row": int, "col": int, "intensity": float}
    """
    ps = power_spectrum.copy()
    rows, cols = ps.shape
    cr, cc = rows // 2, cols // 2

    # Mask central beam (DC component)
    mask_r = min_distance_px * 2
    ps[cr - mask_r: cr + mask_r + 1, cc - mask_r: cc + mask_r + 1] = 0

    threshold = threshold_rel * ps.max()
    peaks = []

    while True:
        idx = np.unravel_index(np.argmax(ps), ps.shape)
        val = ps[idx]
        if val < threshold or len(peaks) >= max_peaks:
            break
        peaks.append({"row": int(idx[0]), "col": int(idx[1]), "intensity": float(val)})
        # Suppress neighbourhood
        r0 = max(0, idx[0] - min_distance_px)
        r1 = min(rows, idx[0] + min_distance_px + 1)
        c0 = max(0, idx[1] - min_distance_px)
        c1 = min(cols, idx[1] + min_distance_px + 1)
        ps[r0:r1, c0:c1] = 0

    return peaks


def extract_d_spacings(
    peaks: list[dict],
    image_shape: tuple[int, int],
    calibration_nm_per_px: float,
) -> list[dict]:
    """Convert peak pixel coordinates to d-spacings in nm.

    Parameters
    ----------
    peaks : list of {"row", "col", "intensity"}
    image_shape : (rows, cols)
    calibration_nm_per_px : float
        Real-space calibration (nm per pixel).

    Returns
    -------
    list of {"row", "col", "intensity", "q_inv_nm", "d_nm", "d_angstrom"}
    """
    rows, cols = image_shape
    cr, cc = rows // 2, cols // 2
    # Reciprocal-space calibration: 1/(N * calibration_nm_per_px)
    q_scale_x = 1.0 / (cols * calibration_nm_per_px)  # 1/nm per pixel
    q_scale_y = 1.0 / (rows * calibration_nm_per_px)

    results = []
    for p in peaks:
        drow = p["row"] - cr
        dcol = p["col"] - cc
        q = np.hypot(drow * q_scale_y, dcol * q_scale_x)
        if q == 0:
            continue
        d_nm = 1.0 / q
        results.append({
            **p,
            "q_inv_nm": float(q),
            "d_nm": float(d_nm),
            "d_angstrom": float(d_nm * 10),
        })
    return results
