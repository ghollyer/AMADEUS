"""Particle morphology, focus/registration helpers, PNG export and EDS
region analysis: the tools that grew out of the 2026-11-12 F200 nanorod
survey. Synthetic data with known answers; no instrument."""

import zlib
from pathlib import Path

import numpy as np
import pytest
from scipy import ndimage as ndi

from client.analysis import eds_regions as R
from client.analysis import imaging as I
from client.analysis import particles as P
from client.state.manager import StateManager
from client.state.models import ImageCalibration, StagePosition


def _rod_image(n=512, rods=((150, 150, 70, 20, 0), (350, 300, 70, 20, 90),
                            (380, 120, 60, 18, 30)), touching=True, nm_px=0.5,
               shell_nm=0.0, shell_level=0.04, noise=0.01, seed=0):
    """DF-like image of filled rods (centre x, y, length nm, width nm, angle)."""
    yy, xx = np.mgrid[0:n, 0:n].astype(float)
    img = np.zeros((n, n))
    shell = np.zeros((n, n), bool)
    items = list(rods)
    if touching:
        items += [(250, 420, 70, 20, 0), (250, 420, 70, 20, 90)]   # a cross
    for cx, cy, length, width, ang in items:
        t = np.deg2rad(ang)
        u = (xx - cx) * np.cos(t) + (yy - cy) * np.sin(t)
        v = -(xx - cx) * np.sin(t) + (yy - cy) * np.cos(t)
        rod = (np.abs(u) <= length / 2 / nm_px) & (np.abs(v) <= width / 2 / nm_px)
        img[rod] = 1.0
        if shell_nm:
            shell |= (np.abs(u) <= (length / 2 + shell_nm) / nm_px) & \
                     (np.abs(v) <= (width / 2 + shell_nm) / nm_px)
    img[shell & (img == 0)] = shell_level
    rng = np.random.default_rng(seed)
    return ndi.gaussian_filter(img, 1.0) + noise * rng.standard_normal(img.shape)


# ---------------------------------------------------------------------------
# particles
# ---------------------------------------------------------------------------

def test_measure_rods_recovers_isolated_rod_size_and_rejects_the_cross():
    out = P.measure_rods(_rod_image(), nm_per_px=0.5)
    iso = out["isolated"]
    assert iso["n"] == 3                                   # the cross is excluded
    assert iso["length_nm"]["median"] == pytest.approx(70, abs=4)
    assert iso["width_nm"]["median"] == pytest.approx(20, abs=3)
    assert iso["aspect_ratio"]["median"] == pytest.approx(3.5, abs=0.5)
    assert out["n_objects"] == 4


def _dots(img, cx, cy, n, spread, rng):
    for _ in range(n):
        x, y = int(cx + rng.integers(-spread, spread)), int(cy + rng.integers(-spread, spread))
        img[y:y + 2, x:x + 4] = 1.0


def test_find_particle_groups_ignores_grid_bar_and_orders_by_size():
    """A survey at 10 nm/px: two clusters of 2-4 px rods, and a 1.5 um
    grid bar along the right edge."""
    rng = np.random.default_rng(4)
    img = np.zeros((600, 600))
    _dots(img, 80, 80, 6, 8, rng)               # small cluster
    _dots(img, 300, 320, 25, 20, rng)           # big cluster
    img[:, 450:] = 0.8                          # grid bar, 1.5 um wide
    img = ndi.gaussian_filter(img, 0.7)
    groups = P.find_particle_groups(img, nm_per_px=10.0)
    assert len(groups) == 2
    assert groups[0]["centroid_px"] == pytest.approx([302, 322], abs=8)
    assert groups[0]["bright_px"] > groups[1]["bright_px"]


def test_find_particle_groups_keeps_rods_at_high_mag():
    """At 0.5 nm/px a 20 nm rod is 40 px wide, nothing like a grid bar."""
    groups = P.find_particle_groups(_rod_image(touching=False), nm_per_px=0.5, join_px=20)
    assert len(groups) >= 1


def test_edge_profile_shows_a_shell_plateau():
    bare = P.edge_distance_profile(_rod_image(touching=False), 0.5, step_nm=1.5)
    coated = P.edge_distance_profile(_rod_image(touching=False, shell_nm=8), 0.5, step_nm=1.5)
    at = lambda prof, d: next(b["fraction"] for b in prof["bins"] if b["from_nm"] <= d < b["to_nm"])
    assert at(coated, 5) > 0.025 > at(bare, 5)             # shell visible at 5 nm
    assert at(coated, 12) < 0.01                           # gone beyond the 8 nm shell


