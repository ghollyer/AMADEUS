"""ZMQClient recovers after a timeout.

A REQ socket that misses its reply refuses every later send (EFSM). The
client must rebuild it so a single timeout (e.g. the relay down at startup)
does not break the rest of the session.
"""

import asyncio
import threading

import pytest
import zmq

from client.transport.zmq_client import ZMQClient

PORT = 29881


def _serve_once(ready: threading.Event):
    ctx = zmq.Context()
    sock = ctx.socket(zmq.REP)
    sock.setsockopt(zmq.LINGER, 0)
    sock.bind(f"tcp://127.0.0.1:{PORT}")
    ready.set()
    try:
        if sock.poll(5000):
            req = sock.recv_json()
            sock.send_json({"status": "ok", "data": {"python_version": "recovered"},
                            "request_id": req.get("request_id", "")})
    finally:
        sock.close()
        ctx.term()


async def test_client_recovers_after_timeout():
    async with ZMQClient(f"tcp://127.0.0.1:{PORT}", timeout_ms=1000) as client:
        with pytest.raises(zmq.Again):
            await client.send_command("ping")       # nobody listening yet
        ready = threading.Event()
        t = threading.Thread(target=_serve_once, args=(ready,), daemon=True)
        t.start()
        ready.wait(3)
        assert await asyncio.wait_for(client.ping(), timeout=5) == "recovered"
        t.join(3)


async def test_second_call_after_timeout_is_not_efsm():
    async with ZMQClient(f"tcp://127.0.0.1:{PORT + 1}", timeout_ms=200) as client:
        for _ in range(2):
            with pytest.raises(zmq.Again):           # not ZMQError(EFSM)
                await client.send_command("ping")


async def test_per_call_timeout_overrides_client_timeout():
    """A quick probe on a long-timeout client fails fast (the console's state
    poll must not wait out the 10-minute acquisition timeout)."""
    import time
    async with ZMQClient(f"tcp://127.0.0.1:{PORT}", timeout_ms=600_000) as client:
        start = time.monotonic()
        with pytest.raises(zmq.Again):
            await client.send_command("ping", timeout_ms=200)
        assert time.monotonic() - start < 5


def test_bridge_tracks_responding(tmp_path):
    """The console's status follows the server: a timeout reports 'not
    responding', and the first reply afterwards reports it answering again."""
    from client.gui import bridge as bridge_mod
    from client.state.image_store import ImageStore

    events = []
    old = bridge_mod.QUICK_TIMEOUT_MS
    bridge_mod.QUICK_TIMEOUT_MS = 300
    b = bridge_mod.ZmqBridge(f"tcp://127.0.0.1:{PORT}", 600_000, ImageStore(tmp_path))
    b.on_link = lambda ok, detail: events.append(ok)
    try:
        with pytest.raises(zmq.Again):
            b.connect().result(timeout=5)     # nobody listening
        assert b.connected and b.responding is False and events == [False]
        assert not b.busy
        ready = threading.Event()
        t = threading.Thread(target=_serve_once, args=(ready,), daemon=True)
        t.start()
        ready.wait(3)
        assert b.ping().result(timeout=5)["server_python"] == "recovered"
        assert b.responding is True and events == [False, True]
        t.join(3)
    finally:
        bridge_mod.QUICK_TIMEOUT_MS = old
        b.close()


async def test_timeout_error_explains_itself_bug30():
    """Regression: a client timeout surfaced as the bare errno text
    "Resource temporarily unavailable". It must name the command and say the
    instrument may still be executing it, and still be a zmq.Again."""
    from client.transport.zmq_client import InstrumentTimeout
    async with ZMQClient("tcp://127.0.0.1:29883", timeout_ms=200) as client:
        with pytest.raises(zmq.Again) as info:
            await client.send_command("move_stage", {"x_m": 1e-6})
    err = info.value
    assert isinstance(err, InstrumentTimeout)
    text = str(err)
    assert "Resource temporarily unavailable" not in text
    assert "'move_stage'" in text and "tcp://127.0.0.1:29883" in text
    assert "may still be executing" in text and "get_stage_status" in text
