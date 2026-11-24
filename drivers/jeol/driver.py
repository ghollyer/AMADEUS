"""JEOL driver: the column and stage slots through PyJEM TEM3.

Online PyJEM (``from PyJEM import TEM3``) is a compiled Windows .pyd that must
run in the vjem310 conda env on the microscope PC. ``--offline`` swaps in
``PyJEM.offline.TEM3``, JEOL's pure-Python simulator with the identical API.

Values are honest device units: lens/deflector/stigmator state is the raw
16-bit DAC (0-65535); the stage is nm/degrees on the JEOL side and metres/
degrees on the wire; HT is volts. Focus additionally reports nanometres when
the instrument calibration provides a measured DAC<->nm slope
(see ``drivers/calibration.py``). JEOL's defaults (CLA1/CLA2 deflectors,
OLf/OLc lenses, a nanometre stage) are layered under the config's
``instrument`` section by ``JEOL_CALIBRATION``.
"""

import time

from drivers.base import ColumnDriver, StageDriver
from drivers.calibration import InstrumentCalibration
from drivers.jeol.stage_motion import InlineMotion, ProcessMotion
from drivers.values import (as_index, as_int_in_range, device_value, exactly_one,
                            jsonable, name_from, stigmation_reply)

DAC_MIN, DAC_MAX = 0, 65535

APERTURE_KINDS = {0: "none", 1: "CLA", 2: "OLA", 3: "HCA", 4: "SAA", 5: "ENTA", 6: "EDS"}
_APERTURE_BY_NAME = {name.lower(): kind for kind, name in APERTURE_KINDS.items()}

# Stigmator name -> Def3 channel.
STIGMATORS = {"objective": "OLs", "condenser": "CLs", "intermediate": "ILs"}

# Function modes by TEM/STEM mode, in EOS3.SelectFunctionMode index order.
FUNCTION_MODES = {
    "TEM": ["MAG", "MAG2", "LowMAG", "SAMAG", "DIFF"],
    "STEM": ["Align", "SM-LMAG", "SM-MAG", "AMAG", "uuDIFF", "Rocking"],
}

DAC_RANGE = [0, 65535]
DAC_NEUTRAL = 32768

# JEOL's defaults under the config's instrument section.
JEOL_CALIBRATION = {
    "label": "uncalibrated JEOL instrument",
    # Stage3.GetPos() is documented as nm (tilts in degrees). Verify with
    # python -m drivers.smoke against the console before trusting moves.
    "stage_scale_to_m": 1e-9,
    # JEOL convention: CLA1 = beam shift, CLA2 = beam tilt. Confirm on the
    # instrument and swap in instrument.deflectors if needed.
    "deflectors": {"beam_shift": "CLA1", "beam_tilt": "CLA2"},
    "lenses": {"focus.fine": "OLf", "focus.coarse": "OLc"},
    "focus": {"by_mode": {
        "TEM": {"lens": "focus.fine", "dac_per_nm": None, "dac_at_zero": None},
        "STEM": {"lens": "focus.fine", "dac_per_nm": None, "dac_at_zero": None},
    }},
}

_SPOT_RANGE = (0, 7)

_STAGE_AXES = ("x", "y", "z", "alpha", "beta")
# wire key -> (absolute setter, relative setter)
_SETTERS = {
    "x_m": ("SetX", "SetXRel"),
    "y_m": ("SetY", "SetYRel"),
    "z_m": ("SetZ", "SetZRel"),
    "alpha_deg": ("SetTiltXAngle", "SetTXRel"),
    "beta_deg": ("SetTiltYAngle", "SetTYRel"),
}
_LENGTH_AXES = ("x_m", "y_m", "z_m")
_DRIVES = {"motor": 0, "piezo": 1}            # Stage3.SelDrvMode
_DRIVE_NAMES = {v: k for k, v in _DRIVES.items()}
_SPEEDS = {"slow": 0, "normal": 1, "fast": 2}  # Stage3.SetSpeedMode
_SPEED_NAMES = {v: k for k, v in _SPEEDS.items()}


def _as_dac(value, label: str) -> int:
    """An int DAC value, rejecting out-of-range writes. PyJEM setters silently
    ignore non-int arguments, which would turn a typo into a no-op on the real
    column, so validate here instead."""
    return as_int_in_range(value, label, DAC_RANGE[0], DAC_RANGE[1])


def dac_value(value: int, **extra) -> dict:
    """A DAC value in the neutral form (value, unit, range)."""
    return device_value(int(value), "dac", DAC_RANGE, **extra)


def function_mode_index(value, mode: str, table: dict) -> int:
    """A function mode name (any case) -> its EOS3 index for *mode*."""
    names = table.get(mode) or []
    return names.index(name_from(value, names, f"{mode} function mode"))


