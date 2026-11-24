"""Multi-vendor architecture: drivers, routing, devices, policy, recovery,
driver hosts. Ports 29920-29939."""

import asyncio
import json
import os
import signal
import sys
import threading
import time

import numpy as np
import pytest
import zmq

from client.confirm import OperatorDeclined
from client.transport.zmq_client import ZMQClient
from common.commands import COMMAND_SLOT, SLOT_COMMANDS
from drivers.base import SLOT_CLASSES, ColumnDriver, Driver, ImageResult, support_level
from drivers.dispatch import call
from drivers.registry import build_driver
from relay.config import RelayConfig
from relay.policy import Policy, PolicyError
from relay.recovery import RequestLedger
from relay.routing import RoutingError, RoutingTable
from relay.server import Relay

SIM_ALL = ["column", "stage", "scan", "camera", "spectrometer", "files"]


# ---------------------------------------------------------------------------
# Driver interfaces
# ---------------------------------------------------------------------------

def test_slot_classes_match_the_wire_command_tables():
    for slot, cls in SLOT_CLASSES.items():
        methods = {n for n, v in vars(cls).items() if callable(v) and not n.startswith("_")}
        assert methods == set(SLOT_COMMANDS[slot]), slot


def test_partial_driver_reports_only_what_it_overrides():
    class Tiny(ColumnDriver):
        kind = "tiny"

        def get_focus(self, params):
            return {"focus_dac": 1}

    caps = Tiny().capabilities()
    assert caps["commands"] == ["get_focus"] and caps["slots"] == ["column"]
    assert caps["level"] == 1 and caps["vendor_call"] is False
    reply = call(Tiny(), {"cmd": "set_focus", "params": {}, "request_id": "r"})
    assert reply["status"] == "error" and "not implemented" in reply["error"]


def test_support_levels():
    assert support_level([]) == 0
    assert support_level(["get_focus"]) == 1
    assert support_level(["get_focus", "move_stage"]) == 2
    assert support_level(["camera_acquire"]) == 3
    assert support_level(["si_start"]) == 4


def test_image_result_checks_its_payload_size():
    with pytest.raises(ValueError, match="needs 64"):
        ImageResult({}, b"\0" * 10, [4, 4], "float32")


def test_sim_driver_fills_every_acquisition_slot():
    sim = build_driver("sim", {})
    caps = sim.capabilities()
    assert set(SIM_ALL) <= set(caps["slots"])
    assert {d["id"] for d in caps["devices"]} >= {"detector/0", "camera/1", "spectrometer/0"}
    sim.set_instrument_mode({"mode": "STEM"})
    meta, payload = call(sim, {"cmd": "stem_acquire", "params": {"device": "detector/0",
                                                                 "width": 64, "height": 32}})
    assert meta["image_meta"]["shape"] == [32, 64] and meta["data"]["detector"] == "HAADF"
    assert meta["image_meta"]["calibration"]["units"] == "nm"
    arr = np.frombuffer(payload, dtype=np.float32).reshape(32, 64)
    assert np.isfinite(arr).all()
    files = sim.list_files({})["files"]
    assert files and isinstance(call(sim, {"cmd": "load_file",
                                           "params": {"path": files[0]["path"]}}), tuple)


def test_sim_driver_picks_devices_by_device_only():
    # Pre-1.0 index arguments (signal_index, camera_id) are not read any more.
    sim = build_driver("sim", {})
    assert sim.set_detector({"signal_index": 1})["signal_index"] == 0
    assert sim.set_detector({"device": "detector/1"})["signal_index"] == 1
    assert sim.get_camera_info({"camera_id": 1})["camera_id"] == 0
    assert sim.get_camera_info({"device": "camera/1"})["camera_id"] == 1


