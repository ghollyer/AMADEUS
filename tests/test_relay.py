"""Tests for the relay — routing, passthrough, fallback toggle, timeouts.

Two in-process mock REP backends stand in for the DM server and the hardware
bridge (threaded, like ``test_zmq_protocol.py``'s mock server); the real
``Relay`` runs as an asyncio task in the test loop and the real ``ZMQClient``
talks to its front door. Ports are in the 299xx range to avoid clashing with
anything live (the 29876 convention from test_zmq_protocol).
"""

import asyncio
import json
import threading

import numpy as np
import pytest
import zmq
import zmq.asyncio

from client.transport.zmq_client import ZMQClient
from relay.config import RelayConfig, load_config
from relay.server import Relay

DM_PORT = 29901
HW_PORT = 29902
RELAY_PORT = 29900
DEAD_PORT = 29999   # nothing listens here


# ---------------------------------------------------------------------------
# Mock backends
# ---------------------------------------------------------------------------

def _mock_backend(name: str, port: int, ready: threading.Event,
                  stop: threading.Event, seen: list) -> None:
    ctx = zmq.Context()
    sock = ctx.socket(zmq.REP)
    sock.bind(f"tcp://127.0.0.1:{port}")
    sock.setsockopt(zmq.RCVTIMEO, 200)
    ready.set()
    while not stop.is_set():
        try:
            raw = sock.recv()
        except zmq.Again:
            continue
        req = json.loads(raw)
        cmd, rid = req.get("cmd"), req.get("request_id", "")
        seen.append(cmd)
        if cmd == "ping":
            sock.send_json({"status": "ok",
                            "data": {"python_version": f"{name}-mock",
                                     "backend": name},
                            "request_id": rid})
        elif cmd == "camera_acquire":
            arr = np.arange(16, dtype=np.float32).reshape(4, 4)
            sock.send_json({"status": "ok", "has_image": True,
                            "image_meta": {"shape": [4, 4], "dtype": "float32"},
                            "data": {"backend": name}, "request_id": rid},
                           flags=zmq.SNDMORE)
            sock.send(arr.tobytes())
        else:
            sock.send_json({"status": "ok",
                            "data": {"backend": name, "cmd": cmd},
                            "request_id": rid})
    sock.close()
    ctx.term()


@pytest.fixture(scope="module")
def backends():
    stop = threading.Event()
    seen = {"dm": [], "hw": []}
    threads = []
    for name, port in (("dm", DM_PORT), ("hw", HW_PORT)):
        ready = threading.Event()
        t = threading.Thread(target=_mock_backend,
                             args=(name, port, ready, stop, seen[name]),
                             daemon=True)
        t.start()
        ready.wait(timeout=3)
        threads.append(t)
    yield seen
    stop.set()
    for t in threads:
        t.join(timeout=2)


ACQ = ["scan", "camera", "live", "spectrometer", "files"]


def _drivers(hw=None, via_dm: bool = False, hw_port: int = HW_PORT,
             dm_port: int = DM_PORT, dm_stream: "str | None" = None) -> dict:
    """The classic pair: a column/stage driver and DM. *hw* is "remote" (the
    mock on hw_port), "sim" (in-process) or a spec; *via_dm* gives column +
    stage to DM (the Gatan EM* fallback)."""
    out = {}
    hw_slots = [] if via_dm else ["column", "stage"]
    if hw == "remote":
        out["hw"] = {"kind": "remote", "host": f"tcp://127.0.0.1:{hw_port}", "slots": hw_slots}
    elif hw == "sim":
        out["hw"] = {"kind": "sim", "host": "in_process", "slots": hw_slots}
    elif isinstance(hw, dict):
        out["hw"] = {**hw, "slots": hw_slots}
    dm = {"kind": "dm", "host": f"tcp://127.0.0.1:{dm_port}",
          "slots": ACQ + (["column", "stage"] if via_dm else [])}
    if dm_stream:
        dm["stream"] = dm_stream
    out["dm"] = dm
    return out


def _config(port: int = RELAY_PORT, via_dm: bool = False,
            hw_port: int = HW_PORT, timeout_ms: int = 3000) -> RelayConfig:
    return RelayConfig(listen=f"tcp://127.0.0.1:{port}",
                       drivers=_drivers("remote", via_dm=via_dm, hw_port=hw_port),
                       timeout_ms=timeout_ms)


