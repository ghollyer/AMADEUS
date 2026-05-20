"""Tests for client-side FFT and segmentation analysis."""

import numpy as np
import pytest

from client.analysis.fft import (
    compute_power_spectrum,
    find_diffraction_peaks,
    extract_d_spacings,
)
from client.analysis.segmentation import segment_particles


# ---------------------------------------------------------------------------
# FFT
# ---------------------------------------------------------------------------

def test_power_spectrum_shape():
    arr = np.random.rand(128, 128).astype(np.float32)
    ps = compute_power_spectrum(arr)
    assert ps.shape == (128, 128)
    assert ps.dtype == np.float32


def test_power_spectrum_peak_detection():
    """A pure sinusoidal grating should produce two symmetric peaks."""
    size = 256
    freq = 10  # cycles across the image
    x = np.arange(size)
    arr = np.cos(2 * np.pi * freq * x / size)[np.newaxis, :] * np.ones((size, 1))
    ps = compute_power_spectrum(arr.astype(np.float32))
    peaks = find_diffraction_peaks(ps, min_distance_px=3, threshold_rel=0.1, max_peaks=5)
    # Two peaks expected (positive and negative frequency)
    assert len(peaks) >= 2
    # Peaks should be symmetric around the centre column
    cols = sorted(p["col"] for p in peaks)
    centre = size // 2
    assert abs((cols[0] - centre) + (cols[-1] - centre)) <= 2


def test_extract_d_spacings():
    peaks = [{"row": 128, "col": 138, "intensity": 1.0}]  # 10 px from centre in 256 image
    spacings = extract_d_spacings(peaks, (256, 256), calibration_nm_per_px=0.1)
    assert len(spacings) == 1
    sp = spacings[0]
    assert "d_nm" in sp
    assert "d_angstrom" in sp
    assert sp["d_angstrom"] == pytest.approx(sp["d_nm"] * 10, rel=1e-6)


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------

def test_segment_particles_finds_blobs():
    """A synthetic image with two bright blobs should yield two particles."""
    arr = np.zeros((128, 128), dtype=np.float32)
    # Place two bright circular blobs
    for cr, cc in [(32, 32), (96, 96)]:
        rr, cols = np.ogrid[:128, :128]
        mask = (rr - cr) ** 2 + (cols - cc) ** 2 <= 10 ** 2
        arr[mask] = 1.0
    label_map, metrics = segment_particles(arr, method="otsu", min_size_px=50)
    assert len(metrics) == 2
    areas = [m["area_px"] for m in metrics]
    assert all(a > 50 for a in areas)


def test_segment_particles_min_size_filter():
    """Objects smaller than min_size_px should be removed."""
    arr = np.zeros((64, 64), dtype=np.float32)
    arr[5:8, 5:8] = 1.0   # 9 px — will be removed with min_size=20
    arr[30:50, 30:50] = 1.0  # large blob
    label_map, metrics = segment_particles(arr, method="otsu", min_size_px=20)
    assert len(metrics) == 1


def test_segment_returns_correct_label_dtype():
    arr = np.random.rand(64, 64).astype(np.float32)
    label_map, _ = segment_particles(arr, method="mean")
    assert label_map.dtype == np.int32
