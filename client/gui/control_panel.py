"""Manual instrument control panel (the operator console's Instrument tab).

Moved from the former standalone ``client/manual_gui.py`` (``amadeus-gui``).
It is the client-side twin of the in-DM ``dm_gui.py``.
"""

from __future__ import annotations

import queue
import time
import traceback

import tkinter as tk
from tkinter import scrolledtext, ttk

from client.gui.render import render_ppm
from client import stage_moves


# ---------------------------------------------------------------------------
# Small widget helpers (kept local: the client GUI must not import the
# DM-side dm_gui module, which launches itself on import).
# ---------------------------------------------------------------------------

def _fval(widget) -> float:
    """Read a float from an Entry-like widget; 0.0 on empty/invalid."""
    try:
        return float(widget.get())
    except ValueError:
        return 0.0


def _ival(widget) -> int:
    try:
        return int(widget.get())
    except ValueError:
        return 0


def _fmt_focus(r: dict) -> str:
    """Focus readout: nm when calibrated, the device value otherwise."""
    focus_nm = r.get("focus_nm")
    if focus_nm is not None:
        return f"{focus_nm:.4f}"
    focus_dac = r.get("focus_dac")
    if focus_dac is not None:
        return f"{focus_dac:.0f} {r.get('unit') or ''}".strip()
    return "—"


class _LabelEntry(ttk.Frame):
    """A label + entry pair in a single row."""

    def __init__(self, parent, label: str, default: str = "", width: int = 10):
        super().__init__(parent)
        ttk.Label(self, text=label, width=14, anchor="e").pack(side="left")
        self.var = tk.StringVar(value=default)
        self.entry = ttk.Entry(self, textvariable=self.var, width=width)
        self.entry.pack(side="left", padx=4)

    def get(self) -> str:
        return self.var.get()

    def set(self, val) -> None:
        self.var.set(str(val))


class _Readout(ttk.Label):
    """A sunken read-only display cell for live instrument values."""

    _PLACEHOLDER = "—"

    def __init__(self, parent, width: int = 12, anchor: str = "center"):
        super().__init__(parent, text=self._PLACEHOLDER, relief="sunken",
                         width=width, anchor=anchor)

    def set(self, value: str) -> None:
        self.config(text=value)

    def clear(self) -> None:
        self.config(text=self._PLACEHOLDER)


# ---------------------------------------------------------------------------
# ZMQ bridge — async client on a background event-loop thread


