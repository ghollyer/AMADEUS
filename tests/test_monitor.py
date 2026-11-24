"""Tests for the telemetry/pause Monitor and its ZMQ-client integration."""

import json

import pytest

from client.monitor import Monitor, PausedError, PAUSE_MESSAGE


@pytest.fixture
def mon(tmp_path):
    return Monitor(tmp_path / "monitor", tmp_path / "images")


# ---------------------------------------------------------------------------
# Pause gate
# ---------------------------------------------------------------------------

class TestPauseGate:
    def test_not_paused_by_default(self, mon):
        assert mon.is_paused() is False
        mon.before_command("get_focus", {})  # must not raise

    def test_paused_vetoes_command(self, mon):
        mon.set_paused(True)
        with pytest.raises(PausedError, match="PAUSED"):
            mon.before_command("set_focus", {"focus_nm": 10.0})
        assert PAUSE_MESSAGE  # message is non-empty / importable

    def test_ping_exempt_from_pause(self, mon):
        mon.set_paused(True)
        mon.before_command("ping", {})  # health checks always allowed

    def test_resume_reenables_commands(self, mon):
        mon.set_paused(True)
        with pytest.raises(PausedError):
            mon.before_command("get_focus", {})
        mon.set_paused(False)
        mon.before_command("get_focus", {})  # no raise

    def test_rejected_command_recorded_in_history(self, mon):
        mon.set_paused(True)
        with pytest.raises(PausedError):
            mon.before_command("set_beam_blank", {"blanked": True})
        hist = mon.snapshot()["history"]
        assert hist[-1]["cmd"] == "set_beam_blank"
        assert hist[-1]["status"] == "rejected"

    def test_malformed_control_file_fails_safe(self, mon):
        (mon.dir / Monitor.CONTROL_FILE).write_text("{ not json", encoding="utf-8")
        assert mon.is_paused() is False  # do not wedge the agent


# ---------------------------------------------------------------------------
# Command lifecycle + history
# ---------------------------------------------------------------------------

class TestCommandLifecycle:
    def test_current_command_set_then_cleared(self, mon):
        mon.before_command("get_focus", {})
        assert mon.snapshot()["current_command"]["cmd"] == "get_focus"
        mon.after_command("get_focus", {"focus_nm": 1.0})
        assert mon.snapshot()["current_command"] is None

    def test_success_recorded(self, mon):
        mon.before_command("get_focus", {})
        mon.after_command("get_focus", {"focus_nm": 1.0})
        h = mon.snapshot()["history"][-1]
        assert h["cmd"] == "get_focus" and h["status"] == "ok" and h["error"] is None

    def test_error_recorded_and_dm_ok_false(self, mon):
        mon.before_command("get_focus", {})
        mon.after_command("get_focus", None, error="boom")
        snap = mon.snapshot()
        assert snap["history"][-1]["status"] == "error"
        assert snap["dm_ok"] is False

    def test_history_bounded(self, tmp_path):
        m = Monitor(tmp_path / "m", tmp_path / "i", history_len=5)
        for i in range(20):
            m.before_command("ping", {})
            m.after_command("ping", {})
        assert len(m.snapshot()["history"]) == 5


# ---------------------------------------------------------------------------
# Parameter ingestion
# ---------------------------------------------------------------------------

