"""Tests for the ZMQ dispatcher (dm_server) using an in-process mock handler."""

import asyncio
import json
import threading
import time

import numpy as np
import pytest
import zmq
import zmq.asyncio

from client.transport.zmq_client import ZMQClient

PORT = 29876  # test port — avoid clashing with live server


# ---------------------------------------------------------------------------
# Minimal mock DM server (synchronous, runs in a thread)
# ---------------------------------------------------------------------------

def _mock_server(ready_event: threading.Event, stop_event: threading.Event) -> None:
    ctx = zmq.Context()
    sock = ctx.socket(zmq.REP)
    sock.bind(f"tcp://127.0.0.1:{PORT}")
    sock.setsockopt(zmq.RCVTIMEO, 200)
    ready_event.set()  # signal that the socket is bound and ready

    while not stop_event.is_set():
        try:
            raw = sock.recv()
        except zmq.Again:
            continue
        req = json.loads(raw)
        cmd = req.get("cmd")
        rid = req.get("request_id", "")

        if cmd == "ping":
            sock.send_json({"status": "ok", "data": {"python_version": "3.11.0 mock"}, "request_id": rid})
        elif cmd == "get_focus":
            sock.send_json({"status": "ok", "data": {"focus": 0.001}, "request_id": rid})
        elif cmd == "echo_image":
            arr = np.ones((8, 8), dtype=np.float32)
            meta = {
                "status": "ok",
                "has_image": True,
                "image_meta": {"shape": [8, 8], "dtype": "float32"},
                "data": {},
                "request_id": rid,
            }
            sock.send_json(meta, flags=zmq.SNDMORE)
            sock.send(arr.tobytes())
        elif cmd == "camera_acquire":
            arr = np.zeros((4, 4), dtype=np.float32)
            meta = {
                "status": "ok",
                "has_image": True,
                "image_meta": {
                    "shape": [4, 4],
                    "dtype": "float32",
                    "calibration": {"x_scale": 0.1, "y_scale": 0.1, "units": "nm"},
                },
                "data": {"camera_id": 0, "camera_name": "MockCam", "exposure_s": 1.0, "binning": 1},
                "request_id": rid,
            }
            sock.send_json(meta, flags=zmq.SNDMORE)
            sock.send(arr.tobytes())
        elif cmd == "list_cameras":
            sock.send_json({
                "status": "ok",
                "data": {"cameras": [{"id": 0, "name": "MockCam"}]},
                "request_id": rid,
            })
        elif cmd == "get_camera_info":
            sock.send_json({
                "status": "ok",
                "data": {
                    "camera_id": 0,
                    "name": "MockCam",
                    "width": 2048,
                    "height": 2048,
                    "pixel_size_um": {"x": 14.0, "y": 14.0},
                },
                "request_id": rid,
            })
        elif cmd == "eds_acquire_point":
            exp = req.get("params", {}).get("exposure_s", 30.0)
            sock.send_json({
                "status": "ok",
                "data": {"acquired": True, "exposure_s": exp},
                "request_id": rid,
            })
        elif cmd == "eds_acquire_map":
            p = req.get("params", {})
            sock.send_json({
                "status": "ok",
                "data": {
                    "acquired": True,
                    "x_pixels": p.get("x_pixels", 64),
                    "y_pixels": p.get("y_pixels", 64),
                },
                "request_id": rid,
            })
        elif cmd == "get_beam_tilt":
            sock.send_json({
                "status": "ok",
                "data": {"x": 0.001, "y": -0.002},
                "request_id": rid,
            })
        elif cmd == "get_beam_position":
            sock.send_json({
                "status": "ok",
                "data": {"x": 0.5, "y": 0.5},
                "request_id": rid,
            })
        elif cmd == "list_dm_files":
            sock.send_json({
                "status": "ok",
                "data": {
                    "directory": req["params"]["directory"],
                    "count": 1,
                    "files": [{
                        "name": "spec.dm4",
                        "path": req["params"]["directory"] + "/spec.dm4",
                        "size_bytes": 12345,
                        "mtime_iso": "2026-05-14T10:00:00",
                    }],
                },
                "request_id": rid,
            })
        elif cmd == "load_dm_file":
            arr = np.zeros((4, 4, 16), dtype=np.float32)
            meta = {
                "status": "ok",
                "has_image": True,
                "image_meta": {
                    "shape":       list(arr.shape),
                    "dtype":       "float32",
                    "name":        "spec.dm4",
                    "calibration": {"x_scale": 2.0, "y_scale": 2.0, "units": "nm"},
                },
                "data": {
                    "source_path":       req["params"]["path"],
                    "name":              "spec.dm4",
                    "ndim":              3,
                    "axes": [
                        {"index": 0, "size": 4,  "origin": 0.0, "scale": 2.0,  "units": "nm"},
                        {"index": 1, "size": 4,  "origin": 0.0, "scale": 2.0,  "units": "nm"},
                        {"index": 2, "size": 16, "origin": 0.0, "scale": 0.01, "units": "keV"},
                    ],
                    "energy_axis_index": 2,
                    "intensity":         {"scale": 1.0, "origin": 0.0, "units": "counts"},
                },
                "request_id": rid,
            }
            sock.send_json(meta, flags=zmq.SNDMORE)
            sock.send(arr.tobytes())
        elif cmd == "live_start":
            p = req.get("params", {})
            sock.send_json({
                "status": "ok",
                "data": {"active": True, "detector": p.get("detector", "stem"),
                         "interval_ms": p.get("interval_ms", 100)},
                "request_id": rid,
            })
        elif cmd == "live_stop":
            sock.send_json({
                "status": "ok",
                "data": {"stopped": True, "detector": "stem"},
                "request_id": rid,
            })
        elif cmd == "live_status":
            sock.send_json({
                "status": "ok",
                "data": {"active": False, "detector": None, "frame_index": 0},
                "request_id": rid,
            })
        elif cmd == "error_cmd":
            sock.send_json({"status": "error", "error": "deliberate error", "request_id": rid})
        else:
            sock.send_json({"status": "error", "error": f"unknown: {cmd}", "request_id": rid})

    sock.close()
    ctx.term()


