"""Operator confirmation (client/confirm.py) and the gates that use it.

The relay holds confirmable commands with ``needs_confirmation``; the client
asks a confirmer and resends. The agent can't confirm: a caller's
``confirmed`` is dropped. Ports 29941-29945.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from client.confirm import (Approve, ChainConfirmer, ConfirmRequest, ElicitConfirmer,
                            MonitorConfirmer, NeedsConfirmation, OperatorDeclined,
                            read_json, write_json)
from client.policy import PreferencePolicy
from client.state.manager import StateManager
from client.transport.zmq_client import ZMQClient
from client.web.agent_channel import OperatorChannel, WebConfirmer
from client.web.queue import EventBus
from relay.config import RelayConfig
from relay.policy import Policy, PolicyError
from relay.server import Relay

COLUMN = {"sim": {"kind": "sim", "slots": ["column", "stage"]}}
F200_POLICY = {"stage_limits": {"x_m": [-0.0009, 0.0009], "y_m": [-0.0009, 0.0009],
                                "z_m": [-0.0002, 0.0002], "alpha_deg": [-25, 25]},
               "confirm": ["set_column_valves", "set_instrument_mode", "set_aperture",
                           "set_function_mode"],
               "confirm_vendor_writes": True}


async def _relay(port: int, **kw):
    cfg = RelayConfig(listen=f"tcp://127.0.0.1:{port}", drivers=COLUMN, timeout_ms=5000, **kw)
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


class _Operator:
    """A scripted operator: answers in order and records what was asked."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked: list[ConfirmRequest] = []

    async def __call__(self, req):
        self.asked.append(req)
        return self.answers.pop(0)


# ---------------------------------------------------------------------------
# Relay policy
# ---------------------------------------------------------------------------

def _check(pol, cmd, params):
    return asyncio.run(pol.check("hw", cmd, params))


def test_raw_vendor_writes_need_confirmation_and_reads_do_not():
    pol = Policy({}, [], None)
    assert _check(pol, "jeol.tem3_call", {"class_name": "Stage3", "method": "GetPos"})
    assert _check(pol, "vendor_call", {"method": "Stage3.GetPos"})
    assert _check(pol, "vendor_call", {"method": "EOS3.IsStemMode"}) is not None
    for cmd, params in (("jeol.tem3_call", {"class_name": "Stage3", "method": "SetX",
                                            "args": [0]}),
                        ("vendor_call", {"method": "HT3.SetHtValue", "args": [1]})):
        with pytest.raises(PolicyError, match="raw vendor write") as info:
            _check(pol, cmd, params)
        assert info.value.confirmable
        assert "confirmed" not in _check(pol, cmd, {**params, "confirmed": True})
    # Other extras are not escape hatches.
    _check(pol, "dm.execute_script", {"script": "1"})
    # The gate can be turned off.
    _check(Policy({"confirm_vendor_writes": False}, [], None), "jeol.tem3_call",
           {"class_name": "Stage3", "method": "SetX", "args": [0]})


def test_stage_limits_are_hard_and_confirm_lists_are_confirmable():
    pol = Policy(F200_POLICY, [], None)
    with pytest.raises(PolicyError) as info:
        _check(pol, "move_stage", {"x_m": 0.002})
    assert not info.value.confirmable
    with pytest.raises(PolicyError) as info:      # confirmed doesn't get past a limit
        _check(pol, "move_stage", {"alpha_deg": 40, "confirmed": True})
    assert not info.value.confirmable
    with pytest.raises(PolicyError) as info:
        _check(pol, "set_function_mode", {"mode": "DIFF"})
    assert info.value.confirmable


# ---------------------------------------------------------------------------
# Through the relay: the client asks the operator and resends
# ---------------------------------------------------------------------------

