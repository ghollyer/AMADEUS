"""amadeus-web: action queue, operator channel, queued MCP calls, HTTP API.

No relay: the console is built with ``connect=False`` and tests use the
client-state tools (plans, saved locations), which never touch ZMQ.
"""

import asyncio
import time

import pytest
from starlette.testclient import TestClient

from client.blocks.capabilities import Capabilities
from client.blocks.catalog import TOOL_META
from client.blocks.model import load_sequence
from client.web.agent_channel import OperatorChannel
from client.web.app import WebConsole, build_app
from client.web.queue import BlockQueue, EventBus


class Recorder:
    def __init__(self, fail=(), delay=0.0):
        self.calls = []
        self.fail = set(fail)
        self.delay = delay

    async def __call__(self, tool, args):
        self.calls.append(tool)
        if self.delay:
            await asyncio.sleep(self.delay)
        if tool in self.fail:
            raise RuntimeError(f"{tool} broke")
        if tool == "take_stem_image":
            return {"image_id": "img1"}
        if tool == "get_instrument_mode":
            return {"mode": "STEM"}
        return {"tool": tool, "args": args}


def make_queue(rec, caps=None):
    bus = EventBus()
    events = bus.subscribe()
    caps = caps or Capabilities.from_manifest(None, TOOL_META)
    q = BlockQueue(rec, bus, lambda: caps)
    q.start()
    return q, events


def drain(events):
    out = []
    while not events.empty():
        out.append(events.get_nowait())
    return out


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------

async def test_agent_call_waits_for_its_block_and_records_it():
    rec = Recorder()
    q, events = make_queue(rec)
    try:
        result = await q.submit_call("get_focus", {}, origin="agent")
        assert result == {"tool": "get_focus", "args": {}}
        block = q.blocks[q.order[0]]
        assert block.origin == "agent" and block.status == "done"
        statuses = [e["data"]["status"] for e in drain(events) if e["kind"] == "block"]
        assert statuses == ["queued", "running", "done"]
    finally:
        await q.stop()


async def test_failed_call_raises_for_agent_and_marks_block():
    q, _ = make_queue(Recorder(fail={"set_focus"}))
    try:
        with pytest.raises(RuntimeError, match="broke"):
            await q.submit_call("set_focus", {"olf_dac": 1})
        assert q.blocks[q.order[0]].status == "error"
    finally:
        await q.stop()


async def test_blocks_run_fifo_and_pause_holds_the_next_one():
    rec = Recorder(delay=0.05)
    q, _ = make_queue(rec)
    try:
        q.pause()
        _, f1 = q.enqueue_call("get_focus")
        _, f2 = q.enqueue_call("get_magnification")
        await asyncio.sleep(0.1)
        assert rec.calls == []
        q.step()
        await f1
        await asyncio.sleep(0.1)
        assert rec.calls == ["get_focus"]
        q.resume()
        await f2
        assert rec.calls == ["get_focus", "get_magnification"]
    finally:
        await q.stop()


async def test_remove_and_clear_queued_blocks():
    rec = Recorder()
    q, _ = make_queue(rec)
    try:
        q.pause()
        b1, f1 = q.enqueue_call("get_focus", origin="agent")
        b2, _ = q.enqueue_call("get_magnification")
        b3, _ = q.enqueue_call("get_spot_size")
        assert q.remove(b1.id)
        with pytest.raises(RuntimeError, match="removed"):
            await f1
        assert q.clear() == 2
        q.resume()
        await asyncio.sleep(0.05)
        assert rec.calls == []
        assert {q.blocks[b].status for b in (b1.id, b2.id, b3.id)} == {"removed"}
    finally:
        await q.stop()


async def test_sequence_appends_child_blocks_and_publishes_images():
    rec = Recorder()
    q, events = make_queue(rec)
    seq = load_sequence({"name": "two shots", "body": [
        {"type": "repeat", "count": 2, "body": [
            {"type": "call", "tool": "take_stem_image"}]}]})
    try:
        parent, future = q.enqueue_sequence(seq)
        await future
        assert parent.status == "done"
        assert [q.blocks[c].tool for c in parent.children] == \
            ["get_instrument_mode", "take_stem_image"] * 2
        snap = q.snapshot()["blocks"]
        assert snap[0]["id"] == parent.id and len(snap) == 5
        assert any(e["kind"] == "image" for e in drain(events))
    finally:
        await q.stop()