@pytest.fixture(scope="module")
def mock_server():
    ready = threading.Event()
    stop = threading.Event()
    t = threading.Thread(target=_mock_server, args=(ready, stop), daemon=True)
    t.start()
    ready.wait(timeout=3)  # wait until socket is bound
    yield
    stop.set()
    t.join(timeout=2)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_ping(mock_server):
    async with ZMQClient(f"tcp://127.0.0.1:{PORT}", timeout_ms=3000) as client:
        ver = await asyncio.wait_for(client.ping(), timeout=5)
    assert "mock" in ver


async def test_send_command(mock_server):
    async with ZMQClient(f"tcp://127.0.0.1:{PORT}", timeout_ms=3000) as client:
        data = await asyncio.wait_for(client.send_command("get_focus"), timeout=5)
    assert data["focus"] == pytest.approx(0.001)


async def test_send_command_image(mock_server):
    async with ZMQClient(f"tcp://127.0.0.1:{PORT}", timeout_ms=3000) as client:
        meta, arr = await asyncio.wait_for(
            client.send_command_image("echo_image"), timeout=5
        )
    assert arr.shape == (8, 8)
    assert arr.dtype == np.float32
    np.testing.assert_array_equal(arr, np.ones((8, 8), dtype=np.float32))


async def test_image_command_single_frame_error_fails_fast(mock_server):
    # Error replies are one frame; the client must not wait for a pixel frame.
    async with ZMQClient(f"tcp://127.0.0.1:{PORT}", timeout_ms=3000) as client:
        with pytest.raises(RuntimeError, match="deliberate error"):
            await asyncio.wait_for(client.send_command_image("error_cmd"), timeout=2)
        # and the socket is still usable afterwards
        assert "mock" in await asyncio.wait_for(client.ping(), timeout=5)


async def test_error_response_raises(mock_server):
    async with ZMQClient(f"tcp://127.0.0.1:{PORT}", timeout_ms=3000) as client:
        with pytest.raises(RuntimeError, match="deliberate error"):
            await asyncio.wait_for(client.send_command("error_cmd"), timeout=5)


async def test_camera_acquire(mock_server):
    async with ZMQClient(f"tcp://127.0.0.1:{PORT}", timeout_ms=3000) as client:
        meta, arr = await asyncio.wait_for(
            client.send_command_image("camera_acquire", {"exposure_s": 1.0}), timeout=5
        )
    assert arr.shape == (4, 4)
    assert arr.dtype == np.float32
    assert meta["data"]["camera_name"] == "MockCam"


async def test_list_cameras(mock_server):
    async with ZMQClient(f"tcp://127.0.0.1:{PORT}", timeout_ms=3000) as client:
        data = await asyncio.wait_for(client.send_command("list_cameras"), timeout=5)
    assert "cameras" in data
    assert data["cameras"][0]["name"] == "MockCam"


