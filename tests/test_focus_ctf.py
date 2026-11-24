"""stem_autofocus (iterated OLf sweep against a fake instrument), the Thon
ring C1/A1 fit behind compute_fft(fit_ctf=True), and the EDS side-band
placement used by eds_map_elements. No instrument."""

import numpy as np
import pytest
from scipy import ndimage as ndi

from client.analysis import ctf as C
from client.analysis import eds_regions as R
from client.analysis import imaging as I
from client.state.manager import StateManager
from client.state.models import ImageCalibration


class _CaptureMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


# ---------------------------------------------------------------------------
# stem_autofocus
# ---------------------------------------------------------------------------

class _FakeScope:
    """OLf lens + DigiScan: frames blur with distance from *true_focus*.
    DM windows are tracked so the frame clean-up can be checked."""

    def __init__(self, olf=32500, true_focus=32571, mode="STEM", fail_after=None):
        self.olf, self.true, self.mode = olf, true_focus, mode
        self.set_calls, self.closed, self.n_acq = [], [], 0
        self.fail_after = fail_after
        rng = np.random.default_rng(0)
        scene = np.zeros((512, 512))
        for _ in range(60):
            y, x = rng.integers(10, 500, 2)
            scene[y:y + 6, x:x + 14] = 1.0
        self.scene = scene
        self.windows = [{"doc_id": 7, "doc_name": "operator image", "title": "A: op"}]
        self.rng = rng

    async def send_command(self, cmd, params=None, **_):
        params = params or {}
        if cmd == "get_instrument_mode":
            return {"mode": self.mode}
        if cmd == "get_focus":
            return {"focus_dac": self.olf, "lens": "focus.fine",
                    "lenses": {"focus.fine": {"value": self.olf, "unit": "dac",
                                              "range": [0, 65535]}}}
        if cmd == "set_focus":
            assert params["lens"] == "focus.fine"
            self.olf = int(params["value"])
            self.set_calls.append(self.olf)
            return {"focus_dac": self.olf}
        if cmd == "dm.list_windows":
            return {"windows": list(self.windows)}
        if cmd == "dm.close_window":
            self.closed.append(params["doc_id"])
            self.windows = [w for w in self.windows if w["doc_id"] != params["doc_id"]]
            return {"closed": {}}
        raise AssertionError(cmd)

    async def send_command_image(self, cmd, params=None, **_):
        assert cmd == "stem_acquire"
        assert (params["width"], params["height"]) == (512, 512) and params["dwell_us"] <= 2
        self.n_acq += 1
        if self.fail_after and self.n_acq > self.fail_after:
            raise RuntimeError("DigiScan timeout")
        sigma = 0.6 + abs(self.olf - self.true) / 15.0
        img = ndi.gaussian_filter(self.scene, sigma) + 0.02 * self.rng.standard_normal((512, 512))
        name = f"STEM_capture_{self.n_acq}"
        self.windows.append({"doc_id": 100 + self.n_acq, "doc_name": name, "title": name})
        return ({"image_meta": {"name": name, "calibration":
                                {"x_scale": 0.5, "y_scale": 0.5, "units": "nm"}}},
                img.astype(np.float32))


def _focus_tools(tmp_path, scope):
    from client.tools import focus
    mcp, state = _CaptureMCP(), StateManager(tmp_path)
    focus.register(mcp, scope, state)
    return mcp.tools["stem_autofocus"], state


def test_two_sharpest_range_and_sweep_values():
    assert I.two_sharpest_range([(0, 1.0), (10, 3.0), (20, 2.0), (30, 0.5)]) == (10, 20)
    assert I.sweep_values(0, 4, 7) == [0, 1, 2, 3, 4]          # duplicates dropped
    assert I.sweep_values(100, 160, 4) == [100, 120, 140, 160]


