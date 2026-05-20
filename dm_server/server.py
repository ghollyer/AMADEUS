"""ZMQ REP dispatcher — runs inside Digital Micrograph.

Start this script from the DM Python console:
    exec(open(r"path/to/dm_server/server.py").read())

Or via DM menu: File > Open Script, then Run.

Protocol
--------
Request:  JSON  {"cmd": "...", "params": {}, "request_id": "uuid"}
Response: JSON  {"status": "ok", "data": {...}, "request_id": "uuid"}
         or for image responses (multipart):
           frame 1: {"status": "ok", "has_image": true, "image_meta": {...}, "data": {...}, "request_id": "uuid"}
           frame 2: raw numpy bytes
Error:    JSON  {"status": "error", "error": "description", "request_id": "uuid"}
"""

import sys
import json
import traceback

import numpy as np
import zmq

# ---------------------------------------------------------------------------
# Import helpers — each module contains stub functions that call the DM API.
# On the real instrument, implement the body of each stub; do not touch this
# dispatcher.
# ---------------------------------------------------------------------------
from dm_server.helpers import microscope as _mic
from dm_server.helpers import camera as _cam
from dm_server.helpers import digiscan as _ds
from dm_server.helpers import stage as _stage
from dm_server.helpers import spectroscopy as _si
from dm_server.helpers import eds as _eds
from dm_server.helpers import files as _files

PORT = 9876

# Handles to the running socket/context, exposed so shutdown_server() can be
# invoked from the DM Python console after run_server() returns.
_active_socket: "zmq.Socket | None" = None
_active_context: "zmq.Context | None" = None

# ---------------------------------------------------------------------------
# Command registry
# Maps cmd string -> (handler_fn, returns_image: bool)
# handler_fn(params: dict) -> dict   or   (dict, np.ndarray)
# ---------------------------------------------------------------------------
COMMANDS = {
    # Inline handlers
    "ping": ("_ping", False),

    # Microscope
    "get_focus":             (_mic.get_focus,             False),
    "set_focus":             (_mic.set_focus,             False),
    "change_focus":          (_mic.change_focus,          False),
    "autofocus":             (_mic.autofocus,             False),
    "get_magnification":     (_mic.get_magnification,     False),
    "set_magnification":     (_mic.set_magnification,     False),
    "get_beam_shift":        (_mic.get_beam_shift,        False),
    "set_beam_shift":        (_mic.set_beam_shift,        False),
    "get_beam_tilt":         (_mic.get_beam_tilt,         False),
    "set_beam_tilt":         (_mic.set_beam_tilt,         False),
    "get_stigmation":        (_mic.get_stigmation,        False),
    "set_stigmation":        (_mic.set_stigmation,        False),
    "get_microscope_state":  (_mic.get_microscope_state,  False),
    "set_beam_blank":        (_mic.set_beam_blank,        False),
    "set_column_valves":     (_mic.set_column_valves,     False),
    "get_aperture":          (_mic.get_aperture,          False),
    "set_aperture":          (_mic.set_aperture,          False),
    "execute_script":        (_mic.execute_script,        False),

    # DigiScan (STEM)
    "stem_acquire":          (_ds.stem_acquire,           True),
    "get_stem_settings":     (_ds.get_stem_settings,      False),
    "set_stem_settings":     (_ds.set_stem_settings,      False),
    "get_detectors":         (_ds.get_detectors,          False),
    "set_detector":          (_ds.set_detector,           False),
    "move_beam_to":          (_ds.move_beam_to,           False),
    "get_beam_position":     (_ds.get_beam_position,      False),

    # Camera (TEM mode)
    "camera_acquire":        (_cam.camera_acquire,        True),
    "get_camera_info":       (_cam.get_camera_info,       False),
    "list_cameras":          (_cam.list_cameras,          False),

    # Stage
    "get_stage_position":    (_stage.get_stage_position,  False),
    "move_stage":            (_stage.move_stage,          False),

    # Spectroscopy / SI
    "si_configure":          (_si.si_configure,           False),
    "si_start":              (_si.si_start,               False),
    "si_stop":               (_si.si_stop,                False),
    "si_get_settings":       (_si.si_get_settings,        False),

    # EDS (SI-based; data stays in DM, no image array returned over ZMQ)
    "eds_acquire_point":     (_eds.eds_acquire_point,     False),
    "eds_acquire_map":       (_eds.eds_acquire_map,       False),

    # Files on the microscope computer (browse + pull .dm3/.dm4)
    "list_dm_files":         (_files.list_files,           False),
    "load_dm_file":          (_files.load_dm_file,         True),
}


