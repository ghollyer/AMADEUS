"""Client MCP tool wiring — params reach the wire as documented.

Registers tool modules on a capturing stand-in for FastMCP and drives them
with a fake ZMQ client, so no MCP runtime, relay or instrument is needed.
"""

import numpy as np

from client.state.manager import StateManager
from client.tools import live as live_tools
from client.tools import stem as stem_tools
from client.tools import windows as window_tools


class _CaptureMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class _FakeZMQ:
    def __init__(self):
        self.sent = []

    async def send_command(self, cmd, params=None, **_):
        self.sent.append((cmd, params))
        return {"active": True}

    async def send_command_image(self, cmd, params=None, **_):
        self.sent.append((cmd, params))
        return ({"image_meta": {"name": "cam", "calibration":
                                {"x_scale": 1.0, "y_scale": 1.0, "units": "nm"}},
                 "data": {}},
                np.zeros((4, 4), dtype=np.float32))


def _register(module, tmp_path):
    mcp, zmq = _CaptureMCP(), _FakeZMQ()
    state = StateManager(tmp_path)
    try:
        module.register(mcp, zmq, state)
    except TypeError:            # live.register takes the stream subscriber too
        module.register(mcp, zmq, state, None)
    return mcp.tools, zmq


# Regression: no way to pick the Metro camera.
async def test_take_camera_image_selects_the_camera_device(tmp_path):
    tools, zmq = _register(stem_tools, tmp_path)
    await tools["take_camera_image"](exposure_s=0.1, camera=1)
    assert zmq.sent[-1] == ("camera_acquire", {
        "exposure_s": 0.1, "binning": 1, "processing": "gain_normalised",
        "device": "camera/1"})
    await tools["take_camera_image"](exposure_s=0.1, camera="diffraction")
    assert zmq.sent[-1][1]["device"] == "camera:diffraction"
    await tools["take_camera_image"](exposure_s=0.1, camera="tvips/camera/0")
    assert zmq.sent[-1][1]["device"] == "tvips/camera/0"


async def test_take_camera_image_sends_no_device_by_default(tmp_path):
    tools, zmq = _register(stem_tools, tmp_path)
    await tools["take_camera_image"](exposure_s=0.1)
    assert "device" not in zmq.sent[-1][1]


async def test_start_live_scan_selects_the_device(tmp_path):
    tools, zmq = _register(live_tools, tmp_path)
    await tools["start_live_scan"](detector="camera", device=1)
    cmd, params = zmq.sent[-1]
    assert cmd == "live_start" and params["device"] == "camera/1"
    await tools["start_live_scan"](detector="stem", device="HAADF")
    assert zmq.sent[-1][1]["device"] == "detector:HAADF"
    await tools["start_live_scan"](detector="camera")
    assert "device" not in zmq.sent[-1][1]


# Regression: link_image_to_object stored IDs of images that
# don't exist, and duplicated repeated links.
async def test_link_image_to_object_validates_and_dedupes(tmp_path):
    import pytest
    from client.state.models import ImageCalibration
    from client.tools import map_tools

    mcp = _CaptureMCP()
    state = StateManager(tmp_path)
    map_tools.register(mcp, state)
    tools = mcp.tools

    obj = await tools["add_map_object"](label="p1", x_nm=0, y_nm=0)
    img = state.images.save(np.zeros((2, 2), dtype=np.float32), name="t",
                            calibration=ImageCalibration(x_scale=1, y_scale=1, units="nm"))

    with pytest.raises(ValueError, match="not found in the local image store"):
        await tools["link_image_to_object"](object_id=obj["id"], image_id="no-such-image")
    for bad in ("../images/" + img.image_id, "", ".hidden"):
        with pytest.raises(ValueError):
            await tools["link_image_to_object"](object_id=obj["id"], image_id=bad)

    await tools["link_image_to_object"](object_id=obj["id"], image_id=img.image_id)
    out = await tools["link_image_to_object"](object_id=obj["id"], image_id=img.image_id)
    assert out["image_ids"] == [img.image_id]


