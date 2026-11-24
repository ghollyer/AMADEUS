"""Operator confirmation: the human half of the safety rules.

The relay refuses some commands until a human agrees (``policy.confirm``,
switching inside an ``exclusive`` device group, raw vendor writes; see
``relay/policy.py``), and the session preferences refuse a few more
(``max_beam_shift``, ``client/policy.py``). Such a refusal is *confirmable*:
the relay's reply carries ``needs_confirmation: true``.

``ZMQClient`` (the chokepoint all instrument traffic goes through) then asks a
**confirmer** and, on Allow, resends the command once with ``confirmed: true``.
It drops any ``confirmed`` a caller passes, so an agent can't confirm for
itself: only a confirmer can.

A confirmer is ``async (ConfirmRequest) -> bool | None``: ``True`` Allow,
``False`` Deny, ``None`` "no operator reachable this way". The interfaces:

* :class:`MonitorConfirmer`: a dialog in ``amadeus-console`` (also in
  ``--read-only``), through files in ``<data-dir>/monitor/`` next to the pause
  flag. Available while a console is running.
* :class:`ElicitConfirmer`: an MCP elicitation prompt in the agent's own host
  (Claude Code), when that client supports elicitation.
* ``WebConfirmer`` (``client/web/agent_channel.py``): a dialog in amadeus-web.
* The Tk console's own manual commands: an OK/Cancel box (``client/gui/bridge.py``).

:class:`ChainConfirmer` tries them in order. If none can reach an operator,
the command is declined.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from pydantic import BaseModel, Field


class NeedsConfirmation(RuntimeError):
    """A confirmable refusal (from the relay or the session preferences)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class OperatorDeclined(RuntimeError):
    """The operator said no, didn't answer in time, or couldn't be asked."""


@dataclass
class ConfirmRequest:
    cmd: str
    params: dict
    reason: str
    id: str = ""
    t: float = field(default_factory=time.time)

    def to_json(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        shown = {k: v for k, v in self.params.items() if k != "confirmed"}
        return f"{self.cmd} {json.dumps(shown, default=str)}"


Confirmer = Callable[[ConfirmRequest], Awaitable["bool | None"]]

NO_OPERATOR = ("no operator interface is available to confirm it. Open amadeus-console "
               "(or amadeus-web) so the operator can answer, then try again")

_ids = itertools.count(1)


def new_id() -> str:
    return f"c{int(time.time() * 1000)}-{next(_ids)}"


class ChainConfirmer:
    """Ask each confirmer in turn; the first that can reach an operator answers."""

    def __init__(self, confirmers: "list[Confirmer]"):
        self.confirmers = [c for c in confirmers if c is not None]

    async def __call__(self, req: ConfirmRequest) -> "bool | None":
        for confirmer in self.confirmers:
            answer = await confirmer(req)
            if answer is not None:
                return bool(answer)
        return None


class MonitorConfirmer:
    """A dialog in amadeus-console, through files in the monitor directory.

    * ``console.json`` — ``{"seen": <unix time>}``, written by the console on
      every refresh. The console counts as present while it is fresh.
    * ``confirm_request.json`` — the open request (written here).
    * ``confirm_reply.json`` — ``{"id", "approved"}`` (written by the console).
    """

    REQUEST_FILE = "confirm_request.json"
    REPLY_FILE = "confirm_reply.json"
    CONSOLE_FILE = "console.json"

    def __init__(self, monitor_dir: "Path | str", timeout_s: float = 120.0,
                 fresh_s: float = 5.0, poll_s: float = 0.2):
        self.dir = Path(monitor_dir)
        self.timeout_s = timeout_s
        self.fresh_s = fresh_s
        self.poll_s = poll_s
        self._lock = asyncio.Lock()

    def console_present(self) -> bool:
        data = read_json(self.dir / self.CONSOLE_FILE)
        try:
            return time.time() - float(data.get("seen", 0)) < self.fresh_s
        except (TypeError, ValueError):
            return False

    async def __call__(self, req: ConfirmRequest) -> "bool | None":
        if not self.console_present():
            return None
        async with self._lock:                       # one dialog at a time
            req.id = req.id or new_id()
            reply_path = self.dir / self.REPLY_FILE
            write_json(self.dir / self.REQUEST_FILE, req.to_json())
            try:
                deadline = time.monotonic() + self.timeout_s
                while time.monotonic() < deadline:
                    reply = read_json(reply_path)
                    if reply.get("id") == req.id:
                        return bool(reply.get("approved"))
                    # The console's heartbeat stops while its dialog is open,
                    # so only the timeout ends the wait.
                    await asyncio.sleep(self.poll_s)
                return False                             # no answer: declined
            finally:
                for path in (self.dir / self.REQUEST_FILE, reply_path):
                    try:
                        path.unlink()
                    except OSError:
                        pass


class Approve(BaseModel):
    approve: bool = Field(description="Allow this microscope command?")


class ElicitConfirmer:
    """An MCP elicitation prompt in the agent's host (e.g. Claude Code).

    Only inside an MCP tool call, and only when the client declared the
    elicitation capability; otherwise ``None``."""

    def __init__(self, mcp):
        self.mcp = mcp

    async def __call__(self, req: ConfirmRequest) -> "bool | None":
        from mcp import types
        try:
            ctx = self.mcp.get_context()
            session = ctx.session
        except Exception:
            return None
        try:
            if not session.check_client_capability(
                    types.ClientCapabilities(elicitation=types.ElicitationCapability())):
                return None
        except Exception:
            return None
        message = (f"AMADEUS needs the operator's go-ahead.\n\n{req.summary()}\n\n"
                   f"{req.reason}\n\nAllow it?")
        result = await ctx.elicit(message, schema=Approve)
        return result.action == "accept" and bool(getattr(result.data, "approve", False))


def read_json(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path: Path, obj: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, default=str), encoding="utf-8")
    os.replace(tmp, path)
