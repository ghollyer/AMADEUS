"""AMADEUS Manual Control GUI — runs inside Digital Micrograph.

Open this file in DM via:
    File > Open Script... > Run

Or from the DM Python console:
    exec(open(r"C:\\path\\to\\AMADEUS\\dm_gui.py").read())

Provides a tabbed tkinter GUI for manually calling:
  - Stage:  get position, move relative, move absolute
  - Focus:  get, change (delta), set absolute
  - STEM:   configure + acquire (search / preview / capture)
  - Scan:   start / stop continuous DigiScan View
  - EDS:    point spectrum + map (requires instrument-specific setup)
  - Script: execute arbitrary DM-Script (escape hatch)
"""

import sys
import os
import gc
import datetime
import traceback
import tkinter as tk
from tkinter import ttk, scrolledtext




# ---------------------------------------------------------------------------
# Ensure the AMADEUS package root is on sys.path so we can import helpers
# ---------------------------------------------------------------------------

# When launched from an embedded Python console, __file__ may be unavailable.
# Set AMADEUS_HOME to the checkout directory if imports cannot be resolved
# from the current working directory.
_AMADEUS_HOME = os.environ.get("AMADEUS_HOME")  # AMADEUS checkout on the VM.
if _AMADEUS_HOME:
    _THIS_DIR = os.path.abspath(_AMADEUS_HOME)
elif "__file__" in globals():
    _THIS_DIR = os.path.dirname(os.path.abspath(__file__))
else:
    _THIS_DIR = os.getcwd()

if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

# ---------------------------------------------------------------------------
# Import DM API and helpers
# ---------------------------------------------------------------------------
try:
    import DigitalMicrograph as DM
    _DM_AVAILABLE = True
except ImportError:
    _DM_AVAILABLE = False

# Import helpers lazily (they also import DM at module level)
def _load_helpers():
    if not _DM_AVAILABLE:
        raise RuntimeError("DigitalMicrograph module not available — run this script inside GMS.")
    from dm_server.helpers import stage        as _stage
    from dm_server.helpers import microscope   as _mic
    from dm_server.helpers import digiscan     as _ds
    from dm_server.helpers import eds          as _eds
    from dm_server.helpers import camera       as _cam
    from dm_server.helpers import spectroscopy as _si
    return _stage, _mic, _ds, _eds, _cam, _si


# ---------------------------------------------------------------------------
# GUI helpers
# ---------------------------------------------------------------------------

def _fval(widget) -> float:
    """Read a float from an Entry widget; returns 0.0 on empty/invalid."""
    try:
        return float(widget.get())
    except ValueError:
        return 0.0


