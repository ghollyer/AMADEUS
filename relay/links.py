"""How the relay reaches a driver: three kinds of link, one contract.

Every link has ``name``, ``address``, ``timeout_ms``, ``kind``,
``async request(frames, timeout_ms) -> frames``, ``translate(cmd, params)``,
``status() -> dict`` and ``close()``.

* :class:`RemoteLink`: a REQ socket to a process that is already running
  (``dm_server`` inside DM, or any remote driver). One request at a time.
* :class:`InProcessLink`: a Python driver hosted inside the relay, on one
  dedicated worker thread (the vendor API only ever sees that thread).
* :class:`HostedLink`: a Python driver in its own process
  (``python -m drivers.host``), started and supervised by the relay. A crash
  in vendor code kills that process, not the relay: the link reports the
  driver down, restarts it with backoff, and the stop path and every other
  driver keep working.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import zmq
import zmq.asyncio

from common.protocol import error_response, make_request, ok_response, parse_request

REPO_ROOT = Path(__file__).resolve().parent.parent
_RESTART_BACKOFF_S = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
_HEALTHY_AFTER_S = 60.0          # uptime that resets the backoff
_HEARTBEAT_S = 5.0
_HOST_IDLE_EXIT_S = 20.0         # the host exits if the relay stops pinging


class BackendTimeout(RuntimeError):
    pass


class DriverDown(RuntimeError):
    pass


def request_info(frames: "list[bytes]") -> "tuple[str, str]":
    """(cmd, request_id) of a request, best effort."""
    try:
        request = json.loads(frames[0])
        return str(request.get("cmd", "?")), str(request.get("request_id", ""))
    except Exception:
        return "?", ""


def _identity(cmd: str, params: dict) -> "tuple[str, dict]":
    return cmd, params


class RemoteLink:
    """A lazily-connected REQ socket to one process, one request at a time."""

    def __init__(self, name: str, address: str, context: zmq.asyncio.Context,
                 timeout_ms: int, kind: str = "remote", translate=None):
        self.name = name
        self.address = address
        self.timeout_ms = timeout_ms
        self.kind = kind
        self.translate = translate or _identity
        self._context = context
        self._socket: "zmq.asyncio.Socket | None" = None
        self._lock = asyncio.Lock()
        self.busy: "tuple | None" = None

    def _ensure_socket(self) -> zmq.asyncio.Socket:
        if self._socket is None:
            self._socket = self._context.socket(zmq.REQ)
            self._socket.setsockopt(zmq.LINGER, 0)
            self._socket.connect(self.address)
        return self._socket

    def _drop_socket(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def locked(self) -> bool:
        return self._lock.locked()

    async def request(self, frames: "list[bytes]",
                      timeout_ms: "int | None" = None) -> "list[bytes]":
        """Send request frames, await reply frames. Raises BackendTimeout."""
        timeout = (timeout_ms or self.timeout_ms) / 1000.0
        async with self._lock:
            sock = self._ensure_socket()
            cmd, rid = request_info(frames)
            self.busy = (cmd, rid, time.monotonic())
            try:
                await asyncio.wait_for(sock.send_multipart(frames), timeout)
                return await asyncio.wait_for(sock.recv_multipart(), timeout)
            except asyncio.TimeoutError:
                # A REQ socket that missed its reply is unusable; rebuild.
                self._drop_socket()
                raise BackendTimeout(
                    f"backend '{self.name}' ({self.address}) did not answer {cmd!r} "
                    f"within {timeout:.1f} s. The command may still be running "
                    f"there; check its state before retrying. The relay and the "
                    f"other backends are unaffected.")
            finally:
                self.busy = None

    def status(self) -> dict:
        return {"host": "remote"}

    def close(self) -> None:
        self._drop_socket()



class InProcessLink:
    """A Python driver hosted in the relay process.

    Calls run on a single dedicated worker thread, which also constructs the
    driver (lazily, and again after a failure or a ``shutdown``), so the
    vendor API always sees the same thread. A call that overruns its timeout
    returns a ``BackendTimeout``; the stuck call keeps the worker busy, so
    later calls queue behind it until it returns.
    """

    address = "in-process"

    def __init__(self, name: str, settings: dict, timeout_ms: int):
        self.name = name
        self.settings = dict(settings)
        # Older settings say {"backend": "jeol"}; drivers say {"kind": ...}.
        self.kind = self.settings.get("kind") or self.settings.get("backend") or "?"
        self.timeout_ms = timeout_ms
        self.translate = _identity
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"amadeus-{name}")
        self._backend = None
        self._lock = asyncio.Lock()
        self.worker_thread: "threading.Thread | None" = None
        # (cmd, request_id, started) of the call running on the worker.
        self.busy: "tuple | None" = None
        self.build_error: "str | None" = None

    def _build(self):
        from drivers.registry import build_driver
        self.worker_thread = threading.current_thread()
        self._backend = build_driver(self.kind, self.settings)
        self.build_error = None
        print(f"[relay] driver {self.name!r} ready: {self._backend.describe()}", flush=True)
        return self._backend

    def start(self) -> "concurrent.futures.Future":
        """Begin constructing the driver now, so problems show at startup."""
        return self._executor.submit(self._safe_build)

    def _safe_build(self):
        try:
            self._build()
        except Exception as exc:
            self.build_error = f"{type(exc).__name__}: {exc}"
            print(f"[relay] driver {self.name!r} unavailable: {self.build_error}", flush=True)

    def _handle(self, raw: bytes) -> "list[bytes]":
        from drivers.dispatch import call, to_frames
        try:
            request = parse_request(raw)
        except ValueError as exc:
            return [json.dumps(error_response(str(exc))).encode()]
        request_id = request.get("request_id", "")
        self.busy = (request.get("cmd", "?"), request_id, time.monotonic())
        try:
            if request.get("cmd") in ("quit", "shutdown"):
                backend, self._backend = self._backend, None   # rebuilt on next request
                if backend is not None:
                    try:
                        backend.close()
                    except Exception as exc:
                        print(f"[relay] driver {self.name!r} close failed: {exc}", flush=True)
                return [json.dumps(ok_response(
                    {"message": f"driver {self.name!r} released"}, request_id)).encode()]
            if self._backend is None:
                try:
                    self._build()
                except Exception as exc:
                    self.build_error = f"{type(exc).__name__}: {exc}"
                    return [json.dumps(error_response(
                        f"driver {self.name!r} ({self.kind}) unavailable: "
                        f"{self.build_error}", request_id)).encode()]
            return to_frames(call(self._backend, request))
        finally:
            self.busy = None

    def locked(self) -> bool:
        return self._lock.locked()

    async def request(self, frames: "list[bytes]",
                      timeout_ms: "int | None" = None) -> "list[bytes]":
        timeout = (timeout_ms or self.timeout_ms) / 1000.0
        loop = asyncio.get_running_loop()
        async with self._lock:
            future = loop.run_in_executor(self._executor, self._handle, frames[0])
            try:
                return await asyncio.wait_for(asyncio.shield(future), timeout)
            except asyncio.TimeoutError:
                raise BackendTimeout(self._timeout_message(frames, timeout))

    def _timeout_message(self, frames: "list[bytes]", timeout: float) -> str:
        cmd, request_id = request_info(frames)
        busy = self.busy
        head = f"driver '{self.name}' (in-process) did not answer {cmd!r} " \
               f"within {timeout:.1f} s."
        if busy is None:
            return head + " The worker is idle now, so the call has finished; " \
                          "check the instrument state before retrying."
        busy_cmd, busy_id, started = busy
        running = time.monotonic() - started
        if busy_id == request_id:
            return (head + f" It is still running on the driver worker "
                    f"({running:.0f} s so far), and the instrument may still be "
                    f"executing it. Later commands to this driver queue behind it "
                    f"until the vendor call returns. The relay and the other "
                    f"drivers are unaffected.")
        return (head + f" It never started: the driver worker has been busy with "
                f"{busy_cmd!r} for {running:.0f} s. Commands to this driver queue "
                f"behind it. The relay and the other drivers are unaffected.")

    def status(self) -> dict:
        out = {"host": "in_process"}
        if self.build_error:
            out["error"] = self.build_error
        return out

    def close(self) -> None:
        backend = self._backend
        if backend is not None:
            self._executor.submit(backend.close)
        self._executor.shutdown(wait=False)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HostedLink:
    """A Python driver in its own supervised process.

    The relay starts ``python -m drivers.host`` with the driver's settings,
    talks to it over a loopback REQ socket, pings it every few seconds (which
    also keeps the host from exiting), and restarts it with backoff if it
    exits. A request that arrives while the driver is down gets an immediate,
    explicit error instead of a timeout."""

    def __init__(self, name: str, spec: dict, context: zmq.asyncio.Context,
                 timeout_ms: int, log_dir: "Path | None" = None,
                 idle_exit_s: float = _HOST_IDLE_EXIT_S):
        self.name = name
        self.spec = dict(spec)
        self.kind = spec["kind"]
        self.timeout_ms = timeout_ms
        self.translate = _identity
        self.address = "(not started)"
        self._context = context
        self._remote: "RemoteLink | None" = None
        self._proc: "subprocess.Popen | None" = None
        self._log_dir = Path(log_dir) if log_dir else REPO_ROOT / "logs"
        self._idle_exit_s = idle_exit_s
        self._settings_file: "str | None" = None
        self._started_at = 0.0
        self._next_start = 0.0
        self._failures = 0
        self.restarts = 0
        self.last_exit: "dict | None" = None
        self.last_ping_ok: "float | None" = None
        self.stopping = False

    # -- process management -------------------------------------------
    def _settings(self) -> dict:
        keys = ("offline", "calibration", "instrument", "options", "class")
        return {k: self.spec[k] for k in keys if self.spec.get(k) is not None}

    def _command(self, address: str) -> list:
        python = self.spec.get("python") or sys.executable
        return [python, "-m", "drivers.host", "--kind", self.kind, "--name", self.name,
                "--bind", address, "--settings", self._settings_file,
                "--idle-exit", str(self._idle_exit_s)]

    def start(self) -> None:
        if self._settings_file is None:
            fd, path = tempfile.mkstemp(prefix=f"amadeus-driver-{self.name}-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._settings(), f)
            self._settings_file = path
        address = f"tcp://127.0.0.1:{_free_port()}"
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT) + (os.pathsep + env["PYTHONPATH"]
                                              if env.get("PYTHONPATH") else "")
        env["PYTHONUNBUFFERED"] = "1"
        self._log_dir.mkdir(parents=True, exist_ok=True)
        log = open(self._log_dir / f"driver_{self.name}.log", "a", encoding="utf-8")
        log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} starting {self.kind} "
                  f"driver {self.name!r} on {address} ===\n")
        log.flush()
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(self._command(address), cwd=str(REPO_ROOT), env=env,
                                      stdout=log, stderr=subprocess.STDOUT,
                                      stdin=subprocess.DEVNULL, **kwargs)
        log.close()
        if self._remote is not None:
            self._remote.close()
        self.address = address
        self._remote = RemoteLink(self.name, address, self._context, self.timeout_ms,
                                  kind=self.kind)
        self._started_at = time.monotonic()
        print(f"[relay] driver {self.name!r} ({self.kind}) started as pid "
              f"{self._proc.pid} on {address}", flush=True)

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _note_exit(self) -> None:
        if self._proc is None or self._proc.poll() is None:
            return
        code = self._proc.returncode
        uptime = time.monotonic() - self._started_at
        if self.last_exit is None or self.last_exit.get("pid") != self._proc.pid:
            self.last_exit = {"pid": self._proc.pid, "code": code,
                              "uptime_s": round(uptime, 1), "at": time.time()}
            self._failures = 0 if uptime > _HEALTHY_AFTER_S else self._failures + 1
            delay = _RESTART_BACKOFF_S[min(self._failures, len(_RESTART_BACKOFF_S) - 1)]
            self._next_start = time.monotonic() + delay
            print(f"[relay] driver {self.name!r} exited (code {code}) after "
                  f"{uptime:.0f} s; restarting in {delay:.0f} s", flush=True)

    def _maybe_restart(self) -> bool:
        """Restart a dead host once its backoff has passed. True if running."""
        if self.stopping:
            return False
        if self.running():
            return True
        self._note_exit()
        if time.monotonic() >= self._next_start:
            self.restarts += 1
            self.start()
            return True
        return False

    async def supervise(self, stop: asyncio.Event) -> None:
        """Restart on exit; ping when idle (the ping is also the host's
        heartbeat, so it exits on its own if the relay disappears)."""
        last_ping = 0.0
        while not stop.is_set():
            await asyncio.sleep(0.5)
            if self.stopping:
                return
            if not self._maybe_restart():
                continue
            if time.monotonic() - last_ping < _HEARTBEAT_S or self._remote.locked():
                continue
            last_ping = time.monotonic()
            try:
                frames = await self._remote.request([make_request("ping")], timeout_ms=3000)
                if json.loads(frames[0]).get("status") == "ok":
                    self.last_ping_ok = time.time()
            except Exception:
                pass

    async def restart(self) -> None:
        """Operator-requested restart (relay-local ``restart_driver``)."""
        self._terminate()
        self._next_start = 0.0
        self.restarts += 1
        self.start()

    def _terminate(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    # -- link contract --------------------------------------------------
    def locked(self) -> bool:
        return self._remote is not None and self._remote.locked()

    @property
    def busy(self):
        return self._remote.busy if self._remote is not None else None

    async def request(self, frames: "list[bytes]",
                      timeout_ms: "int | None" = None) -> "list[bytes]":
        if not self._maybe_restart():
            wait = max(0.0, self._next_start - time.monotonic())
            exit_info = self.last_exit or {}
            raise DriverDown(
                f"driver '{self.name}' ({self.kind}) is down: its process exited with "
                f"code {exit_info.get('code')} after {exit_info.get('uptime_s')} s. The "
                f"relay restarts it in {wait:.0f} s (restart #{self.restarts + 1}); "
                f"logs/driver_{self.name}.log has the details. Other drivers are "
                f"unaffected.")
        try:
            return await self._remote.request(frames, timeout_ms)
        except BackendTimeout as exc:
            if not self.running():
                self._note_exit()
                raise DriverDown(f"driver '{self.name}' ({self.kind}) died while "
                                 f"handling the request; the relay restarts it.") from exc
            raise

    def status(self) -> dict:
        out = {"host": "process", "pid": self._proc.pid if self._proc else None,
               "running": self.running(), "restarts": self.restarts,
               "python": self.spec.get("python") or sys.executable}
        if self.last_exit:
            out["last_exit"] = dict(self.last_exit)
        if self.last_ping_ok:
            out["last_ping_age_s"] = round(time.time() - self.last_ping_ok, 1)
        return out

    def close(self) -> None:
        self.stopping = True
        if self._remote is not None and self.running():
            try:
                sock = zmq.Context.instance().socket(zmq.REQ)
                sock.setsockopt(zmq.LINGER, 0)
                sock.setsockopt(zmq.RCVTIMEO, 2000)
                sock.connect(self.address)
                sock.send(make_request("host_exit"))
                sock.recv()
                sock.close()
            except Exception:
                pass
        if self._proc is not None:
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._terminate()
        if self._remote is not None:
            self._remote.close()
        if self._settings_file:
            try:
                os.unlink(self._settings_file)
            except OSError:
                pass
