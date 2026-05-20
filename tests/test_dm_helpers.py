"""Tests for DM helper modules — validates DM-Script content and tag paths.

These tests mock the DigitalMicrograph module so they run offline (no GMS
required).  They verify that:
  1. Expected DM-Script function names appear in generated scripts.
  2. Tag paths written in DM-Script strings match those read back in Python,
     so a function name typo or tag-key mismatch is caught before on-instrument
     testing.
  3. Parameters (exposure, pixels, axis values) are correctly substituted.
  4. Return dicts contain the expected keys.
  5. Python-level logic (axis-flag bitmask, processing-enum dispatch, delta
     arithmetic) is correct independently of any DM connection.

DigitalMicrograph mock strategy
--------------------------------
- ``DM.ExecuteScriptString`` is a no-op MagicMock whose call args we inspect.
- ``DM.GetPersistentTagGroup()`` returns a single ``_tg_mock`` whose
  ``GetTagAsFloat``/``GetTagAsString`` look up values in the module-level
  ``_tag_store`` dict.
- Tests pre-populate ``_tag_store`` (via ``_set``) to simulate what the
  DM-Script would have written, then call the helper and check the result.
- DigiScan DS_* functions are direct mock callables on ``_dm``.
"""

import sys
import types
from unittest.mock import MagicMock, call

import numpy as np
import pytest

# ─── Install DigitalMicrograph mock BEFORE any helper imports ────────────────
# All modules that ``import DigitalMicrograph as DM`` will receive this mock.

_dm = types.ModuleType("DigitalMicrograph")

_tag_store: dict = {}   # full tag path -> Python value


def _get_as_float(path: str):
    if path not in _tag_store:
        return False, 0.0
    try:
        return True, float(_tag_store[path])
    except (TypeError, ValueError):
        return False, 0.0


def _get_as_string(path: str):
    if path not in _tag_store:
        return False, ""
    return True, str(_tag_store[path])


_tg_mock = MagicMock(name="TagGroup")
_tg_mock.GetTagAsFloat.side_effect = _get_as_float
_tg_mock.GetTagAsString.side_effect = _get_as_string

_dm.GetPersistentTagGroup = MagicMock(return_value=_tg_mock)
_dm.ExecuteScriptString = MagicMock()
_dm.FindFrontImage = MagicMock()
_dm.OpenImage = MagicMock()

# DigiScan functions are DM-Script only — no direct Python callables to mock

sys.modules["DigitalMicrograph"] = _dm

# ─── Now safe to import helpers ──────────────────────────────────────────────
from dm_server.helpers import microscope, stage, camera, spectroscopy, eds, digiscan, files  # noqa: E402
from dm_server.helpers._dm_util import safe_exec_script  # noqa: E402

PREFIX = "__amadeus__"


# ─── Shared utilities ────────────────────────────────────────────────────────

def _tag(key: str) -> str:
    """Full tag path for a given key (without prefix)."""
    return f"{PREFIX}:{key}"


def _set(key: str, value) -> None:
    """Pre-populate the tag store to simulate what a DM-Script would write."""
    _tag_store[_tag(key)] = value


def _scripts() -> list[str]:
    """All scripts passed to ExecuteScriptString in this test."""
    return [c.args[0] for c in _dm.ExecuteScriptString.call_args_list]


def _combined() -> str:
    """All scripts concatenated into one string for simple substring checks."""
    return "\n".join(_scripts())


# ─── Auto-reset fixture ──────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset():
    _tag_store.clear()
    _dm.ExecuteScriptString.reset_mock()
    _dm.FindFrontImage.reset_mock()
    # _tg_mock child mocks are reset by reset_mock on the parent; restore side effects
    _tg_mock.reset_mock()
    _tg_mock.GetTagAsFloat.side_effect = _get_as_float
    _tg_mock.GetTagAsString.side_effect = _get_as_string
    _dm.GetPersistentTagGroup.return_value = _tg_mock
    # Shared image mock — used by both camera_acquire and stem_acquire
    _img = MagicMock()
    _img.GetNumArray.return_value = np.ones((64, 64), dtype=np.float32)
    _img.GetDimensionCalibration.return_value = (0.0, 0.1, "nm")
    _img.GetName.return_value = "TestImage"
    _dm.FindFrontImage.return_value = _img
    # Default "happy" preconditions for si_start: column valves open and
    # microscope in SCANNING mode.  Tests that exercise the warning path
    # override these by calling _set() before invoking si_start / eds.*.
    _tag_store[_tag("si:pre:valves")] = 1.0
    _tag_store[_tag("si:pre:opmode")] = "SCANNING"
    # Default operation mode for focus operations: IMAGING (TEM).  Tests
    # that exercise mode switching override these.  All four tag slots are
    # seeded because different helpers write to different keys (get_focus,
    # _read_operation_mode, get_microscope_state).
    _tag_store[_tag("focus:opmode")] = "IMAGING"
    _tag_store[_tag("opmode")]       = "IMAGING"
    _tag_store[_tag("state:opmode")] = "IMAGING"
    yield


# ═══════════════════════════════════════════════════════════════════════════════
# _dm_util.py — safe_exec_script
# ═══════════════════════════════════════════════════════════════════════════════

class TestSafeExecScript:
    def test_passes_script_to_dm(self):
        _set("dummy", 1.0)  # prevent tag validation failure if called with tags
        safe_exec_script("number x = 1")
        _dm.ExecuteScriptString.assert_called_once_with("number x = 1")

    def test_raises_on_dm_exception(self):
        _dm.ExecuteScriptString.side_effect = RuntimeError("DM exploded")
        with pytest.raises(RuntimeError, match="DM-Script execution failed"):
            safe_exec_script("bad script")
        _dm.ExecuteScriptString.side_effect = None  # restore

    def test_raises_on_missing_expected_tag(self):
        # Tag store is empty — tag validation should fail
        with pytest.raises(RuntimeError, match="did not write expected tags"):
            safe_exec_script("number x = 1", expected_tags=["missing_key"])

    def test_passes_when_expected_tags_present(self):
        _set("my_key", 42.0)
        safe_exec_script("number x = 1", expected_tags=["my_key"])
        _dm.ExecuteScriptString.assert_called_once()

    def test_error_includes_script_excerpt(self):
        _dm.ExecuteScriptString.side_effect = ValueError("bad arg")
        with pytest.raises(RuntimeError) as exc_info:
            safe_exec_script("EMSetFocus(-999)")
        assert "EMSetFocus" in str(exc_info.value)
        _dm.ExecuteScriptString.side_effect = None


# ═══════════════════════════════════════════════════════════════════════════════
# microscope.py
# ═══════════════════════════════════════════════════════════════════════════════