def _ival(widget) -> int:
    try:
        return int(widget.get())
    except ValueError:
        return 0


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
    """A sunken read-only display cell for live instrument values.

    Defaults to an em-dash placeholder so empty readouts are visibly
    distinct from "0". Update via .set("formatted text"); reset via
    .clear(). Callers should still attach to the parent with grid()/pack()
    themselves so the layout decision stays in the tab-builder code.
    """
    _PLACEHOLDER = "—"

    def __init__(self, parent, width: int = 12, anchor: str = "center"):
        super().__init__(parent, text=self._PLACEHOLDER, relief="sunken",
                         width=width, anchor=anchor)

    def set(self, value: str) -> None:
        self.config(text=value)

    def clear(self) -> None:
        self.config(text=self._PLACEHOLDER)


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class AmadeusGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("AMADEUS Manual Control")
        root.resizable(True, True)

        # Open a timestamped log file mirroring everything the GUI prints.
        # Failure here must not block the GUI — fall back to a None handle and
        # surface the reason in the on-screen log.
        self._log_file = None
        self._log_path = None
        self._log_open_err = None
        try:
            log_dir = os.path.join(_THIS_DIR, "logs")
            os.makedirs(log_dir, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            self._log_path = os.path.join(log_dir, f"amadeus_gui_{stamp}.log")
            # Line-buffered so a DM crash still leaves a useful tail on disk.
            self._log_file = open(self._log_path, "a", buffering=1, encoding="utf-8")
        except Exception as exc:
            self._log_open_err = f"{type(exc).__name__}: {exc}"

        # Try to load helpers once; show a warning if DM not available
        try:
            (self._stage, self._mic, self._ds, self._eds,
             self._cam, self._si) = _load_helpers()
            self._helpers_ok = True
        except Exception as exc:
            self._helpers_ok = False
            self._helpers_err = str(exc)

        # Notebook tabs
        nb = ttk.Notebook(root)
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
        # Script tab disabled by default because arbitrary DM-Script execution
        # can change instrument state without validation.
        # self._build_script_tab(nb)

        # Shared log at the bottom
        log_frame = ttk.LabelFrame(root, text="Output / Log")
        log_frame.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.log = scrolledtext.ScrolledText(log_frame, height=8, state="disabled",
                                             font=("Courier", 9))
        self.log.pack(fill="both", expand=True, padx=4, pady=4)
        btn_row = ttk.Frame(log_frame)
        btn_row.pack(anchor="e", padx=4, pady=2)
        ttk.Button(btn_row, text="Clear log", command=self._clear_log).pack(side="left", padx=4)
        ttk.Button(btn_row, text="Shutdown & Unload",
                   command=self.shutdown).pack(side="left", padx=4)

        if self._log_path:
            self._log(f"Logging to {self._log_path}")
        if self._log_open_err:
            self._log(f"WARNING: could not open log file — {self._log_open_err}")

        if not self._helpers_ok:
            self._log(f"WARNING: {self._helpers_err}\n"
                      "All DM buttons will fail until this is resolved.")

    # -----------------------------------------------------------------------
    # Status tab — one-shot read-only snapshot of the microscope state.
    # -----------------------------------------------------------------------
    def _build_status_tab(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="Status")

        ttk.Label(f, text="Microscope:").grid(
            row=0, column=0, sticky="e", padx=6, pady=(8, 4))
        self._st_name = _Readout(f, width=28, anchor="w")
        self._st_name.grid(row=0, column=1, columnspan=3, sticky="w",
                           padx=6, pady=(8, 4))

        # Local helper: add a "label: readout" pair inside a LabelFrame.
        # Scoped to this tab so it doesn't leak as a class-level abstraction.
        def add_row(parent, row, label, width=14):
            ttk.Label(parent, text=label).grid(
                row=row, column=0, sticky="e", padx=4, pady=1)
            r = _Readout(parent, width=width)
            r.grid(row=row, column=1, sticky="w", padx=4, pady=1)
            return r

        # Two columns of grouping frames so the tab stays compact.
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
        ttk.Button(btn_row, text="Refresh All",
                   command=self._status_refresh).pack(side="left", padx=4)
        ttk.Button(btn_row, text="Clear",
                   command=self._status_clear).pack(side="left", padx=4)

        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=1)

    def _status_refresh(self):
        self._run("get_microscope_state",
                  lambda: self._mic.get_microscope_state({}),
                  self._update_status_labels)

    def _update_status_labels(self, r):
        # Beam-shift, tilt, and stigmation values are raw instrument units;
        # display them in scientific notation unless calibrated units exist.
        def vec(v): return f"{v: .3e}"

        self._st_name.set(r.get("microscope_name", "—"))
        self._st_ht.set(f"{r['high_tension_kV']:.1f}")
        self._st_mag.set(f"{r['magnification']:.0f}")
        self._st_mag_idx.set(str(r["mag_index"]))
        self._st_focus.set(f"{r['focus_nm']:.4f}")
        self._st_bs_x.set(vec(r["beam_shift"]["x"]))
        self._st_bs_y.set(vec(r["beam_shift"]["y"]))
        self._st_bt_x.set(vec(r["beam_tilt"]["x"]))
        self._st_bt_y.set(vec(r["beam_tilt"]["y"]))
        self._st_obj_x.set(vec(r["stigmation"]["obj_x"]))
        self._st_obj_y.set(vec(r["stigmation"]["obj_y"]))
        self._st_cond_x.set(vec(r["stigmation"]["cond_x"]))
        self._st_cond_y.set(vec(r["stigmation"]["cond_y"]))
        self._st_blanked.set("BLANKED" if r["beam_blanked"] else "open")
        self._st_valves.set("OPEN" if r["column_valves_open"] else "closed")

    def _status_clear(self):
        for w in (self._st_name, self._st_ht, self._st_mag, self._st_mag_idx,
                  self._st_focus, self._st_bs_x, self._st_bs_y,
                  self._st_bt_x, self._st_bt_y, self._st_obj_x, self._st_obj_y,
                  self._st_cond_x, self._st_cond_y, self._st_blanked,
                  self._st_valves):
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
        for col, (name, unit) in enumerate([("X","µm"), ("Y","µm"), ("Z","µm"),
                                             ("α","°"), ("β","°")]):
            ttk.Label(f, text=f"{name} ({unit})").grid(row=1, column=col, padx=8)
            lbl = _Readout(f, width=12)
            lbl.grid(row=2, column=col, padx=8, pady=2)
            self._pos_labels[name] = lbl

        ttk.Button(f, text="Get Position", command=self._stage_get).grid(
            row=3, column=0, columnspan=2, padx=8, pady=8, sticky="w")

        # Separator
        ttk.Separator(f, orient="horizontal").grid(
            row=4, column=0, columnspan=5, sticky="ew", padx=8, pady=4)

        # Move relative
        ttk.Label(f, text="Move relative (nm / °)", font=("", 9, "bold")).grid(
            row=5, column=0, columnspan=5, sticky="w", padx=8)

        self._rel = {}
        for col, (key, lbl) in enumerate([("x_nm","ΔX (nm)"), ("y_nm","ΔY (nm)"),
                                           ("z_nm","ΔZ (nm)"), ("alpha","Δα (°)"), ("beta","Δβ (°)")]):
            e = _LabelEntry(f, lbl, "0", width=8)
            e.grid(row=6, column=col, padx=4, pady=4)
            self._rel[key] = e

        ttk.Button(f, text="Move Relative", command=self._stage_move_rel).grid(
            row=7, column=0, columnspan=2, padx=8, pady=4, sticky="w")

        # Separator
        ttk.Separator(f, orient="horizontal").grid(
            row=8, column=0, columnspan=5, sticky="ew", padx=8, pady=4)

        # Move absolute
        ttk.Label(f, text="Move absolute (nm / °)", font=("", 9, "bold")).grid(
            row=9, column=0, columnspan=5, sticky="w", padx=8)

        self._abs = {}
        for col, (key, lbl) in enumerate([("x_nm","X (nm)"), ("y_nm","Y (nm)"),
                                           ("z_nm","Z (nm)"), ("alpha","α (°)"), ("beta","β (°)")]):
            e = _LabelEntry(f, lbl, "", width=8)
            e.grid(row=10, column=col, padx=4, pady=4)
            self._abs[key] = e

        ttk.Button(f, text="Move Absolute", command=self._stage_move_abs).grid(
            row=11, column=0, columnspan=2, padx=8, pady=4, sticky="w")

    def _stage_get(self):
        self._run("get_stage_position", lambda: self._stage.get_stage_position({}),
                  self._update_stage_labels)

    def _update_stage_labels(self, result):
        um = 1e6
        self._pos_labels["X"].set(f"{result['x_m']*um:.4f}")
        self._pos_labels["Y"].set(f"{result['y_m']*um:.4f}")
        self._pos_labels["Z"].set(f"{result['z_m']*um:.4f}")
        self._pos_labels["α"].set(f"{result['alpha_deg']:.4f}")
        self._pos_labels["β"].set(f"{result['beta_deg']:.4f}")

    def _stage_move_rel(self):
        params = {
            "mode":      "relative",
            "x_m":       _fval(self._rel["x_nm"])    * 1e-9,
            "y_m":       _fval(self._rel["y_nm"])    * 1e-9,
            "z_m":       _fval(self._rel["z_nm"])    * 1e-9,
            "alpha_deg": _fval(self._rel["alpha"]),
            "beta_deg":  _fval(self._rel["beta"]),
        }
        self._run("move_stage(relative)",
                  lambda: self._stage.move_stage(params),
                  self._update_stage_labels)

    def _stage_move_abs(self):
        params = {"mode": "absolute"}
        mapping = [("x_nm","x_m",1e-9), ("y_nm","y_m",1e-9), ("z_nm","z_m",1e-9),
                   ("alpha","alpha_deg",1.0), ("beta","beta_deg",1.0)]
        for gui_key, param_key, scale in mapping:
            raw = self._abs[gui_key].get().strip()
            if raw:
                try:
                    params[param_key] = float(raw) * scale
                except ValueError:
                    pass
        self._run("move_stage(absolute)",
                  lambda: self._stage.move_stage(params),
                  self._update_stage_labels)

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

        ttk.Button(f, text="Get Focus", command=self._focus_get).grid(
            row=1, column=2, padx=8)

        ttk.Separator(f, orient="horizontal").grid(
            row=2, column=0, columnspan=4, sticky="ew", padx=8, pady=8)

        # Change by delta
        ttk.Label(f, text="Change focus by Δ (nm):").grid(row=3, column=0, sticky="e", padx=8)
        self._focus_delta = ttk.Entry(f, width=12)
        self._focus_delta.insert(0, "0")
        self._focus_delta.grid(row=3, column=1, padx=8, pady=4, sticky="w")
        ttk.Button(f, text="Apply Δ", command=self._focus_change).grid(row=3, column=2, padx=8)

        ttk.Separator(f, orient="horizontal").grid(
            row=4, column=0, columnspan=4, sticky="ew", padx=8, pady=8)

        # Set absolute
        ttk.Label(f, text="Set focus to (nm):").grid(row=5, column=0, sticky="e", padx=8)
        self._focus_target = ttk.Entry(f, width=12)
        self._focus_target.insert(0, "0")
        self._focus_target.grid(row=5, column=1, padx=8, pady=4, sticky="w")
        ttk.Button(f, text="Set Focus", command=self._focus_set).grid(row=5, column=2, padx=8)

        ttk.Separator(f, orient="horizontal").grid(
            row=6, column=0, columnspan=4, sticky="ew", padx=8, pady=8)

        # Magnification
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
        self._run("get_focus",
                  lambda: self._mic.get_focus({}),
                  lambda r: self._focus_lbl.set(f"{r['focus_nm']:.4f}"))

    def _focus_change(self):
        delta_nm = _fval(self._focus_delta)
        self._run("change_focus",
                  lambda: self._mic.change_focus({"delta_nm": delta_nm}),
                  lambda r: self._focus_lbl.set(f"{r['focus_nm']:.4f}"))

    def _focus_set(self):
        target_nm = _fval(self._focus_target)
        self._run("set_focus",
                  lambda: self._mic.set_focus({"focus_nm": target_nm}),
                  None)

    def _mag_get(self):
        def _update(r):
            self._mag_lbl.set(f"{r['magnification']:.0f}")
            self._mag_idx_entry.delete(0, "end")
            self._mag_idx_entry.insert(0, str(r["mag_index"]))
        self._run("get_magnification", lambda: self._mic.get_magnification({}), _update)

    def _mag_set(self):
        idx = _ival(self._mag_idx_entry)
        self._run("set_magnification",
                  lambda: self._mic.set_magnification({"mag_index": idx}),
                  lambda r: self._mag_lbl.set(f"{r['magnification']:.0f}"))

    # -----------------------------------------------------------------------
    # Beam tab — shift, tilt, stigmation, blank/valves, aperture stubs
    # -----------------------------------------------------------------------
    def _build_beam_tab(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="Beam")

        # All four vector sections share the same layout: a "Current" row
        # with two readouts and a Get button, plus a "Set to" row with two
        # entries and an Apply button. Build them via a closure so the four
        # sections stay textually identical.
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

            self._beam_widgets[key] = {
                "cur_x": cx, "cur_y": cy, "set_x": sx_var, "set_y": sy_var,
            }

        section("Beam shift",          "bs",   self._beam_shift_get,   self._beam_shift_set)
        section("Beam tilt",           "bt",   self._beam_tilt_get,    self._beam_tilt_set)
        section("Objective stigmation","obj",  self._obj_stig_get,     self._obj_stig_set)
        section("Condenser stigmation","cond", self._cond_stig_get,    self._cond_stig_set)

        # Direct-action buttons for binary state — explicit Blank/Unblank
        # and Open/Close avoids the "checkbox state may not match instrument"
        # ambiguity entirely. State is observable via the Status tab.
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

        # Aperture stubs surface a NotImplementedError through _run rather
        # than hide the API — easier to discover what's left to wire up.
        ap_box = ttk.LabelFrame(f, text="Aperture (vendor-specific, not yet implemented)")
        ap_box.pack(fill="x", padx=8, pady=4)
        ttk.Button(ap_box, text="Get Aperture",
                   command=self._aperture_get).pack(side="left", padx=4, pady=4)
        ttk.Button(ap_box, text="Set Aperture",
                   command=self._aperture_set).pack(side="left", padx=4, pady=4)

    def _beam_vec_get(self, label, helper_fn, key):
        def _update(r):
            w = self._beam_widgets[key]
            w["cur_x"].set(f"{r['x']: .3e}")
            w["cur_y"].set(f"{r['y']: .3e}")
        self._run(label, lambda: helper_fn({}), _update)

    def _beam_vec_set(self, label, helper_fn, key, extra_params=None):
        w = self._beam_widgets[key]
        try:
            x = float(w["set_x"].get())
            y = float(w["set_y"].get())
        except ValueError:
            self._log(f"[{label}] SKIPPED — x/y must be numbers")
            return
        params = {"x": x, "y": y, **(extra_params or {})}
        self._run(label, lambda: helper_fn(params), None)

    def _beam_shift_get(self):
        self._beam_vec_get("get_beam_shift", self._mic.get_beam_shift, "bs")

    def _beam_shift_set(self):
        self._beam_vec_set("set_beam_shift", self._mic.set_beam_shift, "bs")

    def _beam_tilt_get(self):
        self._beam_vec_get("get_beam_tilt", self._mic.get_beam_tilt, "bt")

    def _beam_tilt_set(self):
        self._beam_vec_set("set_beam_tilt", self._mic.set_beam_tilt, "bt")

    def _obj_stig_get(self):
        # The helper returns obj_x/obj_y/cond_x/cond_y in a single dict; we
        # need to remap to x/y for the readout.
        def _update(r):
            w = self._beam_widgets["obj"]
            w["cur_x"].set(f"{r['obj_x']: .3e}")
            w["cur_y"].set(f"{r['obj_y']: .3e}")
            # Refresh the condenser readouts too while we have the data.
            w2 = self._beam_widgets["cond"]
            w2["cur_x"].set(f"{r['cond_x']: .3e}")
            w2["cur_y"].set(f"{r['cond_y']: .3e}")
        self._run("get_stigmation", lambda: self._mic.get_stigmation({}), _update)

    def _obj_stig_set(self):
        # mode=0 selects objective in the helper's API.
        self._beam_vec_set("set_stigmation(objective)", self._mic.set_stigmation,
                           "obj", extra_params={"mode": 0})

    def _cond_stig_get(self):
        # Same backend as _obj_stig_get — get_stigmation returns both.
        self._obj_stig_get()

    def _cond_stig_set(self):
        # mode=1 selects condenser.
        self._beam_vec_set("set_stigmation(condenser)", self._mic.set_stigmation,
                           "cond", extra_params={"mode": 1})

    def _beam_blank(self, blanked: bool):
        self._run(f"set_beam_blank({'on' if blanked else 'off'})",
                  lambda: self._mic.set_beam_blank({"blanked": blanked}), None)

    def _valves(self, opened: bool):
        self._run(f"set_column_valves({'open' if opened else 'close'})",
                  lambda: self._mic.set_column_valves({"open": opened}), None)

    def _aperture_get(self):
        self._run("get_aperture", lambda: self._mic.get_aperture({}), None)

    def _aperture_set(self):
        self._run("set_aperture", lambda: self._mic.set_aperture({}), None)

    # -----------------------------------------------------------------------
    # STEM acquire tab
    # -----------------------------------------------------------------------
    def _build_stem_tab(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="STEM Acquire")

        params_frame = ttk.LabelFrame(f, text="Acquisition parameters")
        params_frame.pack(fill="x", padx=8, pady=8)

        self._stem_width   = _LabelEntry(params_frame, "Width (px)",    "512")
        self._stem_height  = _LabelEntry(params_frame, "Height (px)",   "512")
        self._stem_dwell   = _LabelEntry(params_frame, "Dwell (µs)",    "2.0")
        self._stem_rot     = _LabelEntry(params_frame, "Rotation (°)",  "0.0")

        for widget in (self._stem_width, self._stem_height, self._stem_dwell,
                       self._stem_rot):
            widget.pack(anchor="w", padx=4, pady=2)

        # Detector selection — populated by get_detectors. The combobox
        # stores "<index>: <name>" strings; we strip the index off at submit
        # time. Empty list means the user hasn't refreshed yet, in which
        # case the manual entry to the right is used as a fallback.
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
        for val, label in [("search","Search (continuous 1 frame)"),
                            ("preview","Preview (medium quality)"),
                            ("capture","Capture (full quality)")]:
            ttk.Radiobutton(mode_frame, text=label, variable=self._stem_mode,
                            value=val).pack(anchor="w", padx=8, pady=2)

        btn_frame = ttk.Frame(f)
        btn_frame.pack(fill="x", padx=8, pady=8)
        ttk.Button(btn_frame, text="Acquire STEM Image",
                   command=self._stem_acquire).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Get Settings",
                   command=self._stem_get_settings).pack(side="left", padx=4)

        # Park-beam subframe — DSSetBeamDSPosition coordinates, not pixels.
        park_box = ttk.LabelFrame(f, text="Park beam (DigiScan coordinates)")
        park_box.pack(fill="x", padx=8, pady=8)

        cur = ttk.Frame(park_box)
        cur.pack(anchor="w", padx=4, pady=2)
        ttk.Label(cur, text="Current:", width=10, anchor="e").pack(side="left")
        self._beam_pos_x = _Readout(cur, width=12)
        self._beam_pos_x.pack(side="left", padx=2)
        self._beam_pos_y = _Readout(cur, width=12)
        self._beam_pos_y.pack(side="left", padx=2)
        ttk.Button(cur, text="Get Position",
                   command=self._beam_pos_get).pack(side="left", padx=8)

        mv = ttk.Frame(park_box)
        mv.pack(anchor="w", padx=4, pady=2)
        ttk.Label(mv, text="Move to:", width=10, anchor="e").pack(side="left")
        self._beam_pos_set_x = ttk.Entry(mv, width=12)
        self._beam_pos_set_x.insert(0, "0")
        self._beam_pos_set_x.pack(side="left", padx=2)
        self._beam_pos_set_y = ttk.Entry(mv, width=12)
        self._beam_pos_set_y.insert(0, "0")
        self._beam_pos_set_y.pack(side="left", padx=2)
        ttk.Button(mv, text="Move",
                   command=self._beam_pos_move).pack(side="left", padx=8)

    def _stem_signal_index(self) -> int:
        """Resolve the selected detector to a signal index.

        Prefers the combobox selection ("N: name") and falls back to the
        manual index entry if the combobox is empty.
        """
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
        params = {
            "width":        _ival(self._stem_width),
            "height":       _ival(self._stem_height),
            "dwell_us":     _fval(self._stem_dwell),
            "rotation":     _fval(self._stem_rot),
            "signal_index": self._stem_signal_index(),
            "mode":         self._stem_mode.get(),
        }
        self._run("stem_acquire", lambda: self._ds.stem_acquire(params), None)

    def _stem_refresh_detectors(self):
        def _update(r):
            options = [f"{d['index']}: {d['name']}" for d in r.get("detectors", [])]
            self._stem_detector["values"] = options
            if options and not self._stem_detector.get():
                self._stem_detector.set(options[0])
        self._run("get_detectors", lambda: self._ds.get_detectors({}), _update)

    def _stem_get_settings(self):
        self._run("get_stem_settings", lambda: self._ds.get_stem_settings({}), None)

    def _stem_set_detector(self):
        idx = self._stem_signal_index()
        self._run(f"set_detector(index={idx})",
                  lambda: self._ds.set_detector({"signal_index": idx}), None)

    def _beam_pos_get(self):
        def _update(r):
            self._beam_pos_x.set(f"{r.get('x', 0.0): .3e}")
            self._beam_pos_y.set(f"{r.get('y', 0.0): .3e}")
        self._run("get_beam_position",
                  lambda: self._ds.get_beam_position({}), _update)

    def _beam_pos_move(self):
        try:
            x = float(self._beam_pos_set_x.get())
            y = float(self._beam_pos_set_y.get())
        except ValueError:
            self._log("[move_beam_to] SKIPPED — x/y must be numbers")
            return
        self._run("move_beam_to",
                  lambda: self._ds.move_beam_to({"x": x, "y": y}), None)

    # -----------------------------------------------------------------------
    # Continuous scan tab
    # -----------------------------------------------------------------------
    def _build_scan_tab(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="Scan")

        ttk.Label(f, text="Continuous DigiScan View/Stop", font=("", 9, "bold")).pack(
            padx=8, pady=(12, 4), anchor="w")
        ttk.Label(f, text="Uses DSStartAcquisition / DSInvokeButton to start/stop the live scan.").pack(
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
        ttk.Button(btn_frame, text="■  Stop",
                   command=self._scan_stop).pack(side="left", padx=4)

    def _scan_start(self):
        """Start continuous DigiScan view via DSStartAcquisition."""
        def _go():
            if not _DM_AVAILABLE:
                raise RuntimeError("DM not available")
            width  = int(self._scan_width.get())
            height = int(self._scan_height.get())
            dwell  = float(self._scan_dwell.get())
            rot    = float(self._scan_rot.get())
            sig    = int(self._scan_signal.get())
            # DSCreateParameters(width, height, rotation, pixelTime_us, lineSynchEnabled)
            script = (
                f"number paramID = DSCreateParameters({width}, {height}, {rot}, {dwell}, 0)\n"
                f"DSSetParametersSignal(paramID, {sig}, 4, 1, 0)\n"
                f"DSStartAcquisition(paramID, 1, 0)"
            )
            DM.ExecuteScriptString(script)
            return {"started": True}
        self._run("scan_start (continuous)", _go, None)

    def _scan_stop(self):
        def _go():
            if not _DM_AVAILABLE:
                raise RuntimeError("DM not available")
            # DSInvokeButton(4) was the old "click the Stop button" path — it
            # returned immediately, leaving DigiScan running for seconds
            # afterwards and breaking any follow-up camera_acquire.  Use the
            # direct stop call and poll until DigiScan reports idle (≤5 s).
            DM.ExecuteScriptString(
                "if ( DSIsAcquisitionActive() ) DSStopAcquisition(-1)\n"
                "number n = 0\n"
                "while ( DSIsAcquisitionActive() && n < 50 )\n"
                "{\n"
                "    Sleep(0.1)\n"
                "    n = n + 1\n"
                "}\n"
            )
            return {"stopped": True}
        self._run("scan_stop", _go, None)

    # -----------------------------------------------------------------------
    # TEM Camera tab — list/info/acquire for the CCD/CMOS camera
    # -----------------------------------------------------------------------
    def _build_camera_tab(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="TEM Camera")

        # Camera selection — Combobox populated by list_cameras. As with
        # the STEM detector picker, the option text is "<id>: <name>" and
        # the id is parsed back out at submit time.
        sel_box = ttk.LabelFrame(f, text="Camera selection")
        sel_box.pack(fill="x", padx=8, pady=4)

        row = ttk.Frame(sel_box)
        row.pack(anchor="w", padx=4, pady=4, fill="x")
        ttk.Label(row, text="Camera:", width=10, anchor="e").pack(side="left")
        self._cam_select = ttk.Combobox(row, width=28, state="readonly", values=[])
        self._cam_select.pack(side="left", padx=4)
        ttk.Button(row, text="Refresh",
                   command=self._cam_list).pack(side="left", padx=4)
        ttk.Button(row, text="Info",
                   command=self._cam_info).pack(side="left", padx=4)

        # Acquisition parameters
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

        # Optional ROI — blank entries mean "full frame".
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

        ttk.Button(f, text="Acquire", command=self._cam_acquire).pack(
            anchor="w", padx=12, pady=8)

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
        self._run("list_cameras", lambda: self._cam.list_cameras({}), _update)

    def _cam_info(self):
        params = {}
        cam_id = self._cam_selected_id()
        if cam_id is not None:
            params["camera_id"] = cam_id
        self._run("get_camera_info",
                  lambda: self._cam.get_camera_info(params), None)

    def _cam_acquire(self):
        params = {
            "exposure_s": _fval(self._cam_exposure),
            "binning":    _ival(self._cam_binning),
            "processing": self._cam_processing.get() or "gain_normalised",
        }
        cam_id = self._cam_selected_id()
        if cam_id is not None:
            params["camera_id"] = cam_id
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
        self._run("camera_acquire",
                  lambda: self._cam.camera_acquire(params), None)

    # -----------------------------------------------------------------------
    # Spectroscopy tab — SI configure/start/stop across all four modes
    # -----------------------------------------------------------------------
    # Per-mode fields mirror dm_server/helpers/spectroscopy.py — keep these
    # in sync if a new mode-specific field is added on the DM side. Each
    # entry is (gui_kwarg, label, default, kind) where kind is "float",
    # "int", or "bool".
    _SI_MODE_FIELDS = {
        "2D Array": [
            ("x_samples",     "X samples",          "64",   "int"),
            ("y_samples",     "Y samples",          "64",   "int"),
            ("sampling",      "Sampling (nm/px)",   "1.0",  "float"),
            ("sampling_lock", "Sampling lock",      False,  "bool"),
        ],
        "Line Scan": [
            ("samples",          "Samples",            "256",  "int"),
            ("sampling",         "Sampling (nm/px)",   "1.0",  "float"),
            ("sampling_lock",    "Sampling lock",      False,  "bool"),
            ("averaging",        "Averaging",          False,  "bool"),
            ("averaging_points", "Averaging points",   "1",    "int"),
            ("averaging_angle",  "Averaging angle (°)", "0.0", "float"),
        ],
        "Multi-Point": [
            ("points", "Points", "1", "int"),
        ],
        "Time Series": [
            ("iterations",       "Iterations",         "10",  "int"),
            ("delay_time_s",     "Delay between (s)",  "0.0", "float"),
            ("scan_2d",          "2D scan",            False, "bool"),
            ("scan_2d_x_points", "2D X points",        "8",   "int"),
            ("scan_2d_y_points", "2D Y points",        "8",   "int"),
        ],
    }

    def _build_spectroscopy_tab(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="Spectroscopy")

        # Common (mode-independent) fields go above the per-mode notebook.
        common = ttk.LabelFrame(f, text="Common parameters")
        common.pack(fill="x", padx=8, pady=4)
        self._si_pixel_time = _LabelEntry(common, "Pixel time (s)",   "0.05")
        self._si_signal     = _LabelEntry(common, "Signal (1-5)",     "1")
        self._si_passes     = _LabelEntry(common, "Passes",           "1")
        for w in (self._si_pixel_time, self._si_signal, self._si_passes):
            w.pack(anchor="w", padx=4, pady=2)
        self._si_drift = tk.BooleanVar(value=False)
        ttk.Checkbutton(common, text="Drift correction",
                        variable=self._si_drift).pack(anchor="w", padx=14, pady=2)

        # One sub-tab per SI mode — the active sub-tab IS the selected mode,
        # which avoids show/hide juggling and keeps mode-specific layouts
        # self-contained.
        sub = ttk.Notebook(f)
        sub.pack(fill="both", expand=True, padx=8, pady=4)
        self._si_subnb = sub
        self._si_fields = {}  # mode -> {kwarg: (widget, kind)}
        for mode, fields in self._SI_MODE_FIELDS.items():
            page = ttk.Frame(sub)
            sub.add(page, text=mode)
            self._si_fields[mode] = self._build_si_mode_page(page, fields)

        # Action buttons.
        btns = ttk.Frame(f)
        btns.pack(fill="x", padx=8, pady=8)
        ttk.Button(btns, text="Configure",
                   command=self._si_configure).pack(side="left", padx=4)
        ttk.Button(btns, text="Start (Capture)",
                   command=self._si_start).pack(side="left", padx=4)
        ttk.Button(btns, text="Stop",
                   command=self._si_stop).pack(side="left", padx=4)
        ttk.Button(btns, text="Get Settings",
                   command=self._si_get_settings).pack(side="left", padx=4)

    def _build_si_mode_page(self, page, fields):
        """Build the input widgets for one SI mode. Returns {kwarg: (widget, kind)}."""
        out = {}
        for row, (kwarg, label, default, kind) in enumerate(fields):
            if kind == "bool":
                var = tk.BooleanVar(value=bool(default))
                ttk.Checkbutton(page, text=label, variable=var).grid(
                    row=row, column=0, columnspan=2, sticky="w", padx=8, pady=2)
                out[kwarg] = (var, kind)
            else:
                ttk.Label(page, text=label).grid(
                    row=row, column=0, sticky="e", padx=4, pady=2)
                ent = ttk.Entry(page, width=12)
                ent.insert(0, str(default))
                ent.grid(row=row, column=1, sticky="w", padx=4, pady=2)
                out[kwarg] = (ent, kind)
        return out

    def _si_active_mode(self) -> str:
        idx = self._si_subnb.index("current")
        return list(self._SI_MODE_FIELDS.keys())[idx]

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
        self._run(f"si_configure({params['mode']})",
                  lambda: self._si.si_configure(params), None)

    def _si_start(self):
        self._run("si_start", lambda: self._si.si_start({}), None)

    def _si_stop(self):
        self._run("si_stop", lambda: self._si.si_stop({}), None)

    def _si_get_settings(self):
        self._run("si_get_settings",
                  lambda: self._si.si_get_settings({}), None)

    # -----------------------------------------------------------------------
    # EDS tab
    # -----------------------------------------------------------------------
    def _build_eds_tab(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="EDS")

        ttk.Label(f,
                  text="EDS acquisition requires instrument-specific setup.\n"
                       "Edit dm_server/helpers/eds.py and uncomment the\n"
                       "block matching your EDS system before using these buttons.",
                  foreground="dark orange", justify="left").pack(padx=8, pady=(8, 4), anchor="w")

        # Point spectrum
        point_frame = ttk.LabelFrame(f, text="Point spectrum")
        point_frame.pack(fill="x", padx=8, pady=4)

        self._eds_exp   = _LabelEntry(point_frame, "Live-time (s)", "30.0")
        self._eds_exp.pack(anchor="w", padx=4, pady=2)
        self._eds_show  = tk.BooleanVar(value=True)
        ttk.Checkbutton(point_frame, text="Show spectrum in DM", variable=self._eds_show).pack(
            anchor="w", padx=8, pady=2)
        ttk.Button(point_frame, text="Acquire Point Spectrum",
                   command=self._eds_point).pack(anchor="w", padx=8, pady=4)

        # Map
        map_frame = ttk.LabelFrame(f, text="EDS map")
        map_frame.pack(fill="x", padx=8, pady=4)

        self._eds_map_x   = _LabelEntry(map_frame, "X pixels",      "64")
        self._eds_map_y   = _LabelEntry(map_frame, "Y pixels",      "64")
        self._eds_map_exp = _LabelEntry(map_frame, "Exposure/px (s)","0.05")
        for w in (self._eds_map_x, self._eds_map_y, self._eds_map_exp):
            w.pack(anchor="w", padx=4, pady=2)
        ttk.Button(map_frame, text="Acquire EDS Map",
                   command=self._eds_map).pack(anchor="w", padx=8, pady=4)

    def _eds_point(self):
        params = {
            "exposure_s":  _fval(self._eds_exp),
            "show_image":  self._eds_show.get(),
        }
        self._run("eds_acquire_point",
                  lambda: self._eds.eds_acquire_point(params), None)

    def _eds_map(self):
        params = {
            "x_pixels":   _ival(self._eds_map_x),
            "y_pixels":   _ival(self._eds_map_y),
            "exposure_s": _fval(self._eds_map_exp),
        }
        self._run("eds_acquire_map",
                  lambda: self._eds.eds_acquire_map(params), None)

    # -----------------------------------------------------------------------
    # Script tab
    # -----------------------------------------------------------------------
    def _build_script_tab(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="Script")

        ttk.Label(f, text="Execute DM-Script (escape hatch)",
                  font=("", 9, "bold")).pack(padx=8, pady=(8, 2), anchor="w")
        ttk.Label(f, text="Enter any DM-Script code and click Run.").pack(
            padx=8, anchor="w")

        self._script_text = scrolledtext.ScrolledText(f, height=16, font=("Courier", 10))
        self._script_text.pack(fill="both", expand=True, padx=8, pady=6)

        # Pre-populate with a helpful example
        self._script_text.insert("1.0", """\
// Example: read focus and print to results window
number f
EMUpdateCalibrationState()
EMGetFocus(f)
Result("Focus = " + f + "\\n")
""")

        btn_frame = ttk.Frame(f)
        btn_frame.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(btn_frame, text="Run Script", command=self._run_script).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Clear", command=lambda: self._script_text.delete("1.0", "end")).pack(
            side="left", padx=4)

    def _run_script(self):
        script = self._script_text.get("1.0", "end-1c")
        if not script.strip():
            self._log("Script tab: nothing to run.")
            return
        self._run("execute_script",
                  lambda: self._mic.execute_script({"script": script}), None)

    # -----------------------------------------------------------------------
    # Shared run / log helpers
    # -----------------------------------------------------------------------
    def _run(self, label: str, fn, on_success):
        """Call *fn()* in the main thread, log the result."""
        if not self._helpers_ok:
            self._log(f"[{label}] SKIPPED — helpers not loaded: {self._helpers_err}")
            return
        self._log(f"[{label}] ...")
        try:
            result = fn()
            self._log(f"[{label}] OK  {result}")
            if on_success and result is not None:
                # For image returns (tuple), pass the dict part
                if isinstance(result, tuple):
                    on_success(result[0])
                else:
                    on_success(result)
        except NotImplementedError as exc:
            self._log(f"[{label}] NOT IMPLEMENTED — {exc}\n"
                      "Edit the stub in dm_server/helpers/ to implement this.")
        except Exception:
            self._log(f"[{label}] ERROR:\n{traceback.format_exc()}")

    def _log(self, msg: str) -> None:
        line = msg.rstrip()
        self.log.config(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.config(state="disabled")
        # Mirror to disk with a wall-clock timestamp on each line. Clearing
        # the on-screen log does NOT truncate the file — the file is the
        # authoritative record for the session.
        if self._log_file is not None:
            try:
                ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
                self._log_file.write(f"{ts} {line}\n")
            except Exception:
                # Disk full / handle closed — don't take the GUI down with us.
                pass

    def _clear_log(self) -> None:
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")
        if self._log_file is not None:
            try:
                self._log_file.write("---- on-screen log cleared ----\n")
            except Exception:
                pass

    def shutdown(self) -> None:
        """Destroy the GUI window. Helper modules are unloaded by ``launch``
        after ``mainloop`` returns, regardless of how the window was closed."""
        try:
            self._log("Shutting down GUI and unloading dm_server.* helpers...")
        except tk.TclError:
            pass  # widget already gone
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None
        self.root.destroy()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _unload_helpers() -> list[str]:
    """Drop every ``dm_server.*`` entry from ``sys.modules``.

    DM has no way to restart its embedded Python interpreter without restarting
    the whole application. Removing the cached helper modules means the next
    ``exec`` of this script reimports them from source — so edits to helper
    code take effect without restarting DM.
    """
    removed = [
        name for name in list(sys.modules)
        if name == "dm_server" or name.startswith("dm_server.")
    ]
    for name in removed:
        del sys.modules[name]
    return removed


def launch():
    """Create and run the AMADEUS GUI.

    Note: DM keeps the embedded Python interpreter alive across script execs,
    and Tcl/Tk cannot be cleanly re-initialised within the same interpreter —
    a second ``tk.Tk()`` from a different DM worker thread trips Tcl's
    thread-affinity checks and re-trips the stdout flush bug.  We detect a
    leftover Tk root and refuse to relaunch with a clear message rather than
    crash GMS.
    """
    if tk._default_root is not None:
        print("[dm_gui] A Tk root from a previous session is still alive in "
              "this Python interpreter. Tk cannot be safely re-initialised "
              "inside DM — restart Digital Micrograph before launching the "
              "GUI again.")
        return

    # DM replaces sys.stdout/stderr with custom writers that are not valid Tcl
    # file channels.  Tcl calls `flush stdout` at many points — Tk init, error
    # reporting, the bell, certain widget updates, mainloop teardown — and any
    # one of those raises "flush: channel not recognized" inside GMS, which can
    # crash DM.  Keep stdout/stderr pointed at a real file descriptor for the
    # entire lifetime of the Tk app, not just the `tk.Tk()` call.
    _save_out, _save_err = sys.stdout, sys.stderr
    _null = open(os.devnull, 'w')
    sys.stdout = sys.stderr = _null
    try:
        root = tk.Tk()
        app = AmadeusGUI(root)
        # Route the window manager's close button through the same shutdown
        # path as the explicit "Shutdown & Unload" button.
        root.protocol("WM_DELETE_WINDOW", app.shutdown)
        root.mainloop()
    finally:
        sys.stdout = _save_out
        sys.stderr = _save_err
        _null.close()
        # Best-effort cleanup of Tk module state.  This does not actually let
        # us relaunch in the same interpreter (Tcl thread state persists at
        # the C level), but it keeps the Python-level invariants tidy so the
        # _default_root guard above behaves predictably.
        try:
            tk._default_root = None
        except Exception:
            pass
        gc.collect()

    removed = _unload_helpers()
    print(f"[dm_gui] Shutdown complete. Unloaded {len(removed)} dm_server.* module(s). "
          "Restart Digital Micrograph before running dm_gui again.")


# When run directly or via exec() in GMS:
if __name__ == "__main__":
    launch()
else:
    # Also launch when exec()'d from the DM Python console
    launch()