# Regression: analysis assumed calibration was nm/px, but DM
# calibrates the F200 OneView in µm — pixel→stage and d-spacings were 1000×
# off. Verified on the instrument: corner of a 1024² image at 0.00277 µm/px
# came back 1.42 nm from centre instead of 1.42 µm.
import pytest  # noqa: E402

from client.analysis.units import UncalibratedImageError, nm_per_px  # noqa: E402


@pytest.mark.parametrize("scale, units, nm", [
    (0.5, "nm", 0.5), (0.00277, "µm", 2.77), (0.00277, "μm", 2.77),
    (0.00277, "um", 2.77), (2.0, "Å", 0.2), (1e-9, "m", 1.0),
    (0.00277, "b'\\xb5m'", 2.77),          # legacy garbled sidecars
])
def test_nm_per_px_units(scale, units, nm):
    assert nm_per_px(scale, units) == pytest.approx(nm)


@pytest.mark.parametrize("units", ["", "px", "pixels", " "])
def test_nm_per_px_refuses_uncalibrated(units):
    with pytest.raises(UncalibratedImageError):
        nm_per_px(1.0, units)


def test_nm_per_px_refuses_reciprocal_units():
    with pytest.raises(ValueError, match="unsupported spatial unit"):
        nm_per_px(0.01, "1/nm")


def _analysis(tmp_path, scale, units, stage_x=1e-6):
    from client.state.models import ImageCalibration, StagePosition
    from client.tools import analysis
    mcp = _CaptureMCP()
    state = StateManager(tmp_path)
    analysis.register(mcp, state)
    img = state.images.save(
        np.zeros((1024, 1024), dtype=np.float32), name="t",
        calibration=ImageCalibration(x_scale=scale, y_scale=scale, units=units),
        stage_position=StagePosition(x_m=stage_x, y_m=0.0, z_m=0.0,
                                     alpha_deg=0.0, beta_deg=0.0))
    return mcp.tools, img.image_id


async def test_pixel_to_stage_honours_micrometres(tmp_path):
    tools, iid = _analysis(tmp_path, 0.00277, "µm")
    out = await tools["convert_pixel_to_stage"](image_id=iid, px=0, py=0)
    assert out["x_m"] == pytest.approx(1e-6 - 512 * 2.77e-9)
    assert out["y_m"] == pytest.approx(-512 * 2.77e-9)


async def test_pixel_to_stage_refuses_uncalibrated(tmp_path):
    tools, iid = _analysis(tmp_path, 1.0, "")
    with pytest.raises(UncalibratedImageError):
        await tools["convert_pixel_to_stage"](image_id=iid, px=0, py=0)


async def test_lattice_spacings_honour_micrometres(tmp_path):
    from client.state.models import ImageCalibration
    from client.tools import analysis
    mcp = _CaptureMCP()
    state = StateManager(tmp_path)
    analysis.register(mcp, state)
    n, period_px = 256, 8                      # fringes every 8 px
    yy, xx = np.mgrid[0:n, 0:n]
    arr = np.cos(2 * np.pi * xx / period_px).astype(np.float32)
    img = state.images.save(arr, name="fringes", calibration=ImageCalibration(
        x_scale=0.001, y_scale=0.001, units="µm"))   # 1 nm/px
    out = await mcp.tools["extract_lattice_spacings"](image_id=img.image_id, max_peaks=2)
    assert out["nm_per_px"] == pytest.approx(1.0)
    assert out["peaks"][0]["d_nm"] == pytest.approx(8.0, rel=0.02)   # 8 px × 1 nm


