"""Tests for the JEOL driver and the column/stage simulator.

A fake ``PyJEM.offline.TEM3`` module is installed in ``sys.modules`` BEFORE
``drivers.jeol`` is imported (the same strategy
``test_dm_helpers.py`` uses for ``DigitalMicrograph``), so these tests run
with no PyJEM installed. The fake mirrors the real offline API shapes:
Stage3.GetPos() -> [x, y, z, tx, ty] in nm/deg, DAC setters take ints, etc.
"""

import sys
import types

import pytest

# ─── Install the PyJEM fake BEFORE importing the JEOL driver ────────────────


class FakeStage3:
    def __init__(self):
        self.pos = [0.0, 0.0, 0.0, 0.0, 0.0]   # x, y, z (nm), tx, ty (deg)
        self.status_sequence = []               # queue of GetStatus replies
        self.stopped = False

    def GetPos(self):
        return list(self.pos)

    def GetStatus(self):
        if self.status_sequence:
            return self.status_sequence.pop(0)
        return [0, 0, 0, 0, 0]

    def SetX(self, v): self.pos[0] = float(v)
    def SetY(self, v): self.pos[1] = float(v)
    def SetZ(self, v): self.pos[2] = float(v)
    def SetTiltXAngle(self, v): self.pos[3] = float(v)
    def SetTiltYAngle(self, v): self.pos[4] = float(v)
    def SetXRel(self, v): self.pos[0] += float(v)
    def SetYRel(self, v): self.pos[1] += float(v)
    def SetZRel(self, v): self.pos[2] += float(v)
    def SetTXRel(self, v): self.pos[3] += float(v)
    def SetTYRel(self, v): self.pos[4] += float(v)
    def Stop(self): self.stopped = True

    # Drive mode / speed / piezo (PyJEM >= 1.3)
    drv_mode = 0                                # 0 motor, 1 piezo
    piezo = [0.0, 0.0]
    speed_mode = {0: [1, 1, 1], 1: [1, 1, 1]}   # per drive: xy, z, tilt
    def GetDrvMode(self): return self.drv_mode
    def SelDrvMode(self, sw): self.drv_mode = sw
    def GetPiezoPosi(self): return list(self.piezo)
    def GetSpeedMode(self, kind): return list(self.speed_mode[kind])
    def SetSpeedMode(self, kind, xy, z, txy): self.speed_mode[kind] = [xy, z, txy]


class FakeEOS3:
    def __init__(self):
        self.mag_index = 5
        self.spot = 1
        self.alpha = 2
        self.temstem = 0
        self.funcmode = 0
        self._funcnames = ["MAG", "MAG2", "LowMAG", "SAMAG", "DIFF"]

    def GetCurrentMagSelectorID(self): return self.mag_index
    def GetMagValue(self): return ["25000", "x", "X25k"]
    def SetSelector(self, v): self.mag_index = int(v)
    def GetSpotSize(self): return self.spot
    def SelectSpotSize(self, v): self.spot = int(v)
    def GetAlpha(self): return self.alpha
    def GetAlphaSelectorEx(self): return [self.alpha, str(self.alpha)]
    def SetAlphaSelector(self, v): self.alpha = int(v)
    def GetTemStemMode(self): return self.temstem
    def SelectTemStem(self, v): self.temstem = int(v)
    def GetFunctionMode(self): return [self.funcmode, self._funcnames[self.funcmode]]
    def SelectFunctionMode(self, v): self.funcmode = int(v)


class FakeLens3:
    def __init__(self):
        self.olf = 32768
        self.olc = 8000

    def GetOLf(self): return self.olf
    def GetOLc(self): return self.olc
    def SetOLf(self, v): self.olf = int(v)
    def SetOLc(self, v): self.olc = int(v)


class FakeDef3:
    def __init__(self):
        self.values = {name: [32768, 32768]
                       for name in ("CLA1", "CLA2", "OLs", "CLs", "ILs")}
        self.blank = 0

    def _get(self, name): return list(self.values[name])
    def _set(self, name, x, y): self.values[name] = [int(x), int(y)]

    def GetCLA1(self): return self._get("CLA1")
    def SetCLA1(self, x, y): self._set("CLA1", x, y)
    def GetCLA2(self): return self._get("CLA2")
    def SetCLA2(self, x, y): self._set("CLA2", x, y)
    def GetOLs(self): return self._get("OLs")
    def SetOLs(self, x, y): self._set("OLs", x, y)
    def GetCLs(self): return self._get("CLs")
    def SetCLs(self, x, y): self._set("CLs", x, y)
    def GetILs(self): return self._get("ILs")
    def SetILs(self, x, y): self._set("ILs", x, y)
    def GetBeamBlank(self): return self.blank
    def SetBeamBlank(self, v): self.blank = int(v)


class FakeApt3:
    def __init__(self):
        self.kind = 0
        self.sizes = {k: 0 for k in range(7)}
        self.position = [128, 128]

    def GetKind(self): return self.kind
    def SelectKind(self, k): self.kind = int(k)
    def GetSize(self, kind): return self.sizes[int(kind)]
    def SetSize(self, size): self.sizes[self.kind] = int(size)
    def GetPosition(self): return list(self.position)
    def SetPosition(self, x, y): self.position = [int(x), int(y)]