async def test_eds_acquire_point(mock_server):
    async with ZMQClient(f"tcp://127.0.0.1:{PORT}", timeout_ms=3000) as client:
        data = await asyncio.wait_for(
            client.send_command("eds_acquire_point", {"exposure_s": 10.0}), timeout=5
        )
    assert data["acquired"] is True
    assert data["exposure_s"] == pytest.approx(10.0)


async def test_get_beam_tilt(mock_server):
    async with ZMQClient(f"tcp://127.0.0.1:{PORT}", timeout_ms=3000) as client:
        data = await asyncio.wait_for(client.send_command("get_beam_tilt"), timeout=5)
    assert "x" in data and "y" in data
    assert data["x"] == pytest.approx(0.001)


async def test_get_beam_position(mock_server):
    async with ZMQClient(f"tcp://127.0.0.1:{PORT}", timeout_ms=3000) as client:
        data = await asyncio.wait_for(client.send_command("get_beam_position"), timeout=5)
    assert data["x"] == pytest.approx(0.5)
    assert data["y"] == pytest.approx(0.5)


async def test_list_dm_files(mock_server):
    async with ZMQClient(f"tcp://127.0.0.1:{PORT}", timeout_ms=3000) as client:
        data = await asyncio.wait_for(
            client.send_command("list_dm_files", {"directory": "C:/Data"}), timeout=5
        )
    assert data["count"] == 1
    assert data["files"][0]["name"] == "spec.dm4"


async def test_load_dm_file_round_trip(mock_server):
    async with ZMQClient(f"tcp://127.0.0.1:{PORT}", timeout_ms=3000) as client:
        meta, arr = await asyncio.wait_for(
            client.send_command_image("load_dm_file", {"path": "C:/Data/spec.dm4"}),
            timeout=5,
        )
    assert arr.shape == (4, 4, 16)
    assert arr.dtype == np.float32
    assert meta["data"]["energy_axis_index"] == 2
    # Energy-axis calibration survives the round trip
    energy_axis = next(a for a in meta["data"]["axes"] if a["index"] == 2)
    assert energy_axis["units"] == "keV"


async def test_live_start_stop_round_trip(mock_server):
    async with ZMQClient(f"tcp://127.0.0.1:{PORT}", timeout_ms=3000) as client:
        started = await asyncio.wait_for(
            client.send_command("live_start", {"detector": "stem", "interval_ms": 50}),
            timeout=5,
        )
        assert started["active"] is True
        assert started["detector"] == "stem"
        stopped = await asyncio.wait_for(client.send_command("live_stop"), timeout=5)
        assert stopped["stopped"] is True


# ---------------------------------------------------------------------------
# Monitor integration — hooks fire through the real client; pause vetoes sends
# ---------------------------------------------------------------------------

async def test_monitor_records_command_through_client(mock_server, tmp_path):
    from client.monitor import Monitor
    mon = Monitor(tmp_path / "monitor", tmp_path / "images")
    async with ZMQClient(f"tcp://127.0.0.1:{PORT}", timeout_ms=3000) as client:
        client.set_monitor(mon)
        await asyncio.wait_for(client.send_command("get_focus"), timeout=5)
    snap = mon.snapshot()
    assert snap["history"][-1] == {**snap["history"][-1], "cmd": "get_focus", "status": "ok"}
    assert snap["current_command"] is None  # cleared after completion


async def test_monitor_pause_vetoes_send(mock_server, tmp_path):
    from client.monitor import Monitor, PausedError
    mon = Monitor(tmp_path / "monitor", tmp_path / "images")
    mon.set_paused(True)
    async with ZMQClient(f"tcp://127.0.0.1:{PORT}", timeout_ms=3000) as client:
        client.set_monitor(mon)
        with pytest.raises(PausedError):
            await asyncio.wait_for(client.send_command("get_focus"), timeout=5)
        # ping is exempt, so health checks still work while paused
        ver = await asyncio.wait_for(client.ping(), timeout=5)
    assert "mock" in ver


async def test_monitor_records_error_through_client(mock_server, tmp_path):
    from client.monitor import Monitor
    mon = Monitor(tmp_path / "monitor", tmp_path / "images")
    async with ZMQClient(f"tcp://127.0.0.1:{PORT}", timeout_ms=3000) as client:
        client.set_monitor(mon)
        with pytest.raises(RuntimeError, match="deliberate error"):
            await asyncio.wait_for(client.send_command("error_cmd"), timeout=5)
    snap = mon.snapshot()
    assert snap["history"][-1]["status"] == "error"
    assert snap["dm_ok"] is False