# DM window tools: the target and discard flag reach the wire unchanged, and
# discard_unsaved is never on unless the caller asks for it.
async def test_dm_window_tools_wire_params(tmp_path):
    tools, zmq = _register(window_tools, tmp_path)
    await tools["list_dm_windows"]()
    assert zmq.sent[-1] == ("dm.list_windows", {})
    await tools["select_dm_window"](doc_id=191)
    assert zmq.sent[-1] == ("dm.select_window", {"doc_id": 191, "title": None, "index": None})
    await tools["close_dm_window"](title="A: HAADF")
    assert zmq.sent[-1] == ("dm.close_window", {"doc_id": None, "title": "A: HAADF",
                                             "index": None, "discard_unsaved": False})
    await tools["close_dm_window"](index=2, discard_unsaved=True)
    assert zmq.sent[-1][1]["discard_unsaved"] is True


def test_dm_window_tools_registered_in_server(tmp_path):
    from client.server import build_server
    mcp, *_ = build_server("tcp://127.0.0.1:1", tmp_path, enable_monitor=False)
    import asyncio
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {"list_dm_windows", "select_dm_window", "close_dm_window"} <= names


async def test_eds_tools_use_dm_axis_order_bug26(tmp_path):
    """Regression: a fetched SI cube arrives as numpy (E, y, x) while its
    DM axes table / energy_axis_index use DM order (x=0, y=1, E=2). The EDS
    tools must map DM dim 2 to numpy axis 0, not use it directly."""
    from client.tools import eds as eds_tools

    nx, ny, nc = 4, 5, 300                     # 0.01 keV/ch → 0–3 keV
    cube = np.zeros((nc, ny, nx), dtype=np.uint32)
    cube[208:217, 2, 3] = 50                   # Au M (2.12 keV) at x=3, y=2
    state = StateManager(tmp_path)
    sc = state.images.save(cube, name="si", tags={
        "type": "dm_file",
        "ndim": 3,
        "axes": [
            {"index": 0, "size": nx, "origin": 0.0, "scale": 0.0016, "units": "µm"},
            {"index": 1, "size": ny, "origin": 0.0, "scale": 0.0016, "units": "µm"},
            {"index": 2, "size": nc, "origin": 0.0, "scale": 0.01, "units": "keV"},
        ],
        "energy_axis_index": 2,
    })
    mcp = _CaptureMCP()
    eds_tools.register(mcp, _FakeZMQ(), state)

    desc = await mcp.tools["eds_describe_si"](sc.image_id)
    assert desc["energy_axis_index"] == 0
    assert desc["spatial_shape"] == [ny, nx]
    assert desc["n_channels"] == nc

    out = await mcp.tools["eds_extract_elemental_map"](sc.image_id, "Au", line="Ma")
    m, _ = state.images.load(out["map_image_id"])
    assert m.shape == (ny, nx)
    assert np.unravel_index(np.argmax(m), m.shape) == (2, 3)


# ---------------------------------------------------------------------------
# Stage tools + pixel -> stage via the measured Jacobian
# ---------------------------------------------------------------------------

F200_STEM_J = [[-0.649, 0.50], [0.44, 0.83]]
F200_INSTRUMENT = {"stage": {"image_mapping": {"STEM": {
    "stage_to_image": F200_STEM_J, "scan_rotation_deg": 0.0, "measured": "test"}}}}


async def test_move_stage_relative_sends_only_moved_axes_and_options(tmp_path):
    from client.tools import stage as stage_tools
    calls = []

    class _Z(_FakeZMQ):
        async def send_command(self, cmd, params=None, timeout_ms=None, **_):
            calls.append((cmd, params, timeout_ms))
            return {"x_m": 0.0, "y_m": 0.0, "z_m": 0.0, "alpha_deg": 0.0,
                    "beta_deg": 0.0, "settled": True}

    mcp, state = _CaptureMCP(), StateManager(tmp_path)
    stage_tools.register(mcp, _Z(), state)
    await mcp.tools["move_stage_relative"](x_nm=50, drive="piezo", speed="slow",
                                           timeout_s=10)
    cmd, params, timeout_ms = calls[-1]
    assert params == {"mode": "relative", "x_m": pytest.approx(5e-8), "drive": "piezo",
                      "speed": "slow", "wait": True, "timeout_s": 10.0}
    assert timeout_ms == 25_000                        # stage wait + margin
    with pytest.raises(ValueError, match="every offset is zero"):
        await mcp.tools["move_stage_relative"]()
    with pytest.raises(ValueError, match="drive"):
        await mcp.tools["move_stage_relative"](x_nm=1, drive="turbo")