class ControlPanel(ttk.Frame):
    """Full manual instrument control, embedded in the operator console.

    Tabs: Status, Stage, Focus, Beam, STEM Acquire, Scan, TEM Camera,
    Spectroscopy, EDS. Every button issues the same vendor-neutral relay
    command the MCP tools use, through the console's shared :class:`ZmqBridge`.
    Images returned over ZMQ are saved into the session ImageStore.

    *on_command(label, cmd, params, result=None, error=None)* is called before
    (result and error both None) and after every command, so the console can
    record operator activity.
    """

    def __init__(self, parent, bridge, state, on_command=None):
        super().__init__(parent)
        self.root = self.winfo_toplevel()
        self._state = state
        self._bridge = bridge
        self._on_command = on_command
        self._live_photo = None
        self._live_signature = None

        # Thread-safe hand-off of UI updates from the loop thread to Tk.
        self._ui_queue: "queue.Queue" = queue.Queue()

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=6)
        self._build_status_tab(nb)
        self._build_stage_tab(nb)
        self._build_focus_tab(nb)
        self._build_beam_tab(nb)
        self._build_stem_tab(nb)
        self._build_scan_tab(nb)
        self._build_camera_tab(nb)
        self._build_spectroscopy_tab(nb)
        self._build_eds_tab(nb)

        log_frame = ttk.LabelFrame(self, text="Output / Log")
        log_frame.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.log = scrolledtext.ScrolledText(log_frame, height=8, state="disabled",
                                             font=("Courier", 9))
        self.log.pack(fill="both", expand=True, padx=4, pady=4)
        ttk.Button(log_frame, text="Clear log", command=self._clear_log).pack(
            anchor="e", padx=4, pady=2)

        self.after(50, self._poll_queue)
        self.after(500, self._refresh_live_view)

    def _ping(self):
        self._run("ping", "ping", {})

    # -----------------------------------------------------------------------
    # Shared run / log helpers
    # -----------------------------------------------------------------------
    def _run(self, label: str, cmd: str, params: dict | None = None,
             on_success=None, *, image: bool = False, name: str = "",
             tags: dict | None = None, timeout_ms: int | None = None,
             out_of_band: bool = False):
        """Submit *cmd* over ZMQ; log the result and run *on_success* on it.
        *out_of_band* sends it on its own socket so it doesn't queue behind a
        command in flight (STOP)."""
        if not self._bridge.connected:
            self._log(f"[{label}] SKIPPED — not connected (use Connect first).")
            return
        self._log(f"[{label}] ...")
        if self._on_command:
            self._on_command(label, cmd, params or {})
        if image:
            fut = self._bridge.call_image(cmd, params or {}, name or cmd,
                                          tags or {"cmd": cmd})
        elif out_of_band:
            fut = self._bridge.call_out_of_band(cmd, params or {}, timeout_ms=timeout_ms)
        else:
            fut = self._bridge.call_data(cmd, params or {}, timeout_ms=timeout_ms)

        def _done(f):
            try:
                result = f.result()
            except Exception as exc:
                if self._on_command:
                    self._on_command(label, cmd, params or {}, error=str(exc))
                self._post(lambda e=exc: self._log(f"[{label}] ERROR: {e}"))
                return
            if self._on_command:
                self._on_command(label, cmd, params or {}, result=result)

            def deliver():
                self._log(f"[{label}] OK  {result}")
                if on_success and result is not None:
                    try:
                        on_success(result)
                    except Exception:
                        self._log(f"[{label}] (display update failed)\n{traceback.format_exc()}")
            self._post(deliver)
        fut.add_done_callback(_done)

    def _post(self, fn) -> None:
        """Queue *fn* to run on the Tk thread."""
        self._ui_queue.put(fn)

    def _poll_queue(self) -> None:
        try:
            while True:
                fn = self._ui_queue.get_nowait()
                try:
                    fn()
                except Exception:
                    self._log(traceback.format_exc())
        except queue.Empty:
            pass
        self.after(50, self._poll_queue)

    def _log(self, msg: str) -> None:
        self.log.config(state="normal")
        self.log.insert("end", msg.rstrip() + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _clear_log(self) -> None:
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    # -----------------------------------------------------------------------
    # Status tab
    # -----------------------------------------------------------------------
    def _build_status_tab(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="Status")

        ttk.Label(f, text="Microscope:").grid(row=0, column=0, sticky="e", padx=6, pady=(8, 4))
        self._st_name = _Readout(f, width=28, anchor="w")
        self._st_name.grid(row=0, column=1, columnspan=3, sticky="w", padx=6, pady=(8, 4))

        def add_row(parent, row, label, width=14):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="e", padx=4, pady=1)
            r = _Readout(parent, width=width)
            r.grid(row=row, column=1, sticky="w", padx=4, pady=1)
            return r

        col_frame = ttk.LabelFrame(f, text="Column")
        col_frame.grid(row=1, column=0, sticky="nsew", padx=6, pady=4)
        self._st_ht      = add_row(col_frame, 0, "HT (kV):")
        self._st_mag     = add_row(col_frame, 1, "Mag (×):")
        self._st_mag_idx = add_row(col_frame, 2, "Mag index:")
        self._st_focus   = add_row(col_frame, 3, "Focus (nm):")

        beam_frame = ttk.LabelFrame(f, text="Beam")
        beam_frame.grid(row=1, column=1, sticky="nsew", padx=6, pady=4)
        self._st_bs_x = add_row(beam_frame, 0, "Shift X:")
        self._st_bs_y = add_row(beam_frame, 1, "Shift Y:")
        self._st_bt_x = add_row(beam_frame, 2, "Tilt X:")
        self._st_bt_y = add_row(beam_frame, 3, "Tilt Y:")

        stig_frame = ttk.LabelFrame(f, text="Stigmation")
        stig_frame.grid(row=2, column=0, sticky="nsew", padx=6, pady=4)
        self._st_obj_x  = add_row(stig_frame, 0, "Objective X:")
        self._st_obj_y  = add_row(stig_frame, 1, "Objective Y:")
        self._st_cond_x = add_row(stig_frame, 2, "Condenser X:")
        self._st_cond_y = add_row(stig_frame, 3, "Condenser Y:")

        state_frame = ttk.LabelFrame(f, text="State")
        state_frame.grid(row=2, column=1, sticky="nsew", padx=6, pady=4)
        self._st_blanked = add_row(state_frame, 0, "Beam:", width=14)
        self._st_valves  = add_row(state_frame, 1, "Column valves:", width=14)

        btn_row = ttk.Frame(f)
        btn_row.grid(row=3, column=0, columnspan=2, sticky="w", padx=6, pady=8)
        ttk.Button(btn_row, text="Refresh All", command=self._status_refresh).pack(side="left", padx=4)
        ttk.Button(btn_row, text="Clear", command=self._status_clear).pack(side="left", padx=4)

        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=1)

    def _status_refresh(self):
        self._run("get_microscope_state", "get_microscope_state", {},
                  self._update_status_labels)

    def _update_status_labels(self, r):
        def vec(v):
            return f"{v: .3e}"
        self._st_name.set(r.get("microscope_name", "—"))
        self._st_ht.set(f"{r['high_tension_kV']:.1f}")
        mag = r.get("magnification")
        self._st_mag.set(f"{mag:.0f}" if mag is not None else "—")
        self._st_mag_idx.set(str(r["mag_index"]))
        self._st_focus.set(_fmt_focus(r))
        self._st_bs_x.set(vec(r["beam_shift"]["x"]))
        self._st_bs_y.set(vec(r["beam_shift"]["y"]))
        self._st_bt_x.set(vec(r["beam_tilt"]["x"]))
        self._st_bt_y.set(vec(r["beam_tilt"]["y"]))
        stig = r.get("stigmation") or {}
        obj, cond = stig.get("objective") or {}, stig.get("condenser") or {}
        self._st_obj_x.set(vec(obj.get("x", 0)))
        self._st_obj_y.set(vec(obj.get("y", 0)))
        self._st_cond_x.set(vec(cond.get("x", 0)))
        self._st_cond_y.set(vec(cond.get("y", 0)))
        self._st_blanked.set("BLANKED" if r["beam_blanked"] else "open")
        self._st_valves.set("OPEN" if r["column_valves_open"] else "closed")

    def _status_clear(self):
        for w in (self._st_name, self._st_ht, self._st_mag, self._st_mag_idx,
                  self._st_focus, self._st_bs_x, self._st_bs_y, self._st_bt_x,
                  self._st_bt_y, self._st_obj_x, self._st_obj_y, self._st_cond_x,
                  self._st_cond_y, self._st_blanked, self._st_valves):
            w.clear()

    # -----------------------------------------------------------------------
    # Stage tab
    # -----------------------------------------------------------------------
    def _build_stage_tab(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="Stage")

        ttk.Label(f, text="Current position", font=("", 9, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(8, 2))

        self._pos_labels = {}
        for col, (name, unit) in enumerate([("X", "µm"), ("Y", "µm"), ("Z", "µm"),
                                            ("α", "°"), ("β", "°")]):
            ttk.Label(f, text=f"{name} ({unit})").grid(row=1, column=col, padx=8)
            lbl = _Readout(f, width=12)
            lbl.grid(row=2, column=col, padx=8, pady=2)
            self._pos_labels[name] = lbl

        ttk.Button(f, text="Get Position", command=self._stage_get).grid(
            row=3, column=0, columnspan=2, padx=8, pady=8, sticky="w")

        ttk.Separator(f, orient="horizontal").grid(
            row=4, column=0, columnspan=5, sticky="ew", padx=8, pady=4)

        jogs = ttk.Frame(f)
        jogs.grid(row=5, column=0, columnspan=3, sticky="nw", padx=8, pady=4)
        self._build_motion_frame(f).grid(row=5, column=3, columnspan=2, sticky="nw",
                                         padx=8, pady=4)
        translation = ttk.LabelFrame(jogs, text="Translation")
        translation.pack(anchor="w", pady=(0, 8))
        self._translation_step = _LabelEntry(translation, "XY step (nm)", "100", width=8)
        self._translation_step.grid(row=0, column=0, columnspan=3, pady=3)
        ttk.Button(translation, text="▲  Y+", width=8,
                   command=lambda: self._stage_jog("y_m", 1)).grid(row=1, column=1)
        ttk.Button(translation, text="◀  X−", width=8,
                   command=lambda: self._stage_jog("x_m", -1)).grid(row=2, column=0)
        ttk.Label(translation, text="XY").grid(row=2, column=1)
        ttk.Button(translation, text="X+  ▶", width=8,
                   command=lambda: self._stage_jog("x_m", 1)).grid(row=2, column=2)
        ttk.Button(translation, text="▼  Y−", width=8,
                   command=lambda: self._stage_jog("y_m", -1)).grid(row=3, column=1)
        self._z_step = _LabelEntry(translation, "Z step (nm)", "100", width=8)
        self._z_step.grid(row=4, column=0, columnspan=3, pady=(8, 3))
        ttk.Button(translation, text="Z ▲", width=8,
                   command=lambda: self._stage_jog("z_m", 1)).grid(row=5, column=0)
        ttk.Button(translation, text="Z ▼", width=8,
                   command=lambda: self._stage_jog("z_m", -1)).grid(row=5, column=2)

        tilt = ttk.LabelFrame(jogs, text="Tilt")
        tilt.pack(anchor="w")
        self._tilt_step = _LabelEntry(tilt, "Step (°)", "0.5", width=8)
        self._tilt_step.grid(row=0, column=0, columnspan=3, pady=3)
        ttk.Button(tilt, text="▲  α+", width=8,
                   command=lambda: self._stage_jog("alpha_deg", 1)).grid(row=1, column=1)
        ttk.Button(tilt, text="◀  β−", width=8,
                   command=lambda: self._stage_jog("beta_deg", -1)).grid(row=2, column=0)
        ttk.Label(tilt, text="α / β").grid(row=2, column=1)
        ttk.Button(tilt, text="β+  ▶", width=8,
                   command=lambda: self._stage_jog("beta_deg", 1)).grid(row=2, column=2)
        ttk.Button(tilt, text="▼  α−", width=8,
                   command=lambda: self._stage_jog("alpha_deg", -1)).grid(row=3, column=1)

        ttk.Separator(f, orient="horizontal").grid(
            row=6, column=0, columnspan=5, sticky="ew", padx=8, pady=4)

        ttk.Label(f, text="Move absolute (nm / °)", font=("", 9, "bold")).grid(
            row=7, column=0, columnspan=5, sticky="w", padx=8)
        self._abs = {}
        for col, (key, lbl) in enumerate([("x_nm", "X (nm)"), ("y_nm", "Y (nm)"),
                                          ("z_nm", "Z (nm)"), ("alpha", "α (°)"),
                                          ("beta", "β (°)")]):
            e = _LabelEntry(f, lbl, "", width=8)
            e.grid(row=8, column=col, padx=4, pady=4)
            self._abs[key] = e
        ttk.Button(f, text="Move Absolute", command=self._stage_move_abs).grid(
            row=9, column=0, columnspan=2, padx=8, pady=4, sticky="w")
        ttk.Label(f, text="With the piezo drive, X/Y are piezo positions (±10 µm), "
                          "not stage positions.", foreground="gray").grid(
            row=9, column=2, columnspan=3, sticky="w", padx=8)

    def _build_motion_frame(self, parent):
        """Speed, drive, backlash approach and wait time for every move from
        this tab (each applies to that move only; the instrument restores its
        own speed/drive afterwards), plus stage status and STOP."""
        box = ttk.LabelFrame(parent, text="Motion")
        ttk.Label(box, text="Speed").grid(row=0, column=0, sticky="e", padx=4, pady=2)
        self._stage_speed = tk.StringVar(value=stage_moves.DEFAULT_LABEL)
        ttk.Combobox(box, textvariable=self._stage_speed, state="readonly", width=9,
                     values=[stage_moves.DEFAULT_LABEL, *stage_moves.SPEEDS]).grid(
            row=0, column=1, sticky="w", padx=4, pady=2)
        ttk.Label(box, text="Drive").grid(row=1, column=0, sticky="e", padx=4, pady=2)
        self._stage_drive = tk.StringVar(value="motor")
        drives = ttk.Frame(box)
        drives.grid(row=1, column=1, sticky="w")
        ttk.Radiobutton(drives, text="Motor", value="motor",
                        variable=self._stage_drive).pack(side="left")
        ttk.Radiobutton(drives, text="Piezo (fine XY)", value="piezo",
                        variable=self._stage_drive).pack(side="left", padx=(6, 0))
        self._stage_approach = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="Backlash approach (X/Y)",
                        variable=self._stage_approach).grid(
            row=2, column=0, columnspan=2, sticky="w", padx=4, pady=2)
        self._stage_wait = _LabelEntry(box, "Wait up to (s)", "20", width=6)
        self._stage_wait.grid(row=3, column=0, columnspan=2, sticky="w", pady=2)

        ttk.Separator(box, orient="horizontal").grid(
            row=4, column=0, columnspan=2, sticky="ew", pady=4)
        self._stage_status = {}
        for r, (key, text) in enumerate([("moving", "Moving"), ("drive", "Drive"),
                                         ("speed", "Speed xy/z/tilt"),
                                         ("piezo", "Piezo X, Y (nm)"),
                                         ("limit", "Limit")], start=5):
            ttk.Label(box, text=text).grid(row=r, column=0, sticky="e", padx=4)
            cell = _Readout(box, width=16)
            cell.grid(row=r, column=1, sticky="w", padx=4, pady=1)
            self._stage_status[key] = cell
        ttk.Button(box, text="Stage status", command=self._stage_status_get).grid(
            row=10, column=0, sticky="w", padx=4, pady=4)
        tk.Button(box, text="STOP STAGE", command=self._stage_stop, bg="#c62828",
                  fg="white", activebackground="#8e0000", activeforeground="white",
                  font=("", 10, "bold")).grid(row=10, column=1, sticky="ew", padx=4, pady=4)
        return box

    def _stage_move(self, label: str, params: dict) -> None:
        """Apply the Motion settings to *params* and send move_stage."""
        try:
            timeout_ms = stage_moves.console_move_options(
                params, self._stage_speed.get(), self._stage_drive.get(),
                self._stage_approach.get(), self._stage_wait.get())
        except ValueError as exc:
            self._log(f"[{label}] not sent: {exc}")
            return
        self._run(label, "move_stage", params, self._after_stage_move, timeout_ms=timeout_ms)

    def _after_stage_move(self, result: dict) -> None:
        if all(k in result for k in ("x_m", "y_m", "z_m", "alpha_deg", "beta_deg")):
            self._update_stage_labels(result)
        if result.get("settled") is False:
            self._log("  stage still moving after the wait time; use Stage status, "
                      "or STOP STAGE to abort it")
        if result.get("limit_error"):
            self._log("  a stage axis reports a HARDWARE LIMIT")

    def _stage_status_get(self) -> None:
        self._run("get_stage_status", "get_stage_status", {}, self._show_stage_status)

    def _show_stage_status(self, status: dict) -> None:
        for key, text in stage_moves.describe_status(status).items():
            self._stage_status[key].set(text)

    def _stage_stop(self) -> None:
        self._run("STOP STAGE", "stop_stage", {}, self._show_stage_status,
                  timeout_ms=10_000, out_of_band=True)

    def _stage_get(self):
        self._run("get_stage_position", "get_stage_position", {},
                  self._update_stage_labels)

    def _update_stage_labels(self, result):
        um = 1e6
        self._pos_labels["X"].set(f"{result['x_m']*um:.4f}")
        self._pos_labels["Y"].set(f"{result['y_m']*um:.4f}")
        self._pos_labels["Z"].set(f"{result['z_m']*um:.4f}")
        self._pos_labels["α"].set(f"{result['alpha_deg']:.4f}")
        self._pos_labels["β"].set(f"{result['beta_deg']:.4f}")

    def _stage_jog(self, axis: str, direction: int):
        if axis in ("x_m", "y_m"):
            step = _fval(self._translation_step) * 1e-9
        elif axis == "z_m":
            step = _fval(self._z_step) * 1e-9
        else:
            step = _fval(self._tilt_step)
        params = {"mode": "relative", axis: direction * step}
        self._stage_move(f"jog {axis}", params)

    def _stage_move_abs(self):
        params = {"mode": "absolute"}
        mapping = [("x_nm", "x_m", 1e-9), ("y_nm", "y_m", 1e-9), ("z_nm", "z_m", 1e-9),
                   ("alpha", "alpha_deg", 1.0), ("beta", "beta_deg", 1.0)]
        for gui_key, param_key, scale in mapping:
            raw = self._abs[gui_key].get().strip()
            if raw:
                try:
                    params[param_key] = float(raw) * scale
                except ValueError:
                    pass
        if len(params) == 1:
            self._log("[move_stage(absolute)] not sent: enter at least one axis")
            return
        self._stage_move("move_stage(absolute)", params)

    # -----------------------------------------------------------------------
    # Focus tab
    # -----------------------------------------------------------------------
    def _build_focus_tab(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="Focus")

        ttk.Label(f, text="Current focus", font=("", 9, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=8, pady=(8, 2))
        ttk.Label(f, text="Focus (nm):").grid(row=1, column=0, sticky="e", padx=8)
        self._focus_lbl = _Readout(f, width=16)
        self._focus_lbl.grid(row=1, column=1, padx=8, pady=4, sticky="w")
        ttk.Button(f, text="Get Focus", command=self._focus_get).grid(row=1, column=2, padx=8)

        ttk.Separator(f, orient="horizontal").grid(
            row=2, column=0, columnspan=4, sticky="ew", padx=8, pady=8)
        ttk.Label(f, text="Change focus by Δ (nm):").grid(row=3, column=0, sticky="e", padx=8)
        self._focus_delta = ttk.Entry(f, width=12)
        self._focus_delta.insert(0, "0")
        self._focus_delta.grid(row=3, column=1, padx=8, pady=4, sticky="w")
        ttk.Button(f, text="Apply Δ", command=self._focus_change).grid(row=3, column=2, padx=8)

        ttk.Separator(f, orient="horizontal").grid(
            row=4, column=0, columnspan=4, sticky="ew", padx=8, pady=8)
        ttk.Label(f, text="Set focus to (nm):").grid(row=5, column=0, sticky="e", padx=8)
        self._focus_target = ttk.Entry(f, width=12)
        self._focus_target.insert(0, "0")
        self._focus_target.grid(row=5, column=1, padx=8, pady=4, sticky="w")
        ttk.Button(f, text="Set Focus", command=self._focus_set).grid(row=5, column=2, padx=8)

        ttk.Separator(f, orient="horizontal").grid(
            row=6, column=0, columnspan=4, sticky="ew", padx=8, pady=8)
        ttk.Label(f, text="Magnification", font=("", 9, "bold")).grid(
            row=7, column=0, columnspan=2, sticky="w", padx=8)
        ttk.Label(f, text="Mag (×):").grid(row=8, column=0, sticky="e", padx=8)
        self._mag_lbl = _Readout(f, width=16)
        self._mag_lbl.grid(row=8, column=1, padx=8, pady=4, sticky="w")
        ttk.Label(f, text="Mag index:").grid(row=9, column=0, sticky="e", padx=8)
        self._mag_idx_entry = ttk.Entry(f, width=8)
        self._mag_idx_entry.insert(0, "0")
        self._mag_idx_entry.grid(row=9, column=1, padx=8, pady=4, sticky="w")
        btn_frame = ttk.Frame(f)
        btn_frame.grid(row=10, column=0, columnspan=3, padx=8, pady=4, sticky="w")
        ttk.Button(btn_frame, text="Get Mag", command=self._mag_get).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Set Mag Index", command=self._mag_set).pack(side="left", padx=4)

    def _focus_get(self):
        self._run("get_focus", "get_focus", {},
                  lambda r: self._focus_lbl.set(_fmt_focus(r)))

    def _focus_change(self):
        self._run("change_focus", "change_focus", {"delta_nm": _fval(self._focus_delta)},
                  lambda r: self._focus_lbl.set(_fmt_focus(r)))

    def _focus_set(self):
        self._run("set_focus", "set_focus", {"focus_nm": _fval(self._focus_target)})

    def _mag_get(self):
        def _update(r):
            mag = r.get("magnification")
            self._mag_lbl.set(f"{mag:.0f}" if mag is not None else "—")
            self._mag_idx_entry.delete(0, "end")
            self._mag_idx_entry.insert(0, str(r["mag_index"]))
        self._run("get_magnification", "get_magnification", {}, _update)

    def _mag_set(self):
        def _update(r):
            mag = r.get("magnification")
            self._mag_lbl.set(f"{mag:.0f}" if mag is not None else "—")
        self._run("set_magnification", "set_magnification",
                  {"mag_index": _ival(self._mag_idx_entry)}, _update)

    # -----------------------------------------------------------------------
    # Beam tab
    # -----------------------------------------------------------------------
    def _build_beam_tab(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="Beam")
        self._beam_widgets = {}

        def section(title, key, get_fn, set_fn):
            box = ttk.LabelFrame(f, text=title)
            box.pack(fill="x", padx=8, pady=4)
            cur = ttk.Frame(box)
            cur.pack(anchor="w", padx=4, pady=2)
            ttk.Label(cur, text="Current:", width=10, anchor="e").pack(side="left")
            cx = _Readout(cur, width=14)
            cx.pack(side="left", padx=2)
            cy = _Readout(cur, width=14)
            cy.pack(side="left", padx=2)
            ttk.Button(cur, text="Get", command=get_fn).pack(side="left", padx=8)
            setrow = ttk.Frame(box)
            setrow.pack(anchor="w", padx=4, pady=2)
            ttk.Label(setrow, text="Set to:", width=10, anchor="e").pack(side="left")
            sx_var = tk.StringVar(value="0")
            sy_var = tk.StringVar(value="0")
            ttk.Entry(setrow, textvariable=sx_var, width=14).pack(side="left", padx=2)
            ttk.Entry(setrow, textvariable=sy_var, width=14).pack(side="left", padx=2)
            ttk.Button(setrow, text="Apply", command=set_fn).pack(side="left", padx=8)
            self._beam_widgets[key] = {"cur_x": cx, "cur_y": cy,
                                       "set_x": sx_var, "set_y": sy_var}

        section("Beam shift",           "bs",   self._beam_shift_get, self._beam_shift_set)
        section("Beam tilt",            "bt",   self._beam_tilt_get,  self._beam_tilt_set)
        section("Objective stigmation", "obj",  self._obj_stig_get,   self._obj_stig_set)
        section("Condenser stigmation", "cond", self._cond_stig_get,  self._cond_stig_set)

        state_box = ttk.LabelFrame(f, text="Column state")
        state_box.pack(fill="x", padx=8, pady=4)
        ttk.Label(state_box, text="Beam:").grid(row=0, column=0, sticky="e", padx=4, pady=2)
        ttk.Button(state_box, text="Blank",
                   command=lambda: self._beam_blank(True)).grid(row=0, column=1, padx=4)
        ttk.Button(state_box, text="Unblank",
                   command=lambda: self._beam_blank(False)).grid(row=0, column=2, padx=4)
        ttk.Label(state_box, text="Column valves:").grid(row=1, column=0, sticky="e", padx=4, pady=2)
        ttk.Button(state_box, text="Open",
                   command=lambda: self._valves(True)).grid(row=1, column=1, padx=4)
        ttk.Button(state_box, text="Close",
                   command=lambda: self._valves(False)).grid(row=1, column=2, padx=4)

        ap_box = ttk.LabelFrame(f, text="Aperture (JEOL path; DM fallback unsupported)")
        ap_box.pack(fill="x", padx=8, pady=4)
        ttk.Button(ap_box, text="Get Aperture",
                   command=lambda: self._run("get_aperture", "get_aperture", {})).pack(
            side="left", padx=4, pady=4)
        ttk.Button(ap_box, text="Set Aperture",
                   command=lambda: self._run("set_aperture", "set_aperture", {})).pack(
            side="left", padx=4, pady=4)

    def _beam_vec_get(self, label, cmd, key):
        def _update(r):
            w = self._beam_widgets[key]
            w["cur_x"].set(f"{r['x']: .3e}")
            w["cur_y"].set(f"{r['y']: .3e}")
        self._run(label, cmd, {}, _update)

    def _beam_vec_set(self, label, cmd, key, extra_params=None):
        w = self._beam_widgets[key]
        try:
            x = float(w["set_x"].get())
            y = float(w["set_y"].get())
        except ValueError:
            self._log(f"[{label}] SKIPPED — x/y must be numbers")
            return
        self._run(label, cmd, {"x": x, "y": y, **(extra_params or {})})

    def _beam_shift_get(self):
        self._beam_vec_get("get_beam_shift", "get_beam_shift", "bs")

    def _beam_shift_set(self):
        self._beam_vec_set("set_beam_shift", "set_beam_shift", "bs")

    def _beam_tilt_get(self):
        self._beam_vec_get("get_beam_tilt", "get_beam_tilt", "bt")

    def _beam_tilt_set(self):
        self._beam_vec_set("set_beam_tilt", "set_beam_tilt", "bt")

    def _obj_stig_get(self):
        # get_stigmation returns every stigmator; refresh both readouts.
        def _update(r):
            stigs = r.get("stigmators") or {}
            for key, name in (("obj", "objective"), ("cond", "condenser")):
                w = self._beam_widgets[key]
                w["cur_x"].set(f"{stigs.get(name, {}).get('x', 0): .3e}")
                w["cur_y"].set(f"{stigs.get(name, {}).get('y', 0): .3e}")
        self._run("get_stigmation", "get_stigmation", {}, _update)

    def _obj_stig_set(self):
        self._beam_vec_set("set_stigmation(objective)", "set_stigmation", "obj",
                           extra_params={"stigmator": "objective"})

    def _cond_stig_get(self):
        self._obj_stig_get()

    def _cond_stig_set(self):
        self._beam_vec_set("set_stigmation(condenser)", "set_stigmation", "cond",
                           extra_params={"stigmator": "condenser"})

    def _beam_blank(self, blanked: bool):
        self._run(f"set_beam_blank({'on' if blanked else 'off'})", "set_beam_blank",
                  {"blanked": blanked})

    def _valves(self, opened: bool):
        self._run(f"set_column_valves({'open' if opened else 'close'})", "set_column_valves",
                  {"open": opened})

    # -----------------------------------------------------------------------
    # STEM acquire tab
    # -----------------------------------------------------------------------
    def _build_stem_tab(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="STEM Acquire")

        params_frame = ttk.LabelFrame(f, text="Acquisition parameters")
        params_frame.pack(fill="x", padx=8, pady=8)
        self._stem_width  = _LabelEntry(params_frame, "Width (px)",   "512")
        self._stem_height = _LabelEntry(params_frame, "Height (px)",  "512")
        self._stem_dwell  = _LabelEntry(params_frame, "Dwell (µs)",   "2.0")
        self._stem_rot    = _LabelEntry(params_frame, "Rotation (°)", "0.0")
        for widget in (self._stem_width, self._stem_height, self._stem_dwell, self._stem_rot):
            widget.pack(anchor="w", padx=4, pady=2)

        det_row = ttk.Frame(params_frame)
        det_row.pack(anchor="w", padx=4, pady=2, fill="x")
        ttk.Label(det_row, text="Detector", width=14, anchor="e").pack(side="left")
        self._stem_detector = ttk.Combobox(det_row, width=24, state="readonly", values=[])
        self._stem_detector.pack(side="left", padx=4)
        ttk.Button(det_row, text="Refresh",
                   command=self._stem_refresh_detectors).pack(side="left", padx=4)
        ttk.Label(det_row, text="or index:").pack(side="left", padx=(8, 2))
        self._stem_signal_entry = ttk.Entry(det_row, width=4)
        self._stem_signal_entry.insert(0, "0")
        self._stem_signal_entry.pack(side="left", padx=2)
        ttk.Button(det_row, text="Set Detector",
                   command=self._stem_set_detector).pack(side="left", padx=4)

        mode_frame = ttk.LabelFrame(f, text="Mode")
        mode_frame.pack(fill="x", padx=8, pady=4)
        self._stem_mode = tk.StringVar(value="capture")
        for val, label in [("search", "Search (continuous 1 frame)"),
                           ("preview", "Preview (medium quality)"),
                           ("capture", "Capture (full quality)")]:
            ttk.Radiobutton(mode_frame, text=label, variable=self._stem_mode,
                            value=val).pack(anchor="w", padx=8, pady=2)

        btn_frame = ttk.Frame(f)
        btn_frame.pack(fill="x", padx=8, pady=8)
        ttk.Button(btn_frame, text="Acquire STEM Image",
                   command=self._stem_acquire).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Get Settings",
                   command=lambda: self._run("get_stem_settings", "get_stem_settings", {})).pack(
            side="left", padx=4)

        park_box = ttk.LabelFrame(f, text="Park beam (DigiScan coordinates)")
        park_box.pack(fill="x", padx=8, pady=8)
        cur = ttk.Frame(park_box)
        cur.pack(anchor="w", padx=4, pady=2)
        ttk.Label(cur, text="Current:", width=10, anchor="e").pack(side="left")
        self._beam_pos_x = _Readout(cur, width=12)
        self._beam_pos_x.pack(side="left", padx=2)
        self._beam_pos_y = _Readout(cur, width=12)
        self._beam_pos_y.pack(side="left", padx=2)
        ttk.Button(cur, text="Get Position", command=self._beam_pos_get).pack(side="left", padx=8)
        mv = ttk.Frame(park_box)
        mv.pack(anchor="w", padx=4, pady=2)
        ttk.Label(mv, text="Move to:", width=10, anchor="e").pack(side="left")
        self._beam_pos_set_x = ttk.Entry(mv, width=12)
        self._beam_pos_set_x.insert(0, "0")
        self._beam_pos_set_x.pack(side="left", padx=2)
        self._beam_pos_set_y = ttk.Entry(mv, width=12)
        self._beam_pos_set_y.insert(0, "0")
        self._beam_pos_set_y.pack(side="left", padx=2)
        ttk.Button(mv, text="Move", command=self._beam_pos_move).pack(side="left", padx=8)

    def _stem_signal_index(self) -> int:
        sel = self._stem_detector.get().strip()
        if sel:
            try:
                return int(sel.split(":", 1)[0])
            except ValueError:
                pass
        try:
            return int(self._stem_signal_entry.get())
        except ValueError:
            return 0

    def _stem_acquire(self):
        idx = self._stem_signal_index()
        mode = self._stem_mode.get()
        params = {
            "width":        _ival(self._stem_width),
            "height":       _ival(self._stem_height),
            "dwell_us":     _fval(self._stem_dwell),
            "rotation":     _fval(self._stem_rot),
            "device":       f"detector/{idx}",
            "mode":         mode,
        }
        self._run("stem_acquire", "stem_acquire", params, image=True,
                  name=f"stem_{mode}", tags={"mode": mode, "signal_index": idx})

    def _stem_refresh_detectors(self):
        def _update(r):
            options = [f"{d['index']}: {d['name']}" for d in r.get("detectors", [])]
            self._stem_detector["values"] = options
            if options and not self._stem_detector.get():
                self._stem_detector.set(options[0])
        self._run("get_detectors", "get_detectors", {}, _update)

    def _stem_set_detector(self):
        idx = self._stem_signal_index()
        self._run(f"set_detector(index={idx})", "set_detector", {"device": f"detector/{idx}"})

    def _beam_pos_get(self):
        def _update(r):
            self._beam_pos_x.set(f"{r.get('x', 0.0): .3e}")
            self._beam_pos_y.set(f"{r.get('y', 0.0): .3e}")
        self._run("get_beam_position", "get_beam_position", {}, _update)

    def _beam_pos_move(self):
        try:
            x = float(self._beam_pos_set_x.get())
            y = float(self._beam_pos_set_y.get())
        except ValueError:
            self._log("[move_beam_to] SKIPPED — x/y must be numbers")
            return
        self._run("move_beam_to", "move_beam_to", {"x": x, "y": y})

    # -----------------------------------------------------------------------
    # Continuous scan tab: live_start / live_stop on whichever driver holds
    # the scan slot (it used to send raw DM-Script, which only DM understood)
    # -----------------------------------------------------------------------
    def _build_scan_tab(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="Scan")
        ttk.Label(f, text="Continuous STEM scan", font=("", 9, "bold")).pack(
            padx=8, pady=(12, 4), anchor="w")
        ttk.Label(f, text="Starts / stops a continuous scan (live_start / live_stop).").pack(
            padx=8, anchor="w")

        scan_params = ttk.LabelFrame(f, text="Scan parameters")
        scan_params.pack(fill="x", padx=8, pady=8)
        self._scan_width  = _LabelEntry(scan_params, "Width (px)",   "512")
        self._scan_height = _LabelEntry(scan_params, "Height (px)",  "512")
        self._scan_dwell  = _LabelEntry(scan_params, "Dwell (µs)",   "1.0")
        self._scan_rot    = _LabelEntry(scan_params, "Rotation (°)", "0.0")
        self._scan_signal = _LabelEntry(scan_params, "Signal index", "0")
        for widget in (self._scan_width, self._scan_height, self._scan_dwell,
                       self._scan_rot, self._scan_signal):
            widget.pack(anchor="w", padx=4, pady=2)

        btn_frame = ttk.Frame(f)
        btn_frame.pack(fill="x", padx=8, pady=8)
        ttk.Button(btn_frame, text="▶  Start View (continuous)",
                   command=self._scan_start).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="■  Stop", command=self._scan_stop).pack(side="left", padx=4)

        live_box = ttk.LabelFrame(
            f, text="Live scan over network (streams frames to the monitor GUI)")
        live_box.pack(fill="x", padx=8, pady=8)
        ttk.Label(live_box,
                  text="Uses live_start / live_stop. Frames publish on the PUB stream;\n"
                       "the agent grabs them with get_live_frame.",
                  justify="left").pack(anchor="w", padx=4, pady=2)
        self._live_interval = _LabelEntry(live_box, "Interval (ms)", "100")
        self._live_interval.pack(anchor="w", padx=4, pady=2)
        live_btns = ttk.Frame(live_box)
        live_btns.pack(fill="x", padx=4, pady=4)
        ttk.Button(live_btns, text="▶  Start Live (STEM)",
                   command=self._live_start).pack(side="left", padx=4)
        ttk.Button(live_btns, text="■  Stop Live",
                   command=self._live_stop).pack(side="left", padx=4)
        ttk.Button(live_btns, text="Status",
                   command=lambda: self._run("live_status", "live_status", {})).pack(
            side="left", padx=4)

        view = ttk.LabelFrame(f, text="Live view")
        view.pack(fill="both", expand=True, padx=8, pady=8)
        self._live_canvas = tk.Canvas(view, width=360, height=360, bg="black",
                                      highlightthickness=0)
        self._live_canvas.pack(padx=4, pady=4)
        self._show_no_live_image()

    def _show_no_live_image(self):
        if not hasattr(self, "_live_canvas"):
            return
        self._live_canvas.delete("all")
        self._live_canvas.create_text(180, 180, text="No image", fill="white",
                                      font=("", 16))
        self._live_signature = None

    def _refresh_live_view(self):
        if not hasattr(self, "_live_canvas"):
            self.after(500, self._refresh_live_view)
            return
        live = self._state.data_dir / "monitor" / "live.npy"
        if live.exists() and time.time() - live.stat().st_mtime < 3.0:
            path = live
        else:
            ids = self._state.images.list_ids()
            path = self._state.images.root / f"{ids[-1]}.npy" if ids else None
        if path is None or not path.exists():
            if self._live_signature is not None:
                self._show_no_live_image()
        else:
            signature = (path, path.stat().st_mtime_ns)
            if signature != self._live_signature:
                out = self._state.data_dir / "monitor" / "_manual_live.ppm"
                out.parent.mkdir(parents=True, exist_ok=True)
                render_ppm(path, out, box=360)
                self._live_photo = tk.PhotoImage(file=str(out))
                self._live_canvas.delete("all")
                self._live_canvas.create_image(180, 180, image=self._live_photo)
                self._live_signature = signature
        self.after(500, self._refresh_live_view)

    def _live_start(self):
        params = {
            "detector":     "stem",
            "width":        _ival(self._scan_width),
            "height":       _ival(self._scan_height),
            "dwell_us":     _fval(self._scan_dwell),
            "rotation":     _fval(self._scan_rot),
            "device":       f"detector/{_ival(self._scan_signal)}",
            "interval_ms":  _ival(self._live_interval),
        }
        self._run("live_start (STEM)", "live_start", params)

    def _live_stop(self):
        self._run("live_stop", "live_stop", {})

    def _scan_start(self):
        self._live_start()

    def _scan_stop(self):
        self._live_stop()

    # -----------------------------------------------------------------------
    # TEM camera tab
    # -----------------------------------------------------------------------
    def _build_camera_tab(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="TEM Camera")

        sel_box = ttk.LabelFrame(f, text="Camera selection")
        sel_box.pack(fill="x", padx=8, pady=4)
        row = ttk.Frame(sel_box)
        row.pack(anchor="w", padx=4, pady=4, fill="x")
        ttk.Label(row, text="Camera:", width=10, anchor="e").pack(side="left")
        self._cam_select = ttk.Combobox(row, width=28, state="readonly", values=[])
        self._cam_select.pack(side="left", padx=4)
        ttk.Button(row, text="Refresh", command=self._cam_list).pack(side="left", padx=4)
        ttk.Button(row, text="Info", command=self._cam_info).pack(side="left", padx=4)

        params_box = ttk.LabelFrame(f, text="Acquisition parameters")
        params_box.pack(fill="x", padx=8, pady=4)
        self._cam_exposure = _LabelEntry(params_box, "Exposure (s)", "1.0")
        self._cam_binning  = _LabelEntry(params_box, "Binning",      "1")
        for w in (self._cam_exposure, self._cam_binning):
            w.pack(anchor="w", padx=4, pady=2)
        proc_row = ttk.Frame(params_box)
        proc_row.pack(anchor="w", padx=4, pady=2, fill="x")
        ttk.Label(proc_row, text="Processing", width=14, anchor="e").pack(side="left")
        self._cam_processing = ttk.Combobox(
            proc_row, width=18, state="readonly",
            values=["unprocessed", "dark_subtracted", "gain_normalised"])
        self._cam_processing.set("gain_normalised")
        self._cam_processing.pack(side="left", padx=4)

        roi_box = ttk.LabelFrame(f, text="Optional ROI (top, left, bottom, right; blank = full frame)")
        roi_box.pack(fill="x", padx=8, pady=4)
        roi_row = ttk.Frame(roi_box)
        roi_row.pack(anchor="w", padx=4, pady=4)
        self._cam_area = {}
        for label in ("top", "left", "bottom", "right"):
            ttk.Label(roi_row, text=f"{label}:").pack(side="left", padx=(8, 2))
            e = ttk.Entry(roi_row, width=6)
            e.pack(side="left", padx=2)
            self._cam_area[label] = e

        ttk.Button(f, text="Acquire", command=self._cam_acquire).pack(anchor="w", padx=12, pady=8)

    def _cam_selected_id(self):
        sel = self._cam_select.get().strip()
        if not sel:
            return None
        try:
            return int(sel.split(":", 1)[0])
        except ValueError:
            return None

    def _cam_list(self):
        def _update(r):
            options = [f"{c['id']}: {c['name']}" for c in r.get("cameras", [])]
            self._cam_select["values"] = options
            if options and not self._cam_select.get():
                self._cam_select.set(options[0])
        self._run("list_cameras", "list_cameras", {}, _update)

    def _cam_info(self):
        params = {}
        cam_id = self._cam_selected_id()
        if cam_id is not None:
            params["device"] = f"camera/{cam_id}"
        self._run("get_camera_info", "get_camera_info", params)

    def _cam_acquire(self):
        params = {
            "exposure_s": _fval(self._cam_exposure),
            "binning":    _ival(self._cam_binning),
            "processing": self._cam_processing.get() or "gain_normalised",
        }
        cam_id = self._cam_selected_id()
        if cam_id is not None:
            params["device"] = f"camera/{cam_id}"
        area = []
        for label in ("top", "left", "bottom", "right"):
            raw = self._cam_area[label].get().strip()
            if not raw:
                area = None
                break
            try:
                area.append(int(raw))
            except ValueError:
                self._log(f"[camera_acquire] SKIPPED — ROI '{label}' must be int")
                return
        if area is not None:
            params["area"] = area
        self._run("camera_acquire", "camera_acquire", params, image=True,
                  name="camera_image",
                  tags={"exposure_s": params["exposure_s"], "binning": params["binning"]})

    # -----------------------------------------------------------------------
    # Spectroscopy tab
    # -----------------------------------------------------------------------
    _SI_MODE_FIELDS = {
        "2D Array": [
            ("x_samples",     "X samples",           "64",  "int"),
            ("y_samples",     "Y samples",           "64",  "int"),
            ("sampling",      "Sampling (nm/px)",    "1.0", "float"),
            ("sampling_lock", "Sampling lock",       False, "bool"),
        ],
        "Line Scan": [
            ("samples",          "Samples",            "256", "int"),
            ("sampling",         "Sampling (nm/px)",   "1.0", "float"),
            ("sampling_lock",    "Sampling lock",      False, "bool"),
            ("averaging",        "Averaging",          False, "bool"),
            ("averaging_points", "Averaging points",   "1",   "int"),
            ("averaging_angle",  "Averaging angle (°)", "0.0", "float"),
        ],
        "Multi-Point": [
            ("points", "Points", "1", "int"),
        ],
        "Time Series": [
            ("iterations",       "Iterations",        "10",  "int"),
            ("delay_time_s",     "Delay between (s)", "0.0", "float"),
            ("scan_2d",          "2D scan",           False, "bool"),
            ("scan_2d_x_points", "2D X points",       "8",   "int"),
            ("scan_2d_y_points", "2D Y points",       "8",   "int"),
        ],
    }

    def _build_spectroscopy_tab(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="Spectroscopy")

        common = ttk.LabelFrame(f, text="Common parameters")
        common.pack(fill="x", padx=8, pady=4)
        self._si_pixel_time = _LabelEntry(common, "Pixel time (s)", "0.05")
        self._si_signal     = _LabelEntry(common, "Signal (1-5)",   "1")
        self._si_passes     = _LabelEntry(common, "Passes",         "1")
        for w in (self._si_pixel_time, self._si_signal, self._si_passes):
            w.pack(anchor="w", padx=4, pady=2)
        self._si_drift = tk.BooleanVar(value=False)
        ttk.Checkbutton(common, text="Drift correction",
                        variable=self._si_drift).pack(anchor="w", padx=14, pady=2)

        sub = ttk.Notebook(f)
        sub.pack(fill="both", expand=True, padx=8, pady=4)
        self._si_subnb = sub
        self._si_fields = {}
        for mode, fields in self._SI_MODE_FIELDS.items():
            page = ttk.Frame(sub)
            sub.add(page, text=mode)
            self._si_fields[mode] = self._build_si_mode_page(page, fields)

        btns = ttk.Frame(f)
        btns.pack(fill="x", padx=8, pady=8)
        ttk.Button(btns, text="Configure", command=self._si_configure).pack(side="left", padx=4)
        ttk.Button(btns, text="Start (Capture)", command=self._si_start).pack(side="left", padx=4)
        ttk.Button(btns, text="Stop", command=self._si_stop).pack(side="left", padx=4)
        ttk.Button(btns, text="Get Settings", command=self._si_get_settings).pack(side="left", padx=4)

    def _build_si_mode_page(self, page, fields):
        out = {}
        for row, (kwarg, label, default, kind) in enumerate(fields):
            if kind == "bool":
                var = tk.BooleanVar(value=bool(default))
                ttk.Checkbutton(page, text=label, variable=var).grid(
                    row=row, column=0, columnspan=2, sticky="w", padx=8, pady=2)
                out[kwarg] = (var, kind)
            else:
                ttk.Label(page, text=label).grid(row=row, column=0, sticky="e", padx=4, pady=2)
                ent = ttk.Entry(page, width=12)
                ent.insert(0, str(default))
                ent.grid(row=row, column=1, sticky="w", padx=4, pady=2)
                out[kwarg] = (ent, kind)
        return out

    def _si_active_mode(self) -> str:
        return list(self._SI_MODE_FIELDS.keys())[self._si_subnb.index("current")]

    def _si_collect_params(self) -> dict:
        mode = self._si_active_mode()
        params = {
            "mode":         mode,
            "pixel_time_s": _fval(self._si_pixel_time),
            "signal":       _ival(self._si_signal),
            "passes":       _ival(self._si_passes),
            "drift":        bool(self._si_drift.get()),
        }
        for kwarg, (widget, kind) in self._si_fields[mode].items():
            if kind == "bool":
                params[kwarg] = bool(widget.get())
            elif kind == "int":
                try:
                    params[kwarg] = int(widget.get())
                except ValueError:
                    pass
            elif kind == "float":
                try:
                    params[kwarg] = float(widget.get())
                except ValueError:
                    pass
        return params

    def _si_configure(self):
        params = self._si_collect_params()
        self._run(f"si_configure({params['mode']})", "si_configure", params)

    def _si_start(self):
        self._run("si_start", "si_start", {})

    def _si_stop(self):
        self._run("si_stop", "si_stop", {})

    def _si_get_settings(self):
        self._run("si_get_settings", "si_get_settings", {})

    # -----------------------------------------------------------------------
    # EDS tab
    # -----------------------------------------------------------------------
    def _build_eds_tab(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="EDS")

        ttk.Label(f,
                  text="EDS acquisition requires instrument-specific setup on the DM side.\n"
                       "If unimplemented, these buttons return an error from the server.",
                  foreground="dark orange", justify="left").pack(padx=8, pady=(8, 4), anchor="w")

        point_frame = ttk.LabelFrame(f, text="Point spectrum")
        point_frame.pack(fill="x", padx=8, pady=4)
        self._eds_exp = _LabelEntry(point_frame, "Live-time (s)", "30.0")
        self._eds_exp.pack(anchor="w", padx=4, pady=2)
        self._eds_show = tk.BooleanVar(value=True)
        ttk.Checkbutton(point_frame, text="Show spectrum in DM",
                        variable=self._eds_show).pack(anchor="w", padx=8, pady=2)
        ttk.Button(point_frame, text="Acquire Point Spectrum",
                   command=self._eds_point).pack(anchor="w", padx=8, pady=4)

        map_frame = ttk.LabelFrame(f, text="EDS map")
        map_frame.pack(fill="x", padx=8, pady=4)
        self._eds_map_x   = _LabelEntry(map_frame, "X pixels",       "64")
        self._eds_map_y   = _LabelEntry(map_frame, "Y pixels",       "64")
        self._eds_map_exp = _LabelEntry(map_frame, "Exposure/px (s)", "0.05")
        for w in (self._eds_map_x, self._eds_map_y, self._eds_map_exp):
            w.pack(anchor="w", padx=4, pady=2)
        ttk.Button(map_frame, text="Acquire EDS Map",
                   command=self._eds_map).pack(anchor="w", padx=8, pady=4)

    def _eds_point(self):
        self._run("eds_acquire_point", "eds_acquire_point",
                  {"exposure_s": _fval(self._eds_exp), "show_image": self._eds_show.get()})

    def _eds_map(self):
        self._run("eds_acquire_map", "eds_acquire_map",
                  {"x_pixels": _ival(self._eds_map_x), "y_pixels": _ival(self._eds_map_y),
                   "exposure_s": _fval(self._eds_map_exp)})