def test_sim_image_follows_the_stage():
    sim = build_driver("sim", {"options": {"seed": 3}})
    sim.set_instrument_mode({"mode": "STEM"})
    sim.set_magnification({"mag_index": 9})              # small field: few particles

    def frame():
        res = sim.stem_acquire({"width": 96, "height": 96, "dwell_us": 1e4})
        return np.frombuffer(res.payload, dtype=np.float32).reshape(96, 96)

    a = frame()
    sim.move_stage({"x_m": 2e-6, "mode": "relative"})
    b = frame()
    sim.move_stage({"x_m": -2e-6, "mode": "relative"})
    c = frame()
    assert np.corrcoef(a.ravel(), c.ravel())[0, 1] > 0.9         # same place, same image
    assert np.corrcoef(a.ravel(), b.ravel())[0, 1] < 0.5         # moved: different image


def test_dm_adapter_translates_neutral_requests():
    from drivers import dm
    assert dm.translate("list_files", {"directory": "C:/"}) == ("list_dm_files",
                                                                 {"directory": "C:/"})
    assert dm.translate("camera_acquire", {"device": "camera/1"}) == \
        ("camera_acquire", {"camera_id": 1})
    assert dm.translate("stem_acquire", {"device": "detector/2"})[1]["signal_index"] == 2
    assert dm.translate("dm.list_windows", {}) == ("list_windows", {})
    assert dm.translate("vendor_call", {"method": "execute_script",
                                        "args": {"script": "x"}}) == \
        ("execute_script", {"script": "x"})
    with pytest.raises(ValueError):
        dm.translate("camera_acquire", {"device": "laser/0"})


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

DRIVERS = {
    "col": {"kind": "jeol", "slots": ["column", "stage"]},
    "gatan": {"kind": "dm", "host": "tcp://x:1", "slots": ["scan", "camera", "live", "files"]},
    "tvips": {"kind": "remote", "host": "tcp://x:2", "slots": ["camera"]},
    "bruker": {"kind": "remote", "host": "tcp://x:3", "slots": ["spectrometer"]},
}
ROLES = {"detectors": {"HAADF": 1}, "cameras": {"imaging": 0, "diffraction": "tvips/camera/0"},
         "spectrometers": {"eds": "bruker/spectrometer/0"}}


def test_every_routed_command_has_a_slot():
    assert set(COMMAND_SLOT) == set().union(*SLOT_COMMANDS.values())


def test_routing_by_slot_device_role_kind_and_alias():
    rt = RoutingTable(DRIVERS, ROLES)
    assert rt.resolve("set_focus")[0] == "col"
    assert rt.resolve("camera_acquire")[0] == "gatan"                 # first holder
    assert rt.resolve("camera_acquire", {"device": "tvips/camera/0"})[:2] == \
        ("tvips", "camera_acquire")
    assert rt.resolve("camera_acquire", {"device": "camera:diffraction"})[2]["device"] == \
        "camera/0"
    name, _, params = rt.resolve("stem_acquire", {"device": "detector:HAADF"})
    assert (name, params["device"]) == ("gatan", "detector/1")
    assert rt.resolve("eds_acquire_point", {"device": "spectrometer:eds"})[0] == "bruker"
    # The console's short form "<kind>/<n>": that device on the slot's first driver.
    assert rt.resolve("set_detector", {"device": "detector/1"})[:3] == \
        ("gatan", "set_detector", {"device": "detector/1"})
    assert rt.resolve("camera_acquire", {"device": "camera/1"})[0] == "gatan"
    assert rt.resolve("dm.execute_script", {"script": "x"})[:2] == ("gatan", "dm.execute_script")
    assert rt.resolve("jeol.tem3_call")[:2] == ("col", "jeol.tem3_call")
    with pytest.raises(RoutingError, match="unknown command 'execute_script'"):
        rt.resolve("execute_script")                         # pre-1.0 names are gone
    assert rt.resolve("vendor_call", {"kind": "jeol", "method": "EOS3.GetSpotSize"})[0] == "col"
    assert rt.resolve("vendor_call", {"driver": "tvips", "method": "x"})[0] == "tvips"