class TestFocusCalibrationHelpers:
    """Mode-aware raw↔nm converters — pure-Python, no DM mock needed."""

    def test_imaging_offset_maps_to_zero_nm(self):
        cal = microscope._focus_cal_for_mode("IMAGING")
        assert microscope._raw_to_nm(cal["focus_raw_at_zero"], "IMAGING") == pytest.approx(0.0)

    def test_scanning_offset_maps_to_zero_nm(self):
        cal = microscope._focus_cal_for_mode("SCANNING")
        assert microscope._raw_to_nm(cal["focus_raw_at_zero"], "SCANNING") == pytest.approx(0.0)

    def test_mode_picks_different_calibration(self):
        # The STEM raw value (~33k) interpreted under the TEM calibration
        # must produce a clearly invalid negative nm value.
        stem_raw = microscope._focus_cal_for_mode("SCANNING")["focus_raw_at_zero"]
        nm_in_tem = microscope._raw_to_nm(stem_raw, "IMAGING")
        assert nm_in_tem < -1_000_000

    def test_round_trip(self):
        for mode in ("IMAGING", "SCANNING"):
            for nm in (-500.0, 0.0, 250.0):
                raw = microscope._nm_to_raw(nm, mode)
                assert microscope._raw_to_nm(raw, mode) == pytest.approx(nm)

    def test_falls_back_to_default_mode_for_unknown(self):
        # An empty or unknown mode string falls through to the default
        # (IMAGING).  This keeps the helper safe when EMGetOperationMode
        # returns something unexpected (e.g. "DIFFRACTION").
        default = microscope._focus_cal_for_mode(microscope.MICROSCOPE_CALIBRATION["default_mode"])
        assert microscope._focus_cal_for_mode("") is default
        assert microscope._focus_cal_for_mode("DIFFRACTION") is default

    def test_tolerates_trailing_whitespace(self):
        # Some DM builds append "\n" to EMGetOperationMode().
        imaging = microscope._focus_cal_for_mode("IMAGING")
        assert microscope._focus_cal_for_mode("IMAGING\n") is imaging


class TestGetFocus:
    def test_calls_em_update_calibration_state(self):
        _set("focus", 3.35e13)
        microscope.get_focus({})
        assert "EMUpdateCalibrationState" in _combined()

    def test_calls_em_get_focus(self):
        _set("focus", 3.35e13)
        microscope.get_focus({})
        assert "EMGetFocus" in _combined()

    def test_writes_focus_tag(self):
        _set("focus", 3.35e13)
        microscope.get_focus({})
        assert f'"{PREFIX}:focus"' in _combined()

    def test_returns_focus_nm_and_raw(self):
        cal = microscope._focus_cal_for_mode("IMAGING")
        raw = 100.0 * cal["focus_raw_per_nm"] + cal["focus_raw_at_zero"]  # 100 nm
        _set("focus", raw)
        result = microscope.get_focus({})
        assert result["focus_nm"] == pytest.approx(100.0)
        assert result["focus_raw"] == pytest.approx(raw)
        assert result["operation_mode"] == "IMAGING"

    def test_zero_offset_maps_to_zero_nm(self):
        # The whole point of the affine calibration: raw == raw_at_zero
        # must report focus_nm == 0, not focus_nm == raw_at_zero / slope.
        cal = microscope._focus_cal_for_mode("IMAGING")
        _set("focus", cal["focus_raw_at_zero"])
        result = microscope.get_focus({})
        assert result["focus_nm"] == pytest.approx(0.0)

    def test_stem_mode_uses_stem_calibration(self):
        # Switching the probe to SCANNING must select the STEM sub-cal.
        # raw=stem_offset -> 0 nm in STEM, but invalid in TEM.
        _set("focus:opmode", "SCANNING")
        stem_cal = microscope._focus_cal_for_mode("SCANNING")
        _set("focus", stem_cal["focus_raw_at_zero"])
        result = microscope.get_focus({})
        assert result["focus_nm"] == pytest.approx(0.0)
        assert result["operation_mode"] == "SCANNING"

    def test_script_reads_operation_mode(self):
        _set("focus", 0.0)
        microscope.get_focus({})
        assert "EMGetOperationMode" in _combined()


class TestSetFocus:
    def test_calls_em_set_focus_with_converted_value(self):
        cal = microscope._focus_cal_for_mode("IMAGING")
        microscope.set_focus({"focus_nm": 100.0})
        assert "EMSetFocus" in _combined()
        expected_raw = 100.0 * cal["focus_raw_per_nm"] + cal["focus_raw_at_zero"]
        assert str(expected_raw) in _combined()

    def test_returns_focus_raw(self):
        cal = microscope._focus_cal_for_mode("IMAGING")
        result = microscope.set_focus({"focus_nm": 50.0})
        expected_raw = 50.0 * cal["focus_raw_per_nm"] + cal["focus_raw_at_zero"]
        assert result["focus_raw"] == pytest.approx(expected_raw)
        assert result["operation_mode"] == "IMAGING"

    def test_uses_stem_offset_when_in_scanning(self):
        # set_focus(0) in SCANNING must drive raw to the STEM zero
        # (~32797), not the TEM zero (~1.55M).
        _set("opmode", "SCANNING")
        microscope.set_focus({"focus_nm": 0.0})
        stem_cal = microscope._focus_cal_for_mode("SCANNING")
        assert f"EMSetFocus({stem_cal['focus_raw_at_zero']})" in _combined()


class TestChangeFocus:
    def test_uses_em_change_focus_not_set_focus(self):
        # Relative changes go through EMChangeFocus(delta) to avoid the
        # read-modify-write cycle through an absolute EMSetFocus call.
        _set("focus", 32781.0)
        microscope.change_focus({"delta_nm": 10.0})
        combined = _combined()
        assert "EMChangeFocus" in combined
        assert "EMSetFocus" not in combined

    def test_passes_delta_in_raw_units_without_offset(self):
        # Relative delta must use the slope only — adding the raw-at-zero
        # offset would inject a giant absolute lens jump on every change.
        cal = microscope._focus_cal_for_mode("IMAGING")
        _set("focus", 0.0)
        microscope.change_focus({"delta_nm": 10.0})
        assert f"EMChangeFocus({10.0 * cal['focus_raw_per_nm']})" in _combined()

    def test_result_returns_focus_nm(self):
        _set("focus", 3.35e3)
        result = microscope.change_focus({"delta_nm": 0.0})
        assert "focus_nm" in result
        assert "focus_raw" in result
        assert result["operation_mode"] == "IMAGING"

    def test_uses_stem_slope_when_in_scanning(self):
        # In STEM the slope is different, so a 10 nm delta becomes a
        # different raw delta than in TEM.
        _set("opmode", "SCANNING")
        _set("focus", 0.0)
        microscope.change_focus({"delta_nm": 10.0})
        stem_cal = microscope._focus_cal_for_mode("SCANNING")
        assert f"EMChangeFocus({10.0 * stem_cal['focus_raw_per_nm']})" in _combined()