def test_jacobian_reproduces_the_f200_cluster3_navigation():
    """2026-11-12: cluster 3 sat at image offset (+1.98, +4.90) um in a x12k
    DF frame. Solving J d = -offset gave the stage move (-1.063, -5.34) um,
    which centred it within ~0.4 um."""
    from client.analysis.stage_mapping import stage_offset_for_image_offset
    dx, dy = stage_offset_for_image_offset((1.98e-6, 4.90e-6), F200_STEM_J)
    assert dx == pytest.approx(-1.063e-6, abs=0.01e-6)
    assert dy == pytest.approx(-5.34e-6, abs=0.01e-6)


def test_jacobian_rejects_singular_matrices():
    from client.analysis.stage_mapping import validate_matrix
    with pytest.raises(ValueError, match="singular"):
        validate_matrix([[1, 2], [2, 4]])
    with pytest.raises(ValueError, match="2x2"):
        validate_matrix([[1, 0, 0], [0, 1, 0]])


def _mapped_analysis(tmp_path, instrument, tags):
    from client.state.models import ImageCalibration, StagePosition
    from client.tools import analysis
    mcp = _CaptureMCP()
    state = StateManager(tmp_path)
    analysis.register(mcp, state, instrument=instrument)
    img = state.images.save(
        np.zeros((1024, 1024), dtype=np.float32), name="t",
        calibration=ImageCalibration(x_scale=0.01915687881410122,
                                     y_scale=0.01915687881410122, units="µm"),
        stage_position=StagePosition(x_m=207.332e-6, y_m=42.217e-6, z_m=0.0,
                                     alpha_deg=0.0, beta_deg=0.0),
        tags=tags)
    return mcp.tools["convert_pixel_to_stage"], img.image_id


async def test_pixel_to_stage_uses_the_measured_jacobian_bug31(tmp_path):
    """The old conversion added the pixel offset straight to the stage
    position: for this feature that's (+1.98, +4.90) um, nowhere near the
    move that actually centred it."""
    convert, iid = _mapped_analysis(tmp_path, F200_INSTRUMENT,
                                    {"imaging_mode": "STEM", "rotation": 0.0})
    px = 512 + 1.98 / 0.01915687881410122
    py = 512 + 4.90 / 0.01915687881410122
    out = await convert(image_id=iid, px=px, py=py)
    assert out["dx_m"] == pytest.approx(-1.063e-6, abs=0.01e-6)
    assert out["dy_m"] == pytest.approx(-5.34e-6, abs=0.01e-6)
    assert out["x_m"] == pytest.approx(207.332e-6 - 1.063e-6, abs=0.01e-6)
    assert out["mapping"]["calibrated"] is True and "warnings" not in out


async def test_pixel_to_stage_infers_stem_from_legacy_tags(tmp_path):
    convert, iid = _mapped_analysis(tmp_path, F200_INSTRUMENT,
                                    {"mode": "capture", "signal_index": 1})
    out = await convert(image_id=iid, px=600, py=600)
    assert out["mapping"]["mode"] == "STEM" and out["mapping"]["calibrated"] is True


async def test_pixel_to_stage_warns_without_mapping_or_on_rotation(tmp_path):
    convert, iid = _mapped_analysis(tmp_path, F200_INSTRUMENT,
                                    {"imaging_mode": "TEM"})
    out = await convert(image_id=iid, px=600, py=600)
    assert out["mapping"]["calibrated"] is False
    assert "no measured stage->image mapping" in out["warnings"][0]
    convert, iid = _mapped_analysis(tmp_path / "r", F200_INSTRUMENT,
                                    {"imaging_mode": "STEM", "rotation": 30.0,
                                     "stage_position_source": "cached"})
    out = await convert(image_id=iid, px=600, py=600)
    assert any("scan rotation" in w for w in out["warnings"])
    assert any("last known position" in w for w in out["warnings"])


