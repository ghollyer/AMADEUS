"""Session-image list ordering and auto-selection in the operator console."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from client.operator_console import newest_new_image, sort_images
from client.state.models import ImageSidecar

T0 = datetime(2026, 11, 12, 12, 0, 0)


def _sc(image_id, name, minutes, shape=(64, 64)):
    return ImageSidecar(image_id=image_id, name=name, shape=list(shape), dtype="float32",
                        timestamp=T0 + timedelta(minutes=minutes))


# IDs deliberately sort in the opposite order to time (list_ids() is by filename).
IMAGES = [_sc("a", "zeta", 3, (512, 512)), _sc("b", "Alpha", 2, (64, 64)),
          _sc("c", "beta", 1, (1024, 1024))]


def test_default_sort_is_latest_first():
    assert [s.image_id for s in sort_images(IMAGES)] == ["a", "b", "c"]
    assert [s.image_id for s in sort_images(list(reversed(IMAGES)))] == ["a", "b", "c"]


def test_sort_by_name_case_insensitive_and_shape_by_pixel_count():
    assert [s.name for s in sort_images(IMAGES, "name", False)] == ["Alpha", "beta", "zeta"]
    assert [s.image_id for s in sort_images(IMAGES, "shape", True)] == ["c", "a", "b"]
    assert [s.image_id for s in sort_images(IMAGES, "time", False)] == ["c", "b", "a"]


def test_sort_handles_mixed_naive_and_aware_timestamps():
    aware = _sc("d", "aware", 0)
    aware.timestamp = datetime(2030, 1, 1, tzinfo=timezone.utc)
    assert sort_images(IMAGES + [aware])[0].image_id == "d"


def test_newest_new_image():
    assert newest_new_image(IMAGES, {"a", "b", "c"}) is None
    assert newest_new_image(IMAGES, {"a"}) == "b"
    assert newest_new_image(IMAGES, set()) == "a"


@pytest.fixture
def console(tmp_path):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display for Tk")
    root.withdraw()
    from client.operator_console import OperatorConsole
    app = OperatorConsole(root, "tcp://127.0.0.1:1", tmp_path, read_only=True)
    yield app
    root.destroy()


def test_console_selects_new_acquisition_and_keeps_sort(console):
    console._image_sidecars = []
    console.state = SimpleNamespace(images=SimpleNamespace(
        list_ids=lambda: [s.image_id for s in images],
        load_sidecar=lambda i: next(s for s in images if s.image_id == i)))
    console._select_image = lambda: None   # don't render .npy files that don't exist

    images = [IMAGES[1], IMAGES[2]]
    console._refresh_images()
    assert console.image_list.get_children() == ("b", "c")
    assert console.image_list.selection() == ("b",)   # first load: latest

    console.image_list.selection_set("c")
    console._refresh_images()                          # nothing new: selection kept
    assert console.image_list.selection() == ("c",)

    images.append(IMAGES[0])                           # a new acquisition arrives
    console._refresh_images()
    assert console.image_list.get_children() == ("a", "b", "c")
    assert console.image_list.selection() == ("a",)

    console._sort_images_by("name")                    # click "Name": A→Z
    assert console.image_list.get_children() == ("b", "c", "a")
    assert console.image_list.selection() == ("a",)
    assert console.image_list.heading("name")["text"].endswith("▲")
    console._sort_images_by("name")                    # click again: Z→A
    assert console.image_list.get_children() == ("a", "c", "b")


def test_stage_tab_motion_controls_send_options_and_stop_out_of_band():
    """Console Stage tab: speed/drive/approach/wait reach move_stage, piezo+Z
    is refused locally, and STOP STAGE goes on its own socket."""
    import concurrent.futures as cf
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display for Tk")
    root.withdraw()
    from client.gui.control_panel import ControlPanel
    calls = []

    class _Bridge:
        connected = True

        def _reply(self, kind, cmd, params, timeout_ms):
            calls.append((kind, cmd, dict(params), timeout_ms))
            fut = cf.Future()
            fut.set_result({"x_m": 0.0, "y_m": 0.0, "z_m": 0.0, "alpha_deg": 0.0,
                            "beta_deg": 0.0, "settled": True, "moving": False,
                            "drive": "motor", "piezo_nm": [0.0, 0.0]})
            return fut

        def call_data(self, cmd, params, timeout_ms=None):
            return self._reply("main", cmd, params, timeout_ms)

        def call_out_of_band(self, cmd, params, timeout_ms=None):
            return self._reply("oob", cmd, params, timeout_ms)

    try:
        panel = ControlPanel(root, _Bridge(), state=None)
        panel._stage_speed.set("slow")
        panel._stage_wait.set("30")
        panel._stage_jog("x_m", 1)
        panel._stage_drive.set("piezo")
        panel._stage_jog("z_m", 1)                  # refused: piezo is X/Y only
        panel._stage_stop()
        assert [(k, c) for k, c, _, _ in calls] == [("main", "move_stage"),
                                                   ("oob", "stop_stage")]
        params, timeout_ms = calls[0][2], calls[0][3]
        assert params["speed"] == "slow" and params["timeout_s"] == 30.0
        assert "drive" not in params and timeout_ms == 45_000
    finally:
        root.destroy()