@pytest.fixture
async def relay(backends):
    r = Relay(_config())
    task = asyncio.create_task(r.serve())
    await asyncio.sleep(0.1)   # let the frontend bind
    yield r
    r.stop()
    await asyncio.wait_for(task, timeout=3)


async def _client(port: int = RELAY_PORT) -> ZMQClient:
    client = ZMQClient(f"tcp://127.0.0.1:{port}", timeout_ms=5000)
    await client.connect()
    return client


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

async def test_hardware_command_routes_to_hw(relay, backends):
    client = await _client()
    try:
        data = await client.send_command("get_focus")
        assert data == {"backend": "hw", "cmd": "get_focus"}
        assert backends["hw"][-1] == "get_focus"
    finally:
        await client.disconnect()


async def test_acquisition_command_routes_to_dm(relay, backends):
    client = await _client()
    try:
        data = await client.send_command("stem_acquire", {"mode": "capture"})
        assert data["backend"] == "dm"
        # No silent default to DM any more: an unknown command is an error
        # that names the drivers, not a guess about which vendor wants it.
        with pytest.raises(RuntimeError, match="unknown command 'some_future_command'"):
            await client.send_command("some_future_command")
    finally:
        await client.disconnect()


async def test_shutdown_hw_rewrites_to_backend_shutdown(relay, backends):
    client = await _client()
    try:
        data = await client.send_command("shutdown_hw")
        assert data == {"backend": "hw", "cmd": "shutdown"}
        assert backends["hw"][-1] == "shutdown"
    finally:
        await client.disconnect()


async def test_dm_shutdown_goes_to_dm(relay, backends):
    client = await _client()
    try:
        data = await client.send_command("dm.shutdown")
        assert data == {"backend": "dm", "cmd": "shutdown"}
        with pytest.raises(RuntimeError, match="unknown command 'shutdown'"):
            await client.send_command("shutdown")          # old bare name: gone
    finally:
        await client.disconnect()


async def test_fallback_toggle_routes_hardware_to_dm(backends):
    r = Relay(_config(port=29904, via_dm=True))
    task = asyncio.create_task(r.serve())
    await asyncio.sleep(0.1)
    client = await _client(29904)
    try:
        data = await client.send_command("get_focus")
        assert data == {"backend": "dm", "cmd": "get_focus"}
        # Non-hardware traffic unaffected
        data = await client.send_command("list_cameras")
        assert data["backend"] == "dm"
    finally:
        await client.disconnect()
        r.stop()
        await asyncio.wait_for(task, timeout=3)


# ---------------------------------------------------------------------------
# Passthrough / relay-local / failure modes
# ---------------------------------------------------------------------------

async def test_multipart_image_passes_through_byte_identical(relay):
    client = await _client()
    try:
        meta, arr = await client.send_command_image("camera_acquire")
        np.testing.assert_array_equal(
            arr, np.arange(16, dtype=np.float32).reshape(4, 4))
        assert meta["data"]["backend"] == "dm"
    finally:
        await client.disconnect()


async def test_relay_ping_reports_backends(relay):
    client = await _client()
    try:
        data = await client.send_command("ping")
        assert data["relay"] is True
        assert data["routing"]["column"] == ["hw"]
        assert data["backends"]["dm"]["ok"] is True
        assert data["backends"]["hw"]["python_version"] == "hw-mock"
    finally:
        await client.disconnect()


async def test_backend_status(relay):
    client = await _client()
    try:
        data = await client.send_command("backend_status")
        assert set(data["backends"]) == {"dm", "hw"}
        assert data["backends"]["hw"]["ok"] is True
    finally:
        await client.disconnect()


async def test_dead_backend_returns_structured_timeout_error(backends):
    r = Relay(_config(port=29903, hw_port=DEAD_PORT, timeout_ms=500))
    task = asyncio.create_task(r.serve())
    await asyncio.sleep(0.1)
    client = await _client(29903)
    try:
        with pytest.raises(RuntimeError, match="did not answer .get_focus."):
            await client.send_command("get_focus")
        # The relay itself is still healthy — DM routing unaffected
        data = await client.send_command("live_status")
        assert data["backend"] == "dm"
    finally:
        await client.disconnect()
        r.stop()
        await asyncio.wait_for(task, timeout=3)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_load_config_without_a_config_file_is_safe():
    cfg = load_config(None)
    assert cfg.drivers["hw"]["kind"] == "jeol"
    assert cfg.drivers["hw"]["offline"] is True              # 1.0: offline unless --online
    assert cfg.drivers["dm"]["host"] == "tcp://127.0.0.1:9876"
    assert cfg.stream_listen is None           # no proxy without a config
    assert load_config(None, hw_mode="online").drivers["hw"]["offline"] is False


