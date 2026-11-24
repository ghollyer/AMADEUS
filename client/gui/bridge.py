"""Thread bridge between Tk and the async ZMQ client.

Tkinter runs on the main thread. A daemon thread hosts an asyncio event loop
that owns the async :class:`ZMQClient`. Button handlers submit a coroutine and
return immediately; results come back as :class:`concurrent.futures.Future`.
The single REQ socket serialises requests, so a long acquisition (EDS / SI)
blocks later commands until it returns; set a generous timeout for those.

"Connected" means the socket exists; ZMQ connects lazily and silently, so
whether the server is *answering* is tracked separately: every reply (even an
error reply) marks it responding, and a transport timeout marks it not
responding. ``on_link(responding, detail)`` is called on the loop thread
whenever that changes. Quick probes (connect ping, state poll) use
``QUICK_TIMEOUT_MS`` so a dead link shows within seconds, not after the long
command timeout.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading

import zmq

from client.state.models import ImageCalibration
from client.transport.zmq_client import ZMQClient

# Set in the task of a command the operator already OK'd in the console.
_preconfirmed = contextvars.ContextVar("amadeus_preconfirmed", default=False)


QUICK_TIMEOUT_MS = 10_000


class ZmqBridge:
    """Owns the async :class:`ZMQClient` on a private asyncio loop thread.

    All public methods are safe to call from the Tk thread. ``call_*`` return
    a :class:`concurrent.futures.Future`; attach ``add_done_callback`` to it
    (the callback runs on the loop thread, so marshal back to Tk yourself).
    """

    def __init__(self, address: str, timeout_ms: int, store):
        self.address = address
        self.timeout_ms = timeout_ms
        self.connected = False
        self.responding: bool | None = None   # None = not known yet
        self.on_link = None                   # callable(responding, detail)
        # callable(ConfirmRequest, answer) run on the Tk side; it must call
        # answer(bool) once. None: commands that need a go-ahead are declined.
        self.on_confirm = None
        self._in_flight = 0
        self._store = store
        self._client: ZMQClient | None = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                        name="amadeus-zmq")
        self._thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro):
        return asyncio.run_coroutine_threadsafe(self._tracked(coro), self._loop)

    @property
    def busy(self) -> bool:
        """A request is in flight (later ones would queue behind it)."""
        return self._in_flight > 0

    async def _tracked(self, coro):
        self._in_flight += 1
        try:
            result = await coro
        except zmq.ZMQError as exc:
            # No reply in time (or socket trouble): the link is not answering.
            self._set_responding(False, f"no reply from {self.address} ({exc})")
            raise
        except RuntimeError as exc:
            # An error *reply* still proves the server answered; "not
            # connected" is ours, not the server's.
            if self._client is not None:
                self._set_responding(True, "")
            raise
        else:
            self._set_responding(True, "")
            return result
        finally:
            self._in_flight -= 1

    def _set_responding(self, responding: bool, detail: str) -> None:
        changed = responding != self.responding
        self.responding = responding
        if changed and self.on_link is not None:
            self.on_link(responding, detail)

    # -- connection ---------------------------------------------------------
    def connect(self, address: str | None = None):
        if address:
            self.address = address
        return self._submit(self._connect())

    async def _connect(self) -> dict:
        if self._client is not None:
            await self._client.disconnect()
        self._client = ZMQClient(self.address, timeout_ms=self.timeout_ms)
        self._client.set_confirmer(self._confirm)
        await self._client.connect()
        # The socket exists now: commands and polling may proceed, and the
        # status recovers on its own once the server starts answering.
        self.connected = True
        self.responding = None
        py = await self._client.ping(timeout_ms=QUICK_TIMEOUT_MS)
        return {"address": self.address, "server_python": py.splitlines()[0] if py else ""}

    async def _confirm(self, req) -> "bool | None":
        """The operator's own manual command needs a go-ahead (policy): ask on
        the Tk side and wait for the answer here, on the loop thread."""
        if _preconfirmed.get():
            return True
        if self.on_confirm is None:
            return None
        future = self._loop.create_future()

        def answer(ok: bool) -> None:
            self._loop.call_soon_threadsafe(
                lambda: future.done() or future.set_result(bool(ok)))
        self.on_confirm(req, answer)
        return await future

    def ping(self):
        return self._submit(self._ping())

    async def _ping(self) -> dict:
        if self._client is None:
            raise RuntimeError("not connected")
        return {"server_python": (await self._client.ping(timeout_ms=QUICK_TIMEOUT_MS)).splitlines()[0]}

    # -- commands -----------------------------------------------------------
    def call_data(self, cmd: str, params: dict, timeout_ms: int | None = None,
                  preconfirmed: bool = False):
        """*preconfirmed*: the operator already said OK to this exact command
        in the console, so a go-ahead check is answered without asking again."""
        return self._submit(self._call_data(cmd, params, timeout_ms, preconfirmed))

    async def _call_data(self, cmd: str, params: dict, timeout_ms: int | None = None,
                         preconfirmed: bool = False) -> dict:
        if self._client is None:
            raise RuntimeError("not connected")
        _preconfirmed.set(preconfirmed)        # this task only
        return await self._client.send_command(cmd, params, timeout_ms=timeout_ms)

    def call_out_of_band(self, cmd: str, params: dict, timeout_ms: int | None = None):
        """Like :meth:`call_data` but on its own socket, so it doesn't queue
        behind a command in flight (used for the Stage tab's STOP)."""
        return self._submit(self._call_out_of_band(cmd, params, timeout_ms))

    async def _call_out_of_band(self, cmd: str, params: dict,
                                timeout_ms: int | None = None) -> dict:
        if self._client is None:
            raise RuntimeError("not connected")
        return await self._client.send_command_out_of_band(cmd, params, timeout_ms=timeout_ms)

    def call_image(self, cmd: str, params: dict, name: str, tags: dict):
        return self._submit(self._call_image(cmd, params, name, tags))

    async def _call_image(self, cmd: str, params: dict, name: str, tags: dict) -> dict:
        if self._client is None:
            raise RuntimeError("not connected")
        meta, arr = await self._client.send_command_image(cmd, params)
        image_meta = meta.get("image_meta", {})
        data = meta.get("data", {})
        cal = image_meta.get("calibration") or {}
        calibration = ImageCalibration(
            x_scale=float(cal.get("x_scale", 1.0)),
            y_scale=float(cal.get("y_scale", 1.0)),
            units=str(cal.get("units", "nm")),
        )
        sidecar = self._store.save(
            arr,
            name=image_meta.get("name", name),
            calibration=calibration,
            tags={**tags, "calibration": cal},
        )
        return {"image_id": sidecar.image_id, "shape": sidecar.shape,
                "dtype": sidecar.dtype, **data}

    def close(self) -> None:
        if self._client is not None:
            try:
                self._submit(self._client.disconnect()).result(timeout=5)
            except Exception:
                pass
        self._loop.call_soon_threadsafe(self._loop.stop)