class FakeHT3:
    def GetHtValue(self): return 200000.0
    def GetHtRange(self): return [200000.0, 10.0]


class FakeFEG3:
    def __init__(self):
        self.valve = 0

    def GetBeamValve(self): return self.valve
    def SetBeamValve(self, v): self.valve = int(v)


class FakeDetector3:
    def GetPosition(self, det_id): return 0


def _install_fake_pyjem():
    tem3 = types.ModuleType("PyJEM.offline.TEM3")
    tem3.Stage3 = FakeStage3
    tem3.EOS3 = FakeEOS3
    tem3.Lens3 = FakeLens3
    tem3.Def3 = FakeDef3
    tem3.Apt3 = FakeApt3
    tem3.HT3 = FakeHT3
    tem3.FEG3 = FakeFEG3
    tem3.Detector3 = FakeDetector3

    pyjem = types.ModuleType("PyJEM")
    offline = types.ModuleType("PyJEM.offline")
    offline.TEM3 = tem3
    pyjem.offline = offline

    sys.modules["PyJEM"] = pyjem
    sys.modules["PyJEM.offline"] = offline
    sys.modules["PyJEM.offline.TEM3"] = tem3


_install_fake_pyjem()

from common.commands import SLOT_COMMANDS                          # noqa: E402
from drivers.base import ColumnDriver, StageDriver                  # noqa: E402
from drivers.calibration import InstrumentCalibration              # noqa: E402
from drivers.dispatch import call as _call                         # noqa: E402
from drivers.jeol.driver import JeolDriver                         # noqa: E402
from drivers.registry import build_driver                          # noqa: E402
from drivers.sim_column import SimColumnDriver                     # noqa: E402

HARDWARE_COMMANDS = SLOT_COMMANDS["column"] | SLOT_COMMANDS["stage"]


def dispatch(driver, request: dict) -> dict:
    reply = _call(driver, request)
    assert not isinstance(reply, tuple), "column/stage replies are JSON"
    return reply


CALIBRATED = InstrumentCalibration({
    "label": "test scope",
    "focus": {"by_mode": {"TEM": {"lens": "focus.fine", "dac_per_nm": 2.0,
                                  "dac_at_zero": 30000}}},
})


@pytest.fixture
def jeol():
    return JeolDriver(offline=True)


@pytest.fixture
def jeol_cal():
    return JeolDriver(offline=True, calibration=CALIBRATED)


# ---------------------------------------------------------------------------
# Registry coherence
# ---------------------------------------------------------------------------

def test_jeol_driver_implements_every_column_and_stage_operation():
    assert set(JeolDriver.operations()) == HARDWARE_COMMANDS
    assert set(SimColumnDriver.operations()) == HARDWARE_COMMANDS


def test_build_driver_kinds():
    assert build_driver("jeol", {"offline": True}).kind == "jeol"
    assert build_driver("sim").kind == "sim"
    with pytest.raises(ValueError):
        build_driver("thermo")


# ---------------------------------------------------------------------------
# Stage — unit conversion and settle logic
# ---------------------------------------------------------------------------

def test_stage_absolute_move_converts_metres_to_nm(jeol):
    result = jeol.move_stage({"mode": "absolute", "x_m": 1e-6, "y_m": -2e-6})
    assert jeol.stage.pos[0] == pytest.approx(1000.0)   # nm
    assert jeol.stage.pos[1] == pytest.approx(-2000.0)
    assert result["x_m"] == pytest.approx(1e-6)
    assert result["settled"] is True


def test_stage_relative_move_and_tilt(jeol):
    jeol.stage.pos = [1000.0, 0.0, 0.0, 5.0, 0.0]
    result = jeol.move_stage({"mode": "relative", "x_m": 5e-7, "alpha_deg": 2.5})
    assert jeol.stage.pos[0] == pytest.approx(1500.0)
    assert jeol.stage.pos[3] == pytest.approx(7.5)
    assert result["alpha_deg"] == pytest.approx(7.5)


def test_stage_move_waits_until_rest(jeol):
    jeol.stage.status_sequence = [[1, 0, 0, 0, 0], [1, 0, 0, 0, 0], [0] * 5]
    result = jeol.move_stage({"x_m": 1e-6})
    assert result["settled"] is True
    assert jeol.stage.status_sequence == []   # polled through the queue


def test_stage_move_timeout_reports_unsettled(jeol):
    jeol.stage.status_sequence = [[1, 0, 0, 0, 0]] * 1000
    result = jeol.move_stage({"x_m": 1e-6, "timeout_s": 0.15})
    assert result["settled"] is False


def test_stage_move_requires_an_axis(jeol):
    with pytest.raises(ValueError, match="no axes"):
        jeol.move_stage({"mode": "absolute"})


def test_stage_status_and_stop(jeol):
    jeol.stage.status_sequence = [[0, 1, 0, 0, 0]]
    status = jeol.get_stage_status({})
    assert status["moving"] is True
    assert status["axes"]["y"] == 1
    jeol.stop_stage({})
    assert jeol.stage.stopped is True


