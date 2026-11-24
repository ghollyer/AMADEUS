"""Unified control-computer console for human/agent microscope sessions.

The console combines operator controls, full manual instrument control (the
Instrument tab, formerly ``amadeus-gui``), current/live view, session images,
stage-coordinate map, task progress, activity, and reconciled state. Operator
commands use the same vendor-neutral command contract as the MCP tools.

``--read-only`` is the passive view for agent sessions (formerly
``amadeus-monitor``). It sends no instrument commands, reads the microscope
state from the MCP server's monitor snapshot, and keeps Pause/Resume.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import time
import tkinter as tk
from pathlib import Path

from common import config as amadeus_config
from tkinter import messagebox, scrolledtext, ttk

from client.confirm import MonitorConfirmer, read_json, write_json
from client.gui.bridge import QUICK_TIMEOUT_MS, ZmqBridge
from client.gui.render import render_ppm
from client.monitor import Monitor
from client.state.manager import StateManager
from client.state.session_events import SessionEventStore

_VIEW = 560

# Session-image list columns: (key, heading, width, sort direction on first click).
_IMAGE_COLUMNS = (("time", "Time", 145, True), ("name", "Name", 220, False),
                  ("shape", "Shape", 110, True))


def image_sort_key(column: str, sidecar):
    """Sort key for one session image under *column* (pure, for tests)."""
    if column == "time":
        try:
            return sidecar.timestamp.timestamp()
        except (AttributeError, OSError, OverflowError, ValueError):
            return 0.0
    if column == "name":
        return (sidecar.name or "").casefold()
    if column == "shape":
        size = 1
        for n in sidecar.shape or ():
            size *= int(n)
        return (size, tuple(sidecar.shape or ()))
    raise ValueError(f"unknown image column {column!r}")


def sort_images(sidecars, column: str = "time", descending: bool = True):
    """Order image sidecars by *column*; image ID breaks ties so order is stable.

    The default (time, descending) puts the latest acquisition at the top.
    """
    ordered = sorted(sidecars, key=lambda s: s.image_id)
    return sorted(ordered, key=lambda s: image_sort_key(column, s), reverse=descending)


def newest_new_image(sidecars, known_ids) -> str | None:
    """ID of the most recent image not in *known_ids*, or None if nothing is new."""
    new = [s for s in sidecars if s.image_id not in known_ids]
    return sort_images(new, "time", True)[0].image_id if new else None


class OperatorConsole:
    def __init__(self, root: tk.Tk, address: str, data_dir: Path,
                 timeout_ms: int = 600_000, poll_ms: int = 500, read_only: bool = False):
        self.root, self.data_dir = root, Path(data_dir)
        self.poll_ms = poll_ms
        self.read_only = read_only
        self.state = StateManager(self.data_dir)
        self.events = SessionEventStore(self.data_dir)
        self.bridge = None if read_only else ZmqBridge(address, timeout_ms, self.state.images)
        self.ui_queue: queue.Queue = queue.Queue()
        self.snapshot_path = self.data_dir / "monitor" / Monitor.SNAPSHOT_FILE
        self.control_path = self.data_dir / "monitor" / Monitor.CONTROL_FILE
        monitor_dir = self.data_dir / "monitor"
        self.console_path = monitor_dir / MonitorConfirmer.CONSOLE_FILE
        self.confirm_request_path = monitor_dir / MonitorConfirmer.REQUEST_FILE
        self.confirm_reply_path = monitor_dir / MonitorConfirmer.REPLY_FILE
        self.answered_confirms: set = set()
        if self.bridge is not None:
            self.bridge.on_confirm = lambda req, answer: self._post(
                lambda: answer(self._ask_confirm(req, "Your command")))
        self.photo = {}
        self.rendered = {}
        self.selected_image = None
        self.image_sort = ("time", True)   # (column, descending): latest first
        self.known_image_ids = None        # None until the first image refresh
        self.last_state_poll = 0.0
        self.last_disk_sig = None

        root.title("AMADEUS Operator Console" + ("  (read-only)" if read_only else ""))
        root.geometry("1450x920")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._build_header(address)
        self.tabs = ttk.Notebook(root)
        self.tabs.pack(fill="both", expand=True, padx=6, pady=6)
        self._build_overview()
        if not read_only:
            self._build_controls()
            self._build_instrument()
        self._build_map()
        self._build_images()
        self._build_task()
        self._build_activity()
        self.root.after(50, self._drain)
        self.root.after(self.poll_ms, self._refresh)
        if not read_only:
            self._connect(address)

    def _build_header(self, address):
        f = ttk.Frame(self.root)
        f.pack(fill="x", padx=8, pady=6)
        ttk.Label(f, text="AMADEUS", font=("", 15, "bold")).pack(side="left")
        if self.read_only:
            ttk.Label(f, text="READ-ONLY monitor: sends no instrument commands; state "
                              "from the MCP server's snapshot", foreground="#555"
                      ).pack(side="left", padx=12)
            self.pause = tk.Button(f, text="⏸ PAUSE", width=13, command=self._toggle_pause)
            self.pause.pack(side="right")
            return
        self.address = tk.StringVar(value=address)
        ttk.Entry(f, textvariable=self.address, width=34).pack(side="left", padx=12)
        ttk.Button(f, text="Connect", command=lambda: self._connect()).pack(side="left")
        self.connection = ttk.Label(f, text="● disconnected", foreground="red")
        self.connection.pack(side="left", padx=10)
        self.pause = tk.Button(f, text="⏸ PAUSE", width=13, command=self._toggle_pause)
        self.pause.pack(side="right")
        ttk.Button(f, text="Blank beam", command=lambda: self._command(
            "set_beam_blank", {"blanked": True}, "Blank beam")).pack(side="right", padx=4)
        ttk.Button(f, text="Stop stage", command=lambda: self._command(
            "stop_stage", {}, "Stop stage")).pack(side="right", padx=4)

    def _build_overview(self):
        f = ttk.Frame(self.tabs)
        self.tabs.add(f, text="Overview")
        left = ttk.LabelFrame(f, text="Current view")
        left.pack(side="left", fill="both", expand=True, padx=5, pady=5)
        self.current_canvas = tk.Canvas(left, width=_VIEW, height=_VIEW, bg="black")
        self.current_canvas.pack(fill="both", expand=True, padx=5, pady=5)
        self._show_no_image(self.current_canvas, _VIEW)
        self.current_caption = ttk.Label(left, text="No image")
        self.current_caption.pack(anchor="w", padx=5, pady=4)
        right = ttk.Frame(f)
        right.pack(side="left", fill="y", padx=5, pady=5)
        state_box = ttk.LabelFrame(right, text="Microscope state")
        state_box.pack(fill="x")
        self.state_labels = {}
        fields = ("mode", "function", "magnification", "focus", "spot", "ht",
                  "beam", "valves", "stage", "live")
        for row, key in enumerate(fields):
            ttk.Label(state_box, text=key.replace("_", " ").title() + ":",
                      width=16, anchor="e").grid(row=row, column=0, padx=4, pady=2)
            label = ttk.Label(state_box, text="—", width=38, relief="sunken")
            label.grid(row=row, column=1, padx=4, pady=2, sticky="w")
            self.state_labels[key] = label
        task_box = ttk.LabelFrame(right, text="Current task")
        task_box.pack(fill="both", expand=True, pady=(8, 0))
        self.task_summary = tk.Text(task_box, width=55, height=16, state="disabled",
                                    wrap="word")
        self.task_summary.pack(fill="both", expand=True, padx=5, pady=5)

    def _build_controls(self):
        f = ttk.Frame(self.tabs)
        self.tabs.add(f, text="Control")
        stage = ttk.LabelFrame(f, text="Stage (relative movement)")
        stage.pack(fill="x", padx=8, pady=6)
        translation = ttk.LabelFrame(stage, text="Translation")
        translation.pack(anchor="w", padx=8, pady=5)
        self.translation_step = tk.StringVar(value="100")
        ttk.Label(translation, text="XY step (nm)").grid(row=0, column=0, columnspan=2)
        ttk.Entry(translation, textvariable=self.translation_step, width=9).grid(
            row=0, column=2)
        ttk.Button(translation, text="▲  Y+", width=8,
                   command=lambda: self._stage_jog("y_m", 1)).grid(row=1, column=1)
        ttk.Button(translation, text="◀  X−", width=8,
                   command=lambda: self._stage_jog("x_m", -1)).grid(row=2, column=0)
        ttk.Label(translation, text="XY").grid(row=2, column=1)
        ttk.Button(translation, text="X+  ▶", width=8,
                   command=lambda: self._stage_jog("x_m", 1)).grid(row=2, column=2)
        ttk.Button(translation, text="▼  Y−", width=8,
                   command=lambda: self._stage_jog("y_m", -1)).grid(row=3, column=1)
        self.z_step = tk.StringVar(value="100")
        ttk.Label(translation, text="Z step (nm)").grid(row=4, column=0, columnspan=2,
                                                        pady=(7, 0))
        ttk.Entry(translation, textvariable=self.z_step, width=9).grid(row=4, column=2,
                                                                      pady=(7, 0))
        ttk.Button(translation, text="Z ▲", width=8,
                   command=lambda: self._stage_jog("z_m", 1)).grid(row=5, column=0)
        ttk.Button(translation, text="Z ▼", width=8,
                   command=lambda: self._stage_jog("z_m", -1)).grid(row=5, column=2)

        tilt = ttk.LabelFrame(stage, text="Tilt")
        tilt.pack(anchor="w", padx=8, pady=5)
        self.tilt_step = tk.StringVar(value="0.5")
        ttk.Label(tilt, text="Step (°)").grid(row=0, column=0, columnspan=2)
        ttk.Entry(tilt, textvariable=self.tilt_step, width=9).grid(row=0, column=2)
        ttk.Button(tilt, text="▲  α+", width=8,
                   command=lambda: self._stage_jog("alpha_deg", 1)).grid(row=1, column=1)
        ttk.Button(tilt, text="◀  β−", width=8,
                   command=lambda: self._stage_jog("beta_deg", -1)).grid(row=2, column=0)
        ttk.Label(tilt, text="α / β").grid(row=2, column=1)
        ttk.Button(tilt, text="β+  ▶", width=8,
                   command=lambda: self._stage_jog("beta_deg", 1)).grid(row=2, column=2)
        ttk.Button(tilt, text="▼  α−", width=8,
                   command=lambda: self._stage_jog("alpha_deg", -1)).grid(row=3, column=1)
        ttk.Button(stage, text="Refresh position", command=self._poll_state).pack(
            anchor="w", padx=10, pady=5)

        optics = ttk.LabelFrame(f, text="Optics and column")
        optics.pack(fill="x", padx=8, pady=6)
        self.focus_var = tk.StringVar(value="0")
        ttk.Label(optics, text="Focus Δ DAC").grid(row=0, column=0, padx=4)
        ttk.Entry(optics, textvariable=self.focus_var, width=12).grid(row=0, column=1)
        ttk.Button(optics, text="Apply", command=lambda: self._number_command(
            "change_focus", "delta_dac", self.focus_var, "Change focus",
            integer=True)).grid(row=0, column=2, padx=4)
        self.mag_var = tk.StringVar(value="0")
        ttk.Label(optics, text="Mag index").grid(row=0, column=3, padx=4)
        ttk.Entry(optics, textvariable=self.mag_var, width=8).grid(row=0, column=4)
        ttk.Button(optics, text="Set", command=lambda: self._number_command(
            "set_magnification", "mag_index", self.mag_var, "Set magnification",
            integer=True)).grid(row=0, column=5, padx=4)
        for col, (label, cmd, params) in enumerate((
            ("Blank", "set_beam_blank", {"blanked": True}),
            ("Unblank", "set_beam_blank", {"blanked": False}),
            ("Open valves", "set_column_valves", {"open": True}),
            ("Close valves", "set_column_valves", {"open": False}))):
            ttk.Button(optics, text=label, command=lambda c=cmd, p=params, l=label:
                       self._confirmed(c, p, l)).grid(row=1, column=col, padx=5, pady=8)

        acquire = ttk.LabelFrame(f, text="Acquisition")
        acquire.pack(fill="x", padx=8, pady=6)
        self.width_var, self.height_var, self.dwell_var = (
            tk.StringVar(value="512"), tk.StringVar(value="512"), tk.StringVar(value="2.0"))
        for col, (label, var) in enumerate((("Width", self.width_var),
                                            ("Height", self.height_var),
                                            ("Dwell µs", self.dwell_var))):
            ttk.Label(acquire, text=label).grid(row=0, column=col * 2, padx=3)
            ttk.Entry(acquire, textvariable=var, width=10).grid(row=0, column=col * 2 + 1)
        ttk.Button(acquire, text="Acquire STEM", command=self._acquire).grid(
            row=0, column=6, padx=8)
        ttk.Button(acquire, text="Start live", command=lambda: self._command(
            "live_start", {"width": int(self.width_var.get()),
                           "height": int(self.height_var.get()),
                           "dwell_us": float(self.dwell_var.get())}, "Start live")).grid(
                               row=0, column=7, padx=4)
        ttk.Button(acquire, text="Stop live", command=lambda: self._command(
            "live_stop", {}, "Stop live")).grid(row=0, column=8, padx=4)
        self.operator_log = scrolledtext.ScrolledText(f, height=18, state="disabled",
                                                      font=("Courier", 9))
        self.operator_log.pack(fill="both", expand=True, padx=8, pady=8)

    def _build_instrument(self):
        from client.gui.control_panel import ControlPanel
        f = ttk.Frame(self.tabs)
        self.tabs.add(f, text="Instrument")
        ControlPanel(f, self.bridge, self.state,
                     on_command=self._record_panel_command).pack(fill="both", expand=True)

    def _record_panel_command(self, label, cmd, params, result=None, error=None):
        """Record Instrument-tab commands as operator activity (any thread)."""
        if error is None and result is None:
            self.events.append("tool_call", actor="operator", summary=label,
                               data={"command": cmd, "params": params, "status": "running"})
        elif error is not None:
            self.events.append("tool_result", actor="operator", summary=f"{label}: ERROR",
                               data={"command": cmd, "error": error})
        else:
            self.events.append("tool_result", actor="operator", summary=f"{label}: OK",
                               data={"command": cmd, "result": result})

    def _build_map(self):
        f = ttk.Frame(self.tabs)
        self.tabs.add(f, text="Nanospace map")
        self.map_canvas = tk.Canvas(f, bg="#101820", width=900, height=650)
        self.map_canvas.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        side = ttk.Frame(f)
        side.pack(side="left", fill="y", padx=6, pady=6)
        ttk.Label(side, text="Stage-space objects", font=("", 11, "bold")).pack(anchor="w")
        self.map_list = tk.Listbox(side, width=42, height=30)
        self.map_list.pack(fill="both", expand=True, pady=5)
        self.map_list.bind("<<ListboxSelect>>", lambda _: self._select_map_object())
        if not self.read_only:
            ttk.Button(side, text="Move to selected…",
                       command=self._move_to_object).pack(fill="x")
        ttk.Button(side, text="Refresh map", command=self._refresh_map).pack(fill="x", pady=4)

    def _build_images(self):
        f = ttk.Frame(self.tabs)
        self.tabs.add(f, text="Session images")
        left = ttk.Frame(f)
        left.pack(side="left", fill="y", padx=6, pady=6)
        self.image_list = ttk.Treeview(left, columns=[c[0] for c in _IMAGE_COLUMNS],
                                       show="headings", height=32, selectmode="browse")
        for key, _, width, _ in _IMAGE_COLUMNS:
            self.image_list.heading(key, command=lambda k=key: self._sort_images_by(k))
            self.image_list.column(key, width=width)
        self._update_image_headings()
        self.image_list.pack(fill="both", expand=True)
        self.image_list.bind("<<TreeviewSelect>>", lambda _: self._select_image())
        right = ttk.Frame(f)
        right.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        self.image_canvas = tk.Canvas(right, bg="black", width=650, height=650)
        self.image_canvas.pack(fill="both", expand=True)
        self.image_meta = scrolledtext.ScrolledText(right, height=8, state="disabled")
        self.image_meta.pack(fill="x", pady=5)

    def _build_task(self):
        f = ttk.Frame(self.tabs)
        self.tabs.add(f, text="Task & goals")
        self.task_text = scrolledtext.ScrolledText(f, font=("Courier", 11), state="disabled")
        self.task_text.pack(fill="both", expand=True, padx=8, pady=8)

    def _build_activity(self):
        f = ttk.Frame(self.tabs)
        self.tabs.add(f, text="Activity")
        self.activity = ttk.Treeview(f, columns=("time", "actor", "type", "summary"),
                                     show="headings")
        for key, width in (("time", 170), ("actor", 90), ("type", 150), ("summary", 850)):
            self.activity.heading(key, text=key.title())
            self.activity.column(key, width=width, anchor="w")
        self.activity.pack(fill="both", expand=True, padx=6, pady=6)

    def _connect(self, address=None):
        address = (address or self.address.get()).strip()
        self.bridge.on_link = lambda ok, detail: self._post(
            lambda: self._show_link(ok, detail))
        self.connection.config(text=f"● connecting to {address}…", foreground="#a60")
        future = self.bridge.connect(address)
        def done(f):
            try:
                result = f.result()
                self.events.append("connection", actor="operator",
                                   summary=f"Connected to {address}", data=result)
            except Exception as exc:
                if not self.bridge.connected:   # socket never created
                    self._post(lambda e=exc: self.connection.config(
                        text=f"● error: {e}", foreground="red"))
        future.add_done_callback(done)

    def _show_link(self, responding, detail=""):
        """Header status: is the server actually answering?"""
        if responding:
            self.connection.config(text="● connected", foreground="dark green")
            self._write_log("Server answering.")
        else:
            secs = QUICK_TIMEOUT_MS // 1000
            self.connection.config(text="● not responding", foreground="red")
            self._write_log(
                f"Not responding: {detail}. Check the cable/route to the microscope PC "
                f"(run amadeus-check). Status recovers by itself once replies arrive; "
                f"state is re-polled with a {secs} s timeout.")

    def _command(self, cmd, params, label, *, image=False, preconfirmed=False):
        if self.read_only:
            return
        if not self.bridge.connected:
            self._write_log("Not connected.")
            return
        self._write_log(f"→ {cmd} {params}")
        self.events.append("tool_call", actor="operator", summary=label,
                           data={"command": cmd, "params": params, "status": "running"})
        future = (self.bridge.call_image(cmd, params, cmd, {"cmd": cmd, "origin": "operator"})
                  if image else self.bridge.call_data(cmd, params, preconfirmed=preconfirmed))
        def done(f):
            try:
                result = f.result()
                self.events.append("tool_result", actor="operator", summary=f"{label}: OK",
                                   data={"command": cmd, "result": result})
                self._post(lambda r=result: self._write_log(f"✓ {cmd} {r}"))
            except Exception as exc:
                self.events.append("tool_result", actor="operator",
                                   summary=f"{label}: ERROR", data={"command": cmd,
                                                                  "error": str(exc)})
                self._post(lambda e=exc: self._write_log(f"✗ {cmd}: {e}"))
        future.add_done_callback(done)

    def _confirmed(self, cmd, params, label):
        # This OK also answers the relay's go-ahead check (policy.confirm),
        # so the operator isn't asked twice.
        if messagebox.askokcancel("Confirm microscope command", f"{label}?"):
            self._command(cmd, params, label, preconfirmed=True)

    def _number_command(self, cmd, key, var, label, integer=False):
        try:
            value = int(var.get()) if integer else float(var.get())
        except ValueError:
            messagebox.showerror("Invalid value", f"{key} must be numeric")
            return
        self._command(cmd, {key: value}, label)

    def _stage_jog(self, axis, direction):
        try:
            if axis in ("x_m", "y_m"):
                step = float(self.translation_step.get()) * 1e-9
            elif axis == "z_m":
                step = float(self.z_step.get()) * 1e-9
            else:
                step = float(self.tilt_step.get())
        except ValueError:
            messagebox.showerror("Invalid stage step", "The selected step must be numeric.")
            return
        self._command("move_stage", {"mode": "relative", axis: direction * step},
                      f"Jog {axis}")

    def _acquire(self):
        try:
            p = {"width": int(self.width_var.get()), "height": int(self.height_var.get()),
                 "dwell_us": float(self.dwell_var.get()), "mode": "capture"}
        except ValueError:
            messagebox.showerror("Invalid acquisition", "Width, height, and dwell must be numeric.")
            return
        self._command("stem_acquire", p, "STEM acquisition", image=True)

    def _poll_state(self):
        if self.read_only:
            self._apply_snapshot_state()
            return
        if not self.bridge.connected or self.bridge.busy:
            # Don't queue a poll behind a running command (e.g. a long
            # acquisition): it would wait for the full command timeout.
            return
        future = self.bridge.call_data("get_microscope_state", {},
                                       timeout_ms=QUICK_TIMEOUT_MS)
        future.add_done_callback(lambda f: self._post(
            lambda: self._apply_state(f.result())) if not f.exception() else None)
        self.last_state_poll = time.monotonic()

    def _apply_snapshot_state(self):
        """Read-only mode: microscope state as last seen by the MCP server."""
        try:
            snap = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        params = dict(snap.get("params") or {})
        if not params.get("stage"):
            params["stage"] = {}
        self._apply_state(params)
        live = params.get("live_active")
        self.state_labels["live"].config(
            text="—" if live is None else ("running" if live else "stopped"))
        self.last_state_poll = time.monotonic()

    def _apply_state(self, p):
        stage = p.get("stage") or p
        values = {
            "mode": p.get("tem_stem_mode") or p.get("operation_mode"),
            "function": p.get("function_mode"),
            "magnification": p.get("magnification"),
            "focus": (f"{p.get('focus_nm')} nm" if p.get("focus_nm") is not None
                      else f"{p.get('focus_dac', '—')} DAC (uncalibrated)"),
            "spot": p.get("spot_size"), "ht": p.get("high_tension_kV"),
            "beam": "BLANKED" if p.get("beam_blanked") else "open",
            "valves": ("OPEN" if p.get("column_valves_open") else "closed"
                       if p.get("column_valves_open") is not None else "—"),
            "stage": "X {x_m:.3e}  Y {y_m:.3e}  Z {z_m:.3e}  α {alpha_deg:.2f}  β {beta_deg:.2f}".format(
                x_m=stage.get("x_m", 0), y_m=stage.get("y_m", 0), z_m=stage.get("z_m", 0),
                alpha_deg=stage.get("alpha_deg", 0), beta_deg=stage.get("beta_deg", 0)),
            "live": "—",
        }
        for key, value in values.items():
            self.state_labels[key].config(text=str(value if value is not None else "—"))

    def _refresh(self):
        self._heartbeat()
        self._check_confirm_request()
        self._refresh_pause()
        if time.monotonic() - self.last_state_poll > 2.0:
            self._poll_state()
        self._refresh_current()
        self._refresh_disk_state()
        self.root.after(self.poll_ms, self._refresh)

    # -- operator confirmation (client/confirm.py) -------------------------------
    def _heartbeat(self):
        """Tell the MCP server a console is here to answer confirmations.
        Writing it is not an instrument command, so --read-only does it too."""
        try:
            write_json(self.console_path, {"seen": time.time(), "read_only": self.read_only})
        except OSError:
            pass

    def _ask_confirm(self, req, who: str) -> bool:
        shown = {k: v for k, v in (req.params or {}).items() if k != "confirmed"}
        return messagebox.askokcancel(
            "AMADEUS: confirm microscope command",
            f"{who} needs your go-ahead:\n\n{req.cmd} {json.dumps(shown, default=str)}\n\n"
            f"{req.reason}\n\nAllow it?", icon="warning", parent=self.root)

    def _check_confirm_request(self):
        """A command held for the operator's go-ahead in the MCP session."""
        data = read_json(self.confirm_request_path)
        cid = data.get("id")
        if not cid or cid in self.answered_confirms:
            return
        self.answered_confirms.add(cid)
        from client.confirm import ConfirmRequest
        req = ConfirmRequest(str(data.get("cmd", "?")), dict(data.get("params") or {}),
                             str(data.get("reason", "")), id=cid)
        approved = self._ask_confirm(req, "The agent's command")
        try:
            write_json(self.confirm_reply_path, {"id": cid, "approved": bool(approved)})
        except OSError as exc:
            messagebox.showerror("AMADEUS", f"Could not send the answer: {exc}")
        self.events.append("operator_confirm", actor="operator",
                           summary=f"{'Allowed' if approved else 'Denied'} {req.cmd}",
                           data={"command": req.cmd, "approved": bool(approved)})

    def _refresh_disk_state(self):
        path = self.data_dir / StateManager.STATE_FILE
        sig = path.stat().st_mtime_ns if path.exists() else 0
        event_sig = self.events.path.stat().st_mtime_ns if self.events.path.exists() else 0
        image_sig = self.state.images.root.stat().st_mtime_ns
        combined = (sig, event_sig, image_sig)
        if combined == self.last_disk_sig:
            return
        self.last_disk_sig = combined
        self.state = StateManager(self.data_dir)
        self._refresh_task()
        self._refresh_map()
        self._refresh_images()
        self._refresh_activity()

    def _refresh_task(self):
        t, p = self.state.task, self.state.plan
        lines = [f"SCIENTIFIC OBJECTIVE\n{p.objective or '—'}", "",
                 f"ACTIVE TASK [{t.status.upper()}]\n{t.objective or '—'}", "",
                 t.status_summary]
        if t.blocking_condition:
            lines += ["", f"BLOCKED: {t.blocking_condition}"]
        lines += ["", "STEPS"]
        for i, step in enumerate(t.steps):
            mark = "→" if i == t.current_step else {"completed": "✓", "failed": "✗"}.get(
                step.status, "○")
            lines.append(f" {mark} {step.label}")
        text = "\n".join(lines)
        for widget in (self.task_text, self.task_summary):
            widget.config(state="normal"); widget.delete("1.0", "end")
            widget.insert("1.0", text); widget.config(state="disabled")

    def _refresh_map(self):
        objs = self.state.list_map_objects()
        self.map_list.delete(0, "end")
        self._map_objects = objs
        self.map_canvas.delete("all")
        if not objs:
            self.map_canvas.create_text(450, 300, text="No map objects yet",
                                        fill="white", font=("", 16))
            return
        xs = [o.position.x_m for o in objs]; ys = [o.position.y_m for o in objs]
        xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
        dx, dy = max(xmax - xmin, 1e-9), max(ymax - ymin, 1e-9)
        for i, o in enumerate(objs):
            x = 50 + 800 * (o.position.x_m - xmin) / dx
            y = 600 - 550 * (o.position.y_m - ymin) / dy
            color = "#4caf50" if o.examined else "#ffb300"
            self.map_canvas.create_oval(x-7, y-7, x+7, y+7, fill=color, outline="white")
            self.map_canvas.create_text(x+10, y, text=o.label, fill="white", anchor="w")
            self.map_list.insert("end", f"{'✓' if o.examined else '○'} {o.label}  "
                                 f"({o.position.x_m*1e6:.3f}, {o.position.y_m*1e6:.3f}) µm")

    def _select_map_object(self):
        pass

    def _move_to_object(self):
        sel = self.map_list.curselection()
        if not sel:
            return
        o = self._map_objects[sel[0]]
        if messagebox.askokcancel("Confirm absolute stage move",
                                  f"Move to {o.label}\n{o.position.model_dump()}?"):
            self._command("move_stage", {**o.position.model_dump(), "mode": "absolute"},
                          f"Move to {o.label}")

    def _refresh_images(self):
        sidecars = []
        for iid in self.state.images.list_ids():
            try:
                sidecars.append(self.state.images.load_sidecar(iid))
            except Exception:
                continue
        self._image_sidecars = sidecars
        # Auto-select a newly arrived acquisition; on the first load, the latest.
        if self.known_image_ids is None:
            target = newest_new_image(sidecars, set())
        else:
            target = newest_new_image(sidecars, self.known_image_ids)
        self.known_image_ids = {s.image_id for s in sidecars}
        self._populate_image_list(select=target)

    def _populate_image_list(self, select=None):
        previous = self.image_list.selection()
        keep = select or (previous[0] if previous else None)
        self.image_list.delete(*self.image_list.get_children())
        column, descending = self.image_sort
        for s in sort_images(getattr(self, "_image_sidecars", []), column, descending):
            self.image_list.insert("", "end", iid=s.image_id,
                                   values=(str(s.timestamp)[:19], s.name,
                                           "×".join(map(str, s.shape))))
        if keep and self.image_list.exists(keep):
            self.image_list.selection_set(keep)
            self.image_list.see(keep)
            if select:
                self.image_list.focus(keep)

    def _sort_images_by(self, column):
        current, descending = self.image_sort
        if column == current:
            descending = not descending
        else:
            descending = next(c[3] for c in _IMAGE_COLUMNS if c[0] == column)
        self.image_sort = (column, descending)
        self._update_image_headings()
        self._populate_image_list()

    def _update_image_headings(self):
        column, descending = self.image_sort
        for key, title, _, _ in _IMAGE_COLUMNS:
            arrow = (" ▼" if descending else " ▲") if key == column else ""
            self.image_list.heading(key, text=title + arrow)

    def _select_image(self):
        sel = self.image_list.selection()
        if not sel:
            return
        iid = sel[0]
        try:
            _, sidecar = self.state.images.load(iid)
            self._render(self.state.images.root / f"{iid}.npy", self.image_canvas,
                         "browser", 650)
            text = sidecar.model_dump_json(indent=2)
            self.image_meta.config(state="normal"); self.image_meta.delete("1.0", "end")
            self.image_meta.insert("1.0", text); self.image_meta.config(state="disabled")
        except Exception as exc:
            messagebox.showerror("Image error", str(exc))

    def _refresh_current(self):
        live = self.data_dir / "monitor" / "live.npy"
        if live.exists() and time.time() - live.stat().st_mtime < 3:
            path = live
        else:
            ids = self.state.images.list_ids()
            path = self.state.images.root / f"{ids[-1]}.npy" if ids else None
        if path:
            self._render(path, self.current_canvas, "current", _VIEW)
            self.current_caption.config(text=path.stem)
        else:
            self._show_no_image(self.current_canvas, _VIEW)
            self.current_caption.config(text="No image")

    def _show_no_image(self, canvas, box):
        if self.rendered.get("current") == "no-image":
            return
        canvas.delete("all")
        canvas.create_text(box // 2, box // 2, text="No image", fill="white",
                           font=("", 18))
        self.rendered["current"] = "no-image"

    def _render(self, path, canvas, key, box):
        try:
            signature = (path, path.stat().st_mtime_ns)
        except OSError:
            return
        if self.rendered.get(key) == signature:
            return
        out = self.data_dir / "monitor" / f"_console_{key}.ppm"
        out.parent.mkdir(parents=True, exist_ok=True)
        render_ppm(path, out, box=box)
        photo = tk.PhotoImage(file=str(out)); self.photo[key] = photo
        canvas.delete("all"); canvas.create_image(box // 2, box // 2, image=photo)
        self.rendered[key] = signature

    def _refresh_activity(self):
        self.activity.delete(*self.activity.get_children())
        combined = self.events.list(300)
        try:
            snap = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
            for h in snap.get("history", []):
                combined.append({"timestamp": h.get("started"), "actor": "agent",
                                 "type": "instrument_command",
                                 "summary": f"{h.get('cmd')} — {h.get('status')}"})
        except (OSError, json.JSONDecodeError):
            pass
        for e in sorted(combined, key=lambda x: x.get("timestamp") or "", reverse=True):
            self.activity.insert("", "end", values=(str(e.get("timestamp", ""))[:23],
                                 e.get("actor"), e.get("type"), e.get("summary")))

    def _read_paused(self):
        try:
            return bool(json.loads(self.control_path.read_text()).get("paused"))
        except (OSError, json.JSONDecodeError):
            return False

    def _toggle_pause(self):
        paused = not self._read_paused()
        self.control_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.control_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"paused": paused}), encoding="utf-8")
        os.replace(tmp, self.control_path)
        self.events.append("control", actor="operator",
                           summary="Paused agent commands" if paused else "Resumed agent commands")
        self._refresh_pause()

    def _refresh_pause(self):
        paused = self._read_paused()
        self.pause.config(text="▶ RESUME" if paused else "⏸ PAUSE",
                          bg="#c0392b" if paused else "SystemButtonFace",
                          fg="white" if paused else "black")

    def _write_log(self, text):
        self.operator_log.config(state="normal")
        self.operator_log.insert("end", text + "\n"); self.operator_log.see("end")
        self.operator_log.config(state="disabled")

    def _post(self, fn):
        self.ui_queue.put(fn)

    def _drain(self):
        try:
            while True:
                self.ui_queue.get_nowait()()
        except queue.Empty:
            pass
        self.root.after(50, self._drain)

    def close(self):
        if self.bridge is not None:
            self.bridge.close()
        self.root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description="AMADEUS unified operator console")
    amadeus_config.add_config_argument(parser)
    parser.add_argument("--address", default=None,
                        help="relay address (default: from amadeus_config.json, "
                             "else tcp://localhost:9875)")
    parser.add_argument("--data-dir", default=None, type=Path,
                        help="data dir shared with amadeus-server (default: from config)")
    parser.add_argument("--timeout", default=None, type=float,
                        help="per-command timeout in seconds (default: from config, else 600)")
    parser.add_argument("--poll-ms", default=500, type=int)
    parser.add_argument("--read-only", action="store_true",
                        help="passive monitor for agent sessions: no instrument "
                             "commands, state from the MCP server's snapshot, "
                             "Pause/Resume kept")
    args = parser.parse_args()
    defaults = amadeus_config.client_defaults(parser, args)
    args.address = args.address or defaults["address"]
    args.data_dir = args.data_dir or defaults["data_dir"]
    args.timeout = args.timeout or defaults["timeout_s"]
    root = tk.Tk()
    OperatorConsole(root, args.address, args.data_dir, int(args.timeout * 1000), args.poll_ms,
                    read_only=args.read_only)
    root.mainloop()


if __name__ == "__main__":
    main()