def test_load_config_from_unified_config_and_cli():
    from common import config as amadeus_config
    unified = amadeus_config.load_defaults()
    amadeus_config.set_hardware(unified, via_dm=True)
    unified["relay"]["timeout_ms"] = 1234
    cfg = load_config(unified)
    assert "column" in cfg.drivers["dm"]["slots"] and cfg.drivers["hw"]["slots"] == []
    assert cfg.timeout_ms == 1234
    assert cfg.stream_listen == "tcp://*:9877"
    assert cfg.drivers["dm"]["stream"] == "tcp://127.0.0.1:9879"
    amadeus_config.set_hardware(unified, via_dm=False)
    cfg = load_config(unified, hw_mode="sim", dm="tcp://x:1", calibration="c.json")
    assert cfg.drivers["hw"]["kind"] == "sim"                # CLI beats config
    assert cfg.drivers["hw"]["calibration"] == "c.json"
    assert cfg.drivers["dm"]["host"] == "tcp://x:1"


def test_config_rejects_a_driver_without_a_kind():
    with pytest.raises(ValueError, match="no kind"):
        RelayConfig(drivers={"x": {"slots": ["column"]}})


# ---------------------------------------------------------------------------
# In-process hardware backend, stream proxy, get_config
# ---------------------------------------------------------------------------



async def _run_relay(cfg: RelayConfig, amadeus_cfg=None):
    r = Relay(cfg, amadeus_cfg)
    task = asyncio.create_task(r.serve())
    await asyncio.sleep(0.15)
    return r, task


async def test_in_process_hardware_runs_on_one_worker_thread(backends):
    cfg = RelayConfig(listen="tcp://127.0.0.1:29905",
                      drivers=_drivers("sim"))
    r, task = await _run_relay(cfg)
    client = await _client(29905)
    try:
        status = await client.send_command("backend_status")
        assert status["backends"]["hw"]["ok"] is True
        assert status["backends"]["hw"]["address"] == "in-process"
        assert status["backends"]["hw"]["backend"] == "sim"
        await client.send_command("set_focus", {"focus_dac": 40000})
        assert (await client.send_command("get_focus"))["focus_dac"] == 40000
        # DM routing is unaffected
        assert (await client.send_command("live_status"))["backend"] == "dm"

        link = r._links["hw"]
        seen = set()
        orig = link._handle
        link._handle = lambda raw: (seen.add(threading.current_thread()), orig(raw))[1]
        for _ in range(3):
            await client.send_command("get_stage_position")
        assert seen == {link.worker_thread}             # built + called on one thread
        assert link.worker_thread is not threading.current_thread()

        # shutdown_hw releases the backend; the next call rebuilds it.
        await client.send_command("shutdown_hw")
        assert link._backend is None
        assert "x_m" in await client.send_command("get_stage_position")
    finally:
        await client.disconnect()
        r.stop()
        await asyncio.wait_for(task, timeout=3)


async def test_in_process_hardware_timeout_is_structured(backends):
    import time as _time
    cfg = RelayConfig(listen="tcp://127.0.0.1:29906",
                      drivers=_drivers("sim"), timeout_ms=300)
    r, task = await _run_relay(cfg)
    client = await _client(29906)
    try:
        await client.send_command("get_focus")          # backend built
        backend = r._links["hw"]._backend
        backend.get_focus = lambda params: (_time.sleep(0.8), {})[1]
        with pytest.raises(RuntimeError, match="in-process.*did not answer .get_focus.*still running on the driver worker"):
            await client.send_command("get_focus")
        assert (await client.send_command("live_status"))["backend"] == "dm"
    finally:
        await client.disconnect()
        r.stop()
        await asyncio.wait_for(task, timeout=3)


async def test_unavailable_hardware_backend_reports_error(backends):
    cfg = RelayConfig(listen="tcp://127.0.0.1:29907",
                      drivers=_drivers({"kind": "nonexistent", "host": "in_process"}))
    r, task = await _run_relay(cfg)
    client = await _client(29907)
    try:
        with pytest.raises(RuntimeError, match="driver 'hw' \\(nonexistent\\) unavailable"):
            await client.send_command("get_focus")
        status = await client.send_command("backend_status")
        assert status["backends"]["hw"]["ok"] is False
    finally:
        await client.disconnect()
        r.stop()
        await asyncio.wait_for(task, timeout=3)