def _record(jeol):
    """Log every setter call made on the backend's (main-process) Stage3."""
    calls = []
    for name in ("SetX", "SetY", "SetXRel", "SetYRel", "SetZ", "SelDrvMode",
                 "SetSpeedMode"):
        orig = getattr(jeol.stage, name)
        setattr(jeol.stage, name,
                lambda *a, _n=name, _o=orig: (calls.append((_n, *a)), _o(*a))[1])
    return calls


def test_stage_relative_move_drives_only_given_axes(jeol):
    calls = _record(jeol)
    jeol.move_stage({"mode": "relative", "x_m": 5e-7})
    assert calls == [("SetXRel", pytest.approx(500.0))]


def test_backlash_approach_overshoots_then_approaches_from_calibrated_side():
    cal = InstrumentCalibration({"stage": {"backlash": {
        "enabled": True, "overshoot_nm": 1000, "approach": [1, -1]}}})
    jeol = JeolDriver(offline=True, calibration=cal)
    jeol.stage.pos = [5000.0, 5000.0, 0.0, 0.0, 0.0]
    calls = _record(jeol)
    jeol.move_stage({"mode": "relative", "x_m": -2e-6, "y_m": 1e-6})
    # targets: x 3000, y 6000. X approached from below (+1), Y from above (-1).
    assert calls == [("SetX", pytest.approx(2000.0)), ("SetY", pytest.approx(7000.0)),
                     ("SetX", pytest.approx(3000.0)), ("SetY", pytest.approx(6000.0))]
    assert jeol.stage.pos[:2] == [pytest.approx(3000.0), pytest.approx(6000.0)]


def test_backlash_per_move_override(jeol):
    calls = _record(jeol)
    jeol.move_stage({"mode": "absolute", "x_m": 1e-6, "approach": True})
    assert [c[0] for c in calls] == ["SetX", "SetX"]      # default overshoot 1 um
    calls.clear()
    jeol.move_stage({"mode": "absolute", "x_m": 2e-6})        # default: disabled
    assert [c[0] for c in calls] == ["SetX"]


def test_piezo_move_selects_piezo_and_restores_motor(jeol):
    calls = _record(jeol)
    jeol.move_stage({"mode": "relative", "x_m": 5e-8, "drive": "piezo"})
    assert calls == [("SelDrvMode", 1), ("SetXRel", pytest.approx(50.0)),
                     ("SelDrvMode", 0)]
    assert jeol.stage.drv_mode == 0


def test_piezo_range_and_axes_are_checked(jeol):
    jeol.stage.piezo = [9500.0, 0.0]
    with pytest.raises(ValueError, match="outside"):
        jeol.move_stage({"mode": "relative", "x_m": 1e-6, "drive": "piezo"})
    with pytest.raises(ValueError, match="X/Y only"):
        jeol.move_stage({"mode": "relative", "z_m": 1e-8, "drive": "piezo"})
    assert jeol.stage.drv_mode == 0


def test_speed_applies_for_one_move_then_restores(jeol):
    calls = _record(jeol)
    jeol.move_stage({"x_m": 1e-6, "speed": "slow"})
    assert calls[0] == ("SetSpeedMode", 0, 0, 0, 0)
    assert calls[-1] == ("SetSpeedMode", 0, 1, 1, 1)
    with pytest.raises(ValueError, match="speed"):
        jeol.move_stage({"x_m": 1e-6, "speed": "ludicrous"})


def test_stage_status_reports_drive_speed_piezo_and_limits(jeol):
    jeol.stage.status_sequence = [[0, 0, 2, 0, 0]]
    status = jeol.get_stage_status({})
    assert status["limit_error"] is True and status["moving"] is False
    assert status["drive"] == "motor"
    assert status["speed"] == {"xy": "normal", "z": "normal", "tilt": "normal"}
    assert status["piezo_nm"] == [0.0, 0.0]
    assert status["motion"]["kind"] == "inline"


# ---------------------------------------------------------------------------
# Stage motion in a child process
# ---------------------------------------------------------------------------

PROCESS_CAL = InstrumentCalibration({"stage": {"motion": "process"}})


@pytest.fixture
def jeol_process(tmp_path, monkeypatch):
    log = tmp_path / "stage.log"
    monkeypatch.setenv("AMADEUS_FAKE_STAGE_LOG", str(log))
    monkeypatch.setenv("AMADEUS_FAKE_STAGE_DELAY", "0.6")
    backend = JeolDriver(offline=True, calibration=PROCESS_CAL,
                          motion_module="tests.fake_tem3_motion")
    yield backend, log
    backend.close()


def _poll_until_final(backend, limit_s=10.0):
    import time as _t
    deadline = _t.monotonic() + limit_s
    polls = 0
    while _t.monotonic() < deadline:
        reply = backend.stage_poll({})
        if "_continue" not in reply:
            return reply, polls
        polls += 1
        _t.sleep(0.05)
    raise AssertionError("move never finished")


def test_process_motion_returns_at_once_and_settles_via_poll(jeol_process):
    import time as _t
    backend, log = jeol_process
    backend.move_stage({"x_m": 1e-7})            # first move also spawns the child
    _poll_until_final(backend)
    t0 = _t.monotonic()
    reply = backend.move_stage({"mode": "absolute", "x_m": 2e-6, "y_m": 1e-6})
    assert _t.monotonic() - t0 < 0.3             # the blocking setter isn't on this thread
    assert reply["_continue"]["cmd"] == "stage_poll"
    status = backend.get_stage_status({})        # the worker is free mid-move
    assert status["moving"] is True and status["motion"]["busy"] is not None
    final, polls = _poll_until_final(backend)
    assert final["settled"] is True and polls > 0
    assert log.read_text().splitlines()[-2:] == ["SetX 2000", "SetY 1000"]
    assert backend.get_stage_status({})["motion"]["busy"] is None