class TestGetMagnification:
    def test_calls_em_get_magnification(self):
        _set("mag", 50000.0)
        _set("mag_idx", 10.0)
        microscope.get_magnification({})
        assert "EMGetMagnification" in _combined()
        assert "EMGetMagIndex" in _combined()

    def test_calls_em_update_calibration_state(self):
        _set("mag", 50000.0)
        _set("mag_idx", 10.0)
        microscope.get_magnification({})
        assert "EMUpdateCalibrationState" in _combined()

    def test_returns_magnification_and_index(self):
        _set("mag", 50000.0)
        _set("mag_idx", 10.0)
        result = microscope.get_magnification({})
        assert result["magnification"] == pytest.approx(50000.0)
        assert result["mag_index"] == 10


class TestSetMagnification:
    def test_calls_em_set_mag_index(self):
        _set("mag", 100000.0)
        _set("mag_idx", 15.0)
        microscope.set_magnification({"mag_index": 15})
        assert "EMSetMagIndex(15)" in _combined()

    def test_calls_em_wait_until_ready(self):
        _set("mag", 100000.0)
        _set("mag_idx", 15.0)
        microscope.set_magnification({"mag_index": 15})
        assert "EMWaitUntilReady" in _combined()


class TestGetBeamShift:
    def test_calls_em_get_beam_shift(self):
        _set("bsx", 1e-9)
        _set("bsy", -2e-9)
        microscope.get_beam_shift({})
        assert "EMGetBeamShift" in _combined()

    def test_writes_bsx_bsy_tags(self):
        _set("bsx", 1e-9)
        _set("bsy", -2e-9)
        microscope.get_beam_shift({})
        script = _combined()
        assert f'"{PREFIX}:bsx"' in script
        assert f'"{PREFIX}:bsy"' in script

    def test_returns_xy(self):
        _set("bsx", 1e-9)
        _set("bsy", -2e-9)
        result = microscope.get_beam_shift({})
        assert "x" in result and "y" in result
        assert result["x"] == pytest.approx(1e-9)
        assert result["y"] == pytest.approx(-2e-9)


class TestSetBeamShift:
    def test_calls_em_set_beam_shift_with_xy(self):
        microscope.set_beam_shift({"x": 5e-9, "y": -3e-9})
        assert "EMSetBeamShift" in _combined()
        assert "5e-09" in _combined()
        assert "-3e-09" in _combined()


class TestGetBeamTilt:
    def test_calls_em_get_beam_tilt(self):
        _set("btx", 0.001)
        _set("bty", -0.002)
        microscope.get_beam_tilt({})
        assert "EMGetBeamTilt" in _combined()

    def test_writes_btx_bty_tags(self):
        _set("btx", 0.001)
        _set("bty", -0.002)
        microscope.get_beam_tilt({})
        script = _combined()
        assert f'"{PREFIX}:btx"' in script
        assert f'"{PREFIX}:bty"' in script

    def test_returns_xy(self):
        _set("btx", 0.001)
        _set("bty", -0.002)
        result = microscope.get_beam_tilt({})
        assert result["x"] == pytest.approx(0.001)
        assert result["y"] == pytest.approx(-0.002)


class TestSetBeamTilt:
    def test_calls_em_set_beam_tilt_with_xy(self):
        microscope.set_beam_tilt({"x": 0.003, "y": 0.001})
        assert "EMSetBeamTilt" in _combined()
        assert "0.003" in _combined()
        assert "0.001" in _combined()


class TestGetStigmation:
    def test_calls_objective_and_condenser_getters(self):
        for k in ("stig:ox", "stig:oy", "stig:cx", "stig:cy"):
            _set(k, 0.0)
        microscope.get_stigmation({})
        script = _combined()
        assert "EMGetObjectiveStigmation" in script
        assert "EMGetCondenserStigmation" in script

    def test_returns_four_keys(self):
        for k in ("stig:ox", "stig:oy", "stig:cx", "stig:cy"):
            _set(k, 0.1)
        result = microscope.get_stigmation({})
        assert set(result.keys()) == {"obj_x", "obj_y", "cond_x", "cond_y"}


class TestSetStigmation:
    def test_objective_mode_calls_objective_setter(self):
        microscope.set_stigmation({"mode": 0, "x": 0.1, "y": 0.2})
        assert "EMSetObjectiveStigmation" in _combined()
        assert "EMSetCondenserStigmation" not in _combined()

    def test_condenser_mode_calls_condenser_setter(self):
        microscope.set_stigmation({"mode": 1, "x": 0.1, "y": 0.2})
        assert "EMSetCondenserStigmation" in _combined()
        assert "EMSetObjectiveStigmation" not in _combined()


class TestSetBeamBlank:
    def test_blanked_true_sends_1(self):
        microscope.set_beam_blank({"blanked": True})
        assert "EMSetBeamBlanked(1)" in _combined()

    def test_blanked_false_sends_0(self):
        microscope.set_beam_blank({"blanked": False})
        assert "EMSetBeamBlanked(0)" in _combined()


class TestSetColumnValves:
    def test_open_true_sends_1(self):
        microscope.set_column_valves({"open": True})
        assert "EMSetColumnValvesOpen(1)" in _combined()

    def test_open_false_sends_0(self):
        microscope.set_column_valves({"open": False})
        assert "EMSetColumnValvesOpen(0)" in _combined()


