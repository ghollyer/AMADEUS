"""Async ZMQ REQ client for communicating with the DM server.

Usage
-----
from client.transport.zmq_client import ZMQClient

async with ZMQClient("tcp://microscope-pc:9876") as client:
    data = await client.send_command("get_focus")
    meta, arr = await client.send_command_image("stem_acquire", {"mode": "capture"})
"""

import asyncio
import json
import uuid
from typing import Any

import numpy as np
import zmq
import zmq.asyncio

from client.confirm import (NO_OPERATOR, ConfirmRequest, NeedsConfirmation,
                            OperatorDeclined)


class InstrumentTimeout(zmq.Again):
    """No reply from the AMADEUS server in time.

    Subclasses ``zmq.Again`` so existing handlers still catch it, but the
    message says what happened and what to check, instead of the errno text
    "Resource temporarily unavailable"."""

    def __init__(self, cmd: str, address: str, timeout_s: float):
        self.cmd = cmd
        self.address = address
        self.timeout_s = timeout_s
        message = (
            f"No reply from the AMADEUS server at {address} to {cmd!r} within "
            f"{timeout_s:.0f} s. The command may still be executing on the "
            f"instrument; a timeout does not cancel a stage move or an "
            f"acquisition. Check the instrument state (get_stage_status / "
            f"get_stage_position, get_si_status, backend_status) before "
            f"re-sending it. If backend_status also times out, the AMADEUS "
            f"server process itself is unresponsive. The client connection "
            f"has been reset.")
        super().__init__()            # zmq.Again ignores errno/msg arguments
        self.strerror = message