def test_process_motion_refuses_a_second_move_while_busy(jeol_process):
    from drivers.jeol.stage_motion import MotionError
    backend, _ = jeol_process
    backend.move_stage({"x_m": 1e-6})
    with pytest.raises(MotionError, match="still executing"):
        backend.move_stage({"x_m": 2e-6})
    _poll_until_final(backend)


def test_process_motion_finalize_reports_unsettled_and_keeps_running(jeol_process):
    backend, _ = jeol_process
    backend.move_stage({"x_m": 1e-6})
    reply = backend.stage_poll({"finalize": True})
    assert reply["settled"] is False and reply["moving"] is True
    assert "stop_stage" in reply["note"]
    final, _ = _poll_until_final(backend)
    assert final["settled"] is True


def test_process_motion_surfaces_vendor_errors(jeol_process):
    backend, _ = jeol_process
    backend.move_stage({"alpha_deg": 5.0})
    with pytest.raises(RuntimeError, match="tilt not supported"):
        _poll_until_final(backend)


def test_process_motion_restores_speed_after_settling(jeol_process):
    backend, _ = jeol_process
    backend.move_stage({"x_m": 1e-6, "speed": "slow"})
    assert backend.stage.speed_mode[0] == [0, 0, 0]       # active during the move
    _poll_until_final(backend)
    assert backend.stage.speed_mode[0] == [1, 1, 1]


def test_motion_mode_resolution():
    assert InstrumentCalibration().stage_motion(offline=False) == "process"
    assert InstrumentCalibration().stage_motion(offline=True) == "inline"
    cal = InstrumentCalibration({"stage": {"motion": "inline"}})
    assert cal.stage_motion(offline=False) == "inline"
    with pytest.raises(ValueError):
        InstrumentCalibration({"stage": {"motion": "thread"}}).stage_motion(False)


# ---------------------------------------------------------------------------
# Focus — DAC honesty + optional nm calibration
# ---------------------------------------------------------------------------

def test_get_focus_uncalibrated_reports_none_nm(jeol):
    focus = jeol.get_focus({})
    assert focus["focus_dac"] == 32768 and focus["vendor"]["OLf"] == 32768
    assert focus["focus_nm"] is None
    assert focus["calibrated"] is False


def test_set_focus_nm_requires_calibration(jeol):
    with pytest.raises(RuntimeError, match="not calibrated"):
        jeol.set_focus({"focus_nm": 100.0})


def test_set_focus_by_dac_always_works(jeol):
    focus = jeol.set_focus({"focus_dac": 40000})
    assert jeol.lens.olf == 40000
    assert focus["focus_dac"] == 40000


def test_calibrated_focus_round_trip(jeol_cal):
    focus = jeol_cal.set_focus({"focus_nm": 100.0})
    # dac = 100 * 2.0 + 30000
    assert jeol_cal.lens.olf == 30200
    assert focus["focus_nm"] == pytest.approx(100.0)
    assert focus["calibrated"] is True


def test_change_focus_delta_nm_uses_slope_only(jeol_cal):
    jeol_cal.lens.olf = 30000
    focus = jeol_cal.change_focus({"delta_nm": 50.0})
    assert jeol_cal.lens.olf == 30100      # +50nm * 2.0 dac/nm, no offset
    assert focus["focus_nm"] == pytest.approx(50.0)


def test_change_focus_delta_dac(jeol):
    jeol.lens.olf = 32768
    jeol.change_focus({"delta_dac": -768})
    assert jeol.lens.olf == 32000


def test_focus_dac_out_of_range_rejected(jeol):
    with pytest.raises(ValueError, match="outside the range 0-65535"):
        jeol.set_focus({"focus_dac": 70000})


# ---------------------------------------------------------------------------
# Deflectors / stigmators
# ---------------------------------------------------------------------------

def test_beam_shift_set_get_dac(jeol):
    jeol.set_beam_shift({"x": 33000, "y": 32000})
    shift = jeol.get_beam_shift({})
    assert shift["x"] == 33000 and shift["y"] == 32000 and shift["vendor"]["deflector"] == "CLA1"
    assert shift["unit"] == "dac" and shift["range"] == [0, 65535] and shift["neutral"] == 32768
    assert jeol.deflector.values["CLA1"] == [33000, 32000]


def test_beam_tilt_uses_cla2(jeol):
    jeol.set_beam_tilt({"x": 30000, "y": 35000})
    assert jeol.deflector.values["CLA2"] == [30000, 35000]


def test_deflector_roles_swappable_in_calibration():
    swapped = InstrumentCalibration(
        {"deflectors": {"beam_shift": "CLA2", "beam_tilt": "CLA1"}})
    backend = JeolDriver(offline=True, calibration=swapped)
    backend.set_beam_shift({"x": 1, "y": 2})
    assert backend.deflector.values["CLA2"] == [1, 2]


