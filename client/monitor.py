"""Telemetry + pause gate for the AMADEUS MCP server.

The MCP server is spawned over stdio by a coding agent and holds all state.
The operator console (``amadeus-console``, including ``--read-only``) is a *separate* process. They
communicate through two small JSON files in ``<data-dir>/monitor/``:

* ``snapshot.json`` — written by this Monitor on every command event. Carries
  the current/most-recent command, a rolling command history, a rolling log,
  and the latest known instrument parameters. The GUI polls it.
* ``control.json``  — written by the GUI. Currently just ``{"paused": bool}``.
  The Monitor reads it before every command.

A :class:`Monitor` instance is attached to the :class:`ZMQClient` via
``set_monitor``. The client calls :meth:`before_command` (which may raise
:class:`PausedError` to veto the send) and :meth:`after_command` around every
DM round-trip — the single chokepoint through which all instrument traffic
flows — so telemetry and the pause gate need no per-tool wiring.

Pixel data is intentionally *not* shipped through telemetry: images are already
written to the ImageStore (``<data-dir>/images``) as ``.npy`` + ``.json`` by
the acquisition tools, and the GUI loads them straight from there.
"""

from __future__ import annotations

import json
import os
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any


PAUSE_MESSAGE = (
    "AMADEUS is PAUSED by the operator via the monitor GUI. Instrument "
    "commands are blocked. Stop issuing microscope/acquisition commands now, "
    "wait, and resume work only after the operator clears the pause "
    "(re-check periodically rather than retrying in a tight loop)."
)

# Commands allowed through even while paused — health checks only, never
# anything that changes instrument state.
_PAUSE_EXEMPT = {"ping", "stop_stage"}   # a pause must never block a stop


class PausedError(RuntimeError):
    """Raised by :meth:`Monitor.before_command` when the operator has paused.

    Propagates out of the ZMQ client and the MCP tool so the agent receives a
    clear, actionable error rather than a silent stall.
    """