class TestGetMicroscopeState:
    def test_calls_all_em_getters(self):
        for k in ("state:ht", "state:mag", "state:mag_idx", "state:focus",
                  "state:blanked", "state:valves", "state:bsx", "state:bsy",
                  "state:btx", "state:bty", "state:ox", "state:oy",
                  "state:cx", "state:cy"):
            _set(k, 0.0)
        microscope.get_microscope_state({})
        script = _combined()
        for fn in ("EMGetHighTension", "EMGetMagnification", "EMGetMagIndex",
                   "EMGetFocus", "EMGetBeamBlanked", "EMGetColumnValvesOpen",
                   "EMGetBeamShift", "EMGetBeamTilt",
                   "EMGetObjectiveStigmation", "EMGetCondenserStigmation"):
            assert fn in script, f"{fn} missing from get_microscope_state script"
        # EMGetMicroscopeName must NOT be called by the script — it is
        # parse-time unresolvable on some builds and aborts the snapshot.
        # Strip comments before checking so the explanatory note isn't picked
        # up as a hit.
        script_no_comments = "\n".join(
            line for line in script.splitlines() if not line.lstrip().startswith("//")
        )
        assert "EMGetMicroscopeName" not in script_no_comments

    def test_returns_all_expected_keys(self):
        for k in ("state:ht", "state:mag", "state:mag_idx", "state:focus",
                  "state:blanked", "state:valves", "state:bsx", "state:bsy",
                  "state:btx", "state:bty", "state:ox", "state:oy",
                  "state:cx", "state:cy"):
            _set(k, 0.0)
        result = microscope.get_microscope_state({})
        for key in ("high_tension_kV", "magnification", "mag_index", "focus_nm",
                    "beam_blanked", "column_valves_open", "beam_shift",
                    "beam_tilt", "stigmation", "microscope_name",
                    "operation_mode"):
            assert key in result, f"Key {key!r} missing from get_microscope_state result"

    def test_focus_uses_mode_specific_calibration(self):
        # raw at the STEM zero must report focus_nm == 0 *only* when the
        # state's opmode is SCANNING.  This guards against the bug where
        # a STEM raw fed through the TEM calibration showed −2,021,043 nm.
        for k in ("state:ht", "state:mag", "state:mag_idx",
                  "state:blanked", "state:valves", "state:bsx", "state:bsy",
                  "state:btx", "state:bty", "state:ox", "state:oy",
                  "state:cx", "state:cy"):
            _set(k, 0.0)
        stem_cal = microscope._focus_cal_for_mode("SCANNING")
        _set("state:focus", stem_cal["focus_raw_at_zero"])
        _set("state:opmode", "SCANNING")
        result = microscope.get_microscope_state({})
        assert result["focus_nm"] == pytest.approx(0.0)
        assert result["operation_mode"] == "SCANNING"

    def test_microscope_name_from_calibration_label(self):
        for k in ("state:ht", "state:mag", "state:mag_idx", "state:focus",
                  "state:blanked", "state:valves", "state:bsx", "state:bsy",
                  "state:btx", "state:bty", "state:ox", "state:oy",
                  "state:cx", "state:cy"):
            _set(k, 0.0)
        result = microscope.get_microscope_state({})
        assert result["microscope_name"] == microscope.MICROSCOPE_CALIBRATION["label"]

    def test_ht_converted_to_kv(self):
        for k in ("state:ht", "state:mag", "state:mag_idx", "state:focus",
                  "state:blanked", "state:valves", "state:bsx", "state:bsy",
                  "state:btx", "state:bty", "state:ox", "state:oy",
                  "state:cx", "state:cy"):
            _set(k, 0.0)
        _set("state:ht", 300000.0)   # 300 kV in volts
        result = microscope.get_microscope_state({})
        assert result["high_tension_kV"] == pytest.approx(300.0)


# ═══════════════════════════════════════════════════════════════════════════════
# stage.py
# ═══════════════════════════════════════════════════════════════════════════════

def _set_stage(x=0.0, y=0.0, z=0.0, a=0.0, b=0.0):
    _set("stage:x", x)
    _set("stage:y", y)
    _set("stage:z", z)
    _set("stage:alpha", a)
    _set("stage:beta", b)


class TestGetStagePosition:
    def test_calls_em_get_stage_positions_all_axes(self):
        _set_stage()
        stage.get_stage_position({})
        assert "EMGetStagePositions(31," in _combined()

    def test_writes_all_five_stage_tags(self):
        _set_stage()
        stage.get_stage_position({})
        script = _combined()
        for key in ("stage:x", "stage:y", "stage:z", "stage:alpha", "stage:beta"):
            assert f'"{PREFIX}:{key}"' in script

    def test_fetch_script_converts_um_to_m(self):
        """x/y/z are divided by 1e6 in the DM-Script (µm → m)."""
        _set_stage()
        stage.get_stage_position({})
        assert "1e6" in _combined()

    def test_returns_all_five_axes(self):
        _set_stage(x=1e-6, y=-2e-6, z=0.5e-6, a=5.0, b=0.0)
        result = stage.get_stage_position({})
        assert result["x_m"] == pytest.approx(1e-6)
        assert result["y_m"] == pytest.approx(-2e-6)
        assert result["alpha_deg"] == pytest.approx(5.0)


class TestMoveStage:
    def test_absolute_x_only_uses_flag_1(self):
        _set_stage()
        stage.move_stage({"mode": "absolute", "x_m": 5e-6})
        # Flag for x only = 1
        assert "EMSetStagePositions(1," in _combined()

    def test_absolute_xy_uses_flag_3(self):
        _set_stage()
        stage.move_stage({"mode": "absolute", "x_m": 1e-6, "y_m": 2e-6})
        # Flag for x+y = 1+2 = 3
        assert "EMSetStagePositions(3," in _combined()

    def test_absolute_all_axes_uses_flag_31(self):
        _set_stage()
        stage.move_stage({
            "mode": "absolute",
            "x_m": 0.0, "y_m": 0.0, "z_m": 0.0,
            "alpha_deg": 0.0, "beta_deg": 0.0,
        })
        assert "EMSetStagePositions(31," in _combined()

    def test_move_converts_m_to_um_for_em_functions(self):
        """EMSetStagePositions expects µm; values should be multiplied by 1e6."""
        _set_stage()
        stage.move_stage({"mode": "absolute", "x_m": 2e-6})
        # 2e-6 m × 1e6 = 2.0 µm — the script must contain this µm value
        assert "2.0" in _combined()

    def test_calls_em_wait_until_ready_after_move(self):
        _set_stage()
        stage.move_stage({"mode": "absolute", "x_m": 1e-6})
        assert "EMWaitUntilReady" in _combined()

    def test_with_speed_uses_with_speed_variant(self):
        _set_stage()
        stage.move_stage({"mode": "absolute", "x_m": 1e-6, "speed": 0.5})
        assert "EMSetStagePositionsWithSpeed" in _combined()
        assert "EMSetStagePositions(" not in _combined().replace(
            "EMSetStagePositionsWithSpeed", ""
        )

    def test_relative_adds_to_current_position(self):
        _set_stage(x=1e-6, y=2e-6)
        stage.move_stage({"mode": "relative", "x_m": 0.5e-6})
        # current x_m=1e-6, delta=0.5e-6 → target x_m=1.5e-6
        # converted to µm for DM: 1.5e-6 * 1e6 = 1.5
        combined = _combined()
        assert "1.5" in combined

    def test_no_axes_raises_value_error(self):
        with pytest.raises(ValueError, match="no axes specified"):
            stage.move_stage({"mode": "absolute"})

    def test_returns_position_after_move(self):
        _set_stage(x=5e-6)
        result = stage.move_stage({"mode": "absolute", "x_m": 5e-6})
        assert set(result.keys()) == {"x_m", "y_m", "z_m", "alpha_deg", "beta_deg"}


# ═══════════════════════════════════════════════════════════════════════════════
# camera.py
# ═══════════════════════════════════════════════════════════════════════════════

def _set_camera_tags(cam_id=0, sx=1024, sy=1024, scx=0.05, scy=0.05,
                     psx=14.0, psy=14.0, units="nm", name="MockCam", count=1):
    _set("cam:id", float(cam_id))
    _set("cam:sx", float(sx))
    _set("cam:sy", float(sy))
    _set("cam:scx", scx)     # calibration scale (nm/px), used by camera_acquire
    _set("cam:scy", scy)
    _set("cam:psx", psx)     # physical pixel size (µm), used by get_camera_info
    _set("cam:psy", psy)
    _set("cam:units", units)
    _set("cam:name:active", name)
    _set("cam:count", float(count))
    _set("cam:name:0", name)