def test_beam_shift_validates_range_and_type(jeol):
    with pytest.raises(ValueError, match="outside the range 0-65535"):
        jeol.set_beam_shift({"x": -1, "y": 0})
    with pytest.raises(ValueError, match="must be a number"):
        jeol.set_beam_shift({"x": "wat", "y": 0})


def test_beam_shift_coerces_float_ints(jeol):
    jeol.set_beam_shift({"x": 33000.0, "y": 32000.4})
    assert jeol.deflector.values["CLA1"] == [33000, 32000]


def test_stigmator_names_map_to_def3_channels(jeol):
    jeol.set_stigmation({"stigmator": "objective", "x": 1, "y": 2})
    jeol.set_stigmation({"stigmator": "condenser", "x": 3, "y": 4})
    jeol.set_stigmation({"stigmator": "intermediate", "x": 5, "y": 6})
    assert jeol.deflector.values["OLs"] == [1, 2]
    assert jeol.deflector.values["CLs"] == [3, 4]
    assert jeol.deflector.values["ILs"] == [5, 6]
    stig = jeol.get_stigmation({})["stigmators"]
    assert stig["objective"] == {"x": 1, "y": 2} and stig["condenser"]["y"] == 4


def test_stigmation_rejects_numbers_and_unknown_names(jeol):
    with pytest.raises(ValueError, match="stigmator must be a name"):
        jeol.set_stigmation({"mode": 0, "x": 0, "y": 0})
    with pytest.raises(ValueError, match="unknown stigmator"):
        jeol.set_stigmation({"stigmator": "projector", "x": 0, "y": 0})


# ---------------------------------------------------------------------------
# Column state / apertures / modes
# ---------------------------------------------------------------------------

def test_beam_blank_bool_round_trip(jeol):
    assert jeol.set_beam_blank({"blanked": True}) == {"blanked": True}
    assert jeol.deflector.blank == 1
    assert jeol.set_beam_blank({"blanked": False}) == {"blanked": False}


def test_column_valves_map_to_feg_beam_valve(jeol):
    assert jeol.set_column_valves({"open": True}) == {"open": True}
    assert jeol.feg.valve == 1


def test_aperture_select_size_position(jeol):
    result = jeol.set_aperture({"kind": "OLA", "size": 2, "x": 1000, "y": 2000})
    assert result["kind"] == 2
    assert result["kind_name"] == "OLA"
    assert result["size"] == 2
    assert result["position"] == {"x": 1000, "y": 2000}
    # Sizes are per-kind
    assert jeol.get_aperture({"kind": "CLA"})["size"] == 0


def test_aperture_rejects_unknown_kind(jeol):
    with pytest.raises(ValueError, match="Unknown aperture"):
        jeol.set_aperture({"kind": "XYZ"})
    with pytest.raises(ValueError, match="0-6"):
        jeol.set_aperture({"kind": 9})


def test_instrument_mode_switch(jeol):
    assert jeol.get_instrument_mode({})["mode"] == "TEM"
    result = jeol.set_instrument_mode({"mode": "STEM"})
    assert result["mode"] == "STEM"
    assert result["operation_mode"] == "SCANNING"
    result = jeol.set_instrument_mode({"mode": "tem"})
    assert result["mode"] == "TEM"
    with pytest.raises(ValueError):
        jeol.set_instrument_mode({"mode": "SEM"})


def test_diffraction_function_mode_maps_operation_mode(jeol):
    jeol.set_function_mode({"mode": "DIFF"})
    assert jeol.get_instrument_mode({})["operation_mode"] == "DIFFRACTION"


def test_high_tension(jeol):
    ht = jeol.get_high_tension({})
    assert ht["ht_v"] == pytest.approx(200000.0)
    assert ht["high_tension_kV"] == pytest.approx(200.0)


def test_spot_size_range(jeol):
    assert jeol.set_spot_size({"spot_size": 3}) == {"spot_size": 3}
    with pytest.raises(ValueError):
        jeol.set_spot_size({"spot_size": 9})


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def test_snapshot_shape(jeol_cal):
    state = jeol_cal.get_microscope_state({})
    for key in ("high_tension_kV", "magnification", "mag_index", "focus_nm",
                "focus_dac", "spot_size", "beam_blanked", "column_valves_open",
                "beam_shift", "beam_tilt", "stigmation", "operation_mode",
                "tem_stem_mode", "stage", "microscope_name"):
        assert key in state, key
    assert state["operation_mode"] == "IMAGING"
    assert state["microscope_name"] == "test scope"
    assert state["stage"]["x_m"] == pytest.approx(0.0)
    assert "errors" not in state


def test_snapshot_survives_partial_failure(jeol):
    def boom():
        raise RuntimeError("no HT board")
    jeol.ht.GetHtValue = boom
    state = jeol.get_microscope_state({})
    assert "high_tension_kV" not in state
    assert "no HT board" in state["errors"]["high_tension_kV"]
    assert state["mag_index"] == 5   # everything else still read


# ---------------------------------------------------------------------------
# tem3_call escape hatch
# ---------------------------------------------------------------------------

def test_tem3_call_reflection(jeol):
    result = jeol.tem3_call({"class_name": "Stage3", "method": "GetPos", "args": []})
    assert result["result"] == [0.0, 0.0, 0.0, 0.0, 0.0]


