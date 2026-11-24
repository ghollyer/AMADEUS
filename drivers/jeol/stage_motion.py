"""Stage drive calls, run where a blocking vendor call can't freeze AMADEUS.

Why this exists: on the F200, online PyJEM's ``Stage3`` setters
(``SetX``/``SetXRel``/...) don't return until the stage has settled, and the
compiled TEM3 ``.pyd`` appears to hold the GIL while it waits. The relay hosts
the hardware backend in-process, so one stage move froze the *whole* AMADEUS
server for up to ~3 minutes. Even relay-local ``ping`` went unanswered, and
``stop_stage`` could not get through.

:class:`ProcessMotion` runs the drive calls in a small child process with its
own ``Stage3``. Whatever that call does to its own interpreter, the relay's
event loop and the hardware worker thread stay free. The worker keeps polling
``GetStatus``/``GetPos`` and can send ``Stop()`` while the child is blocked.

:class:`InlineMotion` makes the calls in the caller's thread. It is used for
PyJEM's offline simulator, whose setters return immediately, and for tests.

Py3.10, stdlib only: this runs inside the vjem310 env.
"""

import importlib
import multiprocessing
import os
import sys
import time
import traceback

_START_TIMEOUT_S = 60.0     # importing online TEM3 can be slow


class MotionError(RuntimeError):
    """A stage drive could not be started or failed in the motion process."""


def describe_calls(calls) -> str:
    return ", ".join(f"{m}({', '.join(f'{a:g}' if isinstance(a, float) else repr(a) for a in args)})"
                     for m, args in calls)


class InlineMotion:
    """Drive calls in the caller's thread. Blocking calls block the caller."""

    kind = "inline"

    def __init__(self, stage):
        self._stage = stage
        self._error = None

    def submit(self, calls, description: str = "") -> None:
        for method, args in calls:
            getattr(self._stage, method)(*args)

    def busy(self) -> "dict | None":
        return None

    def done(self) -> bool:
        return True

    def take_error(self) -> "str | None":
        return None

    def close(self) -> None:
        pass


def _motion_worker(module_name: str, conn) -> None:
    """Child-process loop: own ``Stage3``, run one job (a call list) at a time."""
    try:
        stage = importlib.import_module(module_name).Stage3()
    except Exception as exc:                       # report, then exit
        conn.send(("error", f"{type(exc).__name__}: {exc}"))
        return
    conn.send(("ready", None))
    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            return
        if msg is None:
            return
        job_id, calls = msg
        try:
            for method, args in calls:
                getattr(stage, method)(*args)
            conn.send((job_id, None))
        except Exception as exc:
            conn.send((job_id, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"))


def _use_windowless_python(ctx) -> None:
    """The launcher runs the relay with CREATE_NO_WINDOW. A spawned python.exe
    child of a console-less process would open a console window on the
    microscope PC, so use the pythonw.exe beside it when there is one."""
    if sys.platform != "win32":
        return
    exe = sys.executable or ""
    if os.path.basename(exe).lower() == "python.exe":
        pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.isfile(pythonw):
            ctx.set_executable(pythonw)


class ProcessMotion:
    """Drive calls in a dedicated child process (spawned, so it works on
    Windows and never inherits the relay's sockets).

    One job runs at a time. :meth:`submit` returns at once. :meth:`done` and
    :meth:`busy` report progress without blocking. Use it from one thread
    only: the hardware worker thread.
    """

    kind = "process"

    def __init__(self, module_name: str, start_timeout_s: float = _START_TIMEOUT_S):
        self.module_name = module_name
        ctx = multiprocessing.get_context("spawn")
        _use_windowless_python(ctx)
        self._conn, child = ctx.Pipe()
        self._proc = ctx.Process(target=_motion_worker, args=(module_name, child),
                                 name="amadeus-stage-motion", daemon=True)
        self._proc.start()
        child.close()
        if not self._conn.poll(start_timeout_s):
            self.close()
            raise MotionError(f"stage motion process did not start within "
                              f"{start_timeout_s:.0f} s (importing {module_name})")
        tag, err = self._conn.recv()
        if tag != "ready":
            self.close()
            raise MotionError(f"stage motion process failed to create "
                              f"{module_name}.Stage3: {err}")
        self._seq = 0
        self._job = None            # (job_id, description, started_monotonic)
        self._error = None

    def _drain(self) -> None:
        while self._job is not None and self._conn.poll(0):
            job_id, err = self._conn.recv()
            if job_id == self._job[0]:
                self._error = err
                self._job = None
        if self._job is not None and not self._proc.is_alive():
            self._error = (f"stage motion process exited (code {self._proc.exitcode}) "
                           f"during {self._job[1]}")
            self._job = None

    def busy(self) -> "dict | None":
        """``{"job", "running_s"}`` while a drive is executing, else None."""
        self._drain()
        if self._job is None:
            return None
        return {"job": self._job[1], "running_s": round(time.monotonic() - self._job[2], 1)}

    def done(self) -> bool:
        self._drain()
        return self._job is None

    def take_error(self) -> "str | None":
        err, self._error = self._error, None
        return err

    def submit(self, calls, description: str = "") -> None:
        busy = self.busy()
        if busy is not None:
            raise MotionError(
                f"the stage is still executing {busy['job']} (running {busy['running_s']:.0f} s). "
                f"Wait for it (get_stage_status) or call stop_stage first.")
        if not self._proc.is_alive():
            raise MotionError(f"stage motion process is not running "
                              f"(exit code {self._proc.exitcode})")
        self._seq += 1
        self._error = None
        self._conn.send((self._seq, [(m, list(a)) for m, a in calls]))
        self._job = (self._seq, description or describe_calls(calls), time.monotonic())

    def close(self) -> None:
        try:
            self._conn.send(None)
        except Exception:
            pass
        self._proc.join(2)
        if self._proc.is_alive():
            self._proc.terminate()
            self._proc.join(2)
