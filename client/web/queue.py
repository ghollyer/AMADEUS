"""The action queue: every tool call the web console runs, in order.

Top-level items are single tool calls (from the agent or the operator) or
whole sequences. One runner task executes them FIFO, one block at a time,
which matches the instrument link: the relay connection is one REQ socket
anyway. While a sequence runs, each call it makes is appended as a child
block, so loops scroll the queue down as they execute.

Every state change is published on the :class:`EventBus` (the browser's SSE
stream). The browser never polls the queue.

Operator control (:class:`~client.blocks.executor.RunControl`) is checked
before every block, top-level or inside a sequence. A call already running
is never interrupted here. Stopping the stage uses the out-of-band
``stop_stage`` path instead.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable

from client.blocks.capabilities import Capabilities
from client.blocks.executor import Aborted, Interpreter, RunControl
from client.blocks.model import Sequence

_PREVIEW = 600


class EventBus:
    """Fan-out of JSON events to any number of SSE subscribers."""

    def __init__(self, backlog: int = 500):
        self._subs: set[asyncio.Queue] = set()
        self._backlog = backlog

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._backlog)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, kind: str, data: Any) -> None:
        event = {"kind": kind, "data": data, "t": time.time()}
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:     # a stuck browser tab must not grow memory
                self._subs.discard(q)


def preview(value: Any, limit: int = _PREVIEW) -> str:
    try:
        text = json.dumps(value, default=str)
    except Exception:
        text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


@dataclass
class Block:
    id: str
    kind: str                 # "call" | "sequence"
    origin: str               # "agent" | "manual" | "sequence"
    label: str
    tool: str | None = None
    args: dict = field(default_factory=dict)
    parent: str | None = None
    path: str | None = None
    status: str = "queued"    # queued running done error aborted removed
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    result: str | None = None
    error: str | None = None
    image_id: str | None = None
    children: list[str] = field(default_factory=list)
    note: str | None = None

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class _Item:
    block: Block
    future: asyncio.Future
    sequence: Sequence | None = None
    params: dict | None = None


CallTool = Callable[[str, dict], Awaitable[Any]]


def _future() -> asyncio.Future:
    """A result future nobody may await (manual blocks): mark its exception
    retrieved so asyncio doesn't log "never retrieved" for every failed block."""
    future = asyncio.get_running_loop().create_future()
    future.add_done_callback(lambda f: f.cancelled() or f.exception())
    return future