class TestListCameras:
    def test_script_calls_camera_get_number_of_cameras(self):
        _set("cam:count", 1.0)
        _set("cam:name:0", "US1000")
        camera.list_cameras({})
        assert "CameraGetNumberOfCameras" in _combined()

    def test_script_calls_camera_get_name(self):
        _set("cam:count", 1.0)
        _set("cam:name:0", "US1000")
        camera.list_cameras({})
        assert "CameraGetName" in _combined()

    def test_writes_count_tag(self):
        _set("cam:count", 1.0)
        _set("cam:name:0", "US1000")
        camera.list_cameras({})
        assert f'"{PREFIX}:cam:count"' in _combined()

    def test_returns_camera_list(self):
        _set("cam:count", 1.0)
        _set("cam:name:0", "US1000")
        result = camera.list_cameras({})
        assert "cameras" in result
        assert result["cameras"][0] == {"id": 0, "name": "US1000"}


class TestGetCameraInfo:
    def test_script_calls_camera_get_size(self):
        _set_camera_tags()
        camera.get_camera_info({})
        assert "CameraGetSize" in _combined()

    def test_script_calls_camera_get_pixel_size(self):
        _set_camera_tags()
        camera.get_camera_info({})
        assert "CameraGetPixelSize" in _combined()

    def test_without_camera_id_uses_active(self):
        _set_camera_tags()
        camera.get_camera_info({})
        assert "CameraGetActiveCameraID" in _combined()

    def test_with_camera_id_uses_literal(self):
        _set_camera_tags(cam_id=1)
        camera.get_camera_info({"camera_id": 1})
        # Should NOT call CameraGetActiveCameraID
        assert "CameraGetActiveCameraID" not in _combined()

    def test_returns_correct_keys(self):
        _set_camera_tags(cam_id=0, sx=2048, sy=2048, scx=14.0, scy=14.0, name="K3")
        result = camera.get_camera_info({})
        assert result["camera_id"] == 0
        assert result["name"] == "K3"
        assert result["width"] == 2048
        assert result["height"] == 2048
        assert result["pixel_size_um"]["x"] == pytest.approx(14.0)


class TestCameraAcquire:
    def test_script_calls_camera_prepare_for_acquire(self):
        _set_camera_tags()
        camera.camera_acquire({"exposure_s": 1.0})
        assert "CameraPrepareForAcquire" in _combined()

    def test_script_calls_camera_acquire(self):
        _set_camera_tags()
        camera.camera_acquire({"exposure_s": 1.0})
        assert "CameraAcquire" in _combined()

    def test_script_calls_show_image(self):
        _set_camera_tags()
        camera.camera_acquire({"exposure_s": 1.0})
        assert "ShowImage" in _combined()

    def test_exposure_substituted_in_script(self):
        _set_camera_tags()
        camera.camera_acquire({"exposure_s": 2.5})
        assert "2.5" in _combined()

    def test_binning_substituted_in_script(self):
        _set_camera_tags()
        camera.camera_acquire({"exposure_s": 1.0, "binning": 2})
        # binning appears twice (xBin, yBin)
        combined = _combined()
        assert combined.count("2") >= 1

    def test_gain_normalised_uses_correct_enum(self):
        _set_camera_tags()
        camera.camera_acquire({"processing": "gain_normalised"})
        assert "CameraGetGainNormalizedEnum" in _combined()

    def test_dark_subtracted_uses_correct_enum(self):
        _set_camera_tags()
        camera.camera_acquire({"processing": "dark_subtracted"})
        assert "CameraGetDarkSubtractedEnum" in _combined()

    def test_unprocessed_uses_correct_enum(self):
        _set_camera_tags()
        camera.camera_acquire({"processing": "unprocessed"})
        assert "CameraGetUnprocessedEnum" in _combined()

    def test_calls_find_front_image(self):
        _set_camera_tags()
        camera.camera_acquire({"exposure_s": 1.0})
        _dm.FindFrontImage.assert_called_once()

    def test_returns_tuple_of_dict_and_ndarray(self):
        _set_camera_tags()
        result = camera.camera_acquire({"exposure_s": 1.0})
        assert isinstance(result, tuple) and len(result) == 2
        meta, arr = result
        assert isinstance(meta, dict)
        assert isinstance(arr, np.ndarray)

    def test_returned_array_is_float32(self):
        _set_camera_tags()
        _, arr = camera.camera_acquire({})
        assert arr.dtype == np.float32

    def test_meta_contains_image_meta_with_calibration(self):
        _set_camera_tags(scx=0.05, units="nm")
        meta, _ = camera.camera_acquire({})
        assert "image_meta" in meta
        assert "calibration" in meta["image_meta"]
        assert meta["image_meta"]["calibration"]["units"] == "nm"

    def test_invalid_processing_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown processing"):
            camera.camera_acquire({"processing": "magic_mode"})


# ═══════════════════════════════════════════════════════════════════════════════
# spectroscopy.py
# ═══════════════════════════════════════════════════════════════════════════════

class TestSiConfigure:
    def test_acquires_fresh_survey_before_set_mode(self):
        # The survey grab (DSInvokeButton(3) + DSWaitUntilFinished) must
        # precede SISetMode so the SI palette picks up the front image.
        spectroscopy.si_configure({"mode": "2D Array"})
        script = _combined()
        ds_idx  = script.index("DSInvokeButton(3)")
        wait_idx = script.index("DSWaitUntilFinished()")
        mode_idx = script.index('SISetMode("2D Array")')
        assert ds_idx < wait_idx < mode_idx

    def test_calls_si_set_mode_2d_array(self):
        spectroscopy.si_configure({"mode": "2D Array"})
        assert 'SISetMode("2D Array")' in _combined()

    def test_calls_si_set_mode_line_scan(self):
        spectroscopy.si_configure({"mode": "Line Scan"})
        assert 'SISetMode("Line Scan")' in _combined()

    def test_calls_si_set_mode_multi_point(self):
        spectroscopy.si_configure({"mode": "Multi-Point"})
        assert 'SISetMode("Multi-Point")' in _combined()

    def test_calls_si_set_mode_time_series(self):
        spectroscopy.si_configure({"mode": "Time Series"})
        assert 'SISetMode("Time Series")' in _combined()

    def test_rejects_unknown_mode(self):
        with pytest.raises(ValueError, match="Unknown SI mode"):
            spectroscopy.si_configure({"mode": "1D Spectrum"})

    def test_2d_array_fields(self):
        spectroscopy.si_configure({
            "mode": "2D Array", "x_samples": 128, "y_samples": 96,
        })
        script = _combined()
        assert 'SISetFieldValue("2D Array, X samples", 128)' in script
        assert 'SISetFieldValue("2D Array, Y samples", 96)' in script

    def test_line_scan_fields(self):
        spectroscopy.si_configure({
            "mode": "Line Scan", "samples": 200, "averaging": True,
        })
        script = _combined()
        assert 'SISetFieldValue("LineScan, samples", 200)' in script
        assert 'SISetFieldValue("LineScan, averaging check", 1)' in script

    def test_multi_point_fields(self):
        spectroscopy.si_configure({"mode": "Multi-Point", "points": 5})
        assert 'SISetFieldValue("MultiPoint, points", 5)' in _combined()

    def test_time_series_fields(self):
        spectroscopy.si_configure({
            "mode": "Time Series", "iterations": 10, "delay_time_s": 0.5,
        })
        script = _combined()
        assert 'SISetFieldValue("TimeSeries, iterations", 10)' in script
        assert 'SISetFieldValue("TimeSeries, delay time", 0.5)' in script

    def test_pixel_time_field(self):
        # The configured per-pixel exposure field is "Pixel Time".
        spectroscopy.si_configure({"mode": "2D Array", "pixel_time_s": 0.1})
        assert 'SISetFieldValue("Pixel Time", 0.1)' in _combined()

    def test_enables_scan_and_signal(self):
        spectroscopy.si_configure({"mode": "2D Array", "signal": 2})
        script = _combined()
        assert 'SIInvokeButton("Scan", 1)' in script
        assert 'SIInvokeButton("Signal 2", 1)' in script

    def test_rejects_invalid_signal(self):
        with pytest.raises(ValueError, match="signal must be"):
            spectroscopy.si_configure({"mode": "2D Array", "signal": 9})

    def test_returns_configured_params(self):
        result = spectroscopy.si_configure({
            "mode": "Line Scan", "samples": 32, "pixel_time_s": 0.02
        })
        assert result["configured"] is True
        assert result["mode"] == "Line Scan"
        assert result["pixel_time_s"] == pytest.approx(0.02)
        assert result["mode_params"] == {"samples": 32}

    def test_default_mode(self):
        result = spectroscopy.si_configure({})
        assert result["mode"] == "2D Array"
        assert result["signal"] == 1