class Monitor:
    """Records command telemetry and gates commands on the operator pause flag."""

    SNAPSHOT_FILE = "snapshot.json"
    CONTROL_FILE = "control.json"

    def __init__(
        self,
        monitor_dir: Path | str,
        images_dir: Path | str,
        *,
        history_len: int = 200,
        log_len: int = 500,
    ):
        self.dir = Path(monitor_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.images_dir = Path(images_dir)
        self._snapshot_path = self.dir / self.SNAPSHOT_FILE
        self._control_path = self.dir / self.CONTROL_FILE

        self._history: deque[dict] = deque(maxlen=history_len)
        self._log: deque[str] = deque(maxlen=log_len)
        self._current: dict | None = None
        self._params: dict[str, Any] = {
            # vacuum_state has no DM scripting API (no EMGetVacuum / pressure
            # function exists), so it stays None unless a vendor wires it in.
            "vacuum_state": None,
            "column_valves_open": None,
            "beam_blanked": None,
            "stage": None,
            "focus_nm": None,
            "focus_dac": None,
            "magnification": None,
            "mag_index": None,
            "spot_size": None,
            "operation_mode": None,
            "tem_stem_mode": None,
            "function_mode": None,
            "high_tension_kV": None,
            "detectors": None,
            "active_detector": None,
            "live_active": None,
            "live_detector": None,
            "live_frame_index": None,
        }
        self._dm_ok: bool | None = None
        self._write_snapshot()

    # ------------------------------------------------------------------
    # Pause gate
    # ------------------------------------------------------------------
    def is_paused(self) -> bool:
        try:
            raw = json.loads(self._control_path.read_text(encoding="utf-8"))
            return bool(raw.get("paused", False))
        except FileNotFoundError:
            return False
        except (json.JSONDecodeError, OSError):
            # A partially written control file should fail safe to "not paused"
            # rather than wedge the agent; the next read will catch up.
            return False

    def set_paused(self, paused: bool) -> None:
        """Write the pause flag (used by the GUI; handy in tests too)."""
        self._atomic_write(self._control_path, {"paused": bool(paused)})

    # ------------------------------------------------------------------
    # Command hooks (called by ZMQClient)
    # ------------------------------------------------------------------
    def before_command(self, cmd: str, params: dict) -> None:
        if cmd not in _PAUSE_EXEMPT and self.is_paused():
            self.log(f"REJECTED (paused): {cmd}")
            self._history.append({
                "cmd": cmd,
                "status": "rejected",
                "started": _now(),
                "finished": _now(),
                "duration_ms": 0,
                "error": "paused",
            })
            self._write_snapshot()
            raise PausedError(PAUSE_MESSAGE)

        self._current = {"cmd": cmd, "params": _short(params), "started": _now()}
        self.log(f"→ {cmd} {_short(params)}")
        self._write_snapshot()

    def after_command(self, cmd: str, data: dict | None, error: str | None = None) -> None:
        cur = self._current or {}
        started = cur.get("started", _now())
        req_params = cur.get("params") or {}
        finished = _now()
        self._history.append({
            "cmd": cmd,
            "status": "error" if error else "ok",
            "started": started,
            "finished": finished,
            "duration_ms": _elapsed_ms(started, finished),
            "error": error,
        })
        self._current = None

        if cmd not in _PAUSE_EXEMPT:
            self._dm_ok = error is None
        if error:
            self.log(f"✗ {cmd} ERROR: {error.splitlines()[0] if error else ''}")
        else:
            self.log(f"✓ {cmd}")
            self.ingest_request(cmd, req_params)
            self._ingest(cmd, params=req_params, data=data or {})
        self._write_snapshot()

    # ------------------------------------------------------------------
    # Parameter ingestion — pull known readouts out of requests/responses
    # ------------------------------------------------------------------
    def _ingest(self, cmd: str, params: dict | None, data: dict) -> None:
        p = self._params
        # Generic readouts that appear in many responses.
        for key in ("focus_nm", "focus_dac", "magnification", "mag_index",
                    "spot_size", "operation_mode", "tem_stem_mode",
                    "function_mode", "beam_blanked", "high_tension_kV",
                    "column_valves_open"):
            if data.get(key) is not None:
                p[key] = data[key]

        # Stage position (get_stage_position / move_stage return flat axes).
        if any(k in data for k in ("x_m", "y_m", "z_m", "alpha_deg", "beta_deg")):
            p["stage"] = {
                "x_m": data.get("x_m"), "y_m": data.get("y_m"), "z_m": data.get("z_m"),
                "alpha_deg": data.get("alpha_deg"), "beta_deg": data.get("beta_deg"),
            }

        # Detector list (get_detectors / get_stem_settings).
        if isinstance(data.get("detectors"), list):
            p["detectors"] = data["detectors"]
        elif isinstance(data.get("signals"), list):
            p["detectors"] = data["signals"]

        # Live-scan state (live_start / live_stop / live_status responses).
        if cmd in ("live_start", "live_stop", "live_status"):
            if cmd == "live_stop":
                p["live_active"] = False
            elif "active" in data:
                p["live_active"] = bool(data["active"])
            if data.get("detector") is not None:
                p["live_detector"] = data["detector"]
            if data.get("frame_index") is not None:
                p["live_frame_index"] = data["frame_index"]

        # Active detector.
        if cmd == "set_detector" and data.get("signal_index") is not None:
            p["active_detector"] = {"index": data.get("signal_index"), "name": data.get("name")}
        elif cmd == "stem_acquire" and data.get("signal_index") is not None:
            p["active_detector"] = {"index": data.get("signal_index"),
                                    "name": (p.get("active_detector") or {}).get("name")}

    def ingest_request(self, cmd: str, params: dict) -> None:
        """Capture set-command intent from the request (response is often sparse)."""
        p = self._params
        if cmd == "set_column_valves" and "open" in params:
            p["column_valves_open"] = bool(params["open"])
        elif cmd == "set_beam_blank" and "blanked" in params:
            p["beam_blanked"] = bool(params["blanked"])
        elif cmd == "set_focus" and "focus_nm" in params:
            p["focus_nm"] = params["focus_nm"]

    # ------------------------------------------------------------------
    # Logging + snapshot
    # ------------------------------------------------------------------
    def log(self, msg: str) -> None:
        self._log.append(f"{datetime.now().strftime('%H:%M:%S')} {msg}")

    def snapshot(self) -> dict:
        return {
            "updated": _now(),
            "dm_ok": self._dm_ok,
            "paused": self.is_paused(),
            "current_command": self._current,
            "history": list(self._history),
            "log": list(self._log),
            "params": dict(self._params),
            "images_dir": str(self.images_dir),
        }

    def _write_snapshot(self) -> None:
        self._atomic_write(self._snapshot_path, self.snapshot())

    @staticmethod
    def _atomic_write(path: Path, obj: dict) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            # Telemetry must never take the server down.
            pass


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _elapsed_ms(started: str, finished: str) -> int:
    try:
        dt = datetime.fromisoformat(finished) - datetime.fromisoformat(started)
        return int(dt.total_seconds() * 1000)
    except (ValueError, TypeError):
        return 0


def _short(params: dict, limit: int = 200) -> dict:
    """Trim params for display — drop bulky values, cap string length."""
    out = {}
    for k, v in (params or {}).items():
        if isinstance(v, str) and len(v) > limit:
            out[k] = v[:limit] + "…"
        elif isinstance(v, (list, dict)) and len(str(v)) > limit:
            out[k] = f"<{type(v).__name__} len={len(v)}>"
        else:
            out[k] = v
    return out