def test_tem3_call_rejects_bad_targets(jeol):
    with pytest.raises(ValueError, match="class_name"):
        jeol.tem3_call({"class_name": "OsSystem", "method": "GetPos"})
    with pytest.raises(ValueError, match="Invalid method"):
        jeol.tem3_call({"class_name": "Stage3", "method": "_private"})
    with pytest.raises(ValueError, match="no method"):
        jeol.tem3_call({"class_name": "Stage3", "method": "FormatDisk"})


# ---------------------------------------------------------------------------
# Dispatcher contract
# ---------------------------------------------------------------------------

def test_dispatch_ok_and_error_envelopes(jeol):
    reply = dispatch(jeol, {"cmd": "get_focus", "params": {}, "request_id": "r1"})
    assert reply["status"] == "ok"
    assert reply["request_id"] == "r1"
    assert reply["data"]["focus_dac"] == 32768

    reply = dispatch(jeol, {"cmd": "warp_drive", "request_id": "r2"})
    assert reply["status"] == "error"
    assert "Unknown command" in reply["error"]


def test_dispatch_ping(jeol):
    reply = dispatch(jeol, {"cmd": "ping", "request_id": "r3"})
    assert reply["data"]["reply"] == "pong"
    assert reply["data"]["backend"] == "jeol"


def test_dispatch_converts_not_implemented():
    reply = dispatch(SimColumnDriver(), {"cmd": "vendor_call",
                                    "params": {"method": "Stage3.GetPos"},
                                    "request_id": "r4"})
    assert reply["status"] == "error"
    assert "not implemented" in reply["error"]


def test_jeol_only_extra_is_unknown_to_sim():
    reply = dispatch(SimColumnDriver(), {"cmd": "jeol.tem3_call", "params": {},
                                    "request_id": "r4b"})
    assert reply["status"] == "error" and "Unknown command" in reply["error"]


def test_dispatch_wraps_exceptions(jeol):
    reply = dispatch(jeol, {"cmd": "set_focus", "params": {}, "request_id": "r5"})
    assert reply["status"] == "error"
    assert "ValueError" in reply["error"]


# ---------------------------------------------------------------------------
# SimColumnDriver parity — every hardware command works end-to-end in the sim,
# except the JEOL-only escape hatch
# ---------------------------------------------------------------------------

SIM_PARAMS = {
    "set_focus": {"focus_dac": 33000},
    "change_focus": {"delta_dac": 10},
    "set_magnification": {"mag_index": 3},
    "set_beam_shift": {"x": 1, "y": 2},
    "set_beam_tilt": {"x": 1, "y": 2},
    "set_stigmation": {"stigmator": "objective", "x": 1, "y": 2},
    "set_beam_blank": {"blanked": True},
    "set_column_valves": {"open": True},
    "set_aperture": {"kind": 1, "size": 2},
    "move_stage": {"x_m": 1e-6},
    "set_spot_size": {"spot_size": 2},
    "set_alpha": {"alpha": 1},
    "set_instrument_mode": {"mode": "STEM"},
    "set_function_mode": {"mode": "MAG2"},
}


@pytest.mark.parametrize("cmd", sorted(HARDWARE_COMMANDS))
def test_sim_backend_covers_command(cmd):
    backend = SimColumnDriver()
    reply = dispatch(backend, {"cmd": cmd, "params": SIM_PARAMS.get(cmd, {}),
                               "request_id": "x"})
    assert reply["status"] == "ok", reply.get("error")


def test_sim_snapshot_matches_jeol_shape():
    sim_keys = set(SimColumnDriver().get_microscope_state({}))
    jeol_keys = set(JeolDriver(offline=True,
                                calibration=CALIBRATED).get_microscope_state({}))
    assert jeol_keys <= sim_keys | {"errors", "alpha", "calibrated"}


# ---------------------------------------------------------------------------
# Apt3.GetKind degradation (failed on the F200, 2026-10-26)
# ---------------------------------------------------------------------------

def _break_get_kind(backend):
    def boom():
        raise RuntimeError("GetKind not supported")
    backend.apt.GetKind = boom


def test_aperture_get_kind_failure_falls_back_to_last_selected(jeol):
    _break_get_kind(jeol)
    result = jeol.set_aperture({"kind": "OLA", "size": 1})
    assert result["kind"] == 2
    assert result["kind_source"] == "last_selected"
    assert result["size"] == 1
    assert jeol.get_aperture({"kind": "CLA"})["kind"] == 1


def test_aperture_get_kind_failure_without_selection_raises(jeol):
    _break_get_kind(jeol)
    with pytest.raises(RuntimeError, match="pass 'kind'"):
        jeol.get_aperture({})


def test_aperture_reports_instrument_kind_when_available(jeol):
    assert jeol.get_aperture({"kind": "SAA"})["kind_source"] == "instrument"


# ---------------------------------------------------------------------------
# Calibration from the config's instrument section (instrument profile)
# ---------------------------------------------------------------------------

INSTRUMENT = {"label": "JEOL F200 (test)",
              "focus": {"by_mode": {"TEM": {"lens": "focus.fine", "dac_per_nm": 2.0,
                                            "dac_at_zero": 30000}}}}