async def test_abort_stops_a_sequence_between_blocks():
    rec = Recorder(delay=0.05)
    q, _ = make_queue(rec)
    seq = load_sequence({"name": "long", "body": [
        {"type": "call", "tool": "get_focus"}] * 10})
    try:
        parent, future = q.enqueue_sequence(seq)
        await asyncio.sleep(0.12)
        q.abort()
        with pytest.raises(Exception):
            await future
        assert parent.status == "aborted"
        assert len(rec.calls) < 10
        # the queue keeps working afterwards
        assert await q.submit_call("get_focus") == {"tool": "get_focus", "args": {}}
    finally:
        await q.stop()


# ---------------------------------------------------------------------------
# Operator channel
# ---------------------------------------------------------------------------

async def test_wait_for_instructions_and_timeout():
    ch = OperatorChannel(EventBus())
    assert await ch.wait_for_instructions(0.01) == []
    waiter = asyncio.create_task(ch.wait_for_instructions(5))
    await asyncio.sleep(0.01)
    assert ch.agent_waiting
    ch.post_instruction("image the next rod")
    assert [e.text for e in await waiter] == ["image the next rod"]
    assert not ch.agent_waiting


async def test_ask_and_answer():
    ch = OperatorChannel(EventBus())
    task = asyncio.create_task(ch.ask("Insert OneView?", ["yes", "no"]))
    await asyncio.sleep(0.01)
    (q,) = ch.open_questions()
    assert q["options"] == ["yes", "no"]
    assert ch.answer(q["id"], "yes")
    assert await task == "yes"
    assert ch.open_questions() == []
    with pytest.raises(TimeoutError):
        await ch.ask("still there?", timeout_s=0.01)


# ---------------------------------------------------------------------------
# Web console (HTTP + queued MCP)
# ---------------------------------------------------------------------------

@pytest.fixture
def web(tmp_path):
    console = WebConsole("tcp://127.0.0.1:1", tmp_path, connect=False, enable_monitor=False)
    with TestClient(build_app(console)) as client:
        yield console, client