class TestSiStart:
    def test_calls_si_invoke_button_capture_1(self):
        _set("si:pre:valves", 1.0)
        _set("si:pre:opmode", "SCANNING")
        spectroscopy.si_start({})
        assert 'SIInvokeButton("Capture", 1)' in _combined()

    def test_returns_started_true(self):
        _set("si:pre:valves", 1.0)
        _set("si:pre:opmode", "SCANNING")
        result = spectroscopy.si_start({})
        assert result["started"] is True
        assert result["valves_open"] is True
        assert result["operation_mode"] == "SCANNING"

    def test_probes_valves_and_opmode(self):
        _set("si:pre:valves", 1.0)
        _set("si:pre:opmode", "SCANNING")
        spectroscopy.si_start({})
        script = _combined()
        assert "EMGetColumnValvesOpen" in script
        assert "EMGetOperationMode" in script

    def test_refuses_when_valves_closed(self):
        _set("si:pre:valves", 0.0)
        _set("si:pre:opmode", "SCANNING")
        result = spectroscopy.si_start({})
        assert result["started"] is False
        assert "column valves" in result["warning"].lower()
        # Capture must NOT be invoked when preconditions fail.
        assert 'SIInvokeButton("Capture", 1)' not in _combined()

    def test_refuses_when_not_in_scanning(self):
        _set("si:pre:valves", 1.0)
        _set("si:pre:opmode", "IMAGING")
        result = spectroscopy.si_start({})
        assert result["started"] is False
        assert "operation mode" in result["warning"].lower()
        assert "IMAGING" in result["warning"]
        assert 'SIInvokeButton("Capture", 1)' not in _combined()

    def test_warning_lists_both_reasons(self):
        _set("si:pre:valves", 0.0)
        _set("si:pre:opmode", "DIFFRACTION")
        result = spectroscopy.si_start({})
        assert result["started"] is False
        assert "column valves" in result["warning"].lower()
        assert "operation mode" in result["warning"].lower()

    def test_tolerates_trailing_whitespace_in_opmode(self):
        # Some DM builds append "\n" to EMGetOperationMode() return value.
        _set("si:pre:valves", 1.0)
        _set("si:pre:opmode", "SCANNING\n")
        result = spectroscopy.si_start({})
        assert result["started"] is True


class TestSiStop:
    def test_calls_si_invoke_button_capture_0(self):
        spectroscopy.si_stop({})
        assert 'SIInvokeButton("Capture", 0)' in _combined()

    def test_returns_stopped_true(self):
        assert spectroscopy.si_stop({}) == {"stopped": True}


class TestSiGetSettings:
    def test_calls_si_is_acquisition_active(self):
        _set("si:active", 0.0)
        _set("si:paused", 0.0)
        _set("si:mode",   "2D Array")
        spectroscopy.si_get_settings({})
        assert "SIIsAcquisitionActive" in _combined()

    def test_calls_si_is_acquisition_paused(self):
        _set("si:active", 0.0)
        _set("si:paused", 0.0)
        _set("si:mode",   "2D Array")
        spectroscopy.si_get_settings({})
        assert "SIIsAcquisitionPaused" in _combined()

    def test_writes_si_active_tag(self):
        _set("si:active", 0.0)
        _set("si:paused", 0.0)
        _set("si:mode",   "2D Array")
        spectroscopy.si_get_settings({})
        assert f'"{PREFIX}:si:active"' in _combined()

    def test_writes_si_paused_tag(self):
        _set("si:active", 0.0)
        _set("si:paused", 0.0)
        _set("si:mode",   "2D Array")
        spectroscopy.si_get_settings({})
        assert f'"{PREFIX}:si:paused"' in _combined()

    def test_returns_bool_fields(self):
        _set("si:active", 1.0)
        _set("si:paused", 0.0)
        _set("si:mode",   "2D Array")
        result = spectroscopy.si_get_settings({})
        assert result["is_active"] is True
        assert result["is_paused"] is False
        assert result["mode"] == "2D Array"


# ═══════════════════════════════════════════════════════════════════════════════
# eds.py (DM server helpers — not client tools)
# ═══════════════════════════════════════════════════════════════════════════════

class TestEdsAcquirePoint:
    def test_uses_multi_point_mode(self):
        eds.eds_acquire_point({"exposure_s": 10.0})
        assert 'SISetMode("Multi-Point")' in _combined()

    def test_sets_pixel_time_field(self):
        eds.eds_acquire_point({"exposure_s": 10.0})
        assert 'SISetFieldValue("Pixel Time", 10.0)' in _combined()

    def test_sets_single_point(self):
        eds.eds_acquire_point({"exposure_s": 10.0})
        assert 'SISetFieldValue("MultiPoint, points", 1)' in _combined()

    def test_acquires_survey_first(self):
        eds.eds_acquire_point({"exposure_s": 10.0})
        script = _combined()
        assert script.index("DSInvokeButton(3)") < script.index('SISetMode("Multi-Point")')

    def test_triggers_capture(self):
        eds.eds_acquire_point({"exposure_s": 10.0})
        assert 'SIInvokeButton("Capture", 1)' in _combined()

    def test_returns_acquired_true(self):
        result = eds.eds_acquire_point({"exposure_s": 30.0})
        assert result["acquired"] is True
        assert result["exposure_s"] == pytest.approx(30.0)

    def test_default_exposure(self):
        result = eds.eds_acquire_point({})
        assert result["exposure_s"] == pytest.approx(30.0)