def test_build_backend_uses_inline_instrument_calibration():
    backend = build_driver("jeol", {"offline": True, "instrument": INSTRUMENT})
    assert backend.describe()["instrument"] == "JEOL F200 (test)"
    backend.set_focus({"focus_dac": 30200})
    focus = backend.get_focus({})
    assert focus["calibrated"] is True and focus["focus_nm"] == pytest.approx(100.0)


def test_calibration_file_layers_over_inline(tmp_path):
    path = tmp_path / "cal.json"
    path.write_text('{"label": "from file"}')
    cal = InstrumentCalibration.load(str(path), INSTRUMENT)
    assert cal.label == "from file"
    assert cal.focus_cal("TEM")["dac_per_nm"] == 2.0     # inline value survives


# ---------------------------------------------------------------------------
# Input validation found on the F200
# ---------------------------------------------------------------------------

class _TableEOS3(FakeEOS3):
    """Mirrors PyJEM offline: SetSelector stores any int, GetMagValue only
    resolves inside the table (offline it fails past the end)."""
    TABLE = 40

    def GetMagValue(self):
        if not 0 <= self.mag_index < self.TABLE:
            raise IndexError("mag index out of table")
        return [str(100 * (self.mag_index + 1)), "X", f"X{100 * (self.mag_index + 1)}"]


def _with_table(backend):
    eos = _TableEOS3()
    backend.eos = eos
    return backend, eos


def test_set_magnification_valid_index(jeol):
    backend, eos = _with_table(jeol)
    out = backend.set_magnification({"mag_index": 7})
    assert out["mag_index"] == 7 and out["magnification"] == 800.0


def test_set_magnification_past_table_end_is_restored_and_raises(jeol):
    backend, eos = _with_table(jeol)
    backend.set_magnification({"mag_index": 5})
    with pytest.raises(ValueError, match="mag_index 999 rejected.*restored index 5"):
        backend.set_magnification({"mag_index": 999})
    assert eos.mag_index == 5


@pytest.mark.parametrize("bad", [-1, 2.5, "x", True])
def test_set_magnification_rejects_bad_index_without_writing(jeol, bad):
    backend, eos = _with_table(jeol)
    eos.mag_index = 3
    with pytest.raises(ValueError):
        backend.set_magnification({"mag_index": bad})
    assert eos.mag_index == 3


def test_set_magnification_index_only_degrade_still_works(jeol):
    # Off-instrument GetMagValue can fail for *every* index; that must not be
    # mistaken for "past the end of the table".
    class NoValueEOS3(FakeEOS3):
        def GetMagValue(self):
            raise OSError("mag table resource not found")
    jeol.eos = NoValueEOS3()
    out = jeol.set_magnification({"mag_index": 12})
    assert out["mag_index"] == 12 and out["magnification"] is None


def test_set_magnification_instrument_clamps_is_reported(jeol):
    class ClampEOS3(_TableEOS3):
        def SetSelector(self, v):
            self.mag_index = min(int(v), self.TABLE - 1)
    jeol.eos = ClampEOS3()
    jeol.eos.mag_index = 2
    with pytest.raises(ValueError, match="reports index 39 instead"):
        jeol.set_magnification({"mag_index": 50})
    assert jeol.eos.mag_index == 2


@pytest.mark.parametrize("make", [
    lambda: JeolDriver(offline=True, calibration=CALIBRATED),
    lambda: SimColumnDriver(),
])
@pytest.mark.parametrize("cmd, params", [
    ("set_focus", {"focus_dac": 30000, "lens": "focus.coarse", "value": 30000}),
    ("set_focus", {"focus_nm": 10.0, "focus_dac": 30000}),
    ("set_focus", {}),
    ("change_focus", {"delta_dac": 5, "delta_nm": 5.0}),
    ("change_focus", {}),
])
def test_focus_requires_exactly_one_argument(make, cmd, params):
    backend = make()
    before = backend.get_focus({})
    with pytest.raises(ValueError, match="exactly one of"):
        getattr(backend, cmd)(params)
    assert backend.get_focus({}) == before


# ---------------------------------------------------------------------------
# Capabilities: derived from what a backend overrides, not a hand-kept list
# ---------------------------------------------------------------------------

def test_capabilities_list_only_overridden_operations():
    class Partial(ColumnDriver, StageDriver):
        kind = "partial"

        def get_focus(self, params):
            return {}

    caps = Partial().capabilities()
    assert caps["commands"] == ["get_focus"]
    assert caps["backend"] == "partial"


def test_sim_capabilities_omit_tem3_call_and_jeol_has_it(jeol):
    sim = dispatch(SimColumnDriver(), {"cmd": "get_hw_capabilities", "params": {}})
    assert sim["status"] == "ok"
    assert "jeol.tem3_call" not in sim["data"]["extras"]
    assert "move_stage" in sim["data"]["commands"]
    assert sim["data"]["vendor_call"] is False
    caps = jeol.capabilities()
    # The JEOL escape hatch is a namespaced extra, not a neutral command.
    assert "jeol.tem3_call" in caps["extras"] and caps["vendor_call"] is True
    assert "tem3_call" not in caps["commands"] and "piezo" in caps["features"]
    assert "capabilities" not in caps["commands"]
    assert caps["slots"] == ["column", "stage"] and caps["level"] == 2