async def test_the_operator_confirms_and_the_agents_flag_is_ignored():
    r, task, client = await _relay(29941, policy=F200_POLICY)
    try:
        op = _Operator(False, True)
        client.set_confirmer(op)
        # The caller's own confirmed is dropped, so the operator is still asked.
        before = (await client.send_command("get_microscope_state"))["column_valves_open"]
        with pytest.raises(OperatorDeclined, match="declined set_column_valves"):
            await client.send_command("set_column_valves",
                                      {"open": not before, "confirmed": True})
        state = await client.send_command("get_microscope_state")
        assert state["column_valves_open"] is before             # nothing was sent
        out = await client.send_command("set_column_valves", {"open": not before})
        assert out["open"] is (not before)
        assert [a.cmd for a in op.asked] == ["set_column_valves"] * 2
        assert "confirmed" not in op.asked[0].params
        assert "policy.confirm" in op.asked[0].reason
    finally:
        await _close(r, task, client)


async def test_a_stage_limit_is_refused_without_asking():
    r, task, client = await _relay(29942, policy=F200_POLICY)
    try:
        op = _Operator(True)
        client.set_confirmer(op)
        with pytest.raises(RuntimeError, match="outside this microscope's limits") as info:
            await client.send_command("move_stage", {"x_m": 0.0015})
        assert not isinstance(info.value, OperatorDeclined) and op.asked == []
    finally:
        await _close(r, task, client)


async def test_beam_shift_beyond_max_needs_the_operator(tmp_path):
    r, task, client = await _relay(29943)
    try:
        state = StateManager(tmp_path)
        client.set_policy(PreferencePolicy(state))
        op = _Operator(False, True)
        client.set_confirmer(op)
        await client.send_command("set_beam_shift", {"x": 32768 + 500, "y": 32768})
        assert op.asked == []                                   # within max_beam_shift
        with pytest.raises(OperatorDeclined, match="max_beam_shift"):
            await client.send_command("set_beam_shift", {"x": 32768 + 5000, "y": 32768})
        assert (await client.send_command("get_beam_shift"))["x"] == 32768 + 500
        await client.send_command("set_beam_shift", {"x": 32768 + 5000, "y": 32768})
        assert (await client.send_command("get_beam_shift"))["x"] == 32768 + 5000
    finally:
        await _close(r, task, client)


async def test_loosening_a_safety_preference_asks(tmp_path):
    from client.tools import preferences

    class _CaptureMCP:
        def __init__(self):
            self.tools = {}

        def tool(self, *a, **k):
            def deco(fn):
                self.tools[fn.__name__] = fn
                return fn
            return deco

    zmq = ZMQClient("tcp://127.0.0.1:1")          # never connected: confirm() only
    op = _Operator(False, True)
    zmq.set_confirmer(op)
    mcp, state = _CaptureMCP(), StateManager(tmp_path)
    preferences.register(mcp, state, zmq)
    setp = mcp.tools["set_operational_preferences"]
    await setp(max_beam_shift=1000)                            # tightening: no question
    assert op.asked == []
    with pytest.raises(OperatorDeclined):
        await setp(max_beam_shift=8000)
    assert state.preferences.max_beam_shift == 1000
    await setp(max_beam_shift=8000)
    assert state.preferences.max_beam_shift == 8000 and len(op.asked) == 2


# ---------------------------------------------------------------------------
# The interfaces
# ---------------------------------------------------------------------------

async def test_no_operator_reachable_means_declined():
    zmq = ZMQClient("tcp://127.0.0.1:1")
    with pytest.raises(OperatorDeclined, match="no operator interface"):
        await zmq.confirm("set_column_valves", {}, "needs a go-ahead.")
    zmq.set_confirmer(ChainConfirmer([_Operator(None), _Operator(None)]))
    with pytest.raises(OperatorDeclined, match="no operator interface"):
        await zmq.confirm("set_column_valves", {}, "needs a go-ahead.")
    chain = ChainConfirmer([_Operator(None), _Operator(True)])
    assert await chain(ConfirmRequest("x", {}, "r")) is True