class BlockQueue:
    def __init__(self, call_tool: CallTool, bus: EventBus,
                 caps: Callable[[], Capabilities],
                 ask: Callable[[str, list[str]], Awaitable[str]] | None = None,
                 schemas: Callable[[], dict] | None = None,
                 history: int = 2000):
        self._call_tool = call_tool
        self.bus = bus
        self._caps = caps
        self._ask = ask
        self._schemas = schemas or (lambda: {})
        self.control = RunControl()
        self.blocks: dict[str, Block] = {}
        self.order: list[str] = []            # top-level ids, oldest first
        self._pending: asyncio.Queue[_Item] = asyncio.Queue()
        self._items: dict[str, _Item] = {}
        self._ids = itertools.count(1)
        self._history = history
        self._runner: asyncio.Task | None = None
        self.current: str | None = None

    # -- lifecycle -------------------------------------------------------------
    def start(self) -> None:
        if self._runner is None:
            self._runner = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        if self._runner:
            self._runner.cancel()
            try:
                await self._runner
            except (asyncio.CancelledError, Exception):
                pass
            self._runner = None

    # -- submission ------------------------------------------------------------
    def _new_id(self) -> str:
        return f"b{next(self._ids)}"

    def _add(self, block: Block, top_level: bool = True) -> None:
        self.blocks[block.id] = block
        if top_level:
            self.order.append(block.id)
            self._trim()
        self.bus.publish("block", block.to_json())

    def _trim(self) -> None:
        while len(self.order) > self._history:
            oldest = self.blocks.get(self.order[0])
            if oldest is None or oldest.status in ("queued", "running"):
                break
            self.order.pop(0)
            for cid in [oldest.id, *oldest.children]:
                self.blocks.pop(cid, None)

    def enqueue_call(self, tool: str, args: dict | None = None, origin: str = "manual",
                     label: str | None = None) -> tuple[Block, asyncio.Future]:
        block = Block(id=self._new_id(), kind="call", origin=origin, tool=tool,
                      args=dict(args or {}), label=label or tool)
        future = _future()
        item = _Item(block, future)
        self._items[block.id] = item
        self._add(block)
        self._pending.put_nowait(item)
        return block, future

    async def submit_call(self, tool: str, args: dict | None = None,
                          origin: str = "agent") -> Any:
        """Queue a call and wait for its raw result (raises on tool error)."""
        _, future = self.enqueue_call(tool, args, origin)
        return await future

    def enqueue_sequence(self, seq: Sequence, params: dict | None = None,
                         origin: str = "manual") -> tuple[Block, asyncio.Future]:
        block = Block(id=self._new_id(), kind="sequence", origin=origin, label=seq.name,
                      args=dict(params or {}))
        future = _future()
        item = _Item(block, future, sequence=seq, params=params)
        self._items[block.id] = item
        self._add(block)
        self._pending.put_nowait(item)
        return block, future

    def remove(self, block_id: str) -> bool:
        """Drop a block that hasn't started yet."""
        item = self._items.get(block_id)
        if item is None or item.block.status != "queued":
            return False
        self._set(item.block, status="removed", finished=time.time(),
                  error="removed by operator")
        if not item.future.done():
            item.future.set_exception(RuntimeError(
                "the operator removed this call from the queue before it ran"))
        return True

    def clear(self) -> int:
        """Remove every block still waiting to run."""
        return sum(self.remove(bid) for bid in list(self._items)
                   if self._items[bid].block.status == "queued")

    # -- control ---------------------------------------------------------------
    def pause(self) -> None:
        self.control.pause()
        self._publish_state()

    def resume(self) -> None:
        self.control.resume()
        self._publish_state()

    def step(self) -> None:
        self.control.step()
        self._publish_state()

    def abort(self) -> None:
        """Abort the running item at its next block boundary."""
        if self.current is not None:
            self.control.abort()
        self._publish_state()

    def state(self) -> dict:
        return {"paused": self.control.paused, "current": self.current,
                "queued": sum(1 for i in self._items.values() if i.block.status == "queued")}

    def _publish_state(self) -> None:
        self.bus.publish("queue_state", self.state())

    def snapshot(self) -> dict:
        blocks = []
        for bid in self.order:
            top = self.blocks.get(bid)
            if top is None:
                continue
            blocks.append(top.to_json())
            blocks.extend(self.blocks[c].to_json() for c in top.children if c in self.blocks)
        return {"state": self.state(), "blocks": blocks}

    # -- execution -------------------------------------------------------------
    def _set(self, block: Block, **changes) -> None:
        for key, value in changes.items():
            setattr(block, key, value)
        self.bus.publish("block", block.to_json())

    async def _run_forever(self) -> None:
        while True:
            item = await self._pending.get()
            if item.block.status != "queued":
                self._items.pop(item.block.id, None)
                continue
            if item.sequence is None:
                # A sequence gates before each of its own blocks instead, so a
                # single "step" runs one block, not "start the sequence".
                try:
                    await self.control.gate()
                except Aborted:
                    pass                  # abort with nothing running: ignore
            self.control.reset_abort()
            if item.block.status != "queued":       # removed while paused
                self._items.pop(item.block.id, None)
                continue
            self.current = item.block.id
            self._publish_state()
            try:
                if item.sequence is not None:
                    await self._run_sequence(item)
                else:
                    await self._run_call(item)
            finally:
                self.current = None
                self.control.reset_abort()
                self._items.pop(item.block.id, None)
                self._publish_state()

    async def _execute(self, block: Block) -> Any:
        """Run one tool call for *block*, recording the outcome on it."""
        self._set(block, status="running", started=time.time())
        try:
            result = await self._call_tool(block.tool, block.args)
        except Exception as exc:
            self._set(block, status="error", finished=time.time(), error=str(exc))
            raise
        image_id = result.get("image_id") if isinstance(result, dict) else None
        self._set(block, status="done", finished=time.time(), result=preview(result),
                  image_id=image_id if isinstance(image_id, str) else None)
        if block.image_id:
            self.bus.publish("image", {"image_id": block.image_id, "block": block.id})
        return result

    async def _run_call(self, item: _Item) -> None:
        try:
            result = await self._execute(item.block)
        except Exception as exc:
            if not item.future.done():
                item.future.set_exception(exc)
            return
        if not item.future.done():
            item.future.set_result(result)

    async def _run_sequence(self, item: _Item) -> None:
        parent = item.block
        self._set(parent, status="running", started=time.time())

        async def call_tool(tool: str, args: dict, path: str) -> Any:
            child = Block(id=self._new_id(), kind="call", origin="sequence", tool=tool,
                          args=args, label=tool, parent=parent.id, path=path)
            parent.children.append(child.id)
            self._add(child, top_level=False)
            return await self._execute(child)

        def emit(event: dict) -> None:
            self.bus.publish("sequence_event", {"block": parent.id, **event})

        interp = Interpreter(call_tool, self._caps(), emit=emit, control=self.control,
                             ask=self._ask, schemas=self._schemas())
        try:
            out = await interp.run(item.sequence, item.params)
        except Aborted as exc:
            self._set(parent, status="aborted", finished=time.time(), error=str(exc))
            if not item.future.done():
                item.future.set_exception(exc)
            return
        except Exception as exc:
            self._set(parent, status="error", finished=time.time(), error=str(exc))
            if not item.future.done():
                item.future.set_exception(exc)
            return
        variables = {k: v for k, v in out.items() if k not in ("last",)}
        self._set(parent, status="done", finished=time.time(), result=preview(variables))
        if not item.future.done():
            item.future.set_result(out)
