"""Async interpreter for action-block trees.

The interpreter knows nothing about the web console or MCP transport: it is
given a ``call_tool(name, args, path)`` coroutine and an ``emit(event)``
callback. The web queue supplies both, so that every call it makes shows up
as a block in the queue, and so do the tests (with fakes).

Run control (:class:`RunControl`) is checked *between* blocks: pause holds the
run before its next block, step lets exactly one block through, and abort
ends the run at the next check. A tool call in progress is never interrupted
from here. Stopping the stage mid-move is the out-of-band ``stop_stage``
path, which doesn't go through the queue.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from client.blocks import expr
from client.blocks.capabilities import Capabilities
from client.blocks.catalog import meta_for
from client.blocks.model import (Ask, Call, Fail, ForEach, If, Pause, Repeat, Seq, Sequence,
                                 SetVar, While)

CallTool = Callable[[str, dict, str], Awaitable[Any]]
Emit = Callable[[dict], None]


class Aborted(Exception):
    pass


class SequenceFailed(RuntimeError):
    pass


class RunControl:
    """Pause / step / abort shared by the queue and its running sequence."""

    def __init__(self) -> None:
        self._running = asyncio.Event()
        self._running.set()
        self._steps = 0
        self.aborted = False
        self.reason = ""

    @property
    def paused(self) -> bool:
        return not self._running.is_set()

    def pause(self) -> None:
        self._running.clear()

    def resume(self) -> None:
        self._steps = 0
        self._running.set()

    def step(self) -> None:
        """Let one block through, then pause again."""
        self._steps += 1
        self._running.set()

    def abort(self, reason: str = "aborted by operator") -> None:
        self.aborted = True
        self.reason = reason
        self._running.set()

    def reset_abort(self) -> None:
        self.aborted = False
        self.reason = ""

    async def gate(self) -> None:
        """Wait until the next block may start; raise :class:`Aborted`."""
        if self.aborted:
            raise Aborted(self.reason)
        await self._running.wait()
        if self.aborted:
            raise Aborted(self.reason)
        if self._steps:
            self._steps -= 1
            if not self._steps:
                self._running.clear()


class Interpreter:
    def __init__(self, call_tool: CallTool, caps: Capabilities | None = None,
                 emit: Emit | None = None, control: RunControl | None = None,
                 ask: Callable[[str, list[str]], Awaitable[str]] | None = None,
                 schemas: dict[str, dict] | None = None):
        self.call_tool = call_tool
        self.caps = caps or Capabilities()
        self.emit = emit or (lambda e: None)
        self.control = control or RunControl()
        self.ask = ask
        self.schemas = schemas or {}
        self.vars: dict[str, Any] = {}
        self._mode: str | None = None
        self.on_error = "abort"

    # -- expression environment ------------------------------------------------
    def _functions(self) -> dict:
        return {"has": self.caps.has, "mode": lambda: self._mode}

    async def _refresh_mode(self, path: str) -> str | None:
        if not self.caps.has("tool:get_instrument_mode"):
            return None
        result = await self.call_tool("get_instrument_mode", {}, path + "/mode?")
        if isinstance(result, dict):
            self._mode = result.get("mode")
        return self._mode

    async def _eval(self, source: str, path: str) -> Any:
        if expr.uses_function(source, "mode"):
            await self._refresh_mode(path)
        return expr.evaluate(source, self.vars, self._functions())

    # -- entry points ----------------------------------------------------------
    async def run(self, seq: Sequence, params: dict | None = None) -> dict:
        self.on_error = seq.on_error
        for p in seq.params:
            self.vars[p.name] = (params or {}).get(p.name, p.default)
        for key, value in (params or {}).items():
            self.vars.setdefault(key, value)
        await self.run_body(seq.body, "")
        return self.vars

    async def run_body(self, body: list, path: str) -> None:
        for i, node in enumerate(body):
            here = f"{path}/{i}"
            await self.control.gate()
            await self._run_node(node, here)

    # -- nodes -----------------------------------------------------------------
    async def _run_node(self, node, path: str) -> None:
        if isinstance(node, Call):
            await self._call(node, path)
        elif isinstance(node, Seq):
            await self.run_body(node.children, path)
        elif isinstance(node, If):
            taken = bool(await self._eval(node.cond, path))
            self.emit({"event": "branch", "path": path, "cond": node.cond, "taken": taken})
            await self.run_body(node.then if taken else node.orelse,
                                path + ("/then" if taken else "/else"))
        elif isinstance(node, Repeat):
            for k in range(node.count):
                if node.var:
                    self.vars[node.var] = k
                self.emit({"event": "iteration", "path": path, "index": k,
                           "of": node.count})
                await self.run_body(node.body, f"{path}/{k}")
        elif isinstance(node, While):
            k = 0
            while await self._eval(node.cond, path):
                if k >= node.max_iter:
                    raise SequenceFailed(f"{path}: while loop hit max_iter={node.max_iter}")
                self.emit({"event": "iteration", "path": path, "index": k})
                await self.run_body(node.body, f"{path}/{k}")
                k += 1
        elif isinstance(node, ForEach):
            items = await self._eval(node.items, path)
            if not isinstance(items, (list, tuple)):
                raise SequenceFailed(f"{path}: for-each needs a list, got "
                                     f"{type(items).__name__}")
            if len(items) > node.max_iter:
                raise SequenceFailed(f"{path}: {len(items)} items > max_iter={node.max_iter}")
            for k, item in enumerate(items):
                self.vars[node.var] = item
                self.emit({"event": "iteration", "path": path, "index": k,
                           "of": len(items)})
                await self.run_body(node.body, f"{path}/{k}")
        elif isinstance(node, SetVar):
            self.vars[node.var] = await self._eval(node.value, path)
            self.emit({"event": "set", "path": path, "var": node.var,
                       "value": _preview(self.vars[node.var])})
        elif isinstance(node, Ask):
            if self.ask is None:
                raise SequenceFailed(f"{path}: no operator channel to ask {node.question!r}")
            self.emit({"event": "ask", "path": path, "question": node.question})
            answer = await self.ask(node.question, node.options)
            if node.as_var:
                self.vars[node.as_var] = answer
        elif isinstance(node, Pause):
            self.emit({"event": "pause", "path": path, "message": node.message})
            self.control.pause()
            await self.control.gate()
        elif isinstance(node, Fail):
            raise SequenceFailed(node.message)
        else:
            raise SequenceFailed(f"{path}: unknown block {node!r}")

    async def _call(self, node: Call, path: str) -> None:
        meta = meta_for(node.tool)
        mode = meta.get("mode")
        if mode and self.caps.has("tool:get_instrument_mode"):
            current = await self._refresh_mode(path)
            if current and current != mode:
                if not node.ensure_mode:
                    raise SequenceFailed(
                        f"{path}: {node.tool} needs {mode} mode but the column is in "
                        f"{current}. Switch first, or set ensure_mode on the block.")
                await self.call_tool("set_instrument_mode", {"mode": mode},
                                     path + "/ensure_mode")
                self._mode = mode
        try:
            args = expr.resolve_args(node.args, self.vars, self._functions(),
                                     self.caps.resolve_role)
        except (expr.ExprError, LookupError) as exc:
            raise SequenceFailed(f"{path}: {node.tool} arguments: {exc}") from None
        schema = self.schemas.get(node.tool)
        if schema:
            _validate(node.tool, args, schema, path)
        try:
            result = await self.call_tool(node.tool, args, path)
        except Aborted:
            raise
        except Exception as exc:
            if self.on_error == "continue":
                self.vars["last_error"] = str(exc)
                self.emit({"event": "continue_after_error", "path": path,
                           "error": str(exc)})
                return
            raise
        self.vars["last"] = result
        if node.as_var:
            self.vars[node.as_var] = result


def _validate(tool: str, args: dict, schema: dict, path: str) -> None:
    try:
        import jsonschema
    except ImportError:          # pragma: no cover - jsonschema comes with mcp
        return
    try:
        jsonschema.validate(args, schema)
    except jsonschema.ValidationError as exc:
        where = ".".join(str(p) for p in exc.absolute_path) or "arguments"
        raise SequenceFailed(f"{path}: {tool} {where}: {exc.message}") from None


def _preview(value: Any, limit: int = 200) -> Any:
    text = repr(value)
    return value if len(text) <= limit else text[:limit] + "…"