def test_routing_errors_name_what_is_configured():
    rt = RoutingTable(DRIVERS, ROLES)
    with pytest.raises(RoutingError, match="unknown command"):
        rt.resolve("warp")
    with pytest.raises(RoutingError, match="no camera role 'eels'"):
        rt.resolve("camera_acquire", {"device": "camera:eels"})
    with pytest.raises(RoutingError, match="doesn't hold the spectrometer slot"):
        rt.resolve("si_start", {"device": "gatan/spectrometer/0"})
    with pytest.raises(RoutingError, match="no driver on this microscope holds"):
        RoutingTable({"col": DRIVERS["col"]}).resolve("stem_acquire")
    with pytest.raises(RoutingError, match="needs a 'nion' driver"):
        rt.resolve("nion.thing")


# ---------------------------------------------------------------------------
# Policy and recovery helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def test_policy_confirm_and_stage_limits():
    rt = RoutingTable(DRIVERS, ROLES)
    pol = Policy({"stage_limits": {"x_m": [-1e-4, 1e-4]}, "confirm": ["set_column_valves"]},
                 [], rt)
    with pytest.raises(PolicyError, match="operator's go-ahead"):
        _run(pol.check("col", "set_column_valves", {"open": True}))
    assert _run(pol.check("col", "set_column_valves", {"open": True, "confirmed": True})) \
        == {"open": True}                                       # confirmed is stripped
    with pytest.raises(PolicyError, match="outside this microscope's limits"):
        _run(pol.check("col", "move_stage", {"x_m": 2e-4}))

    async def pos():
        return {"x_m": 9e-5}
    with pytest.raises(PolicyError, match="target 0.00015"):
        _run(pol.check("col", "move_stage", {"x_m": 6e-5, "mode": "relative"}, pos))
    _run(pol.check("col", "move_stage", {"x_m": -6e-5, "mode": "relative"}, pos))


def test_policy_exclusive_cameras_need_confirmation_to_switch():
    rt = RoutingTable(DRIVERS, ROLES)
    pol = Policy({}, [["camera:imaging", "camera:diffraction"]], rt)
    _run(pol.check("gatan", "camera_acquire", {"device": "camera/0"}))
    _run(pol.check("gatan", "camera_acquire", {"device": "camera/0"}))    # same one: fine
    with pytest.raises(PolicyError, match="exclusive group"):
        _run(pol.check("tvips", "camera_acquire", {"device": "camera/0"}))
    _run(pol.check("tvips", "camera_acquire", {"device": "camera/0", "confirmed": True}))
    assert pol.active[0] == "tvips/camera/0"
    with pytest.raises(PolicyError):
        _run(pol.check("gatan", "camera_acquire", {"device": "camera/0"}))


def test_request_ledger_returns_the_first_reply():
    async def go():
        ledger = RequestLedger()
        assert await ledger.lookup("r1") is None
        ledger.begin("r1")
        waiter = asyncio.create_task(ledger.lookup("r1"))
        await asyncio.sleep(0)
        ledger.finish("r1", [b"reply"])
        assert await waiter == [b"reply"]
        assert await ledger.lookup("r1") == [b"reply"]
        assert await ledger.lookup("") is None
    asyncio.run(go())


# ---------------------------------------------------------------------------
# A relay running drivers
# ---------------------------------------------------------------------------

async def _relay(port: int, drivers: dict, **kw):
    cfg = RelayConfig(listen=f"tcp://127.0.0.1:{port}", drivers=drivers, timeout_ms=5000, **kw)
    r = Relay(cfg)
    task = asyncio.create_task(r.serve())
    await asyncio.sleep(0.15)
    client = ZMQClient(f"tcp://127.0.0.1:{port}", timeout_ms=15000)
    await client.connect()
    return r, task, client


async def _close(r, task, client):
    await client.disconnect()
    r.stop()
    await asyncio.wait_for(task, timeout=10)