async def test_stem_autofocus_converges_and_cleans_up(tmp_path):
    scope = _FakeScope()
    run, state = _focus_tools(tmp_path, scope)
    out = await run(settle_s=0, steps=7, iterations=3)
    assert abs(out["best"] - 32571) <= 4
    assert scope.olf == out["best"] == out["final"]
    assert out["start"] == 32500
    its = out["iterations"]
    assert its[0]["range"] == [32300, 32700]
    # Each round's range is the span of the previous round's two sharpest.
    for prev, nxt in zip(its, its[1:]):
        assert nxt["range"] == prev["next_range"]
        top2 = sorted(prev["points"], key=lambda p: -p["sharpness"])[:2]
        assert sorted(p["value"] for p in top2) == prev["next_range"]
    assert its[-1]["step"] < its[0]["step"] / 5
    # Only the sweep's own DM windows are closed; the operator's stays.
    assert out["dm_frames"]["closed"] == out["n_frames"] and 7 not in scope.closed
    arr, side = state.images.load(out["best_image_id"])
    assert side.tags["value"] == out["best"] and arr.shape == (512, 512)
    assert side.tags["lens"] == out["lens"] == "focus.fine"
    assert "warnings" not in out


async def test_stem_autofocus_warns_at_range_edge_and_can_restore(tmp_path):
    scope = _FakeScope(olf=32300, true_focus=32700)
    run, _ = _focus_tools(tmp_path, scope)
    out = await run(half_range=100, iterations=1, settle_s=0, set_best=False)
    assert "edge" in out["warnings"][0]
    assert scope.olf == 32300 == out["final"]


async def test_stem_autofocus_refuses_tem_and_restores_focus_on_error(tmp_path):
    run, _ = _focus_tools(tmp_path, _FakeScope(mode="TEM"))
    with pytest.raises(ValueError, match="STEM"):
        await run(settle_s=0)
    scope = _FakeScope(fail_after=4)
    run, _ = _focus_tools(tmp_path, scope)
    with pytest.raises(RuntimeError, match="restored to focus.fine 32500"):
        await run(settle_s=0)
    assert scope.olf == 32500
    with pytest.raises(ValueError, match="range"):
        await run(focus_min=500, focus_max=400, settle_s=0)


# ---------------------------------------------------------------------------
# CTF fit
# ---------------------------------------------------------------------------

def _thon(n=1024, px=0.05, df=400.0, a1=30.0, th_deg=35.0, kv=200.0, cs_mm=1.0,
          noise=0.5, seed=0):
    """Amorphous-film image whose spectrum is the CTF with known df / A1."""
    lam = C.electron_wavelength_nm(kv)
    rng = np.random.default_rng(seed)
    f = np.fft.fftfreq(n, px)
    qx, qy = np.meshgrid(f, f)
    q2, phi = qx ** 2 + qy ** 2, np.arctan2(qy, qx)
    chi = (np.pi * lam * q2 * (df + a1 * np.cos(2 * (phi - np.deg2rad(th_deg))))
           - 0.5 * np.pi * cs_mm * 1e6 * lam ** 3 * q2 ** 2)
    ctf = np.sin(chi + np.arcsin(0.07)) * np.exp(-q2 / 16.0)
    img = np.real(np.fft.ifft2(np.fft.fft2(rng.standard_normal((n, n))) * ctf))
    return img + noise * img.std() * rng.standard_normal((n, n))


def test_wavelength_200kv():
    assert C.electron_wavelength_nm(200) == pytest.approx(0.002508, abs=2e-6)


@pytest.mark.parametrize("df,a1,th", [(400, 30, 35), (-600, 50, 120), (200, 20, 80)])
def test_fit_ctf_recovers_defocus_and_astigmatism(df, a1, th):
    out = C.fit_ctf(_thon(df=df, a1=a1, th_deg=th), nm_per_px=0.05)
    assert out["reliable"]
    assert out["defocus_nm"] == pytest.approx(df, abs=0.03 * abs(df) + 5)
    assert out["C1_nm"] == pytest.approx(-out["defocus_nm"])
    assert out["A1_nm"] == pytest.approx(a1, abs=5)
    d_ang = abs(out["A1_angle_deg"] - th) % 180
    assert min(d_ang, 180 - d_ang) < 5