def _wait_done(client, block_id, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        blocks = {b["id"]: b for b in client.get("/api/queue").json()["blocks"]}
        if blocks[block_id]["status"] not in ("queued", "running"):
            return blocks[block_id]
        time.sleep(0.02)
    raise AssertionError("block never finished")


def test_catalog_lists_every_tool_with_availability(web):
    console, client = web
    data = client.get("/api/catalog").json()
    names = {t["name"] for t in data["tools"]}
    assert {"take_stem_image", "wait_for_operator", "move_stage_relative"} <= names
    assert data["capabilities"]["known"] is False
    # capabilities unknown -> nothing greyed out for instrument reasons
    stem = next(t for t in data["tools"] if t["name"] == "take_stem_image")
    assert stem["available"] and stem["mode"] == "STEM"


def test_catalog_greys_out_unsupported_tools_when_caps_known(web):
    console, client = web
    console.caps = Capabilities.from_manifest(
        {"drivers": {"hw": {"ok": True, "kind": "sim", "slots": ["column"],
                            "commands": ["get_focus"], "modes": ["TEM"]},
                     "dm": {"ok": False, "kind": "dm", "error": "down"}}},
        console._tool_names())
    console.caps_known = True
    tools = {t["name"]: t for t in client.get("/api/catalog").json()["tools"]}
    assert tools["get_focus"]["available"]
    assert not tools["take_stem_image"]["available"]
    assert "cmd:stem_acquire" in tools["take_stem_image"]["missing"]
    seq = {"name": "s", "body": [{"type": "call", "tool": "take_stem_image"}]}
    r = client.post("/api/queue/sequence", json={"sequence": seq})
    assert r.status_code == 409 and not r.json()["report"]["ok"]


def test_manual_block_runs_through_queue(web):
    console, client = web
    block = client.post("/api/queue/call", json={"tool": "list_saved_locations"}).json()
    done = _wait_done(client, block["id"])
    assert done["status"] == "done" and done["origin"] == "manual"
    assert client.post("/api/queue/call", json={"tool": "nope"}).status_code == 400


def test_agent_mcp_call_becomes_queue_block_and_gets_operator_messages(web):
    console, client = web
    client.post("/api/chat", json={"text": "also check the plan"})
    result = client.portal.call(console.mcp.call_tool, "get_experimental_plan", {})
    content = result[0] if isinstance(result, tuple) else result
    texts = [c.text for c in content]
    assert any("operator_message: also check the plan" in t for t in texts)
    blocks = client.get("/api/queue").json()["blocks"]
    assert blocks[-1]["tool"] == "get_experimental_plan" and blocks[-1]["origin"] == "agent"
    # operator tools bypass the queue
    client.portal.call(console.mcp.call_tool, "tell_operator", {"message": "hi"})
    assert len(client.get("/api/queue").json()["blocks"]) == len(blocks)
    chat = client.get("/api/chat").json()["transcript"]
    assert chat[-1]["text"] == "hi" and chat[0]["delivered"]


def test_sequence_files_round_trip_and_names_are_checked(web):
    console, client = web
    seq = {"name": "Survey", "body": [{"type": "call", "tool": "list_saved_locations"}]}
    assert client.put("/api/sequences/survey 1", json=seq).status_code == 200
    assert client.put("/api/sequences/..%2Fevil", json=seq).status_code in (400, 404)
    listing = client.get("/api/sequences").json()["sequences"]
    assert listing == [{"name": "survey 1", "title": "Survey", "description": "",
                        "ok": True, "missing": []}]
    assert client.get("/api/sequences/survey 1").json()["name"] == "Survey"
    check = client.post("/api/sequences/check", json=seq).json()
    assert check["ok"] and check["capabilities_known"] is False
    run = client.post("/api/queue/sequence", json={"sequence": seq}).json()
    assert _wait_done(client, run["block"]["id"])["status"] == "done"
    assert client.delete("/api/sequences/survey 1").status_code == 200


def test_queue_control_and_unknown_action(web):
    console, client = web
    assert client.post("/api/queue/control", json={"action": "pause"}).json()["paused"]
    block = client.post("/api/queue/call", json={"tool": "list_saved_locations"}).json()
    assert client.delete(f"/api/queue/{block['id']}").json()["removed"]
    assert not client.post("/api/queue/control", json={"action": "resume"}).json()["paused"]
    assert client.post("/api/queue/control", json={"action": "x"}).status_code == 400


def test_image_endpoints(web):
    import numpy as np
    console, client = web
    iid = console.state.images.save(np.arange(64, dtype=np.float32).reshape(8, 8),
                                    name="test", tags={"dwell_us": 2.0}).image_id
    listing = client.get("/api/images").json()["images"]
    assert listing[0]["image_id"] == iid and listing[0]["tags"]["dwell_us"] == 2.0
    png = client.get(f"/api/images/{iid}.png")
    assert png.status_code == 200 and png.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert client.get("/api/images/../../etc.png").status_code == 404
    fft = client.get(f"/api/images/{iid}/fft.png")
    assert fft.status_code == 200 and fft.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert client.get("/api/images/nope/fft.png").status_code == 404
    assert client.get("/api/live.png").status_code == 404


def test_header_readouts_from_microscope_state():
    from client.web.app import readouts
    state = {"magnification": 3200.0, "high_tension_kV": 200.0, "tem_stem_mode": "TEM",
             "focus_nm": None, "stage": {"x_m": 0.0}, "beam_shift": {"x": 1}}
    assert readouts(state) == {"magnification": 3200.0, "high_tension_kV": 200.0,
                               "tem_stem_mode": "TEM", "stage": {"x_m": 0.0}}