async def test_sim_driver_runs_the_whole_stack_without_dm():
    r, task, client = await _relay(29920, {"sim": {"kind": "sim", "slots": SIM_ALL}})
    try:
        await client.send_command("set_instrument_mode", {"mode": "STEM"})
        meta, arr = await client.send_command_image(
            "stem_acquire", {"device": "sim/detector/1", "width": 32, "height": 32})
        assert arr.shape == (32, 32) and meta["data"]["detector"] == "BF"
        meta, arr = await client.send_command_image("camera_acquire", {"exposure_s": 0.1})
        assert arr.shape == (512, 512)
        caps = await client.send_command("get_capabilities")
        sim = caps["drivers"]["sim"]
        assert sim["ok"] and "stem_acquire" in sim["commands"] and sim["level"] == 4
        assert "sim/camera/1" in {d["id"] for d in caps["devices"]}
        assert caps["routing"]["scan"] == ["sim"]
        assert "hw" not in caps and "dm" not in caps    # no pre-1.0 manifest keys
    finally:
        await _close(r, task, client)


def _mock_dm(port: int, ready, stop, seen, errors=()):
    ctx = zmq.Context()
    sock = ctx.socket(zmq.REP)
    sock.bind(f"tcp://127.0.0.1:{port}")
    sock.setsockopt(zmq.RCVTIMEO, 200)
    ready.set()
    while not stop.is_set():
        try:
            req = json.loads(sock.recv())
        except zmq.Again:
            continue
        seen.append((req["cmd"], req.get("params")))
        if req["cmd"] in errors:
            sock.send_json({"status": "error", "error": "Command is not yet implemented "
                            "on this instrument.", "request_id": req.get("request_id")})
            continue
        if req["cmd"] == "list_cameras":
            data = {"cameras": [{"id": 0, "name": "OneView"}]}
        elif req["cmd"] == "get_detectors":
            data = {"detectors": [{"index": 0, "name": "HAADF"}]}
        else:
            data = {"backend": "dm", "cmd": req["cmd"], "params": req.get("params")}
        sock.send_json({"status": "ok", "data": data, "request_id": req.get("request_id")})
    sock.close()
    ctx.term()


@pytest.fixture
def mock_dm():
    stop, ready, seen = threading.Event(), threading.Event(), []
    t = threading.Thread(target=_mock_dm, args=(29921, ready, stop, seen), daemon=True)
    t.start()
    ready.wait(3)
    yield seen
    stop.set()
    t.join(3)


async def test_devices_from_two_vendors_in_one_slot(mock_dm):
    drivers = {"sim": {"kind": "sim", "slots": ["column", "stage", "camera"]},
               "gatan": {"kind": "dm", "host": "tcp://127.0.0.1:29921",
                         "slots": ["camera", "scan"]}}
    roles = {"cameras": {"imaging": "gatan/camera/0", "diffraction": "sim/camera/1"}}
    r, task, client = await _relay(29922, drivers, roles=roles,
                                   exclusive=[["camera:imaging", "camera:diffraction"]])
    try:
        out = await client.send_command("get_camera_info", {"device": "camera:imaging"})
        assert out["cmd"] == "get_camera_info" and out["params"] == {"camera_id": 0}
        asked = []

        async def operator(req):                # the operator's answer
            asked.append(req)
            return len(asked) > 1               # Deny first, then Allow
        client.set_confirmer(operator)
        with pytest.raises(OperatorDeclined, match="exclusive group"):
            await client.send_command_image("camera_acquire",
                                            {"device": "camera:diffraction"})
        meta, arr = await client.send_command_image(
            "camera_acquire", {"device": "camera:diffraction", "exposure_s": 0.1})
        assert meta["data"]["camera_id"] == 1 and arr.shape == (256, 256)
        assert [a.cmd for a in asked] == ["camera_acquire", "camera_acquire"]
        # DM-only extras and old names reach DM, translated
        await client.send_command("dm.execute_script", {"script": "1"})
        await client.send_command("dm.list_windows")
        assert ("execute_script", {"script": "1"}) in mock_dm
        assert ("list_windows", {}) in mock_dm
        caps = await client.send_command("get_capabilities")
        assert {"gatan/camera/0", "sim/camera/0", "gatan/detector/0"} <= \
            {d["id"] for d in caps["devices"]}
        assert caps["drivers"]["gatan"]["commands"] == sorted(
            SLOT_COMMANDS["camera"] | SLOT_COMMANDS["scan"])
    finally:
        await _close(r, task, client)


