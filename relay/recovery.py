"""Recovery helpers: duplicate requests and the state after a timeout.

* :class:`RequestLedger`: a client that re-sends a request with the same
  ``request_id`` (after a dropped connection, say) gets the original reply
  instead of running the command twice. A duplicate that arrives while the
  original is still running waits for it. Only JSON replies are kept, for a
  few minutes.
* :data:`READBACK`: after a command times out, the relay reads the matching
  state back (if the driver is free to answer) and puts it in the error, so
  the caller knows what is actually set instead of guessing whether the
  command landed.
"""

from __future__ import annotations

import asyncio
import collections
import time

# command -> the read that shows its effect.
READBACK = {
    "set_focus": "get_focus", "change_focus": "get_focus",
    "set_magnification": "get_magnification",
    "set_beam_shift": "get_beam_shift", "set_beam_tilt": "get_beam_tilt",
    "set_stigmation": "get_stigmation", "set_spot_size": "get_spot_size",
    "set_alpha": "get_alpha", "set_aperture": "get_aperture",
    "set_instrument_mode": "get_instrument_mode",
    "set_function_mode": "get_function_mode",
    "move_stage": "get_stage_status", "stage_poll": "get_stage_status",
    "stop_stage": "get_stage_status",
    "live_start": "live_status", "live_stop": "live_status",
    "si_start": "si_get_settings", "si_stop": "si_get_settings",
}

# Replies larger than this are not kept (images go through only once).
_MAX_KEPT_BYTES = 256 * 1024


class RequestLedger:
    def __init__(self, ttl_s: float = 300.0, max_entries: int = 256):
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._done: "collections.OrderedDict[str, tuple]" = collections.OrderedDict()
        self._running: dict = {}

    def _expire(self) -> None:
        now = time.monotonic()
        while self._done:
            rid, (t, _) = next(iter(self._done.items()))
            if now - t <= self.ttl_s and len(self._done) <= self.max_entries:
                break
            self._done.popitem(last=False)

    async def lookup(self, request_id: str) -> "list[bytes] | None":
        """The earlier reply to *request_id*, waiting for it if it is still
        running; ``None`` for a new request."""
        if not request_id:
            return None
        self._expire()
        if request_id in self._done:
            return list(self._done[request_id][1])
        future = self._running.get(request_id)
        if future is not None:
            return list(await asyncio.shield(future))
        return None

    def begin(self, request_id: str) -> None:
        if request_id:
            self._running[request_id] = asyncio.get_running_loop().create_future()

    def finish(self, request_id: str, frames: "list[bytes]") -> None:
        if not request_id:
            return
        future = self._running.pop(request_id, None)
        if future is not None and not future.done():
            future.set_result(list(frames))
        if sum(len(f) for f in frames) <= _MAX_KEPT_BYTES:
            self._done[request_id] = (time.monotonic(), list(frames))
            self._done.move_to_end(request_id)
            self._expire()
