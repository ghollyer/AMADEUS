"""Browser <-> agent conversation for the sidecar Claude Code session.

The agent runs in its own Claude Code session, attached to this process's
``/mcp`` endpoint. It can't be pushed to, so messages from the browser reach
it in two ways:

* ``wait_for_operator`` (an MCP tool) long-polls for instructions. The agent
  calls it whenever it has nothing else to do.
* While the agent is busy, unread instructions are appended to the result of
  its next tool call (:meth:`OperatorChannel.drain_unread`), so it sees them
  mid-task.

``ask_operator`` shows a question in the terminal pane and blocks until the
operator answers. :class:`WebConfirmer` does the same for a command that needs
the operator's go-ahead (``client/confirm.py``): an Allow/Deny entry, also
shown as a banner in the header.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import asdict, dataclass, field

from client.web.queue import EventBus


@dataclass
class Entry:
    id: str
    kind: str              # instruction | question | confirm | answer | status | system
    text: str
    who: str               # operator | agent | system
    t: float = field(default_factory=time.time)
    options: list[str] = field(default_factory=list)
    answered: bool = False
    answer: str | None = None
    delivered: bool = False

    def to_json(self) -> dict:
        return asdict(self)


class OperatorChannel:
    def __init__(self, bus: EventBus, history: int = 1000):
        self.bus = bus
        self.transcript: list[Entry] = []
        self._ids = itertools.count(1)
        self._unread: list[Entry] = []
        self._new = asyncio.Event()
        self._questions: dict[str, asyncio.Future] = {}
        self._history = history
        self.agent_seen: float | None = None   # last time the agent touched the channel
        self.agent_waiting = False             # agent is blocked in wait_for_operator

    def _add(self, entry: Entry) -> Entry:
        self.transcript.append(entry)
        del self.transcript[:-self._history]
        self.bus.publish("chat", entry.to_json())
        return entry

    def _touch(self) -> None:
        self.agent_seen = time.time()
        self.bus.publish("agent_presence", {"seen": self.agent_seen,
                                            "waiting": self.agent_waiting})

    # -- from the browser --------------------------------------------------------
    def post_instruction(self, text: str) -> Entry:
        entry = self._add(Entry(id=f"m{next(self._ids)}", kind="instruction",
                                text=text.strip(), who="operator"))
        self._unread.append(entry)
        self._new.set()
        return entry

    def answer(self, question_id: str, text: str) -> bool:
        future = self._questions.get(question_id)
        if future is None or future.done():
            return False
        future.set_result(text)
        return True

    # -- from the agent ------------------------------------------------------------
    def drain_unread(self) -> list[Entry]:
        entries, self._unread = self._unread, []
        self._new.clear()
        for e in entries:
            e.delivered = True
            self.bus.publish("chat", e.to_json())
        return entries

    async def wait_for_instructions(self, timeout_s: float) -> list[Entry]:
        self.agent_waiting = True
        self._touch()
        try:
            if not self._unread:
                try:
                    await asyncio.wait_for(self._new.wait(), timeout=max(0.0, timeout_s))
                except asyncio.TimeoutError:
                    return []
            return self.drain_unread()
        finally:
            self.agent_waiting = False
            self._touch()

    async def ask(self, question: str, options: list[str] | None = None,
                  timeout_s: float | None = None, who: str = "agent",
                  kind: str = "question") -> str:
        if who == "agent":
            self._touch()
        entry = self._add(Entry(id=f"q{next(self._ids)}", kind=kind,
                                text=question.strip(), who=who,
                                options=list(options or [])))
        future = asyncio.get_running_loop().create_future()
        self._questions[entry.id] = future
        try:
            if timeout_s:
                answer = await asyncio.wait_for(future, timeout=timeout_s)
            else:
                answer = await future
        except asyncio.TimeoutError:
            entry.answered = True
            entry.answer = None
            self.bus.publish("chat", entry.to_json())
            raise TimeoutError(f"the operator did not answer within {timeout_s:.0f} s")
        finally:
            self._questions.pop(entry.id, None)
        entry.answered = True
        entry.answer = answer
        self.bus.publish("chat", entry.to_json())
        self._add(Entry(id=f"a{next(self._ids)}", kind="answer", text=answer,
                        who="operator"))
        return answer

    def tell(self, message: str, who: str = "agent") -> Entry:
        if who == "agent":
            self._touch()
        return self._add(Entry(id=f"s{next(self._ids)}", kind="status",
                               text=message.strip(), who=who))

    async def confirm(self, req, timeout_s: float = 300.0) -> bool:
        """Allow/Deny for a held command (a ``client.confirm.ConfirmRequest``).
        No answer in time counts as Deny."""
        text = f"Allow {req.summary()}?\n{req.reason}"
        try:
            answer = await self.ask(text, ["Allow", "Deny"], timeout_s, who="system",
                                    kind="confirm")
        except TimeoutError:
            return False
        return answer.strip().lower() == "allow"

    def open_questions(self) -> list[dict]:
        return [e.to_json() for e in self.transcript
                if e.kind in ("question", "confirm") and not e.answered]


class WebConfirmer:
    """Operator confirmation in the browser (first in amadeus-web's chain).
    Bound to its channel after construction, since the channel is built after
    the MCP server; unbound, it can't reach anyone (``None``)."""

    def __init__(self, channel: "OperatorChannel | None" = None, timeout_s: float = 300.0):
        self.channel = channel
        self.timeout_s = timeout_s

    async def __call__(self, req) -> "bool | None":
        if self.channel is None:
            return None
        return await self.channel.confirm(req, self.timeout_s)