async def test_resent_request_id_runs_once(mock_dm):
    r, task, client = await _relay(
        29923, {"gatan": {"kind": "dm", "host": "tcp://127.0.0.1:29921", "slots": ["scan"]}})
    ctx = zmq.asyncio.Context.instance()
    sock = ctx.socket(zmq.REQ)
    sock.setsockopt(zmq.LINGER, 0)
    sock.connect("tcp://127.0.0.1:29923")
    try:
        request = json.dumps({"cmd": "move_beam_to", "params": {"x": 0.1, "y": 0.2},
                              "request_id": "same-id"}).encode()
        replies = []
        for _ in range(2):
            await sock.send(request)
            replies.append(await sock.recv())
        assert replies[0] == replies[1]
        assert [c for c, _ in mock_dm].count("move_beam_to") == 1
    finally:
        sock.close()
        await _close(r, task, client)


async def test_timeout_reports_what_could_be_read_back():
    r, task, client = await _relay(29924, {"sim": {"kind": "sim", "slots": ["column"]}})
    try:
        await client.send_command("get_focus")
        link = r._links["sim"]
        backend = link._backend
        orig = backend.set_focus
        backend.set_focus = lambda params: (time.sleep(0.6), orig(params))[1]
        with pytest.raises(RuntimeError) as info:
            await client.send_command("set_focus", {"focus_dac": 30000}, timeout_ms=400)
        assert "still busy with 'set_focus'" in str(info.value)
        # Read-back path when the driver is free again.
        await asyncio.sleep(0.5)
        state = await r._read_back("sim", "set_focus", {})
        assert state["command"] == "get_focus" and state["data"]["focus_dac"] == 30000
    finally:
        await _close(r, task, client)


async def test_policy_is_enforced_by_the_relay():
    r, task, client = await _relay(
        29925, {"sim": {"kind": "sim", "slots": ["column", "stage"]}},
        policy={"stage_limits": {"x_m": [-1e-5, 1e-5]}, "confirm": ["set_column_valves"]})
    try:
        with pytest.raises(RuntimeError, match="outside this microscope's limits"):
            await client.send_command("move_stage", {"x_m": 5e-5})
        assert (await client.send_command("get_stage_position"))["x_m"] == 0.0
        # No operator interface: declined, and the agent's own flag is ignored.
        with pytest.raises(OperatorDeclined, match="go-ahead.*no operator interface"):
            await client.send_command("set_column_valves", {"open": False, "confirmed": True})

        async def allow(req):
            return True
        client.set_confirmer(allow)
        out = await client.send_command("set_column_valves", {"open": False})
        assert out["open"] is False
    finally:
        await _close(r, task, client)


# ---------------------------------------------------------------------------
# Driver in its own process
# ---------------------------------------------------------------------------

