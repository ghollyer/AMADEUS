"""Action blocks: model, expression safety, capabilities, executor."""

import asyncio
import tempfile

import pytest

from client.blocks import expr
from client.blocks.capabilities import Capabilities, check, requirements
from client.blocks.catalog import TOOL_META, build_catalog, parse_arg_docs
from client.blocks.executor import Aborted, Interpreter, RunControl, SequenceFailed
from client.blocks.model import load_sequence

def _dm_manifest():
    from drivers import dm
    slots = ["scan", "camera", "live", "spectrometer", "files"]
    cameras = [{"id": 0, "name": "OneView"}]
    detectors = [{"index": 0, "name": "BF"}, {"index": 1, "name": "HAADF det"}]
    return {"ok": True, "kind": "dm", "slots": slots, "commands": dm.commands_for(slots),
            "extras": [f"dm.{e}" for e in dm.EXTRAS], "vendor_call": True,
            "modes": [], "features": [], "cameras": cameras, "detectors": detectors,
            "devices": dm.devices_from_probe(cameras, detectors)}


SIM_MANIFEST = {
    "drivers": {
        "hw": {"ok": True, "kind": "sim", "slots": ["column", "stage"],
               "commands": ["get_focus", "set_focus", "get_instrument_mode",
                            "set_instrument_mode", "get_stage_position", "move_stage"],
               "extras": [], "modes": ["TEM", "STEM"], "features": ["piezo"],
               "devices": []},
        "dm": _dm_manifest(),
    },
    "instrument": {"focus": {"by_mode": {"TEM": {"dac_per_nm": 0.1, "dac_at_zero": 1},
                                         "STEM": None}}},
}


def caps(manifest=SIM_MANIFEST, tools=None):
    return Capabilities.from_manifest(manifest, tools if tools is not None else TOOL_META)


# ---------------------------------------------------------------------------
# Expressions: useful, but never a way to run code
# ---------------------------------------------------------------------------

def test_expr_reads_results_and_calls_whitelisted_functions():
    v = {"img": {"sharpness": [0.2, 0.9]}, "n": 3}
    assert expr.evaluate("$img.sharpness[1] > 0.5 and n == 3", v)
    assert expr.evaluate("max(img.sharpness) * 2", v) == 1.8
    assert expr.evaluate("has('mode:STEM')", {}, {"has": lambda c: c == "mode:STEM"})
    assert expr.evaluate("'a' if n > 2 else 'b'", v) == "a"


@pytest.mark.parametrize("source", [
    "__import__('os').system('true')",
    "().__class__.__bases__",
    "img.__class__",
    "open('/etc/passwd')",
    "(lambda: 1)()",
    "[x for x in range(3)]",
    "'a' * 10**9",
    "2 ** 10000",
    "len(x=1)",
])
def test_expr_rejects_escapes(source):
    with pytest.raises(expr.ExprError):
        expr.check(source)
        expr.evaluate(source, {"img": {"a": 1}})


def test_expr_attribute_is_dict_lookup_only():
    with pytest.raises(expr.ExprError):
        expr.evaluate("s.upper", {"s": "abc"})


def test_has_calls_only_counts_guarding_terms():
    assert expr.has_calls("has('a') and x > 1") == {"a"}
    assert expr.has_calls("has('a') or has('b')") == set()
    assert expr.has_calls("not has('a')") == set()


def test_resolve_args_expr_and_role():
    c = caps()
    args = expr.resolve_args({"x_nm": {"$expr": "t.x * 2"}, "signal_index": {"$role":
                              "detector:HAADF"}, "keep": [1, {"$expr": "1+1"}]},
                             {"t": {"x": 5}}, roles=c.resolve_role)
    assert args == {"x_nm": 10, "signal_index": 1, "keep": [1, 2]}


# ---------------------------------------------------------------------------
# Capabilities and compatibility
# ---------------------------------------------------------------------------

def test_manifest_to_capabilities():
    c = caps()
    assert {"cmd:move_stage", "cmd:stem_acquire", "mode:STEM", "feature:piezo",
            "detector:any", "detector:HAADF", "detector:BF", "camera:imaging",
            "calib:focus.TEM", "feature:scripting"} <= c.caps
    assert "calib:focus.STEM" not in c.caps
    assert c.resolve_role("detector:HAADF") == 1


def test_config_roles_win_and_unknown_indices_are_dropped():
    m = {**SIM_MANIFEST, "instrument": {"roles": {"detectors": {"HAADF": 0, "DF4": 7}}}}
    c = caps(m)
    assert c.resolve_role("detector:HAADF") == 0
    assert "detector:DF4" not in c.caps and any("DF4" in n for n in c.notes)