def test_measure_rods_rejects_bad_scale():
    with pytest.raises(ValueError):
        P.measure_rods(np.zeros((10, 10)), 0)


# ---------------------------------------------------------------------------
# imaging
# ---------------------------------------------------------------------------

def test_sharpness_prefers_the_focused_frame():
    sharp = _rod_image(touching=False)
    blurred = ndi.gaussian_filter(sharp, 4)
    assert I.sharpness(sharp) > I.sharpness(blurred)


def test_phase_correlation_recovers_a_known_shift():
    rng = np.random.default_rng(1)
    base = ndi.gaussian_filter(rng.random((256, 256)), 3)
    moved = np.roll(np.roll(base, 17, axis=1), -9, axis=0)    # features right 17, up 9
    shift = I.phase_correlation_shift(base, moved)
    assert (shift["dx_px"], shift["dy_px"]) == (17, -9)
    assert shift["confidence"] > 20


def test_fit_stage_to_image_recovers_the_jacobian():
    j = np.array([[-0.649, 0.50], [0.44, 0.83]])
    d = np.array([[3e-6, 0], [0, 3e-6], [2e-6, -1e-6]])
    fit = I.fit_stage_to_image(d.tolist(), (d @ j.T).tolist())
    assert np.allclose(fit["stage_to_image"], j, atol=1e-3)
    assert fit["rms_residual_m"] < 1e-12
    with pytest.raises(ValueError, match="non-parallel"):
        I.fit_stage_to_image([[1e-6, 0], [2e-6, 0]], [[0, 0], [0, 0]])