async def test_stream_proxy_forwards_frames_unchanged(backends):
    ctx = zmq.Context.instance()
    dm_pub = ctx.socket(zmq.PUB)
    dm_pub.setsockopt(zmq.LINGER, 0)
    dm_pub.bind("tcp://127.0.0.1:29908")
    cfg = RelayConfig(listen="tcp://127.0.0.1:29909",
                      drivers=_drivers(None, via_dm=True,
                                       dm_stream="tcp://127.0.0.1:29908"),
                      stream_listen="tcp://127.0.0.1:29910")
    r, task = await _run_relay(cfg)
    sub = zmq.asyncio.Context.instance().socket(zmq.SUB)
    sub.setsockopt(zmq.LINGER, 0)
    sub.setsockopt(zmq.SUBSCRIBE, b"")
    sub.connect("tcp://127.0.0.1:29910")
    frames = [b'{"frame": 1}', np.arange(6, dtype=np.uint16).tobytes()]
    try:
        received = None
        for _ in range(40):                 # slow-joiner: resend until it arrives
            dm_pub.send_multipart(frames)
            if await sub.poll(100):
                received = await sub.recv_multipart()
                break
        assert received == frames
    finally:
        sub.close()
        dm_pub.close()
        r.stop()
        await asyncio.wait_for(task, timeout=3)


async def test_get_config_serves_public_unified_config(backends):
    from common import config as amadeus_config
    unified = amadeus_config.load_defaults()
    unified["microscope_pc"]["ip"] = "192.168.50.2"
    cfg = RelayConfig(listen="tcp://127.0.0.1:29911",
                      drivers=_drivers(None, via_dm=True))
    r, task = await _run_relay(cfg, unified)
    client = await _client(29911)
    try:
        data = (await client.send_command("get_config"))["config"]
        assert data["microscope_pc"]["ip"] == "192.168.50.2"
        assert not any(k.startswith("_") for k in data)
    finally:
        await client.disconnect()
        r.stop()
        await asyncio.wait_for(task, timeout=3)


# ---------------------------------------------------------------------------
# Waiting stage moves are followed by the relay
# ---------------------------------------------------------------------------

async def _sim_relay_with_slow_stage(port: int, move_time_s: float):
    cfg = RelayConfig(listen=f"tcp://127.0.0.1:{port}",
                      drivers=_drivers("sim"))
    r, task = await _run_relay(cfg)
    client = await _client(port)
    await client.send_command("get_focus")                     # backend built
    r._links["hw"]._backend.move_time_s = move_time_s
    return r, task, client


async def test_relay_and_hardware_stay_responsive_during_a_stage_move(backends):
    import time as _time
    r, task, client = await _sim_relay_with_slow_stage(29912, move_time_s=1.2)
    other = await _client(29912)
    try:
        move = asyncio.create_task(
            client.send_command("move_stage", {"x_m": 1e-6}, timeout_ms=10_000))
        await asyncio.sleep(0.3)
        for cmd in ("ping", "get_focus", "get_stage_status"):     # relay, hw, hw
            t0 = _time.monotonic()
            reply = await other.send_command(cmd)
            assert _time.monotonic() - t0 < 0.5, cmd
        assert reply["moving"] is True
        result = await move
        assert result["settled"] is True and result["x_m"] == pytest.approx(1e-6)
        assert "_continue" not in result
    finally:
        await client.disconnect()
        await other.disconnect()
        r.stop()
        await asyncio.wait_for(task, timeout=3)


async def test_stop_stage_gets_through_mid_move(backends):
    import time as _time
    r, task, client = await _sim_relay_with_slow_stage(29913, move_time_s=5.0)
    other = await _client(29913)
    try:
        t0 = _time.monotonic()
        move = asyncio.create_task(
            client.send_command("move_stage", {"x_m": 1e-6}, timeout_ms=20_000))
        await asyncio.sleep(0.3)
        await other.send_command("stop_stage")
        result = await move
        assert result["settled"] is True
        assert _time.monotonic() - t0 < 2.0          # didn't sit out the 5 s move
    finally:
        await client.disconnect()
        await other.disconnect()
        r.stop()
        await asyncio.wait_for(task, timeout=3)


