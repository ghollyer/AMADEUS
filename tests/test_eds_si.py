"""Tests for client/analysis/eds_si.py — energy axis, elemental maps, quant."""

import numpy as np
import pytest

from client.analysis.eds_si import (
    EDS_LINES,
    K_FACTORS_VS_SI,
    cliff_lorimer_quantify,
    energy_axis_values,
    extract_elemental_map,
    lookup_line_keV,
)


# ---------------------------------------------------------------------------
# Energy axis
# ---------------------------------------------------------------------------

def test_energy_axis_keV_passthrough():
    e = energy_axis_values(n_channels=2048, origin=0.0, scale=0.010, units="keV")
    assert e.shape == (2048,)
    assert e[0] == pytest.approx(0.0)
    assert e[1] == pytest.approx(0.010)
    assert e[-1] == pytest.approx(0.010 * 2047)


def test_energy_axis_eV_converted_to_keV():
    # 10 eV per channel → 0.010 keV per channel
    e = energy_axis_values(n_channels=100, origin=0.0, scale=10.0, units="eV")
    assert e[1] == pytest.approx(0.010)
    assert e[-1] == pytest.approx(0.010 * 99)


def test_energy_axis_origin_shifts_zero():
    # origin=50 means channel 50 corresponds to 0 keV
    e = energy_axis_values(n_channels=200, origin=50.0, scale=0.010, units="keV")
    assert e[50] == pytest.approx(0.0)
    assert e[0] == pytest.approx(-0.5)


def test_energy_axis_rejects_unknown_units():
    with pytest.raises(ValueError, match="eV or keV"):
        energy_axis_values(n_channels=10, origin=0.0, scale=1.0, units="counts")


# ---------------------------------------------------------------------------
# Line lookup
# ---------------------------------------------------------------------------

def test_lookup_line_keV_defaults_to_Ka():
    assert lookup_line_keV("Fe") == pytest.approx(EDS_LINES["Fe"]["Ka"])


def test_lookup_line_keV_explicit_line():
    assert lookup_line_keV("Cu", "La") == pytest.approx(EDS_LINES["Cu"]["La"])


def test_lookup_line_keV_raises_for_missing():
    with pytest.raises(KeyError):
        lookup_line_keV("Xx", "Ka")
    with pytest.raises(KeyError):
        lookup_line_keV("C", "La")  # carbon has no L line in our table


# ---------------------------------------------------------------------------
# Elemental map extraction
# ---------------------------------------------------------------------------

def _make_synthetic_si(
    spatial_shape: tuple[int, int],
    energies_keV: np.ndarray,
    background_per_channel: float,
    peaks: list[tuple[float, float, np.ndarray]],
    energy_axis: int = 2,
) -> np.ndarray:
    """Synthesise an SI cube with constant background + Gaussian peaks.

    Each *peak* is ``(center_keV, sigma_keV, intensity_map_2d)``. The
    intensity_map gives the per-pixel peak amplitude (counts at line
    centre per channel — so total integrated counts depend on sigma).
    """
    ny, nx = spatial_shape
    nc = energies_keV.shape[0]
    cube = np.full((ny, nx, nc), background_per_channel, dtype=np.float64)
    for center, sigma, intensity in peaks:
        prof = np.exp(-0.5 * ((energies_keV - center) / sigma) ** 2)
        cube += intensity[..., np.newaxis] * prof[np.newaxis, np.newaxis, :]
    return np.moveaxis(cube, -1, energy_axis)


def test_extract_map_recovers_constant_intensity():
    energies = energy_axis_values(n_channels=1024, origin=0.0, scale=0.010, units="keV")
    intensity = np.full((8, 8), 100.0)  # uniform field
    cube = _make_synthetic_si(
        spatial_shape=(8, 8),
        energies_keV=energies,
        background_per_channel=5.0,
        peaks=[(6.400, 0.065, intensity)],  # Fe Kα
        energy_axis=2,
    )

    result = extract_elemental_map(
        cube,
        energy_axis=2,
        energies_keV=energies,
        line_keV=6.398,
        window_keV=0.150,
        bg_offset_keV=0.400,
        bg_width_keV=0.100,
        element="Fe",
        line="Ka",
    )
    assert result.map.shape == (8, 8)
    # Net map should be ~uniform — std/mean < 5% on this clean synthetic
    rel_std = result.map.std() / result.map.mean()
    assert rel_std < 0.05
    assert result.total_counts > 0