class JeolDriver(ColumnDriver, StageDriver):
    kind = "jeol"
    extras = {"tem3_call": "tem3_call"}

    def __init__(self, offline: bool = False,
                 calibration: "InstrumentCalibration | None" = None,
                 motion_module: "str | None" = None):
        self.offline = offline
        self.cal = (calibration or InstrumentCalibration()).with_defaults(JEOL_CALIBRATION)
        # Stage drive executor, created on the first move (see _get_motion).
        self._motion = None
        self._motion_module = motion_module    # tests: a fake TEM3 module
        self._move = None                       # the move a stage_poll is following
        if offline:
            from PyJEM.offline import TEM3
        else:
            from PyJEM import TEM3
        self._tem3 = TEM3
        self.stage = TEM3.Stage3()
        self.eos = TEM3.EOS3()
        self.lens = TEM3.Lens3()
        self.deflector = TEM3.Def3()
        self.apt = TEM3.Apt3()
        # Last aperture kind this adapter selected; used when Apt3.GetKind()
        # is unavailable (seen failing on the F200, 2026-10-26).
        self._selected_aperture_kind: "int | None" = None
        self.ht = TEM3.HT3()
        self.feg = TEM3.FEG3()
        self.detector = TEM3.Detector3()
        if not offline and self.cal.stage_motion(offline) == "process":
            # Start the motion process now (relay startup), not on the first
            # move: importing online TEM3 in the child takes seconds.
            self._get_motion()

    def describe(self) -> dict:
        return {"backend": self.kind, "kind": self.kind, "offline": self.offline,
                "instrument": self.cal.label,
                "stage_motion": self.cal.stage_motion(self.offline)}

    def capabilities(self, params: "dict | None" = None) -> dict:
        caps = super().capabilities(params)
        caps["features"] = ["piezo", "stage_speed", "backlash_approach", "alpha",
                            "stigmator.intermediate"]
        caps["apertures"] = [name for kind, name in sorted(APERTURE_KINDS.items()) if kind]
        caps["instrument"] = self.cal.label
        caps["offline"] = self.offline
        caps["function_modes"] = {m: list(v) for m, v in FUNCTION_MODES.items()}
        caps["stigmators"] = list(STIGMATORS)
        caps["lenses"] = {role: {"vendor_name": name, "unit": "dac", "range": list(DAC_RANGE)}
                          for role, name in self.cal.lens_roles().items()}
        caps["deflectors"] = {role: {"vendor_name": self.cal.deflector_for(role),
                                     "unit": "dac", "range": list(DAC_RANGE),
                                     "neutral": DAC_NEUTRAL}
                              for role in ("beam_shift", "beam_tilt")}
        caps["spot_size_range"] = list(_SPOT_RANGE)
        caps["magnification_by_value"] = sorted(self.cal.mag_tables())
        caps["unreliable_reads"] = {
            "get_aperture": "Apt3.GetKind() fails on some instruments (the F200); "
                            "pass 'kind' to read a given aperture"}
        return caps

    def vendor_call(self, params: dict) -> dict:
        """``{"method": "Stage3.GetPos", "args": [...]}``: one public TEM3
        method (the generic form of ``jeol.tem3_call``)."""
        method = str(params.get("method") or "")
        cls_name, _, name = method.partition(".")
        if not name:
            raise ValueError("vendor_call method must be '<TEM3 class>.<method>', "
                             f"e.g. 'Stage3.GetPos'; got {method!r}")
        return self.tem3_call({"class_name": cls_name, "method": name,
                               "args": params.get("args") or []})

    # ------------------------------------------------------------------
    # Mode helpers
    # ------------------------------------------------------------------
    def _mode(self) -> str:
        """"TEM" or "STEM" (JEOL calls STEM "ASID")."""
        return "STEM" if self.eos.GetTemStemMode() == 1 else "TEM"

    def _function_mode(self):
        fm = self.eos.GetFunctionMode()
        if isinstance(fm, (list, tuple)) and len(fm) >= 2:
            return int(fm[0]), str(fm[1])
        return None, None

    def _operation_mode(self) -> str:
        """Coarse operation mode, the same strings the DM fallback reports:
        IMAGING (TEM), SCANNING (STEM), DIFFRACTION (either, in a DIFF mode)."""
        mode = self._mode()
        _, fm_name = self._function_mode()
        if fm_name and "DIFF" in fm_name.upper():
            return "DIFFRACTION"
        return "SCANNING" if mode == "STEM" else "IMAGING"

    # ------------------------------------------------------------------
    # Focus — OL fine/coarse DAC, optional nm calibration
    # ------------------------------------------------------------------
    # Lens roles (focus.fine / focus.coarse) map to Lens3 channels through the
    # calibration's "lenses" table (default OLf / OLc).
    def _read_lens(self, vendor_name: str) -> int:
        return int(getattr(self.lens, f"Get{vendor_name}")())

    def _write_lens(self, vendor_name: str, dac: int) -> None:
        getattr(self.lens, f"Set{vendor_name}")(dac)

    def _focus_lens_dac(self, mode: str) -> int:
        return self._read_lens(self.cal.focus_lens_vendor(mode))

    def _set_focus_lens(self, mode: str, dac: int) -> None:
        self._write_lens(self.cal.focus_lens_vendor(mode), dac)

    def get_focus(self, params: dict) -> dict:
        mode = self._mode()
        roles = self.cal.lens_roles()
        values = {name: self._read_lens(name) for name in sorted(set(roles.values()))}
        focus_role = self.cal.focus_lens_role(mode)
        focus_dac = values[roles[focus_role]]
        focus_nm = self.cal.dac_to_nm(focus_dac, mode)
        return {
            "focus_dac": focus_dac,
            "unit": "dac",
            "range": list(DAC_RANGE),
            "lens": focus_role,
            "focus_nm": focus_nm,
            "calibrated": focus_nm is not None,
            "lenses": {role: dac_value(values[name], vendor_name=name)
                       for role, name in roles.items()},
            "vendor": dict(values),
            "mode": mode,
            "operation_mode": self._operation_mode(),
        }

    def set_focus(self, params: dict) -> dict:
        """Absolute focus. Exactly one of: ``focus_nm`` (needs calibration),
        ``focus_dac`` (the lens that carries focus in this mode), or ``lens``
        (a role such as ``"focus.fine"``) with ``value``."""
        key = exactly_one(params, ("focus_nm", "focus_dac", "lens"), "set_focus")
        mode = self._mode()
        if key == "lens":
            if params.get("value") is None:
                raise ValueError("set_focus: 'lens' needs a 'value'")
            self._write_lens(self.cal.lens_vendor(params["lens"]),
                             _as_dac(params["value"], f"{params['lens']} value"))
        elif key == "focus_dac":
            self._set_focus_lens(mode, _as_dac(params["focus_dac"], "focus_dac"))
        else:
            dac = self.cal.nm_to_dac(float(params["focus_nm"]), mode)
            self._set_focus_lens(mode, _as_dac(dac, "focus dac"))
        return self.get_focus({})

    def change_focus(self, params: dict) -> dict:
        """Relative focus change: ``delta_dac`` (always available) or
        ``delta_nm`` (needs calibration)."""
        key = exactly_one(params, ("delta_nm", "delta_dac"), "change_focus")
        mode = self._mode()
        if key == "delta_dac":
            try:
                delta = int(round(float(params["delta_dac"])))
            except (TypeError, ValueError):
                raise ValueError(f"delta_dac must be a number, got {params['delta_dac']!r}")
        else:
            delta = self.cal.delta_nm_to_dac(float(params["delta_nm"]), mode)
        target = self._focus_lens_dac(mode) + delta
        self._set_focus_lens(mode, _as_dac(target, "focus dac"))
        return self.get_focus({})

    # ------------------------------------------------------------------
    # Magnification
    # ------------------------------------------------------------------
    def get_magnification(self, params: dict) -> dict:
        mag_index = int(self.eos.GetCurrentMagSelectorID())
        magnification = None
        mag_unit = mag_label = None
        try:
            value = self.eos.GetMagValue()
            if isinstance(value, (list, tuple)) and len(value) >= 3:
                magnification = float(value[0])
                mag_unit, mag_label = str(value[1]), str(value[2])
        except Exception:
            # PyJEM's offline mag table can fail to load off-instrument;
            # the index is still authoritative.
            pass
        return {
            "magnification": magnification,
            "mag_index": mag_index,
            "mag_unit": mag_unit,
            "mag_label": mag_label,
        }

    def set_magnification(self, params: dict) -> dict:
        """Select a mag-table index, verified by read-back.

        TEM3 has no call for the size of the mag table (it differs per
        function mode), and ``EOS3.SetSelector`` accepts any int — offline it
        stores 999 happily. So: reject negatives up front, then read back;
        if the instrument didn't take the index, or the mag value stops
        resolving where it resolved before (index past the end of the
        table), restore the previous index and raise.
        """
        key = exactly_one(params, ("magnification", "mag_index"), "set_magnification")
        if key == "magnification":
            idx = self._mag_index_for_value(float(params["magnification"]))
        else:
            idx = as_index(params["mag_index"], "mag_index")
        before = self.get_magnification({})
        self.eos.SetSelector(idx)
        after = self.get_magnification({})
        bad = None
        if after["mag_index"] != idx:
            bad = f"the instrument reports index {after['mag_index']} instead"
        elif before["magnification"] is not None and after["magnification"] is None:
            bad = "it is not in this mode's magnification table"
        if bad:
            self.eos.SetSelector(int(before["mag_index"]))
            raise ValueError(f"mag_index {idx} rejected: {bad}; restored index "
                             f"{before['mag_index']}")
        return after

    def _mag_index_for_value(self, value: float) -> int:
        """Index of *value* in this mode's magnification table (from the
        calibration's ``magnifications``; TEM3 has no call that lists it)."""
        _, fm_name = self._function_mode()
        key, table = self.cal.mag_table(self._mode(), fm_name)
        if not table:
            raise ValueError(
                f"no magnification table for {self._mode()}/{fm_name} in the instrument "
                f"calibration, so a magnification can't be set by value here; use "
                f"mag_index, or add instrument.magnifications[{self._mode()!r}]"
                f"[{fm_name!r}] (see drivers/calibration.py)")
        best = min(range(len(table)), key=lambda i: abs(table[i] - value))
        if abs(table[best] - value) > 0.02 * value:
            raise ValueError(f"magnification {value:g} is not in the {key} table; nearest "
                             f"is {table[best]:g} (index {best})")
        return best

    # ------------------------------------------------------------------
    # Deflectors — beam shift / tilt (roles configurable per instrument)
    # ------------------------------------------------------------------
    def _get_deflector(self, role: str) -> dict:
        name = self.cal.deflector_for(role)
        x, y = getattr(self.deflector, f"Get{name}")()
        return {"x": int(x), "y": int(y), "unit": "dac", "range": list(DAC_RANGE),
                "neutral": DAC_NEUTRAL, "vendor": {"deflector": name}}

    def _set_deflector(self, role: str, params: dict) -> dict:
        name = self.cal.deflector_for(role)
        x = _as_dac(params["x"], f"{role}.x")
        y = _as_dac(params["y"], f"{role}.y")
        getattr(self.deflector, f"Set{name}")(x, y)
        return {"x": x, "y": y, "unit": "dac", "range": list(DAC_RANGE),
                "neutral": DAC_NEUTRAL, "vendor": {"deflector": name}}

    def get_beam_shift(self, params: dict) -> dict:
        return self._get_deflector("beam_shift")

    def set_beam_shift(self, params: dict) -> dict:
        return self._set_deflector("beam_shift", params)

    def get_beam_tilt(self, params: dict) -> dict:
        return self._get_deflector("beam_tilt")

    def set_beam_tilt(self, params: dict) -> dict:
        return self._set_deflector("beam_tilt", params)

    # ------------------------------------------------------------------
    # Stigmators
    # ------------------------------------------------------------------
    def get_stigmation(self, params: dict) -> dict:
        obj = self.deflector.GetOLs()
        cond = self.deflector.GetCLs()
        il = self.deflector.GetILs()
        return stigmation_reply({"objective": obj, "condenser": cond, "intermediate": il},
                                "dac", DAC_RANGE, DAC_NEUTRAL)

    def set_stigmation(self, params: dict) -> dict:
        label = name_from(params.get("stigmator"), list(STIGMATORS), "stigmator")
        stig = STIGMATORS[label]
        x = _as_dac(params["x"], f"{label} stig x")
        y = _as_dac(params["y"], f"{label} stig y")
        getattr(self.deflector, f"Set{stig}")(x, y)
        return {"stigmator": label, "x": x, "y": y, "unit": "dac",
                "range": list(DAC_RANGE), "neutral": DAC_NEUTRAL,
                "vendor": {"deflector": stig}}

    # ------------------------------------------------------------------
    # Spot size / alpha
    # ------------------------------------------------------------------
    def get_spot_size(self, params: dict) -> dict:
        return {"spot_size": int(self.eos.GetSpotSize())}

    def set_spot_size(self, params: dict) -> dict:
        spot = int(params["spot_size"])
        if not (_SPOT_RANGE[0] <= spot <= _SPOT_RANGE[1]):
            raise ValueError(f"spot_size must be in {_SPOT_RANGE}, got {spot}")
        self.eos.SelectSpotSize(spot)
        return self.get_spot_size({})

    def get_alpha(self, params: dict) -> dict:
        alpha = self.eos.GetAlphaSelectorEx()
        if isinstance(alpha, (list, tuple)) and len(alpha) >= 2:
            return {"alpha": int(alpha[0]), "alpha_name": str(alpha[1])}
        return {"alpha": int(self.eos.GetAlpha()), "alpha_name": None}

    def set_alpha(self, params: dict) -> dict:
        self.eos.SetAlphaSelector(int(params["alpha"]))
        return self.get_alpha({})

    # ------------------------------------------------------------------
    # Column state
    # ------------------------------------------------------------------
    def set_beam_blank(self, params: dict) -> dict:
        self.deflector.SetBeamBlank(1 if params.get("blanked") else 0)
        return {"blanked": bool(self.deflector.GetBeamBlank())}

    def set_column_valves(self, params: dict) -> dict:
        # On a JEOL FEG the "column valve" is the gun beam valve (V1).
        self.feg.SetBeamValve(1 if params.get("open") else 0)
        return {"open": bool(self.feg.GetBeamValve())}

    def get_aperture(self, params: dict) -> dict:
        if "kind" in params:
            self._select_aperture(self._aperture_kind(params["kind"]))
        kind, kind_source = self._current_aperture_kind()
        position = self.apt.GetPosition()
        return {
            "kind": kind,
            "kind_name": APERTURE_KINDS.get(kind, str(kind)),
            "kind_source": kind_source,
            "size": int(self.apt.GetSize(kind)),
            "position": {"x": int(position[0]), "y": int(position[1])},
        }

    def _select_aperture(self, kind: int) -> None:
        self.apt.SelectKind(kind)
        self._selected_aperture_kind = kind

    def _current_aperture_kind(self):
        """``(kind, source)``. Prefers ``Apt3.GetKind()``; if the instrument
        doesn't support it, falls back to the kind this adapter last selected
        (source ``"last_selected"``). With neither, it raises rather than guess."""
        try:
            return int(self.apt.GetKind()), "instrument"
        except Exception as exc:
            if self._selected_aperture_kind is None:
                raise RuntimeError(
                    f"Apt3.GetKind() failed ({exc}); pass 'kind' to select an "
                    "aperture explicitly") from exc
            return self._selected_aperture_kind, "last_selected"

    def set_aperture(self, params: dict) -> dict:
        """Select an aperture and optionally set its hole and position.

        params: kind (int or name, e.g. 2 / "OLA"), size (0=open, 1-4=hole),
                x, y (position DACs 0-4095).
        """
        kind = self._aperture_kind(params["kind"])
        self._select_aperture(kind)
        if params.get("size") is not None:
            self.apt.SetSize(int(params["size"]))
        if params.get("x") is not None and params.get("y") is not None:
            self.apt.SetPosition(int(params["x"]), int(params["y"]))
        return self.get_aperture({})

    @staticmethod
    def _aperture_kind(kind) -> int:
        if isinstance(kind, str):
            try:
                return _APERTURE_BY_NAME[kind.strip().lower()]
            except KeyError:
                raise ValueError(
                    f"Unknown aperture {kind!r}; expected one of "
                    f"{sorted(_APERTURE_BY_NAME)} or an index 0-6")
        kind = int(kind)
        if kind not in APERTURE_KINDS:
            raise ValueError(f"Aperture kind index must be 0-6, got {kind}")
        return kind

    # ------------------------------------------------------------------
    # Modes
    # ------------------------------------------------------------------
    def get_instrument_mode(self, params: dict) -> dict:
        fm_index, fm_name = self._function_mode()
        return {
            "mode": self._mode(),
            "function_mode_index": fm_index,
            "function_mode": fm_name,
            "operation_mode": self._operation_mode(),
        }

    def set_instrument_mode(self, params: dict) -> dict:
        """``mode`` "TEM" or "STEM" (JEOL calls STEM "ASID"). A large state change."""
        mode = name_from(params.get("mode"), ["TEM", "STEM"], "instrument mode")
        self.eos.SelectTemStem(1 if mode == "STEM" else 0)
        return self.get_instrument_mode({})

    def get_function_mode(self, params: dict) -> dict:
        fm_index, fm_name = self._function_mode()
        return {"function_mode_index": fm_index, "function_mode": fm_name,
                "mode": self._mode()}

    def set_function_mode(self, params: dict) -> dict:
        """``mode`` by name (``"DIFF"``; capabilities list them per TEM/STEM)."""
        self.eos.SelectFunctionMode(
            function_mode_index(params["mode"], self._mode(), FUNCTION_MODES))
        return self.get_function_mode({})

    def get_high_tension(self, params: dict) -> dict:
        ht_v = float(self.ht.GetHtValue())
        return {
            "ht_v": ht_v,
            "high_tension_kV": ht_v / 1000.0,
            "range_v": list(self.ht.GetHtRange()),
        }

    # ------------------------------------------------------------------
    # Stage — metres/degrees on the wire, nm/degrees on the JEOL side
    # ------------------------------------------------------------------
    def _read_position(self) -> dict:
        pos = self.stage.GetPos()
        scale = self.cal.stage_scale_to_m
        return {
            "x_m": pos[0] * scale,
            "y_m": pos[1] * scale,
            "z_m": pos[2] * scale,
            "alpha_deg": float(pos[3]),
            "beta_deg": float(pos[4]),
        }

    def get_stage_position(self, params: dict) -> dict:
        return self._read_position()

    # Stage drive calls go through self._motion (drivers/jeol/stage_motion.py):
    # a child process online, so a setter that blocks until the stage settles
    # (and holds the GIL meanwhile) can't freeze the relay. A
    # waiting move_stage then returns a "_continue" marker, and the relay polls
    # stage_poll on this worker until the move settles. Between polls the
    # worker stays free for other commands, including stop_stage.

    def _get_motion(self):
        if self._motion is None:
            if self.cal.stage_motion(self.offline) == "process":
                module = self._motion_module or (
                    "PyJEM.offline.TEM3" if self.offline else "PyJEM.TEM3")
                self._motion = ProcessMotion(module)
            else:
                self._motion = InlineMotion(self.stage)
        return self._motion

    def _stage_extras(self) -> dict:
        """Drive mode, speed modes and piezo position, where PyJEM has them."""
        out = {}
        readers = (
            ("drive", lambda: _DRIVE_NAMES.get(int(self.stage.GetDrvMode()))),
            ("speed", lambda: {name: _SPEED_NAMES.get(int(v)) for name, v in
                               zip(("xy", "z", "tilt"),
                                   self.stage.GetSpeedMode(int(self.stage.GetDrvMode())))}),
            ("piezo_nm", lambda: [float(v) for v in list(self.stage.GetPiezoPosi())[:2]]),
        )
        for key, read in readers:
            try:
                out[key] = read()
            except Exception:
                pass
        return out

    def get_stage_status(self, params: dict) -> dict:
        self._finish_move_if_done()
        status = list(self.stage.GetStatus())
        axes = dict(zip(_STAGE_AXES, status))
        motion = self._motion
        busy = motion.busy() if motion is not None else None
        return {
            "moving": any(s == 1 for s in status) or busy is not None,
            "limit_error": any(s == 2 for s in status),
            "axes": axes,
            "motion": {"kind": motion.kind if motion else self.cal.stage_motion(self.offline),
                       "busy": busy},
            **self._stage_extras(),
        }

    def stop_stage(self, params: dict) -> dict:
        self.stage.Stop()
        return self.get_stage_status({})

    def move_stage(self, params: dict) -> dict:
        """Absolute or relative stage move; same param scheme as the DM helper.

        params:
          mode: "absolute" | "relative"
          x_m/y_m/z_m (metres), alpha_deg/beta_deg (degrees): any subset
          wait (default True), timeout_s (default 20): how long to wait for rest
          speed: "slow" | "normal" | "fast" — for this move only (restored after)
          drive: "motor" (default) | "piezo" — piezo is the fine X/Y drive
                 (+-piezo_range_nm, default 10 um); restored to motor after
          approach: bool — backlash-compensated X/Y approach from a fixed
                 direction (default: calibration stage.backlash.enabled)
        """
        mode = params.get("mode", "absolute")
        if mode not in ("absolute", "relative"):
            raise ValueError(f"move_stage: mode must be absolute or relative, got {mode!r}")
        relative = mode == "relative"
        provided = [k for k in _SETTERS if k in params and params[k] is not None]
        if not provided:
            raise ValueError("move_stage: no axes specified in params")
        drive = params.get("drive") or "motor"
        if drive not in _DRIVES:
            raise ValueError(f"move_stage: drive must be motor or piezo, got {drive!r}")
        speed = params.get("speed")
        if speed is not None and speed not in _SPEEDS:
            raise ValueError(f"move_stage: speed must be slow, normal or fast, got {speed!r}")
        stage_cal = self.cal.stage
        approach = params.get("approach")
        if approach is None:
            approach = bool(stage_cal["backlash"].get("enabled"))
        timeout_s = float(params.get("timeout_s", 20.0))
        wait = bool(params.get("wait", True))

        scale = self.cal.stage_scale_to_m
        values = {}
        for key in provided:
            try:
                value = float(params[key])
            except (TypeError, ValueError):
                raise ValueError(f"move_stage: {key} must be a number, got {params[key]!r}")
            values[key] = value / scale if key in _LENGTH_AXES else value   # -> nm

        self._finish_move_if_done()
        restore = []
        if drive == "piezo":
            if set(provided) - {"x_m", "y_m"}:
                raise ValueError("move_stage: the piezo drive moves X/Y only")
            approach = False
            self._check_piezo_range(values, relative, float(stage_cal["piezo_range_nm"]))
        calls = self._drive_calls(values, relative, approach and drive == "motor", stage_cal)

        try:
            restore = self._apply_drive_settings(drive, speed)
            description = f"{mode} {drive} move " + ", ".join(
                f"{k}={float(params[k]):g}" for k in provided)
            motion = self._get_motion()
            motion.submit(calls, description)
        except Exception:
            self._restore(restore)
            raise

        if motion.kind == "inline":
            settled = self._wait_for_stage(timeout_s) if wait else True
            self._restore(restore)
            result = self._read_position()
            result["settled"] = settled
            return result

        self._move = {"description": description, "restore": restore,
                      "started": time.monotonic(),
                      "deadline": time.monotonic() + timeout_s}
        if not wait:
            return {**self._read_position(), "settled": False, "moving": True,
                    "job": description}
        return {"_continue": {"cmd": "stage_poll", "params": {}, "interval_s": 0.1},
                "job": description}

    def stage_poll(self, params: dict) -> dict:
        """Continuation of a waiting move_stage (driven by the relay)."""
        move = self._move
        if move is None:
            return {**self._read_position(),
                    "settled": not any(s == 1 for s in self.stage.GetStatus())}
        elapsed = round(time.monotonic() - move["started"], 1)
        motion = self._get_motion()
        status = list(self.stage.GetStatus())
        if motion.done() and not any(s == 1 for s in status):
            self._finish_move()
            result = self._read_position()
            result.update(settled=True, elapsed_s=elapsed)
            if any(s == 2 for s in status):
                result["limit_error"] = True
            return result
        if params.get("finalize") or time.monotonic() > move["deadline"]:
            return {**self._read_position(), "settled": False, "moving": True,
                    "job": move["description"], "elapsed_s": elapsed,
                    "note": "The move is still running. get_stage_status reports progress "
                            "and stop_stage aborts it. Any speed or drive change made for "
                            "it is restored when it finishes."}
        return {"_continue": {"cmd": "stage_poll", "params": {}, "interval_s": 0.1}}

    # -- move helpers ------------------------------------------------------
    def _drive_calls(self, values: dict, relative: bool, approach: bool,
                     stage_cal: dict) -> list:
        """Vendor setter calls for one move, in order."""
        calls = []
        xy = [k for k in ("x_m", "y_m") if k in values]
        for key, value in values.items():
            if approach and key in xy:
                continue
            absolute, rel = _SETTERS[key]
            calls.append((rel if relative else absolute, (value,)))
        if approach and xy:
            bl = stage_cal["backlash"]
            over = float(bl.get("overshoot_nm", 0.0)) * 1e-9 / self.cal.stage_scale_to_m
            direction = list(bl.get("approach") or [1, 1])
            pos = self.stage.GetPos()
            final = []
            for key in xy:
                idx = 0 if key == "x_m" else 1
                target = pos[idx] + values[key] if relative else values[key]
                sign = 1.0 if float(direction[idx]) >= 0 else -1.0
                calls.append((_SETTERS[key][0], (target - sign * over,)))
                final.append((_SETTERS[key][0], (target,)))
            calls.extend(final)
        return calls

    def _check_piezo_range(self, values: dict, relative: bool, limit_nm: float) -> None:
        limit = limit_nm * 1e-9 / self.cal.stage_scale_to_m
        current = [0.0, 0.0]
        if relative:
            try:
                current = [float(v) for v in list(self.stage.GetPiezoPosi())[:2]]
            except Exception as exc:
                raise RuntimeError(f"cannot read the piezo position for a relative "
                                   f"piezo move: {exc}")
        for key, idx in (("x_m", 0), ("y_m", 1)):
            if key in values:
                target = current[idx] + values[key] if relative else values[key]
                if abs(target) > limit:
                    raise ValueError(
                        f"move_stage: piezo {key[0]} target {target:.0f} nm is outside "
                        f"+-{limit:.0f} nm. Use the motor drive for coarse moves.")

    def _apply_drive_settings(self, drive: str, speed: "str | None") -> list:
        """Switch drive/speed for one move; return the calls that undo it."""
        restore = []
        try:
            if drive == "piezo":
                previous = int(self.stage.GetDrvMode())
                if previous != _DRIVES["piezo"]:
                    self.stage.SelDrvMode(_DRIVES["piezo"])
                    restore.append(("SelDrvMode", (previous,)))
            if speed is not None:
                kind = _DRIVES[drive]
                previous = [int(v) for v in self.stage.GetSpeedMode(kind)]
                wanted = [_SPEEDS[speed]] * 3
                if previous != wanted:
                    self.stage.SetSpeedMode(kind, *wanted)
                    restore.append(("SetSpeedMode", (kind, *previous)))
        except AttributeError as exc:
            self._restore(restore)
            raise RuntimeError(f"this PyJEM has no stage speed/drive control "
                               f"({exc}); PyJEM >= 1.3 provides SetSpeedMode") from exc
        except Exception:
            self._restore(restore)
            raise
        return restore

    def _restore(self, restore: list) -> "list[str]":
        failed = []
        for method, args in reversed(restore):
            try:
                getattr(self.stage, method)(*args)
            except Exception as exc:
                failed.append(f"{method}{args}: {exc}")
        return failed

    def _finish_move(self) -> None:
        move, self._move = self._move, None
        failed = self._restore(move["restore"]) if move else []
        err = self._motion.take_error() if self._motion is not None else None
        if err:
            raise RuntimeError(f"stage move failed ({move['description'] if move else ''}): {err}")
        if failed:
            raise RuntimeError("stage move finished but restoring the speed/drive "
                               "setting failed: " + "; ".join(failed))

    def _finish_move_if_done(self) -> None:
        if self._move is not None and self._motion is not None and self._motion.done() \
                and not any(s == 1 for s in self.stage.GetStatus()):
            self._finish_move()

    def _wait_for_stage(self, timeout_s: float) -> bool:
        """Poll GetStatus until every axis rests (JEOL's EMWaitUntilReady)."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not any(self.stage.GetStatus()):
                return True
            time.sleep(0.05)
        return False

    def close(self) -> None:
        if self._motion is not None:
            self._motion.close()
            self._motion = None

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------
    def get_microscope_state(self, params: dict) -> dict:
        """Best-effort composite snapshot; individual read failures land in
        ``errors`` instead of failing the whole call."""
        state: dict = {"microscope_name": self.cal.label, "unit": "dac"}
        errors: dict = {}

        def grab(key, fn):
            try:
                state[key] = fn()
            except Exception as exc:
                errors[key] = f"{type(exc).__name__}: {exc}"

        grab("high_tension_kV", lambda: float(self.ht.GetHtValue()) / 1000.0)
        grab("magnification", lambda: self.get_magnification({})["magnification"])
        grab("mag_index", lambda: int(self.eos.GetCurrentMagSelectorID()))
        grab("spot_size", lambda: int(self.eos.GetSpotSize()))
        grab("alpha", lambda: int(self.eos.GetAlpha()))
        grab("beam_blanked", lambda: bool(self.deflector.GetBeamBlank()))
        grab("column_valves_open", lambda: bool(self.feg.GetBeamValve()))
        grab("beam_shift", lambda: {k: self._get_deflector("beam_shift")[k] for k in ("x", "y")})
        grab("beam_tilt", lambda: {k: self._get_deflector("beam_tilt")[k] for k in ("x", "y")})
        grab("stigmation", lambda: self.get_stigmation({})["stigmators"])
        grab("tem_stem_mode", self._mode)
        grab("function_mode", lambda: self._function_mode()[1])
        grab("operation_mode", self._operation_mode)
        grab("stage", self._read_position)

        def focus():
            f = self.get_focus({})
            state["focus_dac"] = f["focus_dac"]
            state["calibrated"] = f["calibrated"]
            return f["focus_nm"]
        grab("focus_nm", focus)

        if errors:
            state["errors"] = errors
        return state

    # ------------------------------------------------------------------
    # Escape hatch — call any public TEM3 method by name (no arbitrary code)
    # ------------------------------------------------------------------
    def tem3_call(self, params: dict) -> dict:
        instances = {
            "Stage3": self.stage,
            "EOS3": self.eos,
            "Lens3": self.lens,
            "Def3": self.deflector,
            "Apt3": self.apt,
            "HT3": self.ht,
            "FEG3": self.feg,
            "Detector3": self.detector,
        }
        cls_name = params.get("class_name") or params.get("class")
        method_name = params.get("method", "")
        if cls_name not in instances:
            raise ValueError(f"class_name must be one of {sorted(instances)}, got {cls_name!r}")
        if not method_name or method_name.startswith("_"):
            raise ValueError(f"Invalid method name {method_name!r}")
        instance = instances[cls_name]
        method = getattr(instance, method_name, None)
        if not callable(method):
            raise ValueError(f"{cls_name} has no method {method_name!r}")
        args = params.get("args") or []
        result = method(*args)
        return {"class_name": cls_name, "method": method_name,
                "args": args, "result": jsonable(result)}