async def test_stem_image_records_fresh_stage_and_mode(tmp_path):
    calls = []

    class _Z(_FakeZMQ):
        async def send_command(self, cmd, params=None, **_):
            calls.append(cmd)
            return {"x_m": 1e-6, "y_m": 2e-6, "z_m": 0.0, "alpha_deg": 0.0,
                    "beta_deg": 0.0}

    mcp, state = _CaptureMCP(), StateManager(tmp_path)
    stem_tools.register(mcp, _Z(), state)
    out = await mcp.tools["take_stem_image"](rotation=12.0)
    _, sidecar = state.images.load(out["image_id"])
    # stage read fresh first; then the optics reads that tag the image
    assert calls == ["get_stage_position", "get_magnification", "get_high_tension",
                     "get_spot_size"]
    assert sidecar.stage_position.y_m == pytest.approx(2e-6)
    assert sidecar.tags["imaging_mode"] == "STEM"
    assert sidecar.tags["rotation"] == 12.0
    assert sidecar.tags["stage_position_source"] == "read"


# ---------------------------------------------------------------------------
# Shared stage-move options (MCP tools + console Stage tab)
# ---------------------------------------------------------------------------

def test_console_move_options_map_widgets_to_params():
    from client.stage_moves import console_move_options
    params = {"mode": "relative", "x_m": 1e-7}
    ms = console_move_options(params, "slow", "piezo", True, "30")
    assert params == {"mode": "relative", "x_m": 1e-7, "speed": "slow", "drive": "piezo",
                      "wait": True, "timeout_s": 30.0}            # no approach with piezo
    assert ms == 45_000
    params = {"mode": "absolute", "x_m": 1e-6}
    console_move_options(params, "default", "motor", False, "")   # all instrument defaults
    assert params == {"mode": "absolute", "x_m": 1e-6, "wait": True, "timeout_s": 20.0}
    with pytest.raises(ValueError, match="X/Y only"):
        console_move_options({"mode": "relative", "z_m": 1e-8}, "default", "piezo", False, "20")


def test_describe_status_for_console_readouts():
    from client.stage_moves import describe_status
    out = describe_status({"moving": True, "limit_error": True, "drive": "piezo",
                           "speed": {"xy": "slow", "z": "normal", "tilt": "fast"},
                           "piezo_nm": [12.4, -3.0], "motion": {"busy": {"running_s": 4.2}}})
    assert out == {"moving": "yes (4 s)", "drive": "piezo", "speed": "slow/normal/fast",
                   "piezo": "12, -3", "limit": "LIMIT"}
    assert describe_status({})["speed"] == "?"


def test_operator_pause_never_blocks_stop_stage(tmp_path):
    from client.monitor import Monitor, PausedError
    mon = Monitor(tmp_path / "mon", tmp_path / "img")
    mon.set_paused(True)
    with pytest.raises(PausedError):
        mon.before_command("move_stage", {})
    mon.before_command("stop_stage", {})                          # no raise


async def test_save_dm_window_saves_then_fetches(tmp_path):
    """save_dm_window(fetch=True): one call from a finished SI in DM to an
    image_id the EDS tools can use."""
    class _Z(_FakeZMQ):
        async def send_command(self, cmd, params=None, **_):
            self.sent.append((cmd, params))
            return {"saved": {"doc_id": 516}, "path": r"C:\d\si.dm4",
                    "size_bytes": 10, "unsaved": False}

        async def send_command_image(self, cmd, params=None, **_):
            self.sent.append((cmd, params))
            return ({"image_meta": {"calibration": {"x_scale": 0.006, "y_scale": 0.006,
                                                    "units": "µm"}},
                     "data": {"ndim": 3, "energy_axis_index": 2, "axes": []}},
                    np.zeros((8, 4, 4), dtype=np.float32))

    mcp, zmq = _CaptureMCP(), _Z()
    state = StateManager(tmp_path)
    window_tools.register(mcp, zmq, state)
    out = await mcp.tools["save_dm_window"](path=r"C:\d\si", doc_id=516, fetch=True)
    assert zmq.sent[0] == ("dm.save_window", {"doc_id": 516, "title": None, "index": None,
                                           "path": r"C:\d\si", "overwrite": False})
    assert zmq.sent[1] == ("load_file", {"path": r"C:\d\si.dm4"})   # DM's real path
    arr, side = state.images.load(out["fetched"]["image_id"])
    assert arr.shape == (8, 4, 4) and side.name == "si.dm4"
    out = await mcp.tools["save_dm_window"](path=r"C:\d\si", doc_id=516)
    assert "fetched" not in out and len(zmq.sent) == 3