def test_extract_map_handles_energy_axis_0():
    energies = energy_axis_values(n_channels=512, origin=0.0, scale=0.020, units="keV")
    intensity = np.full((4, 4), 50.0)
    cube = _make_synthetic_si(
        spatial_shape=(4, 4),
        energies_keV=energies,
        background_per_channel=2.0,
        peaks=[(1.740, 0.060, intensity)],  # Si Kα
        energy_axis=0,
    )
    assert cube.shape == (512, 4, 4)
    result = extract_elemental_map(
        cube,
        energy_axis=0,
        energies_keV=energies,
        line_keV=1.740,
        window_keV=0.150,
    )
    assert result.map.shape == (4, 4)
    assert result.total_counts > 0


def test_extract_map_spatially_resolves_two_phases():
    """A cube where half the field has Fe, the other half doesn't, should
    produce a map that clearly distinguishes the two halves."""
    energies = energy_axis_values(n_channels=1024, origin=0.0, scale=0.010, units="keV")
    intensity = np.zeros((16, 16))
    intensity[:, :8] = 200.0   # Fe-rich half
    intensity[:, 8:] = 0.0     # Fe-free half
    cube = _make_synthetic_si(
        spatial_shape=(16, 16),
        energies_keV=energies,
        background_per_channel=3.0,
        peaks=[(6.400, 0.065, intensity)],
        energy_axis=2,
    )
    result = extract_elemental_map(
        cube,
        energy_axis=2,
        energies_keV=energies,
        line_keV=6.398,
        window_keV=0.150,
    )
    fe_side = result.map[:, :8].mean()
    no_fe = result.map[:, 8:].mean()
    assert fe_side > 10 * max(no_fe, 1.0)


def test_extract_map_validates_cube_dimensionality():
    energies = energy_axis_values(n_channels=10, origin=0.0, scale=0.010, units="keV")
    cube_2d = np.zeros((8, 10))
    with pytest.raises(ValueError, match="must be 3-D"):
        extract_elemental_map(cube_2d, 1, energies, 0.05)


def test_extract_map_validates_axis_length():
    energies = energy_axis_values(n_channels=100, origin=0.0, scale=0.010, units="keV")
    cube = np.zeros((8, 8, 50))  # 50 channels, but energies has 100
    with pytest.raises(ValueError, match="energy axis length"):
        extract_elemental_map(cube, 2, energies, 0.4)


def test_extract_map_window_outside_axis_raises():
    energies = energy_axis_values(n_channels=100, origin=0.0, scale=0.010, units="keV")
    cube = np.zeros((4, 4, 100))
    with pytest.raises(ValueError, match="does not overlap"):
        extract_elemental_map(cube, 2, energies, line_keV=50.0)


# ---------------------------------------------------------------------------
# Cliff-Lorimer quantification
# ---------------------------------------------------------------------------

def test_quant_empty_returns_empty():
    assert cliff_lorimer_quantify({}) == {}


def test_quant_single_element_is_unity():
    assert cliff_lorimer_quantify({"Fe": 1000.0}) == {"Fe": 1.0}


def test_quant_sums_to_one():
    fr = cliff_lorimer_quantify({"Fe": 500.0, "O": 1500.0, "Si": 250.0})
    assert sum(fr.values()) == pytest.approx(1.0)
    # All fractions in [0, 1]
    assert all(0.0 <= v <= 1.0 for v in fr.values())


def test_quant_uses_k_factor_table():
    # Equal intensities, k_Fe > k_Si, so Fe fraction should be > Si.
    fr = cliff_lorimer_quantify({"Fe": 100.0, "Si": 100.0})
    assert fr["Fe"] > fr["Si"]
    # Sanity: ratio matches k ratio for equal intensities
    expected = K_FACTORS_VS_SI["Fe"] / (K_FACTORS_VS_SI["Fe"] + K_FACTORS_VS_SI["Si"])
    assert fr["Fe"] == pytest.approx(expected)


def test_quant_negative_intensities_floor_to_zero():
    fr = cliff_lorimer_quantify({"Fe": 1000.0, "O": -50.0})
    assert fr["O"] == pytest.approx(0.0)
    assert fr["Fe"] == pytest.approx(1.0)


def test_quant_zero_total_returns_zeros():
    fr = cliff_lorimer_quantify({"Fe": 0.0, "Si": 0.0})
    assert fr == {"Fe": 0.0, "Si": 0.0}


def test_quant_custom_k_factor_override():
    # Force Fe k=1 and Si k=2 so Si dominates despite equal intensities
    fr = cliff_lorimer_quantify(
        {"Fe": 100.0, "Si": 100.0},
        k_factors={"Fe": 1.0, "Si": 2.0},
    )
    assert fr["Si"] == pytest.approx(2.0 / 3.0)
    assert fr["Fe"] == pytest.approx(1.0 / 3.0)