def test_dm_down_greys_out_dm_tools():
    m = {**SIM_MANIFEST, "drivers": {**SIM_MANIFEST["drivers"],
                                     "dm": {"ok": False, "kind": "dm", "error": "down"}}}
    seq = load_sequence({"name": "s", "body": [{"type": "call", "tool": "take_stem_image"}]})
    report = check(seq, caps(m))
    assert not report.ok
    assert {"cmd:stem_acquire", "detector:any"} <= {x.cap for x in report.missing}


def test_has_guard_removes_requirement_from_then_branch_only():
    seq = load_sequence({"name": "g", "body": [
        {"type": "if", "cond": "has('cmd:jeol.tem3_call')",
         "then": [{"type": "call", "tool": "execute_tem3_call",
                   "args": {"class_name": "EOS3", "method": "GetMagValue"}}],
         "else": [{"type": "call", "tool": "get_focus"}]}]})
    reqs = {c for c, _, _ in requirements(seq.body)}
    assert "cmd:jeol.tem3_call" not in reqs and "cmd:get_focus" in reqs
    assert check(seq, caps()).ok


def test_mode_guard_and_role_requirements_reported():
    seq = load_sequence({"name": "r", "body": [
        {"type": "call", "tool": "take_stem_image",
         "args": {"signal_index": {"$role": "detector:ABF"}}}]})
    report = check(seq, caps())
    assert [m.cap for m in report.missing] == ["detector:ABF"]
    assert report.mode_guards[0]["mode"] == "STEM"


def test_bad_expression_fails_check():
    seq = load_sequence({"name": "x", "body": [{"type": "set", "var": "a",
                                                 "value": "__import__('os')"}]})
    assert not check(seq, caps()).ok


def test_unregistered_tool_is_missing():
    seq = load_sequence({"name": "x", "body": [{"type": "call", "tool": "execute_dm_script",
                                                 "args": {"script": "1"}}]})
    tools = set(TOOL_META) - {"execute_dm_script"}
    report = check(seq, Capabilities.from_manifest(SIM_MANIFEST, tools,
                                                   allow_scripting=False))
    assert {"tool:execute_dm_script", "feature:scripting"} <= {m.cap for m in report.missing}


def test_sequence_round_trip_and_future_version_refused():
    data = {"name": "s", "body": [{"type": "if", "cond": "1", "then": [],
                                   "else": [{"type": "pause"}]}]}
    seq = load_sequence(data)
    assert load_sequence(seq.dump()).dump() == seq.dump()
    assert "else" in seq.dump()["body"][0]
    with pytest.raises(ValueError):
        load_sequence({**data, "schema_version": 99})


# ---------------------------------------------------------------------------
# The catalog covers every registered tool
# ---------------------------------------------------------------------------

def _registered_tools():
    from client.server import build_server
    mcp, *_ = build_server("tcp://127.0.0.1:1", tempfile.mkdtemp(), enable_monitor=False)
    return asyncio.run(mcp.list_tools())


def test_every_registered_tool_has_tool_meta():
    names = {t.name for t in _registered_tools()}
    assert names - set(TOOL_META) == set(), "add these tools to client/blocks/catalog.py"


def _known_wire_commands() -> set:
    from common.commands import COMMAND_SLOT, DRIVER_COMMANDS, RELAY_LOCAL_COMMANDS
    from drivers import dm
    from drivers.jeol.driver import JeolDriver
    return (set(COMMAND_SLOT) | DRIVER_COMMANDS | RELAY_LOCAL_COMMANDS
            | {f"dm.{e}" for e in dm.EXTRAS}
            | {f"jeol.{e}" for e in JeolDriver.extras} | {"shutdown_hw"})


def test_tool_meta_commands_are_real_relay_commands():
    known = _known_wire_commands()
    for name, meta in TOOL_META.items():
        for cap in meta["requires"]:
            if cap.startswith("cmd:"):
                assert cap[4:] in known, (name, cap)


def test_catalog_enriches_schema():
    entries = {e["name"]: e for e in build_catalog(_registered_tools())}
    props = entries["move_stage_relative"]["input_schema"]["properties"]
    assert props["speed"]["enum"] == ["slow", "normal", "fast"]
    assert entries["take_stem_image"]["input_schema"]["properties"]["detector"][
        "x-role"] == "detector"
    assert "dwell" in entries["take_stem_image"]["input_schema"]["properties"]["dwell_us"][
        "description"].lower()


def test_parse_arg_docs():
    summary, args = parse_arg_docs("""Do a thing.

    More text.

    Args:
        a: first
            continues
        b (int): second

    Returns:
        x
    """)
    assert summary == "Do a thing."
    assert args == {"a": "first continues", "b": "second"}


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