def test_scripting_tools_left_out_when_disabled(tmp_path):
    from client.server import build_server
    import asyncio
    hatches = {"execute_vendor_call", "execute_dm_script", "execute_tem3_call"}
    mcp, *_ = build_server("tcp://127.0.0.1:1", tmp_path, enable_monitor=False)
    on = {t.name for t in asyncio.run(mcp.list_tools())}
    mcp, *_ = build_server("tcp://127.0.0.1:1", tmp_path / "b", enable_monitor=False,
                           allow_scripting=False)
    off = {t.name for t in asyncio.run(mcp.list_tools())}
    assert hatches <= on and not hatches & off
    assert on - off == hatches                 # nothing else lost


async def test_acquisitions_tag_magnification_ht_and_spot(tmp_path):
    tools, zmq = _register(stem_tools, tmp_path)

    async def reply(cmd, params=None, **_):
        zmq.sent.append((cmd, params))
        return {"get_magnification": {"magnification": 50000.0, "mag_index": 7,
                                      "mag_unit": "x"},
                "get_high_tension": {"high_tension_kV": 200.0},
                "get_spot_size": {"spot_size": 3}}.get(cmd, {})

    zmq.send_command = reply
    out = await tools["take_stem_image"](dwell_us=4.0)
    tags = StateManager(tmp_path).images.load_sidecar(out["image_id"]).tags
    assert tags["magnification"] == 50000.0 and tags["high_tension_kV"] == 200.0
    assert tags["spot_size"] == 3 and tags["dwell_us"] == 4.0
    assert zmq.sent[-1][0] == "stem_acquire"


async def test_acquisition_survives_missing_optics_reads(tmp_path):
    tools, zmq = _register(stem_tools, tmp_path)

    async def failing(cmd, params=None, **_):
        raise RuntimeError("not implemented")

    zmq.send_command = failing
    out = await tools["take_camera_image"](exposure_s=0.5)
    tags = StateManager(tmp_path).images.load_sidecar(out["image_id"]).tags
    assert tags["exposure_s"] == 0.5 and "magnification" not in tags


def test_vendor_tools_follow_the_configured_driver_kinds(tmp_path):
    from client.server import build_server
    import asyncio

    def names(kinds, sub):
        mcp, *_ = build_server("tcp://127.0.0.1:1", tmp_path / sub, enable_monitor=False,
                               driver_kinds=kinds)
        return {t.name for t in asyncio.run(mcp.list_tools())}
    sim_only = names({"sim"}, "a")
    assert "execute_vendor_call" in sim_only and "take_stem_image" in sim_only
    assert not {"execute_dm_script", "execute_tem3_call", "list_dm_windows",
                "save_dm_window"} & sim_only
    jeol_dm = names({"jeol", "dm"}, "b")
    assert {"execute_dm_script", "execute_tem3_call", "list_dm_windows"} <= jeol_dm


def test_driver_kinds_from_config():
    from client.server import driver_kinds_from
    from common import config as amadeus_config
    cfg = amadeus_config.load_defaults()
    assert driver_kinds_from(cfg) == {"jeol", "dm"}
    cfg["drivers"] = {"s": {"kind": "sim", "slots": ["column"]}}
    assert driver_kinds_from(cfg) == {"sim"}
    assert driver_kinds_from(None) is None