class TestParamIngestion:
    def _run(self, mon, cmd, params, data):
        mon.before_command(cmd, params)
        mon.after_command(cmd, data)
        return mon.snapshot()["params"]

    def test_microscope_state_populates_panel(self, mon):
        p = self._run(mon, "get_microscope_state", {}, {
            "magnification": 50000.0, "mag_index": 12, "focus_nm": -3.2,
            "spot_size": 4, "operation_mode": "SCANNING", "high_tension_kV": 200.0,
            "column_valves_open": True, "beam_blanked": False,
        })
        assert p["magnification"] == 50000.0
        assert p["operation_mode"] == "SCANNING"
        assert p["spot_size"] == 4
        assert p["column_valves_open"] is True

    def test_stage_position_ingested(self, mon):
        p = self._run(mon, "get_stage_position", {},
                      {"x_m": 1e-6, "y_m": 2e-6, "z_m": 0.0, "alpha_deg": 5.0, "beta_deg": 0.0})
        assert p["stage"]["x_m"] == 1e-6 and p["stage"]["alpha_deg"] == 5.0

    def test_detectors_and_active_from_set_detector(self, mon):
        self._run(mon, "get_detectors", {}, {"detectors": [{"index": 0, "name": "HAADF"},
                                                            {"index": 1, "name": "BF"}]})
        p = self._run(mon, "set_detector", {"signal_index": 1}, {"signal_index": 1, "name": "BF"})
        assert [d["name"] for d in p["detectors"]] == ["HAADF", "BF"]
        assert p["active_detector"] == {"index": 1, "name": "BF"}

    def test_set_command_intent_from_request(self, mon):
        # set_column_valves' response is sparse; intent comes from the request.
        p = self._run(mon, "set_column_valves", {"open": False}, {})
        assert p["column_valves_open"] is False

    def test_vacuum_is_na(self, mon):
        # No DM scripting API for vacuum -> stays None for the GUI to show n/a.
        assert mon.snapshot()["params"]["vacuum_state"] is None


# ---------------------------------------------------------------------------
# Snapshot file
# ---------------------------------------------------------------------------

class TestSnapshotFile:
    def test_snapshot_written_and_parseable(self, mon):
        mon.before_command("get_focus", {})
        mon.after_command("get_focus", {"focus_nm": 1.0})
        raw = json.loads((mon.dir / Monitor.SNAPSHOT_FILE).read_text(encoding="utf-8"))
        assert raw["params"]["focus_nm"] == 1.0
        assert raw["history"][-1]["cmd"] == "get_focus"
        assert "images_dir" in raw

    def test_long_script_param_trimmed(self, mon):
        mon.before_command("execute_script", {"script": "x" * 5000})
        cur = mon.snapshot()["current_command"]
        assert len(cur["params"]["script"]) < 5000


# ---------------------------------------------------------------------------
# Image rendering (client.gui.render) — numpy -> PPM, no Tk/display needed
# ---------------------------------------------------------------------------

class TestRenderPPM:
    def test_downsamples_and_collapses_cube(self, tmp_path):
        import numpy as np
        from client.gui.render import render_ppm
        # 3D cube exercises the >2D collapse + downsample + normalize path.
        arr = (np.random.rand(2000, 1500, 4) * 1000).astype(np.float32)
        np.save(tmp_path / "img.npy", arr)
        w, h = render_ppm(tmp_path / "img.npy", tmp_path / "out.ppm", box=440)
        assert w <= 440 and h <= 440
        header = (tmp_path / "out.ppm").read_bytes().split(b"\n", 1)[0]
        assert header.startswith(b"P6 ")        # valid PPM Tk can read
        assert header == f"P6 {w} {h} 255".encode()

    def test_flat_image_does_not_crash(self, tmp_path):
        import numpy as np
        from client.gui.render import render_ppm
        np.save(tmp_path / "flat.npy", np.zeros((50, 50), dtype=np.float32))
        w, h = render_ppm(tmp_path / "flat.npy", tmp_path / "out.ppm")
        assert (w, h) == (50, 50)


class TestFormatters:
    def test_mode_mapping(self):
        from client.gui.render import fmt_mode as _fmt_mode
        assert _fmt_mode("SCANNING \n").startswith("STEM")
        assert _fmt_mode("IMAGING").startswith("TEM")
        assert _fmt_mode(None) == "—"

    def test_unit_helpers(self):
        from client.gui.render import fmt_um as _um, fmt_deg as _deg
        assert _um(1.5e-6) == "1.500"
        assert _um(None) == "—"
        assert _deg(2.0) == "2.000"