def test_fit_ctf_flags_noise_as_unreliable():
    for seed in range(3):
        out = C.fit_ctf(np.random.default_rng(seed).standard_normal((1024, 1024)), 0.05)
        assert not out["reliable"]


async def test_compute_fft_tool_fits_ctf(tmp_path):
    from client.tools import analysis
    mcp, state = _CaptureMCP(), StateManager(tmp_path)
    analysis.register(mcp, state)
    img = state.images.save(_thon(df=300, a1=0, th_deg=0).astype(np.float32), name="film",
                            calibration=ImageCalibration(x_scale=0.05, y_scale=0.05,
                                                         units="nm"))
    plain = await mcp.tools["compute_fft"](image_id=img.image_id)
    assert "ctf" not in plain
    out = await mcp.tools["compute_fft"](image_id=img.image_id, fit_ctf=True)
    assert out["ctf"]["defocus_nm"] == pytest.approx(300, abs=15)
    assert out["ctf"]["A1_nm"] < 8 and "warning" not in out["ctf"]
    raw = state.images.save(np.zeros((256, 256), np.float32), name="uncal",
                            calibration=ImageCalibration(units=""))
    with pytest.raises(ValueError):
        await mcp.tools["compute_fft"](image_id=raw.image_id, fit_ctf=True)


# ---------------------------------------------------------------------------
# EDS background bands
# ---------------------------------------------------------------------------

def test_sidebands_avoid_neighbouring_peaks():
    # O Ka (0.525): a fixed 0.265 keV offset would put the left band on C Ka.
    left, right = R.clean_sidebands(0.525, 0.09, [0.0, 0.277, 0.93], 0.0, 20.0)
    assert left is None or abs(left - 0.277) >= 0.195
    assert right is not None and abs(right - 0.93) >= 0.195 and right > 0.525 + 0.09


def test_net_line_map_is_unbiased_on_pure_background():
    rng = np.random.default_rng(0)
    e = np.arange(400) * 0.01
    cube = rng.poisson(5.0, size=(400, 30, 30)).astype(float)
    nm = R.net_line_map(cube, 0, e, 2.0, 0.09, 1.7, 2.3)
    # Unclipped: noise averages to ~0 instead of a positive bias.
    assert abs(nm["net"].mean()) < 0.5 and (nm["net"] < 0).any()


async def test_stem_autofocus_uses_the_lens_range_the_driver_reports(tmp_path):
    # A vendor whose focus lens isn't a 16-bit DAC: the range comes from
    # get_focus, not a JEOL constant.
    scope = _FakeScope(olf=500, true_focus=520)
    orig = scope.send_command

    async def narrow(cmd, params=None, **kw):
        if cmd == "get_focus":
            return {"lenses": {"focus.fine": {"value": scope.olf, "range": [0, 1000]}}}
        return await orig(cmd, params, **kw)
    scope.send_command = narrow
    run, _ = _focus_tools(tmp_path, scope)
    with pytest.raises(ValueError, match="0 <= min < max <= 1000"):
        await run(focus_min=900, focus_max=1200, settle_s=0)
    out = await run(half_range=100, steps=7, iterations=3, settle_s=0)
    assert abs(out["best"] - 520) <= 10


async def test_stem_autofocus_on_a_microscope_without_dm(tmp_path):
    scope = _FakeScope()
    orig = scope.send_command

    async def no_dm(cmd, params=None, **kw):
        if cmd.startswith("dm."):
            raise RuntimeError("'dm.list_windows' needs a 'dm' driver, and this "
                               "microscope has none")
        return await orig(cmd, params, **kw)
    scope.send_command = no_dm
    run, _ = _focus_tools(tmp_path, scope)
    out = await run(settle_s=0, iterations=1)
    assert "dm_frames" not in out and abs(out["best"] - 32571) <= 40