class FakeScope:
    def __init__(self, mode="STEM", fail=()):
        self.mode = mode
        self.calls = []
        self.fail = set(fail)

    async def __call__(self, tool, args, path):
        self.calls.append((tool, args))
        if tool in self.fail:
            raise RuntimeError(f"{tool} failed")
        if tool == "get_instrument_mode":
            return {"mode": self.mode}
        if tool == "set_instrument_mode":
            self.mode = args["mode"]
            return {"mode": self.mode}
        if tool == "image_sharpness":
            return {"scores": [0.1 * len(self.calls)]}
        return {"tool": tool, "args": args}


def _tools(scope):
    return [t for t, _ in scope.calls if t != "get_instrument_mode"]


async def test_executor_loops_branches_and_variables():
    scope = FakeScope()
    seq = load_sequence({"name": "loop", "params": [{"name": "n", "default": 3}], "body": [
        {"type": "repeat", "count": 2, "var": "i", "body": [
            {"type": "call", "tool": "get_focus", "as_var": "f"}]},
        {"type": "set", "var": "k", "value": "i + n"},
        {"type": "if", "cond": "k == 4 and f.tool == 'get_focus'",
         "then": [{"type": "call", "tool": "set_focus", "args": {"olf_dac": {"$expr": "k"}}}],
         "else": [{"type": "fail", "message": "wrong"}]},
        {"type": "for_each", "var": "x", "items": "[1, 2]", "body": [
            {"type": "call", "tool": "change_focus", "args": {"delta_dac": {"$expr": "x"}}}]},
    ]})
    out = await Interpreter(scope, caps()).run(seq, {"n": 3})
    assert _tools(scope) == ["get_focus", "get_focus", "set_focus", "change_focus",
                             "change_focus"]
    assert scope.calls[2] == ("set_focus", {"olf_dac": 4})
    assert out["k"] == 4


async def test_executor_mode_guard_refuses_or_switches():
    scope = FakeScope(mode="TEM")
    seq = load_sequence({"name": "g", "body": [{"type": "call", "tool": "take_stem_image"}]})
    with pytest.raises(SequenceFailed, match="needs STEM"):
        await Interpreter(scope, caps()).run(seq)
    seq.body[0].ensure_mode = True
    await Interpreter(scope, caps()).run(seq)
    assert _tools(scope)[-2:] == ["set_instrument_mode", "take_stem_image"]


async def test_executor_while_is_bounded():
    seq = load_sequence({"name": "w", "body": [
        {"type": "while", "cond": "True", "max_iter": 3, "body": []}]})
    with pytest.raises(SequenceFailed, match="max_iter"):
        await Interpreter(FakeScope(), caps()).run(seq)


async def test_executor_error_policy():
    scope = FakeScope(fail={"get_focus"})
    body = [{"type": "call", "tool": "get_focus"}, {"type": "call", "tool": "set_focus",
                                                     "args": {"olf_dac": 1}}]
    with pytest.raises(RuntimeError):
        await Interpreter(scope, caps()).run(load_sequence({"name": "a", "body": body}))
    out = await Interpreter(scope, caps()).run(
        load_sequence({"name": "c", "body": body, "on_error": "continue"}))
    assert "get_focus failed" in out["last_error"]
    assert scope.calls[-1][0] == "set_focus"


async def test_executor_validates_arguments_against_schema():
    seq = load_sequence({"name": "v", "body": [{"type": "call", "tool": "set_focus",
                                                 "args": {"olf_dac": "high"}}]})
    schemas = {"set_focus": {"type": "object", "properties": {
        "olf_dac": {"anyOf": [{"type": "integer"}, {"type": "null"}]}}}}
    scope = FakeScope()
    with pytest.raises(SequenceFailed, match="olf_dac"):
        await Interpreter(scope, caps(), schemas=schemas).run(seq)
    assert scope.calls == []


async def test_run_control_pause_step_abort():
    control = RunControl()
    scope = FakeScope()
    seq = load_sequence({"name": "p", "body": [{"type": "call", "tool": "get_focus"}] * 3})
    control.pause()
    task = asyncio.create_task(Interpreter(scope, caps(), control=control).run(seq))
    await asyncio.sleep(0.05)
    assert scope.calls == []
    control.step()
    await asyncio.sleep(0.05)
    assert len(scope.calls) == 1
    control.abort()
    with pytest.raises(Aborted):
        await task
    assert len(scope.calls) == 1


async def test_pause_block_waits_for_resume():
    control = RunControl()
    scope = FakeScope()
    seq = load_sequence({"name": "p", "body": [{"type": "pause"},
                                               {"type": "call", "tool": "get_focus"}]})
    task = asyncio.create_task(Interpreter(scope, caps(), control=control).run(seq))
    await asyncio.sleep(0.05)
    assert control.paused and scope.calls == []
    control.resume()
    await task
    assert _tools(scope) == ["get_focus"]