def _png_size(path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    idat = data[data.index(b"IDAT") + 4:data.index(b"IEND") - 8]
    zlib.decompress(idat)                                   # valid stream
    return w, h, data[25]


def test_png_export_grey_and_overlay(tmp_path):
    img = _rod_image()
    w, h, ctype = _png_size(I.to_png(img, tmp_path / "a.png", "low", max_px=256))
    assert (w, h, ctype) == (256, 256, 0)
    w, h, ctype = _png_size(I.overlay_png(img, img.T, tmp_path / "b.png", upscale=2))
    assert (w, h, ctype) == (1024, 1024, 2)


# ---------------------------------------------------------------------------
# EDS regions
# ---------------------------------------------------------------------------

def _synthetic_si(n=60, px_nm=6.0, gain=-0.004, offset=0.0):
    """Au rod core + Si shell + Cu everywhere, as an (E, y, x) cube."""
    e = np.arange(2048) * 0.01
    obs = lambda kev: kev * (1 + gain) + offset
    peak = lambda kev: np.exp(-0.5 * ((e - obs(kev)) / 0.06) ** 2)
    au = 4 * peak(2.122) + 2 * peak(9.711) + 0.8 * peak(11.442) + 0.4 * peak(2.409)
    si = 1.5 * peak(1.740) + 1.0 * peak(0.525)
    cu = 1.0 * peak(8.040) + 0.4 * peak(0.928) + 0.15 * peak(8.905)
    c = 2.0 * peak(0.277)
    yy, xx = np.mgrid[0:n, 0:n]
    core = (np.abs(xx - 30) <= 6) & (np.abs(yy - 30) <= 2)      # 72 x 30 nm rod
    shell = (np.abs(xx - 30) <= 8) & (np.abs(yy - 30) <= 4)      # +12 nm silica
    cube = (core[None] * au[:, None, None] * 20 + shell[None] * si[:, None, None] * 6
            + cu[:, None, None] * 1.0 + c[:, None, None] * 2.0 + 0.05)
    rng = np.random.default_rng(2)
    cube = rng.poisson(cube).astype(np.float32)
    df = ndi.gaussian_filter(core.astype(float), 0.7)
    return cube, e, df


def test_region_peaks_identify_core_and_shell_with_calibration_error():
    cube, e, df = _synthetic_si()
    masks = R.region_masks(df)
    spectra = R.region_spectra(cube, 0, masks)
    cal = R.identify_peaks(sum(spectra.values()), e)["calibration"]
    assert cal["gain"] == pytest.approx(-0.004, abs=0.0015)
    top = lambda reg: {p["candidates"][0]["line"]
                       for p in R.identify_peaks(spectra[reg], e, calibration=cal)["peaks"]}
    particle = top("particle")
    assert {"Au Ma", "Au La", "Au Lb"} <= particle          # not W Lb / Nb La
    assert "Si Ka" in top("halo") and "Cu Ka" in top("far")


def test_edge_profile_and_ratio_separate_core_shell_and_background():
    cube, e, df = _synthetic_si()
    maps = R.element_maps(cube, 0, e, {"Au Ma": 2.122, "Si Ka": 1.740, "Cu Ka": 8.040},
                          energy_gain=-0.004)
    prof = R.edge_profile(maps, df, px_nm=6.0, bin_nm=6.0)
    at = lambda d, k: next(b[k] for b in prof["bins"] if b["from_nm"] <= d < b["to_nm"])
    assert at(-3, "Au Ma") > 10 * max(at(9, "Au Ma"), 0.05)  # core inside only
    assert at(9, "Si Ka") > 5 * max(at(30, "Si Ka"), 0.05)   # shell outside the core
    ratio = R.ratio_to_reference(maps, R.region_masks(df), "Cu Ka", "Au Ma")
    assert abs(ratio["ratio"]) < 0.05                        # uniform Cu cancels


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

class _CaptureMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


async def test_image_analysis_tools_end_to_end(tmp_path):
    from client.tools import image_analysis
    mcp, state = _CaptureMCP(), StateManager(tmp_path)
    instrument = {"stage": {"image_mapping": {"STEM": {
        "stage_to_image": [[-0.649, 0.50], [0.44, 0.83]]}}}}
    image_analysis.register(mcp, state, instrument=instrument)
    img = state.images.save(
        _rod_image().astype(np.float32), name="rods",
        calibration=ImageCalibration(x_scale=0.0005, y_scale=0.0005, units="µm"),
        stage_position=StagePosition(x_m=1e-4, y_m=2e-5, z_m=0.0, alpha_deg=0, beta_deg=0),
        tags={"imaging_mode": "STEM"})
    rods = await mcp.tools["measure_nanorods"](image_id=img.image_id)
    assert rods["isolated"]["n"] == 3 and "particles" not in rods
    clusters = await mcp.tools["find_particle_clusters"](image_id=img.image_id)
    assert clusters["n_clusters"] >= 1 and "stage_target_m" in clusters["clusters"][0]
    assert "warnings" not in clusters                       # calibrated mapping used
    png = await mcp.tools["export_image_png"](image_id=img.image_id, stretch="low")
    assert png["path"].endswith(".png") and (tmp_path / "exports").is_dir()
    with pytest.raises(ValueError):
        await mcp.tools["export_image_png"](image_id=img.image_id, stretch="gamma")
    ranked = await mcp.tools["image_sharpness"](image_ids=[img.image_id])
    assert ranked["sharpest"] == img.image_id


async def test_fit_stage_image_mapping_tool(tmp_path):
    from client.tools import image_analysis
    mcp, state = _CaptureMCP(), StateManager(tmp_path)
    image_analysis.register(mcp, state)
    rng = np.random.default_rng(3)
    base = ndi.gaussian_filter(rng.random((256, 256)), 3).astype(np.float32)
    j = np.array([[-0.649, 0.50], [0.44, 0.83]])
    cal = ImageCalibration(x_scale=10.0, y_scale=10.0, units="nm")      # 10 nm/px
    ids = []
    for d in ([0, 0], [300e-9, 0], [0, 300e-9]):
        shift_px = np.rint(j @ np.array(d) / 10e-9).astype(int)
        moved = np.roll(np.roll(base, shift_px[0], axis=1), shift_px[1], axis=0)
        ids.append(state.images.save(moved, name="m", calibration=cal,
                                     stage_position=StagePosition(x_m=d[0], y_m=d[1], z_m=0,
                                                                  alpha_deg=0, beta_deg=0),
                                     tags={"imaging_mode": "STEM"}).image_id)
    out = await mcp.tools["fit_stage_image_mapping"](image_ids=ids)
    assert np.allclose(out["stage_to_image"], j, atol=0.04)              # 1 px quantisation
    assert "STEM" in out["config_entry"]["stage"]["image_mapping"]


async def test_eds_region_tools_end_to_end(tmp_path):
    from client.tools import eds as eds_tools
    mcp, state = _CaptureMCP(), StateManager(tmp_path)
    eds_tools.register(mcp, object(), state)
    cube, e, df = _synthetic_si()
    axes = [{"index": 0, "size": 60, "origin": 0.0, "scale": 0.006, "units": "µm"},
            {"index": 1, "size": 60, "origin": 0.0, "scale": 0.006, "units": "µm"},
            {"index": 2, "size": 2048, "origin": 0.0, "scale": 0.01, "units": "keV"}]
    si = state.images.save(cube, name="si", tags={"axes": axes, "energy_axis_index": 2})
    dfi = state.images.save(df.astype(np.float32), name="df")
    regions = await mcp.tools["eds_region_spectra"](si_image_id=si.image_id,
                                                    df_image_id=dfi.image_id)
    assert regions["detector_calibration"]["gain"] == pytest.approx(-0.004, abs=0.0015)
    assert regions["regions"]["particle"]["peaks"][0]["candidates"][0]["line"].startswith("Au")
    prof = await mcp.tools["eds_edge_profile"](
        si_image_id=si.image_id, df_image_id=dfi.image_id, lines=["Au Ma", "Si", "Cu Ka"],
        energy_gain=-0.004, ratio=["Cu Ka", "Au Ma"], save_maps=True)
    assert set(prof["lines_keV"]) == {"Au Ma", "Si Ka", "Cu Ka"}
    assert prof["px_nm"] == pytest.approx(6.0)
    assert set(prof["map_image_ids"]) == {"Au Ma", "Si Ka", "Cu Ka"}
    with pytest.raises(ValueError, match="doesn't match"):
        await mcp.tools["eds_region_spectra"](si_image_id=si.image_id,
                                              df_image_id=state.images.save(
                                                  np.zeros((5, 5), np.float32)).image_id)


async def test_eds_map_elements_auto_picks_elements_and_writes_pngs(tmp_path):
    """One call from SI to maps: elements found from the peaks, best line
    per element, detector gain corrected, core/shell visible per region."""
    from client.tools import eds as eds_tools
    mcp, state = _CaptureMCP(), StateManager(tmp_path)
    eds_tools.register(mcp, object(), state)
    cube, e, df = _synthetic_si()
    axes = [{"index": 0, "size": 60, "origin": 0.0, "scale": 0.006, "units": "µm"},
            {"index": 1, "size": 60, "origin": 0.0, "scale": 0.006, "units": "µm"},
            {"index": 2, "size": 2048, "origin": 0.0, "scale": 0.01, "units": "keV"}]
    si = state.images.save(cube, name="si", tags={"axes": axes, "energy_axis_index": 2})
    dfi = state.images.save(df.astype(np.float32), name="df")
    out = await mcp.tools["eds_map_elements"](si_image_id=si.image_id,
                                              df_image_id=dfi.image_id)
    els = out["elements"]
    by_el = {k.split()[0]: k for k in els}
    assert {"Au", "Si", "Cu"} <= set(by_el)
    assert out["detector_calibration"]["gain"] == pytest.approx(-0.004, abs=0.0015)
    au, si_, cu = els[by_el["Au"]], els[by_el["Si"]], els[by_el["Cu"]]
    assert au["detected"] and si_["detected"]
    assert au["particle_minus_far"] > 10 * abs(cu["particle_minus_far"]) + 1   # Cu uniform
    assert si_["per_px"]["halo"] > 3 * max(si_["per_px"]["far"], 0.05)        # shell
    assert set(out["overlay"]["channels"].values()) >= {by_el["Au"], by_el["Si"]}
    assert all(Path(v["png"]).is_file() for v in els.values())
    assert Path(out["overlay"]["png"]).is_file()
    saved, _ = state.images.load(au["map_image_id"])
    assert saved.shape == (60, 60)

    # Explicit list, with a line label and a bare element; grid excluded.
    out2 = await mcp.tools["eds_map_elements"](si_image_id=si.image_id,
                                               elements=["Au Ma", "Si"], export_png=False)
    assert set(out2["elements"]) == {"Au Ma", "Si Ka"}
    assert "overlay" not in out2
    assert any(p["best_guess"].startswith("Cu") for p in out2["unmapped_peaks"])
    with pytest.raises(ValueError, match="no tabulated"):
        await mcp.tools["eds_map_elements"](si_image_id=si.image_id, elements=["Xx"])


def test_elements_from_peaks_skips_escape_and_excluded():
    peaks = [{"height": 10, "candidates": [{"line": "Au La"}]},
             {"height": 8, "candidates": [{"line": "Cu Ka"}]},
             {"height": 5, "candidates": [{"line": "Au Ma"}]},
             {"height": 1, "candidates": [{"line": "Ar Ka"}], "possible_escape_of": ["Au La"]},
             {"height": 3, "candidates": [{"line": "Si Ka"}]}]
    assert R.elements_from_peaks(peaks) == ["Au", "Cu", "Si"]
    assert R.elements_from_peaks(peaks, exclude=["Cu"]) == ["Au", "Si"]
    assert R.overlapping_lines({"Si Ka": 1.740, "W Ma": 1.775, "Au Ma": 2.122})
