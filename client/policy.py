"""Operational preferences, enforced.

``OperationalPreferences`` was stored but never acted on. This applies two of
its rules at the ZMQ chokepoint, so every tool, the web console's queue and
sequences get them without per-tool code. It is vendor-neutral: it only uses
``set_beam_blank``, which any column driver may implement.

* ``blank_between_acquisitions``: the beam is unblanked just before an
  acquisition and blanked again right after a synchronous one (STEM frame,
  camera image), so the sample isn't exposed between acquisitions. Only a
  blank this policy applied is undone: a beam the operator blanked stays
  blanked. Asynchronous acquisitions (spectrum imaging, EDS, live view) are
  never blanked mid-run; the beam is blanked when the live view stops.
* ``post_move_pause_s``: after a stage move that settled, wait this long
  before the next command, so drift from the move can die down.
* ``max_beam_shift``: a ``set_beam_shift`` further than this from neutral (in
  the device units ``get_beam_shift`` reports) needs the operator's
  confirmation (:mod:`client.confirm`).

The relay-side policy (``relay/policy.py``) covers instrument safety rules
from the config; this covers the session's preferences, which live here.
"""

from __future__ import annotations

import asyncio
import contextvars

from client.confirm import NeedsConfirmation

# Acquisitions that need the beam (unblank first, when we blanked it).
NEEDS_BEAM = frozenset({"stem_acquire", "camera_acquire", "eds_acquire_point",
                        "eds_acquire_map", "si_start", "live_start"})
# Acquisitions that are finished when the reply arrives (blank after).
SYNCHRONOUS = frozenset({"stem_acquire", "camera_acquire"})

_inside = contextvars.ContextVar("amadeus_policy_inside", default=False)


class PreferencePolicy:
    def __init__(self, state):
        self.state = state
        self.blanked_by_us = False
        self.live_running = False
        self.unsupported = False         # the column driver can't blank
        self.beam_shift_neutral = None   # read once from get_beam_shift
        self.notes: list[str] = []

    @staticmethod
    def active() -> bool:
        """True while the policy itself is sending a command."""
        return _inside.get()

    async def _blank(self, zmq, blanked: bool) -> None:
        token = _inside.set(True)
        try:
            await zmq.send_command("set_beam_blank", {"blanked": blanked})
        except Exception as exc:
            text = str(exc)
            if "not implemented" in text or "no driver" in text or "unknown command" in text:
                self.unsupported = True
                self.notes.append(f"blank_between_acquisitions has no effect: {text}")
                return
            raise
        finally:
            _inside.reset(token)
        self.blanked_by_us = blanked

    async def _neutral(self, zmq) -> float:
        if self.beam_shift_neutral is None:
            token = _inside.set(True)
            try:
                data = await zmq.send_command("get_beam_shift")
            finally:
                _inside.reset(token)
            neutral = data.get("neutral")
            if neutral is None:
                rng = data.get("range") or [0, 65535]
                neutral = (float(rng[0]) + float(rng[1]) + 1) / 2
            self.beam_shift_neutral = float(neutral)
        return self.beam_shift_neutral

    async def before(self, zmq, cmd: str, params: dict) -> None:
        prefs = self.state.preferences
        if cmd == "set_beam_shift" and not params.get("confirmed"):
            limit = float(prefs.max_beam_shift)
            neutral = await self._neutral(zmq)
            worst = max(abs(float(params.get(k, neutral)) - neutral) for k in ("x", "y"))
            if worst > limit:
                raise NeedsConfirmation(
                    f"this beam shift is {worst:g} device units from neutral ({neutral:g}), "
                    f"beyond max_beam_shift {limit:g} (operational preferences).")
        if (cmd in NEEDS_BEAM and prefs.blank_between_acquisitions
                and self.blanked_by_us and not self.unsupported):
            await self._blank(zmq, False)

    async def after(self, zmq, cmd: str, data, error) -> None:
        prefs = self.state.preferences
        if error is not None:
            return
        if cmd == "live_start":
            self.live_running = True
        elif cmd == "live_stop":
            self.live_running = False
        blank_now = (cmd in SYNCHRONOUS and not self.live_running) or cmd == "live_stop"
        if blank_now and prefs.blank_between_acquisitions and not self.unsupported:
            await self._blank(zmq, True)
        if (cmd == "move_stage" and isinstance(data, dict) and data.get("settled")
                and prefs.post_move_pause_s > 0):
            await asyncio.sleep(float(prefs.post_move_pause_s))