DRIVER_MANIFEST = {
    "hw": {}, "dm": {},
    "drivers": {
        "col": {"ok": True, "kind": "jeol", "slots": ["column", "stage"],
                "commands": ["get_focus", "set_focus", "move_stage"],
                "extras": ["jeol.tem3_call"], "vendor_call": True,
                "modes": ["TEM", "STEM"], "features": ["piezo"], "devices": []},
        "tvips": {"ok": True, "kind": "remote", "slots": ["camera"],
                  "commands": ["camera_acquire", "list_cameras"], "extras": [],
                  "devices": [{"id": "camera/0", "slot": "camera", "name": "XF416"}]},
        "gatan": {"ok": False, "kind": "dm", "slots": ["scan"], "error": "DM is down"},
    },
    "instrument": {"roles": {"cameras": {"diffraction": "tvips/camera/0",
                                         "eels": "gatan/camera/9"}}},
}


def test_capabilities_from_the_per_driver_manifest():
    c = caps(DRIVER_MANIFEST)
    assert {"cmd:get_focus", "cmd:camera_acquire", "cmd:jeol.tem3_call",
            "cmd:vendor_call", "driver:jeol", "driver:remote", "feature:piezo",
            "camera:any", "camera:diffraction", "camera:imaging"} <= c.caps
    assert "driver:dm" not in c.caps and "cmd:stem_acquire" not in c.caps
    assert "camera:eels" not in c.caps                      # no such device
    assert any("gatan" in n and "DM is down" in n for n in c.notes)
    assert c.resolve_role("camera:diffraction") == "tvips/camera/0"


class _RecordingZMQ:
    """Answers everything plausibly and records the commands a tool sends."""

    def __init__(self):
        self.sent = []

    def _data(self, cmd):
        return {"mode": "STEM", "focus_dac": 32768, "lenses": {
                    "focus.fine": {"value": 32768, "range": [0, 65535]}},
                "x_m": 0.0, "y_m": 0.0, "z_m": 0.0, "alpha_deg": 0.0, "beta_deg": 0.0,
                "settled": True, "windows": [], "files": [], "eof": True}

    async def send_command(self, cmd, params=None, **_):
        self.sent.append(cmd)
        return self._data(cmd)

    send_command_out_of_band = send_command

    async def send_command_image(self, cmd, params=None, **_):
        import numpy as np
        self.sent.append(cmd)
        return ({"image_meta": {"name": "x", "calibration": {}}, "data": {}},
                np.zeros((8, 8), dtype=np.float32))


def _fill(schema: dict) -> dict:
    """Plausible arguments for a tool's required parameters."""
    out = {}
    for name in schema.get("required", []):
        prop = schema["properties"][name]
        kinds = {t.get("type") for t in prop.get("anyOf", [prop])}
        out[name] = (0.5 if "number" in kinds else 1 if "integer" in kinds
                     else True if "boolean" in kinds else [] if "array" in kinds
                     else {} if "object" in kinds else "x")
    return out


async def test_tools_send_only_the_commands_their_tool_meta_declares(tmp_path):
    # A tool that sends a command its TOOL_META entry doesn't list would show
    # as available on a microscope that can't run it. Run every tool against
    # a recording fake and compare.
    from mcp.server.fastmcp import FastMCP
    from client.server import build_server
    from common.commands import RELAY_LOCAL_COMMANDS
    mcp, zmq_client, state, _ = build_server("tcp://127.0.0.1:1", tmp_path,
                                             enable_monitor=False)
    fake = _RecordingZMQ()
    optional = {"stem_autofocus": {"dm.list_windows", "dm.close_window"},
                "take_stem_image": {"get_magnification", "get_high_tension",
                                    "get_spot_size"},
                "take_camera_image": {"get_magnification", "get_high_tension",
                                      "get_spot_size"},
                "save_dm_window": {"load_file", "read_file_chunk"}}
    checked = 0
    for tool in await mcp.list_tools():
        meta = TOOL_META[tool.name]
        if not meta["hardware"] or not any(r.startswith("cmd:") for r in meta["requires"]):
            continue
        fake.sent.clear()
        fn = mcp._tool_manager.get_tool(tool.name).fn
        # Swap the client the tool closed over for the recorder.
        cells = [c for c in (fn.__closure__ or []) if c.cell_contents is zmq_client]
        if not cells:
            continue
        for c in cells:
            c.cell_contents = fake
        try:
            await fn(**_fill(tool.inputSchema))
        except Exception:
            pass
        finally:
            for c in cells:
                c.cell_contents = zmq_client
        declared = {r[4:] for r in meta["requires"] if r.startswith("cmd:")}
        extra = set(fake.sent) - declared - optional.get(tool.name, set()) \
            - RELAY_LOCAL_COMMANDS - {"set_beam_blank"}
        assert not extra, (tool.name, extra)
        checked += 1
    assert checked > 25