def _ping(params: dict) -> dict:
    return {"reply": "pong", "python_version": sys.version}


def _unload_helpers() -> list[str]:
    """Drop every ``dm_server.*`` entry from ``sys.modules``.

    DM has no way to restart its embedded Python interpreter without restarting
    the whole application. After a shutdown we delete the cached helper modules
    so the next ``exec(open("dm_server/server.py").read())`` re-imports them
    from source — making code edits take effect without an app restart.
    """
    removed = [
        name for name in list(sys.modules)
        if name == "dm_server" or name.startswith("dm_server.")
    ]
    for name in removed:
        del sys.modules[name]
    return removed


def shutdown_server() -> None:
    """Close the active ZMQ socket/context and unload helper modules.

    Idempotent. Normally invoked automatically from ``run_server``'s finally
    block when a ``quit``/``shutdown`` command arrives, but also safe to call
    by hand from the DM Python console if the loop ever exits abnormally.
    """
    global _active_socket, _active_context
    if _active_socket is not None:
        try:
            _active_socket.setsockopt(zmq.LINGER, 0)
            _active_socket.close()
        except zmq.ZMQError:
            pass
        _active_socket = None
    if _active_context is not None:
        try:
            _active_context.term()
        except zmq.ZMQError:
            pass
        _active_context = None
    removed = _unload_helpers()
    print(f"[dm_server] Shutdown complete. Unloaded {len(removed)} dm_server.* module(s).")


def _dispatch(sock: zmq.Socket, request: dict) -> None:
    """Dispatch one request and send the reply on *sock*."""
    request_id = request.get("request_id", "")
    cmd = request.get("cmd", "")
    params = request.get("params", {})

    if cmd not in COMMANDS:
        sock.send_json({
            "status": "error",
            "error": f"Unknown command: {cmd!r}",
            "request_id": request_id,
        })
        return

    handler, returns_image = COMMANDS[cmd]

    # Resolve inline string handlers
    if handler == "_ping":
        handler = _ping

    try:
        result = handler(params)
    except NotImplementedError:
        sock.send_json({
            "status": "error",
            "error": f"Command {cmd!r} is not yet implemented on this instrument.",
            "request_id": request_id,
        })
        return
    except Exception as exc:
        sock.send_json({
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
            "request_id": request_id,
        })
        return

    if returns_image:
        data, img_arr = result
        meta = {
            "status": "ok",
            "has_image": True,
            "image_meta": {
                "shape": list(img_arr.shape),
                "dtype": str(img_arr.dtype),
                **data.get("image_meta", {}),
            },
            "data": {k: v for k, v in data.items() if k != "image_meta"},
            "request_id": request_id,
        }
        sock.send_json(meta, flags=zmq.SNDMORE)
        sock.send(np.ascontiguousarray(img_arr).tobytes())
    else:
        sock.send_json({
            "status": "ok",
            "data": result,
            "request_id": request_id,
        })


def run_server(port: int = PORT) -> None:
    """Start the blocking ZMQ REP loop. Send the cmd 'quit' or 'shutdown' to stop."""
    global _active_socket, _active_context
    context = zmq.Context()
    sock = context.socket(zmq.REP)
    # Bounded linger ensures the shutdown ack flushes to the client without
    # the close hanging forever if the peer disappeared.
    sock.setsockopt(zmq.LINGER, 1000)
    sock.bind(f"tcp://*:{port}")
    _active_context = context
    _active_socket = sock
    print(f"[dm_server] Listening on tcp://*:{port}")

    try:
        while True:
            raw = sock.recv()
            try:
                request = json.loads(raw)
            except json.JSONDecodeError as exc:
                sock.send_json({"status": "error", "error": f"JSON parse error: {exc}"})
                continue

            if request.get("cmd") in ("quit", "shutdown"):
                sock.send_json({
                    "status": "ok",
                    "data": {"message": "Server shutting down; helpers will be unloaded"},
                    "request_id": request.get("request_id", ""),
                })
                break

            _dispatch(sock, request)
    finally:
        shutdown_server()


if __name__ == "__main__":
    run_server()