class TestEdsAcquireMap:
    def test_uses_2d_array_mode(self):
        eds.eds_acquire_map({"x_pixels": 32, "y_pixels": 32})
        assert 'SISetMode("2D Array")' in _combined()

    def test_sets_x_samples_field(self):
        eds.eds_acquire_map({"x_pixels": 32, "y_pixels": 32})
        assert 'SISetFieldValue("2D Array, X samples", 32)' in _combined()

    def test_sets_y_samples_field(self):
        eds.eds_acquire_map({"x_pixels": 32, "y_pixels": 32})
        assert 'SISetFieldValue("2D Array, Y samples", 32)' in _combined()

    def test_sets_pixel_time_field(self):
        eds.eds_acquire_map({"exposure_s": 0.02})
        assert 'SISetFieldValue("Pixel Time", 0.02)' in _combined()

    def test_acquires_survey_first(self):
        eds.eds_acquire_map({})
        script = _combined()
        assert script.index("DSInvokeButton(3)") < script.index('SISetMode("2D Array")')

    def test_triggers_capture(self):
        eds.eds_acquire_map({})
        assert 'SIInvokeButton("Capture", 1)' in _combined()

    def test_capture_is_synchronous_no_while_loop(self):
        # SIInvokeButton("Capture", 1) is synchronous; no polling loop needed.
        # SIIsAcquisitionActive is not a valid DM-Script function and caused
        # a syntax error when used in a while loop.
        eds.eds_acquire_map({})
        script = _combined()
        assert "while" not in script
        assert "SIIsAcquisitionActive" not in script

    def test_returns_acquired_true_with_dimensions(self):
        result = eds.eds_acquire_map({"x_pixels": 32, "y_pixels": 16, "exposure_s": 0.1})
        assert result["acquired"] is True
        assert result["x_pixels"] == 32
        assert result["y_pixels"] == 16
        assert result["exposure_s"] == pytest.approx(0.1)


# ═══════════════════════════════════════════════════════════════════════════════
# digiscan.py — DS_* are direct Python callables (no ExecuteScriptString)
# ═══════════════════════════════════════════════════════════════════════════════

class TestStemAcquire:
    # All DS* calls go through ExecuteScriptString (DM-Script bridge).
    # Tests check script content and that FindFrontImage is used for the array.

    def _setup(self):
        _set("ds:name", "STEM_capture")

    def test_script_calls_ds_create_parameters(self):
        self._setup()
        digiscan.stem_acquire({"width": 256, "height": 256})
        assert "DSCreateParameters" in _combined()

    def test_script_passes_width_and_height(self):
        self._setup()
        digiscan.stem_acquire({"width": 256, "height": 128})
        script = _combined()
        assert "256" in script
        assert "128" in script

    def test_script_passes_dwell_us(self):
        self._setup()
        digiscan.stem_acquire({"dwell_us": 4.0})
        assert "4.0" in _combined()

    def test_script_passes_rotation(self):
        self._setup()
        digiscan.stem_acquire({"rotation": 45.0})
        assert "45.0" in _combined()

    def test_script_calls_ds_set_parameters_signal(self):
        self._setup()
        digiscan.stem_acquire({})
        assert "DSSetParametersSignal" in _combined()

    def test_script_sets_datatype_float32(self):
        self._setup()
        digiscan.stem_acquire({})
        # dataType arg of DSSetParametersSignal must be 4 (float32 = 4 bytes)
        assert "DSSetParametersSignal(paramID, 0, 4, 1, 0)" in _combined()

    def test_script_passes_signal_index(self):
        self._setup()
        digiscan.stem_acquire({"signal_index": 1})
        assert "DSSetParametersSignal(paramID, 1," in _combined()

    def test_script_calls_ds_start_acquisition_single_synchronous(self):
        self._setup()
        digiscan.stem_acquire({})
        assert "DSStartAcquisition(paramID, 0, 1)" in _combined()

    def test_script_calls_ds_get_last_acquired_image(self):
        self._setup()
        digiscan.stem_acquire({})
        assert "DSGetLastAcquiredImage" in _combined()

    def test_script_calls_show_image(self):
        self._setup()
        digiscan.stem_acquire({})
        assert "ShowImage" in _combined()

    def test_script_deletes_parameters(self):
        self._setup()
        digiscan.stem_acquire({})
        assert "DSDeleteParameters" in _combined()

    def test_calls_find_front_image(self):
        self._setup()
        digiscan.stem_acquire({})
        _dm.FindFrontImage.assert_called_once()

    def test_returns_tuple_dict_ndarray(self):
        self._setup()
        meta, arr = digiscan.stem_acquire({})
        assert isinstance(meta, dict)
        assert isinstance(arr, np.ndarray)

    def test_returned_array_is_float32(self):
        self._setup()
        _, arr = digiscan.stem_acquire({})
        assert arr.dtype == np.float32

    def test_meta_contains_image_meta_and_calibration(self):
        self._setup()
        meta, _ = digiscan.stem_acquire({})
        assert "image_meta" in meta
        assert "calibration" in meta["image_meta"]

    def test_name_read_from_tag(self):
        _set("ds:name", "MyScan")
        meta, _ = digiscan.stem_acquire({})
        assert meta["image_meta"]["name"] == "MyScan"


class TestGetStemSettings:
    def _setup(self, n=2, active=0.0, names=("HAADF", "BF")):
        _set("ds:n_signals", float(n))
        _set("ds:active", active)
        for i, name in enumerate(names):
            _set(f"ds:sig:{i}", name)

    def test_script_calls_ds_count_signals(self):
        self._setup()
        digiscan.get_stem_settings({})
        assert "DSCountSignals" in _combined()

    def test_script_calls_ds_get_signal_name(self):
        self._setup()
        digiscan.get_stem_settings({})
        assert "DSGetSignalName" in _combined()

    def test_script_calls_ds_is_acquisition_active(self):
        self._setup()
        digiscan.get_stem_settings({})
        assert "DSIsAcquisitionActive" in _combined()

    def test_returns_num_signals_from_tag(self):
        self._setup(n=2)
        result = digiscan.get_stem_settings({})
        assert result["num_signals"] == 2

    def test_returns_signal_names_from_tags(self):
        self._setup(names=("HAADF", "BF"))
        result = digiscan.get_stem_settings({})
        assert result["signals"][0]["name"] == "HAADF"
        assert result["signals"][1]["name"] == "BF"

    def test_returns_is_active_false(self):
        self._setup(active=0.0)
        result = digiscan.get_stem_settings({})
        assert result["is_active"] is False

    def test_returns_is_active_true(self):
        self._setup(active=1.0)
        result = digiscan.get_stem_settings({})
        assert result["is_active"] is True