async def test_relay_answers_inside_the_clients_timeout_with_move_state(backends):
    """A move longer than the client's patience gets a structured
    settled=false reply just before the client would time out, rather than a
    dead socket."""
    r, task, client = await _sim_relay_with_slow_stage(29914, move_time_s=10.0)
    try:
        result = await client.send_command("move_stage", {"x_m": 1e-6},
                                           timeout_ms=2_500)
        assert result["settled"] is False and result["moving"] is True
        status = await client.send_command("get_stage_status")
        assert status["moving"] is True
    finally:
        await client.disconnect()
        r.stop()
        await asyncio.wait_for(task, timeout=3)


async def test_hardware_timeout_names_the_command_blocking_the_worker(backends):
    import time as _time
    cfg = RelayConfig(listen="tcp://127.0.0.1:29915",
                      drivers=_drivers("sim"),
                      timeout_ms=400)
    r, task = await _run_relay(cfg)
    client = await _client(29915)
    other = await _client(29915)
    try:
        await client.send_command("get_focus")
        backend = r._links["hw"]._backend
        backend.get_alpha = lambda params: (_time.sleep(1.5), {})[1]
        slow = asyncio.create_task(client.send_command("get_alpha"))
        await asyncio.sleep(0.1)
        with pytest.raises(RuntimeError, match="never started.*busy with 'get_alpha'"):
            await other.send_command("get_focus")
        with pytest.raises(RuntimeError, match="still running"):
            await slow
        assert (await other.send_command("ping"))["reply"] == "pong"   # relay itself fine
    finally:
        await client.disconnect()
        await other.disconnect()
        r.stop()
        await asyncio.wait_for(task, timeout=3)


async def test_out_of_band_stop_from_the_same_client_beats_its_waiting_move(backends):
    """The client's main REQ socket is busy with the waiting move, so a plain
    stop_stage on it would queue behind the move. The out-of-band send (MCP
    stop_stage tool, console STOP) gets through at once."""
    import time as _time
    r, task, client = await _sim_relay_with_slow_stage(29916, move_time_s=5.0)
    try:
        t0 = _time.monotonic()
        move = asyncio.create_task(
            client.send_command("move_stage", {"x_m": 1e-6}, timeout_ms=20_000))
        await asyncio.sleep(0.3)
        stopped = await client.send_command_out_of_band("stop_stage", timeout_ms=5_000)
        assert _time.monotonic() - t0 < 1.5
        assert stopped["moving"] is False
        assert (await move)["settled"] is True
        assert _time.monotonic() - t0 < 2.0
    finally:
        await client.disconnect()
        r.stop()
        await asyncio.wait_for(task, timeout=3)


# ---------------------------------------------------------------------------
# Capability manifest (web console / action blocks)
# ---------------------------------------------------------------------------

async def test_get_capabilities_merges_hw_dm_and_instrument(backends):
    from common import config as amadeus_config
    unified = amadeus_config.load_defaults()
    unified["instrument"] = {"label": "test scope",
                             "roles": {"detectors": {"HAADF": 0}}}
    cfg = RelayConfig(listen="tcp://127.0.0.1:29917",
                      drivers=_drivers("sim"))
    r, task = await _run_relay(cfg, unified)
    client = await _client(29917)
    try:
        data = await client.send_command("get_capabilities")
        hw = data["drivers"]["hw"]
        assert hw["ok"] is True and hw["kind"] == "sim"
        assert "move_stage" in hw["commands"]
        assert "jeol.tem3_call" not in hw["extras"]             # sim lacks it
        assert data["drivers"]["dm"]["ok"] is True
        assert data["instrument"]["roles"]["detectors"]["HAADF"] == 0
        assert "hw" not in data and "hardware_backend" not in data
    finally:
        await client.disconnect()
        r.stop()
        await asyncio.wait_for(task, timeout=3)


async def test_get_capabilities_survives_dm_down_and_reports_dm_fallback(backends):
    cfg = RelayConfig(listen="tcp://127.0.0.1:29918",
                      drivers=_drivers(None, via_dm=True, dm_port=DEAD_PORT))
    r, task = await _run_relay(cfg)
    client = await _client(29918)
    try:
        data = await client.send_command("get_capabilities", timeout_ms=10_000)
        dm = data["drivers"]["dm"]
        assert data["routing"]["column"] == ["dm"]                 # the EM* fallback
        assert "get_aperture" not in dm["commands"] and "get_focus" in dm["commands"]
        assert dm["ok"] is False and dm["cameras"] is None
        assert data["instrument"] == {}
    finally:
        await client.disconnect()
        r.stop()
        await asyncio.wait_for(task, timeout=3)