class ZMQClient:
    """Async ZMQ REQ socket that talks to the DM server."""

    def __init__(self, address: str = "tcp://localhost:9876", timeout_ms: int = 30_000):
        self.address = address
        self.timeout_ms = timeout_ms
        self._context: zmq.asyncio.Context | None = None
        self._socket: zmq.asyncio.Socket | None = None
        self._lock = asyncio.Lock()
        # Optional observer notified around every command (telemetry + pause
        # gate). Must expose before_command(cmd, params) — which may raise to
        # veto the send — and after_command(cmd, data, error=None). Kept as a
        # duck-typed hook so the transport carries no dependency on the monitor.
        self._monitor = None
        # Optional session-preference policy (client.policy.PreferencePolicy):
        # async before(zmq, cmd, params) / after(zmq, cmd, data, error),
        # which may send commands of their own (beam blanking).
        self._policy = None
        # Operator confirmer (client.confirm): async (ConfirmRequest) -> bool | None.
        self._confirmer = None

    def set_confirmer(self, confirmer) -> None:
        """Attach the operator confirmer (see client.confirm)."""
        self._confirmer = confirmer

    async def _with_confirmation(self, cmd: str, params: dict, send):
        """Run *send(params)*. If it is refused pending the operator's go-ahead,
        ask the confirmer and, on Allow, send once more with ``confirmed``.
        A caller's own ``confirmed`` is dropped: only a confirmer confirms."""
        params = {k: v for k, v in (params or {}).items() if k != "confirmed"}
        try:
            return await send(params)
        except NeedsConfirmation as exc:
            reason = exc.reason
        await self.confirm(cmd, params, reason)
        return await send({**params, "confirmed": True})

    async def confirm(self, cmd: str, params: dict, reason: str) -> None:
        """Ask the operator through the confirmer; raise OperatorDeclined
        unless they allow it. Also used for client-side settings that loosen
        a safety rule (set_operational_preferences)."""
        req = ConfirmRequest(cmd, dict(params), reason)
        answer = await self._confirmer(req) if self._confirmer is not None else None
        if answer is None:
            raise OperatorDeclined(f"{cmd} was not done: {reason} But {NO_OPERATOR}.")
        if not answer:
            raise OperatorDeclined(
                f"The operator declined {cmd} ({reason}). Nothing was done. Don't retry "
                f"it; ask the operator what they want instead.")

    @staticmethod
    def _raise_for(reply: dict, default: str) -> None:
        if reply.get("status") == "ok":
            return
        message = reply.get("error", default)
        if reply.get("needs_confirmation"):
            raise NeedsConfirmation(message)
        raise RuntimeError(message)

    def set_monitor(self, monitor) -> None:
        """Attach a telemetry/pause observer (see client.monitor.Monitor)."""
        self._monitor = monitor

    def set_policy(self, policy) -> None:
        """Attach the session-preference policy (see client.policy)."""
        self._policy = policy

    def _policy_on(self) -> bool:
        return self._policy is not None and not self._policy.active()

    async def _policy_before(self, cmd: str, params: dict) -> None:
        if self._policy_on():
            await self._policy.before(self, cmd, params)

    async def _policy_after(self, cmd: str, data, error) -> None:
        if self._policy_on():
            await self._policy.after(self, cmd, data, error)

    def _before(self, cmd: str, params: dict) -> None:
        if self._monitor is not None:
            self._monitor.before_command(cmd, params)

    def _after(self, cmd: str, data: dict | None, error: str | None = None) -> None:
        if self._monitor is not None:
            self._monitor.after_command(cmd, data, error=error)

    async def connect(self) -> None:
        self._context = zmq.asyncio.Context()
        self._open_socket()

    def _open_socket(self) -> None:
        self._socket = self._context.socket(zmq.REQ)
        self._socket.setsockopt(zmq.LINGER, 0)
        self._socket.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
        self._socket.setsockopt(zmq.SNDTIMEO, self.timeout_ms)
        self._socket.connect(self.address)

    def _apply_timeout(self, timeout_ms: int | None) -> int:
        """Per-call timeout (default: the client's). The REQ socket serialises
        calls, so a short probe and a long acquisition can share it."""
        ms = self.timeout_ms if timeout_ms is None else int(timeout_ms)
        self._socket.setsockopt(zmq.RCVTIMEO, ms)
        self._socket.setsockopt(zmq.SNDTIMEO, ms)
        return ms

    def _timeout_error(self, cmd: str, ms: int) -> InstrumentTimeout:
        return InstrumentTimeout(cmd, self.address, ms / 1000.0)

    def _reset_socket(self) -> None:
        """Rebuild the REQ socket. A REQ that missed its reply is stuck in the
        "awaiting reply" state and refuses every later send (EFSM), so one
        timeout would otherwise break all later commands."""
        if self._socket is not None:
            self._socket.close(linger=0)
        if self._context is not None:
            self._open_socket()

    async def disconnect(self) -> None:
        if self._socket:
            self._socket.close()
            self._socket = None
        if self._context:
            self._context.term()
            self._context = None

    async def __aenter__(self) -> "ZMQClient":
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.disconnect()

    def _build_request(self, cmd: str, params: dict,
                       timeout_ms: int | None = None) -> bytes:
        request = {
            "cmd": cmd,
            "params": params,
            "request_id": str(uuid.uuid4()),
        }
        if timeout_ms is not None:
            # Only for calls that asked for their own timeout (e.g. a long
            # stage wait): the relay then answers within it instead of its
            # configured backend timeout, so a slow command comes back as a
            # structured reply rather than a dead socket.
            request["timeout_ms"] = int(timeout_ms)
        return json.dumps(request).encode()

    async def send_command(self, cmd: str, params: dict | None = None, *,
                           timeout_ms: int | None = None) -> dict[str, Any]:
        """Send a command and receive a plain JSON response.

        ``timeout_ms`` overrides the client's timeout for this call only.
        A command that needs the operator's go-ahead is confirmed through the
        confirmer first (:mod:`client.confirm`).

        Returns
        -------
        dict
            The ``data`` field from the server response.

        Raises
        ------
        RuntimeError
            If the server returns status != "ok".
        OperatorDeclined
            If the command needed confirmation and didn't get it.
        """
        return await self._with_confirmation(
            cmd, params, lambda p: self._send_json(cmd, p, timeout_ms))

    async def _send_json(self, cmd: str, params: dict,
                         timeout_ms: int | None) -> dict[str, Any]:
        # before_command may raise (e.g. PausedError) to veto the send; let it
        # propagate without recording an after_command for a command that never
        # reached the instrument.
        self._before(cmd, params)
        await self._policy_before(cmd, params)
        data: dict | None = None
        err: str | None = None
        try:
            async with self._lock:
                ms = self._apply_timeout(timeout_ms)
                try:
                    await self._socket.send(self._build_request(cmd, params, timeout_ms))
                    raw = await self._socket.recv()
                except zmq.Again:
                    self._reset_socket()
                    raise self._timeout_error(cmd, ms) from None
                except zmq.ZMQError:
                    self._reset_socket()
                    raise

            reply = json.loads(raw)
            self._raise_for(reply, "Unknown error from DM server")
            data = reply.get("data", {})
        except Exception as exc:
            err = str(exc)
            raise
        finally:
            self._after(cmd, data, error=err)
        await self._policy_after(cmd, data, err)
        return data

    async def send_command_out_of_band(self, cmd: str, params: dict | None = None, *,
                                       timeout_ms: int | None = None) -> dict[str, Any]:
        """Send one command on a fresh, short-lived socket.

        The main REQ socket handles one request at a time, so a command sent
        while a long one is in flight (e.g. ``stop_stage`` during a waiting
        ``move_stage``) would queue behind it. This doesn't. Monitor hooks
        still run, so telemetry and the pause gate see it. Use it only for
        commands that must get through now."""
        if self._context is None:
            raise RuntimeError("not connected")
        # Never confirmed: nothing that needs a go-ahead belongs out of band.
        params = {k: v for k, v in (params or {}).items() if k != "confirmed"}
        self._before(cmd, params)
        data: dict | None = None
        err: str | None = None
        ms = self.timeout_ms if timeout_ms is None else int(timeout_ms)
        sock = self._context.socket(zmq.REQ)
        sock.setsockopt(zmq.LINGER, 0)
        sock.setsockopt(zmq.RCVTIMEO, ms)
        sock.setsockopt(zmq.SNDTIMEO, ms)
        sock.connect(self.address)
        try:
            try:
                await sock.send(self._build_request(cmd, params, timeout_ms))
                raw = await sock.recv()
            except zmq.Again:
                raise self._timeout_error(cmd, ms) from None
            reply = json.loads(raw)
            if reply.get("status") != "ok":
                raise RuntimeError(reply.get("error", "Unknown error from server"))
            data = reply.get("data", {})
            return data
        except Exception as exc:
            err = str(exc)
            raise
        finally:
            sock.close(linger=0)
            self._after(cmd, data, error=err)

    async def send_command_image(
        self, cmd: str, params: dict | None = None, *, timeout_ms: int | None = None
    ) -> tuple[dict[str, Any], np.ndarray]:
        """Send a command and receive a multipart response (JSON + image bytes).

        Returns
        -------
        (metadata_dict, numpy_array)
            metadata_dict — the full server reply (image_meta + data fields)
            numpy_array   — reconstructed image array
        """
        return await self._with_confirmation(
            cmd, params, lambda p: self._send_image(cmd, p, timeout_ms))

    async def _send_image(self, cmd: str, params: dict, timeout_ms: int | None):
        self._before(cmd, params)
        await self._policy_before(cmd, params)
        data: dict | None = None
        err: str | None = None
        try:
            async with self._lock:
                ms = self._apply_timeout(timeout_ms)
                try:
                    await self._socket.send(self._build_request(cmd, params, timeout_ms))
                    meta_raw = await self._socket.recv()
                    # Error replies are a single JSON frame; only read the
                    # pixel frame if one actually follows.
                    img_raw = (await self._socket.recv()
                               if self._socket.getsockopt(zmq.RCVMORE) else None)
                except zmq.Again:
                    self._reset_socket()
                    raise self._timeout_error(cmd, ms) from None
                except zmq.ZMQError:
                    self._reset_socket()
                    raise

            meta = json.loads(meta_raw)
            self._raise_for(meta, "Unknown error from DM server")

            if img_raw is None:
                raise RuntimeError(f"{cmd}: server replied without image data")
            image_meta = meta.get("image_meta", {})
            shape = image_meta["shape"]
            dtype = np.dtype(image_meta["dtype"])
            arr = np.frombuffer(img_raw, dtype=dtype).reshape(shape).copy()
            # Hand the monitor the JSON data plus a flag that an image landed,
            # so it can note the acquisition (the GUI loads pixels from the
            # ImageStore, not from telemetry).
            data = {**meta.get("data", {}), "_has_image": True,
                    "_image_shape": list(shape)}
        except Exception as exc:
            err = str(exc)
            raise
        finally:
            self._after(cmd, data, error=err)
        await self._policy_after(cmd, data, err)
        return meta, arr

    async def ping(self, *, timeout_ms: int | None = None) -> str:
        """Ping the AMADEUS server (answered by the relay itself, not DM).

        Returns the server's Python version string (the vjem310 interpreter).
        """
        data = await self.send_command("ping", timeout_ms=timeout_ms)
        return data.get("python_version", "")