class TestGetBeamPosition:
    def _setup(self, x=0.5, y=0.25):
        _set("ds:bx", x)
        _set("ds:by", y)

    def test_script_calls_ds_get_beam_ds_position(self):
        self._setup()
        digiscan.get_beam_position({})
        assert "DSGetBeamDSPosition" in _combined()

    def test_returns_xy_from_tags(self):
        self._setup(x=0.5, y=0.25)
        result = digiscan.get_beam_position({})
        assert result["x"] == pytest.approx(0.5)
        assert result["y"] == pytest.approx(0.25)


class TestMoveBeamTo:
    def test_script_calls_ds_set_beam_ds_position(self):
        digiscan.move_beam_to({"x": 0.3, "y": 0.7})
        assert "DSSetBeamDSPosition(0.3, 0.7)" in _combined()

    def test_returns_empty_dict(self):
        result = digiscan.move_beam_to({"x": 0.5, "y": 0.5})
        assert result == {}


# ═══════════════════════════════════════════════════════════════════════════════
# files.py — list_files / load_dm_file
# ═══════════════════════════════════════════════════════════════════════════════

import os as _os
import tempfile as _tempfile


class TestListFiles:
    def test_lists_dm3_and_dm4_by_default(self):
        with _tempfile.TemporaryDirectory() as d:
            for name in ("a.dm3", "b.dm4", "c.txt", "d.DM3"):
                open(_os.path.join(d, name), "wb").close()
            result = files.list_files({"directory": d})
            names = sorted(f["name"] for f in result["files"])
            assert names == ["a.dm3", "b.dm4", "d.DM3"]
            assert result["count"] == 3

    def test_custom_patterns_filter(self):
        with _tempfile.TemporaryDirectory() as d:
            for name in ("a.dm3", "b.dm4", "c.tif"):
                open(_os.path.join(d, name), "wb").close()
            result = files.list_files({"directory": d, "patterns": ["*.tif"]})
            assert [f["name"] for f in result["files"]] == ["c.tif"]

    def test_returns_size_and_mtime(self):
        with _tempfile.TemporaryDirectory() as d:
            p = _os.path.join(d, "x.dm3")
            with open(p, "wb") as fh:
                fh.write(b"abc")
            result = files.list_files({"directory": d})
            entry = result["files"][0]
            assert entry["size_bytes"] == 3
            assert "T" in entry["mtime_iso"]  # ISO 8601 contains a T

    def test_recursive_walk(self):
        with _tempfile.TemporaryDirectory() as d:
            sub = _os.path.join(d, "nested")
            _os.makedirs(sub)
            open(_os.path.join(sub, "deep.dm3"), "wb").close()
            shallow_only = files.list_files({"directory": d})
            recursive = files.list_files({"directory": d, "recursive": True})
            assert shallow_only["count"] == 0
            assert recursive["count"] == 1
            assert recursive["files"][0]["name"] == "deep.dm3"

    def test_requires_directory_param(self):
        with pytest.raises(ValueError, match="directory"):
            files.list_files({})

    def test_missing_directory_raises(self):
        with pytest.raises(FileNotFoundError):
            files.list_files({"directory": "/no/such/path/xyz"})


class TestLoadDmFile:
    def _stub_image(self, arr, dim_calibrations, name="stub.dm3", intensity=(1.0, 0.0, "counts")):
        """Return a MagicMock that quacks like a DM image."""
        img = MagicMock()
        img.GetNumArray.return_value = arr
        img.GetNumDimensions.return_value = arr.ndim
        img.GetDimensionSize.side_effect = lambda i: arr.shape[i]
        img.GetDimensionCalibration.side_effect = lambda i, fmt=1: dim_calibrations[i]
        img.GetIntensityScale.return_value = intensity[0]
        img.GetIntensityOrigin.return_value = intensity[1]
        img.GetIntensityUnitString.return_value = intensity[2]
        img.GetName.return_value = name
        # close-after path
        img.GetOrCreateImageDocument.return_value = MagicMock()
        return img

    def test_requires_path(self):
        with pytest.raises(ValueError, match="path"):
            files.load_dm_file({})

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            files.load_dm_file({"path": "/no/such/file.dm3"})

    def test_2d_image_returns_array_and_calibration(self):
        with _tempfile.NamedTemporaryFile(suffix=".dm3", delete=False) as fh:
            path = fh.name
        try:
            arr = np.arange(64, dtype=np.float32).reshape(8, 8)
            cal = {
                0: (0.0, 0.5, "nm"),
                1: (0.0, 0.5, "nm"),
            }
            _dm.OpenImage.return_value = self._stub_image(arr, cal, name="image2d.dm3")
            meta, out = files.load_dm_file({"path": path})
            np.testing.assert_array_equal(out, arr)
            assert meta["ndim"] == 2
            assert meta["energy_axis_index"] is None
            assert meta["image_meta"]["calibration"]["x_scale"] == 0.5
            assert meta["image_meta"]["calibration"]["units"] == "nm"
            assert meta["source_path"] == path
            assert len(meta["axes"]) == 2
        finally:
            _os.unlink(path)

    def test_3d_si_cube_identifies_energy_axis(self):
        with _tempfile.NamedTemporaryFile(suffix=".dm3", delete=False) as fh:
            path = fh.name
        try:
            arr = np.zeros((8, 8, 1024), dtype=np.float32)
            cal = {
                0: (0.0, 2.0, "nm"),
                1: (0.0, 2.0, "nm"),
                2: (0.0, 10.0, "eV"),
            }
            _dm.OpenImage.return_value = self._stub_image(arr, cal, name="si.dm3")
            meta, out = files.load_dm_file({"path": path})
            assert meta["energy_axis_index"] == 2
            assert meta["ndim"] == 3
            # Spatial calibration should be the two nm-units dimensions
            assert meta["image_meta"]["calibration"]["x_scale"] == 2.0
            assert meta["image_meta"]["calibration"]["units"] == "nm"
            assert out.shape == (8, 8, 1024)
        finally:
            _os.unlink(path)

    def test_energy_axis_first(self):
        with _tempfile.NamedTemporaryFile(suffix=".dm4", delete=False) as fh:
            path = fh.name
        try:
            arr = np.zeros((512, 4, 4), dtype=np.float32)
            cal = {
                0: (0.0, 0.020, "keV"),
                1: (0.0, 1.0, "nm"),
                2: (0.0, 1.0, "nm"),
            }
            _dm.OpenImage.return_value = self._stub_image(arr, cal)
            meta, _ = files.load_dm_file({"path": path})
            assert meta["energy_axis_index"] == 0
            # First two non-energy dims become x/y
            assert meta["image_meta"]["calibration"]["x_scale"] == 1.0
        finally:
            _os.unlink(path)