async def test_monitor_confirmer_talks_to_the_console_through_files(tmp_path):
    conf = MonitorConfirmer(tmp_path, timeout_s=5, poll_s=0.02)
    req = ConfirmRequest("set_column_valves", {"open": True}, "needs a go-ahead.")
    assert await conf(req) is None                              # no console running
    write_json(tmp_path / MonitorConfirmer.CONSOLE_FILE, {"seen": time.time() - 60})
    assert await conf(req) is None                              # a stale console

    write_json(tmp_path / MonitorConfirmer.CONSOLE_FILE, {"seen": time.time()})

    async def console(approved):
        while True:                                              # what the console does
            data = read_json(tmp_path / MonitorConfirmer.REQUEST_FILE)
            if data.get("id"):
                assert data["cmd"] == "set_column_valves"
                write_json(tmp_path / MonitorConfirmer.REPLY_FILE,
                           {"id": data["id"], "approved": approved})
                return
            await asyncio.sleep(0.01)

    for approved in (True, False):
        answer, _ = await asyncio.gather(
            conf(ConfirmRequest("set_column_valves", {"open": True}, "r")), console(approved))
        assert answer is approved
    assert not (tmp_path / MonitorConfirmer.REQUEST_FILE).exists()

    quick = MonitorConfirmer(tmp_path, timeout_s=0.1, poll_s=0.02)
    assert await quick(ConfirmRequest("x", {}, "r")) is False   # no answer in time


class _Session:
    def __init__(self, elicitation: bool):
        self.elicitation = elicitation

    def check_client_capability(self, caps) -> bool:
        return self.elicitation


class _Ctx:
    def __init__(self, elicitation, result=None):
        self.session = _Session(elicitation)
        self.result = result
        self.messages = []

    async def elicit(self, message, schema):
        self.messages.append(message)
        assert schema is Approve
        return self.result


class _MCP:
    def __init__(self, ctx):
        self.ctx = ctx

    def get_context(self):
        if self.ctx is None:
            raise ValueError("Context is not available outside of a request")
        return self.ctx


async def test_elicit_confirmer_asks_in_the_agents_host():
    from mcp.server.elicitation import AcceptedElicitation, DeclinedElicitation
    req = ConfirmRequest("set_column_valves", {"open": True}, "needs a go-ahead.")
    assert await ElicitConfirmer(_MCP(None))(req) is None       # not inside a tool call
    assert await ElicitConfirmer(_MCP(_Ctx(False)))(req) is None  # client can't elicit
    ctx = _Ctx(True, AcceptedElicitation(data=Approve(approve=True)))
    assert await ElicitConfirmer(_MCP(ctx))(req) is True
    assert "set_column_valves" in ctx.messages[0]
    assert await ElicitConfirmer(_MCP(_Ctx(True, AcceptedElicitation(
        data=Approve(approve=False)))))(req) is False
    assert await ElicitConfirmer(_MCP(_Ctx(True, DeclinedElicitation())))(req) is False


async def test_web_confirmer_shows_allow_and_deny_in_the_browser():
    assert await WebConfirmer()(ConfirmRequest("x", {}, "r")) is None    # unbound
    channel = OperatorChannel(EventBus())
    conf = WebConfirmer(channel, timeout_s=5)
    for answer, expected in (("Allow", True), ("Deny", False)):
        task = asyncio.create_task(conf(ConfirmRequest("set_aperture", {"size": 2}, "r")))
        await asyncio.sleep(0.05)
        open_ = channel.open_questions()
        assert len(open_) == 1 and open_[0]["kind"] == "confirm"
        assert open_[0]["options"] == ["Allow", "Deny"] and "set_aperture" in open_[0]["text"]
        assert channel.answer(open_[0]["id"], answer)
        assert await task is expected
    assert channel.agent_seen is None                  # the system asked, not the agent
    quick = WebConfirmer(channel, timeout_s=0.05)
    assert await quick(ConfirmRequest("x", {}, "r")) is False


def test_needs_confirmation_carries_the_reason():
    exc = NeedsConfirmation("because")
    assert exc.reason == "because" and json.dumps({"r": exc.reason})
