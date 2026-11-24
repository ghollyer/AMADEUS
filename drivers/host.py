"""Driver host: one driver in its own process, served over ZMQ.

The relay starts one of these per driver configured with ``"host":
"process"`` (``relay/links.py``, ``HostedLink``) and supervises it:

* The driver runs in the vendor's own Python (``"python"`` in the driver's
  config: vjem310 for PyJEM, another interpreter for another vendor).
* A crash, hang or deadlock in vendor code takes down this process, not the
  relay. The relay notices, reports the driver as down, and restarts it.
* Every vendor call happens on this process's main thread, so the vendor API
  sees a single thread, as it does in-process.

Protocol: a REP socket on loopback, the same JSON / image frames as
``dm_server`` (``drivers/dispatch.py``). Extra commands answered by the host:
``shutdown`` (release the driver; rebuilt on the next request, like
``shutdown_hw``) and ``host_exit``.

The host exits by itself when no request (the relay pings every few seconds)
has arrived for ``--idle-exit`` seconds, so a relay that dies can't leave an
orphan holding the instrument.

Stdlib + pyzmq + common only, Python 3.10 (runs under vjem310).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback


def _log(message: str) -> None:
    print(f"[driver-host {os.getpid()}] {message}", flush=True)


class Host:
    def __init__(self, kind: str, name: str, settings: dict):
        self.kind = kind
        self.name = name
        self.settings = settings
        self.driver = None
        self.build_error = None

    def build(self):
        from drivers.registry import build_driver
        try:
            self.driver = build_driver(self.kind, self.settings)
            self.build_error = None
            _log(f"driver {self.name!r} ({self.kind}) ready: {self.driver.describe()}")
        except Exception as exc:
            self.driver = None
            self.build_error = f"{type(exc).__name__}: {exc}"
            _log(f"driver {self.name!r} ({self.kind}) unavailable: {self.build_error}")
            traceback.print_exc()

    def release(self) -> None:
        driver, self.driver = self.driver, None
        if driver is not None:
            try:
                driver.close()
            except Exception as exc:
                _log(f"driver close failed: {exc}")

    def handle(self, raw: bytes) -> "tuple[list, bool]":
        """(reply frames, exit?) for one request."""
        from common.protocol import error_response, ok_response
        from drivers.dispatch import call, to_frames
        try:
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
        except ValueError as exc:
            return [json.dumps(error_response(f"JSON parse error: {exc}")).encode()], False
        cmd = request.get("cmd", "")
        rid = request.get("request_id", "")
        if cmd == "host_exit":
            self.release()
            return [json.dumps(ok_response({"message": "driver host exiting"}, rid)).encode()], True
        if cmd in ("shutdown", "quit"):
            self.release()
            return [json.dumps(ok_response({"message": "driver released"}, rid)).encode()], False
        if self.driver is None:
            self.build()
        if self.driver is None:
            if cmd == "ping":
                return [json.dumps(ok_response({
                    "reply": "pong", "python_version": sys.version, "kind": self.kind,
                    "backend": self.kind, "driver_ok": False,
                    "error": self.build_error}, rid)).encode()], False
            return [json.dumps(error_response(
                f"driver {self.name!r} ({self.kind}) unavailable: {self.build_error}",
                rid)).encode()], False
        return to_frames(call(self.driver, request)), False


def serve(host: Host, bind: str, idle_exit_s: float) -> None:
    import zmq
    context = zmq.Context()
    sock = context.socket(zmq.REP)
    sock.setsockopt(zmq.LINGER, 1000)
    sock.bind(bind)
    _log(f"serving driver {host.name!r} ({host.kind}) on {bind}")
    host.build()
    last = time.monotonic()
    try:
        while True:
            if not sock.poll(500):
                if idle_exit_s and time.monotonic() - last > idle_exit_s:
                    _log(f"no request for {idle_exit_s:.0f} s; the relay is gone. Exiting.")
                    break
                continue
            raw = sock.recv()
            last = time.monotonic()
            frames, done = host.handle(raw)
            sock.send_multipart(frames)
            if done:
                break
    finally:
        host.release()
        sock.close()
        context.term()
        _log("stopped")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="AMADEUS driver host (started by the relay)")
    parser.add_argument("--kind", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--bind", required=True, help="e.g. tcp://127.0.0.1:29950")
    parser.add_argument("--settings", default=None,
                        help="JSON file with the driver settings")
    parser.add_argument("--idle-exit", type=float, default=60.0,
                        help="exit after this many seconds without a request (0 = never)")
    args = parser.parse_args(argv)
    settings = {}
    if args.settings:
        with open(args.settings, "r", encoding="utf-8") as f:
            settings = json.load(f)
    serve(Host(args.kind, args.name, settings), args.bind, args.idle_exit)


if __name__ == "__main__":
    # Spawn-safe: the JEOL stage-motion child process re-imports __main__.
    main()