async def _wait_for(pred, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await pred():
            return True
        await asyncio.sleep(0.2)
    return False


async def test_hosted_driver_crash_is_isolated_and_restarted(tmp_path):
    drivers = {"sim": {"kind": "sim", "host": "process", "slots": SIM_ALL}}
    r, task, client = await _relay(29926, drivers, log_dir=str(tmp_path))
    try:
        assert "x_m" in await client.send_command("get_stage_position")
        link = r._links["sim"]
        pid = link.status()["pid"]
        assert pid != os.getpid()
        os.kill(pid, signal.SIGKILL if sys.platform != "win32" else signal.SIGTERM)
        await asyncio.sleep(0.5)
        # The relay itself stays up and says what happened.
        status = await client.send_command("backend_status")
        assert status["backends"]["sim"]["host"] == "process"

        async def back():
            try:
                await client.send_command("get_stage_position")
                return True
            except RuntimeError as exc:
                assert "is down" in str(exc) or "died" in str(exc) or "unavailable" in str(exc)
                return False
        assert await _wait_for(back)
        assert link.status()["pid"] != pid and link.restarts >= 1
        assert (tmp_path / "driver_sim.log").exists()
        out = await client.send_command("restart_driver", {"driver": "sim"})
        assert out["restarted"] == "sim"
        assert await _wait_for(back)
    finally:
        await _close(r, task, client)


# ---------------------------------------------------------------------------
# Session preferences enforced at the client chokepoint
# ---------------------------------------------------------------------------

async def test_blank_between_acquisitions_and_post_move_pause(tmp_path):
    from client.policy import PreferencePolicy
    from client.state.manager import StateManager
    r, task, client = await _relay(29927, {"sim": {"kind": "sim", "slots": SIM_ALL}})
    state = StateManager(tmp_path)
    client.set_policy(PreferencePolicy(state))
    try:
        sim = lambda: r._links["sim"]._backend                      # noqa: E731
        await client.send_command("set_instrument_mode", {"mode": "STEM"})
        # Off (the default): nothing is blanked.
        await client.send_command_image("stem_acquire", {"width": 16, "height": 16})
        assert sim()._blanked is False
        state.preferences.blank_between_acquisitions = True
        state.preferences.post_move_pause_s = 0.3
        await client.send_command_image("stem_acquire", {"width": 16, "height": 16})
        assert sim()._blanked is True                     # blanked after the frame
        orig = sim().stem_acquire
        seen = []
        sim().stem_acquire = lambda p: (seen.append(sim()._blanked), orig(p))[1]
        await client.send_command_image("stem_acquire", {"width": 16, "height": 16})
        assert seen == [False] and sim()._blanked is True  # unblanked for it, then again
        # A beam the operator blanked stays blanked.
        policy = client._policy
        policy.blanked_by_us = False
        seen.clear()
        await client.send_command_image("stem_acquire", {"width": 16, "height": 16})
        assert seen == [True]
        t0 = time.monotonic()
        out = await client.send_command("move_stage", {"x_m": 1e-7})
        assert out["settled"] and time.monotonic() - t0 >= 0.3
    finally:
        await _close(r, task, client)


async def test_blanking_preference_is_harmless_without_a_blanker(tmp_path):
    from client.policy import PreferencePolicy
    from client.state.manager import StateManager
    # Scan only, no column driver: set_beam_blank has nowhere to go.
    r, task, client = await _relay(29928, {"sim": {"kind": "sim", "slots": ["scan"]}})
    state = StateManager(tmp_path)
    state.preferences.blank_between_acquisitions = True
    client.set_policy(PreferencePolicy(state))
    try:
        await client.send_command("get_detectors")
        r._links["sim"]._backend._mode = 1
        _, arr = await client.send_command_image("stem_acquire", {"width": 8, "height": 8})
        assert arr.shape == (8, 8)
        assert client._policy.unsupported and "no effect" in client._policy.notes[0]
    finally:
        await _close(r, task, client)


# ---------------------------------------------------------------------------
# Driver SDK: contract checks, template, smoke test
# ---------------------------------------------------------------------------

def test_reference_drivers_pass_the_contract():
    from drivers import contract
    sim = build_driver("sim", {})
    results = contract.check_driver(sim, writes=True, acquire=True)
    assert contract.failures(results) == 0, [r for r in results if r.level == "FAIL"]
    sim.set_instrument_mode({"mode": "STEM"})
    stem = [r for r in contract.check_driver(sim, acquire=True) if r.name == "stem_acquire"]
    assert stem[0].level == "PASS"
    template = build_driver("template", {})
    assert contract.failures(contract.check_driver(template, writes=True)) == 0


def test_contract_catches_the_usual_mistakes():
    from drivers import contract
    from drivers.base import StageDriver

    class Bad(ColumnDriver, StageDriver):
        kind = "bad"

        def get_focus(self, params):
            return {"focus_dac": 1, "focus_nm": 12.0, "calibrated": False}  # no unit/range

        def get_stage_position(self, params):                 # nanometres on the wire
            return {"x_m": 15000.0, "y_m": 0.0, "z_m": 0.0, "alpha_deg": 0, "beta_deg": 0}

        def get_stigmation(self, params):
            return {"obj_x": 1}                                 # old flat JEOL shape

    text = "\n".join(map(repr, contract.check_driver(Bad())))
    assert "FAIL get_focus" in text and "never invent physical units" in text
    assert "nanometres sent as metres" in text
    assert "FAIL get_stigmation" in text


def test_contract_cli_runs(capsys):
    from drivers import contract
    assert contract.main(["--kind", "sim", "--writes"]) == 0
    assert "all contract checks passed" in capsys.readouterr().out


def test_jeol_driver_passes_the_contract_on_a_fake_pyjem():
    # Import the fake PyJEM the backend tests use, then check JEOL like any driver.
    import tests.test_jeol_driver  # noqa: F401  (installs the fake PyJEM modules)
    from drivers import contract
    from drivers.jeol.driver import JeolDriver
    results = contract.check_driver(JeolDriver(offline=True), writes=True)
    assert contract.failures(results) == 0, [r for r in results if r.level == "FAIL"]


async def test_dm_without_cameras_is_still_up():
    # Offline DM answers list_cameras with an error. That is a DM with no
    # cameras, not a DM that is down; marking it down greyed out every DM block.
    stop, ready, seen = threading.Event(), threading.Event(), []
    t = threading.Thread(target=_mock_dm, args=(29929, ready, stop, seen, ("list_cameras",)),
                         daemon=True)
    t.start()
    ready.wait(3)
    r, task, client = await _relay(29930, {"gatan": {"kind": "dm",
                                                     "host": "tcp://127.0.0.1:29929",
                                                     "slots": ["camera", "scan", "files"]}})
    try:
        caps = await client.send_command("get_capabilities")
        dm = caps["drivers"]["gatan"]
        assert dm["ok"] is True and dm["cameras"] == [] and "list_cameras" in dm["probe_errors"]
        assert dm["detectors"] == [{"index": 0, "name": "HAADF"}]
    finally:
        await _close(r, task, client)
        stop.set()
        t.join(3)


async def test_unconfigured_roles_are_guessed_from_the_hardware(mock_dm):
    # The F200 has no roles table: "HAADF" must still find the detector
    # whose name says HAADF, as the client's capability matching assumes.
    r, task, client = await _relay(29931, {"gatan": {"kind": "dm",
                                                     "host": "tcp://127.0.0.1:29921",
                                                     "slots": ["scan", "camera"]}})
    try:
        out = await client.send_command("set_detector", {"device": "detector:HAADF"})
        assert out["params"] == {"signal_index": 0}
        out = await client.send_command("get_camera_info", {"device": "camera:imaging"})
        assert out["params"] == {"camera_id": 0}
        with pytest.raises(RuntimeError, match="no detector role 'ABF'"):
            await client.send_command("set_detector", {"device": "detector:ABF"})
    finally:
        await _close(r, task, client)


def test_known_unreliable_reads_warn_instead_of_failing():
    # The F200's Apt3.GetKind() fails; the old pyjem_smoke reported it as WARN,
    # and the driver smoke test must not fail the whole install on it.
    import tests.test_jeol_driver  # noqa: F401  (fake PyJEM)
    from drivers import contract
    from drivers.jeol.driver import JeolDriver
    d = JeolDriver(offline=True)

    def fail():
        raise RuntimeError("Apt3.GetKind failed")
    d.apt.GetKind = fail
    results = {r.name: r for r in contract.check_driver(d)}
    assert results["get_aperture"].level == "WARN"
    assert contract.failures(list(results.values())) == 0
