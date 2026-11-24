"""Turn one wire request into a call on a driver, and its result into frames.

Used wherever a Python driver runs: in the relay process (``InProcessLink``)
and in a driver host process (``drivers/host.py``). The reply frames are
byte-for-byte what ``dm_server`` sends, so the relay and the client handle
every driver the same way:

* JSON reply: one frame, ``{"status", "data", "request_id"}``.
* Image reply: frame 1 JSON with ``"has_image": true`` and ``"image_meta"``
  (shape, dtype + the operation's ``image_meta``), frame 2 the raw bytes.
* Error: one frame, ``{"status": "error", "error", "request_id"}``.
  ``NotImplementedError`` becomes an error, never a crash.
"""

from __future__ import annotations

import json
import sys
import traceback

from common.protocol import error_response, ok_response
from drivers.base import ImageResult

# Lifecycle commands a host or link answers itself (not driver operations).
_CAPABILITY_COMMANDS = ("get_driver_capabilities", "get_hw_capabilities")


def resolve(driver, cmd: str):
    """The bound method serving *cmd*, or ``None``.

    *cmd* is a slot operation (``get_focus``), an extra with or without its
    kind prefix (``jeol.tem3_call`` / ``tem3_call``), or a generic driver
    command."""
    if cmd in _CAPABILITY_COMMANDS:
        return driver.capabilities
    if cmd == "list_devices":
        return lambda params: {"devices": driver.devices()}
    if cmd == "vendor_call":
        return driver.vendor_call
    prefix = f"{driver.kind}."
    name = cmd[len(prefix):] if cmd.startswith(prefix) else cmd
    if name in driver.extras:
        return getattr(driver, driver.extras[name])
    if cmd in type(driver).operations():
        return getattr(driver, cmd)
    # An un-overridden slot method: dispatch it anyway so the caller gets the
    # standard "not implemented" error instead of "unknown command".
    if not cmd.startswith("_") and callable(getattr(driver, cmd, None)) \
            and cmd in _all_slot_commands():
        return getattr(driver, cmd)
    return None


def _all_slot_commands():
    from common.commands import COMMAND_SLOT
    return COMMAND_SLOT


def call(driver, request: dict):
    """Run *request* on *driver*. Returns a reply dict, or ``(meta, payload)``
    for an image reply."""
    request_id = request.get("request_id", "")
    cmd = request.get("cmd", "")
    params = request.get("params") or {}
    if cmd == "ping":
        return ok_response({"reply": "pong", "python_version": sys.version,
                            **driver.describe()}, request_id)
    method = resolve(driver, cmd)
    if method is None:
        return error_response(f"Unknown command for the {driver.kind!r} driver: "
                              f"{cmd!r}", request_id)
    try:
        result = method(params)
    except NotImplementedError:
        return error_response(
            f"Command {cmd!r} is not implemented by the {driver.kind!r} driver.",
            request_id)
    except Exception as exc:
        return error_response(
            f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}", request_id)
    if isinstance(result, ImageResult):
        data = dict(result.data)
        image_meta = data.pop("image_meta", {}) or {}
        meta = {"status": "ok", "has_image": True,
                "image_meta": {"shape": result.shape, "dtype": result.dtype, **image_meta},
                "data": data, "request_id": request_id}
        return meta, result.payload
    return ok_response(result, request_id)


def to_frames(reply) -> "list[bytes]":
    """Encode a :func:`call` result as ZMQ frames."""
    if isinstance(reply, tuple):
        meta, payload = reply
        return [json.dumps(meta).encode(), payload]
    return [json.dumps(reply).encode()]


def dispatch_frames(driver, raw: bytes) -> "list[bytes]":
    """Decode a raw request, run it, and encode the reply."""
    try:
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise ValueError("request must be a JSON object")
    except ValueError as exc:
        return [json.dumps(error_response(f"JSON parse error: {exc}")).encode()]
    return to_frames(call(driver, request))