# ---------------------------------------------------------------------------
# Vendor-neutral wire forms (multi-vendor plan, stage 1)
# ---------------------------------------------------------------------------

def test_focus_reports_lens_roles_unit_range_and_vendor_detail(jeol):
    jeol.set_focus({"lens": "focus.fine", "value": 32600})
    f = jeol.get_focus({})
    assert f["lens"] == "focus.fine" and f["focus_dac"] == 32600
    assert f["unit"] == "dac" and f["range"] == [0, 65535]
    assert f["lenses"]["focus.fine"] == {"value": 32600, "unit": "dac",
                                         "range": [0, 65535], "vendor_name": "OLf"}
    assert f["vendor"]["OLf"] == 32600 and "olf_dac" not in f


def test_set_focus_by_focus_dac_and_coarse_role(jeol):
    jeol.set_focus({"focus_dac": 31000})
    assert jeol.get_focus({})["vendor"]["OLf"] == 31000
    jeol.set_focus({"lens": "focus.coarse", "value": 9000})
    assert jeol.get_focus({})["vendor"]["OLc"] == 9000
    with pytest.raises(ValueError, match="unknown lens role"):
        jeol.set_focus({"lens": "focus.medium", "value": 1})
    with pytest.raises(ValueError, match="exactly one"):
        jeol.set_focus({"focus_dac": 1, "lens": "focus.fine", "value": 1})


def test_lens_roles_follow_the_calibration():
    cal = InstrumentCalibration({"lenses": {"focus.fine": "OLc", "focus.coarse": "OLf"}})
    b = JeolDriver(offline=True, calibration=cal)
    b.set_focus({"focus_dac": 12345})
    assert b.get_focus({})["vendor"]["OLc"] == 12345


def test_stigmator_by_name(jeol):
    out = jeol.set_stigmation({"stigmator": "condenser", "x": 30000, "y": 31000})
    assert out["stigmator"] == "condenser" and out["range"] == [0, 65535]
    got = jeol.get_stigmation({})
    assert got["stigmators"]["condenser"] == {"x": 30000, "y": 31000}
    assert "cond_x" not in got and "units" not in got
    jeol.set_stigmation({"stigmator": "Objective", "x": 1, "y": 2})          # any case
    assert jeol.get_stigmation({})["stigmators"]["objective"] == {"x": 1, "y": 2}
    with pytest.raises(ValueError, match="unknown stigmator"):
        jeol.set_stigmation({"stigmator": "projector", "x": 1, "y": 1})


def test_function_mode_by_name(jeol):
    jeol.eos.temstem = 0
    out = jeol.set_function_mode({"mode": "diff"})
    assert jeol.eos.funcmode == 4
    with pytest.raises(ValueError, match="unknown TEM function mode"):
        jeol.set_function_mode({"mode": "uuDIFF"})
    with pytest.raises(ValueError, match="must be a name"):
        jeol.set_function_mode({"mode": 2})
    jeol.set_function_mode({"mode": "LowMAG"})
    assert jeol.eos.funcmode == 2 and out["mode"] == "TEM"


def test_magnification_by_value_needs_a_table(jeol):
    with pytest.raises(ValueError, match="no magnification table"):
        jeol.set_magnification({"magnification": 25000})
    cal = InstrumentCalibration({"magnifications": {"TEM": {"*": [1000, 5000, 25000]}}})
    b = JeolDriver(offline=True, calibration=cal)
    b.eos.temstem = 0
    b.set_magnification({"magnification": 25000})
    assert b.eos.mag_index == 2
    with pytest.raises(ValueError, match="not in the TEM"):
        b.set_magnification({"magnification": 7000})
    assert "TEM/*" in b.capabilities()["magnification_by_value"]


def test_jeol_vendor_call_is_the_generic_tem3_form(jeol):
    out = jeol.vendor_call({"method": "Stage3.GetPos", "args": []})
    assert out["class_name"] == "Stage3" and out["method"] == "GetPos"
    with pytest.raises(ValueError, match="<TEM3 class>"):
        jeol.vendor_call({"method": "GetPos"})


def test_capabilities_list_value_vocabularies(jeol):
    caps = jeol.capabilities()
    assert caps["function_modes"]["TEM"][4] == "DIFF"
    assert caps["stigmators"] == ["objective", "condenser", "intermediate"]
    assert caps["lenses"]["focus.fine"]["vendor_name"] == "OLf"
    assert caps["deflectors"]["beam_shift"]["neutral"] == 32768


def test_sim_speaks_the_same_neutral_forms():
    sim = SimColumnDriver()
    sim.set_focus({"lens": "focus.coarse", "value": 7000})
    assert sim.get_focus({})["lenses"]["focus.coarse"]["value"] == 7000
    sim.set_stigmation({"stigmator": "intermediate", "x": 5, "y": 6})
    assert sim.get_stigmation({})["stigmators"]["intermediate"] == {"x": 5, "y": 6}
    sim.set_function_mode({"mode": "SAMAG"})
    assert sim.get_function_mode({})["function_mode"] == "SAMAG"
    sim.set_magnification({"magnification": 800})
    assert sim.get_magnification({})["magnification"] == 800
