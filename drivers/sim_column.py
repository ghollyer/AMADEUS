"""SimColumnDriver: a dependency-free simulated column and stage.

The column/stage half of the AMADEUS simulator (``drivers/sim.py`` adds the
acquisition slots). It needs no vendor software, so the whole stack runs on a
development machine, and it is the second implementation that keeps the
driver interfaces honest. Its device values are 16-bit DACs, like JEOL's, but
it shares no code with the JEOL driver.
"""

import time

from drivers.base import ColumnDriver, StageDriver
from drivers.calibration import InstrumentCalibration
from drivers.values import (as_index, as_int_in_range, device_value, exactly_one,
                            name_from, stigmation_reply)

DAC_RANGE = [0, 65535]
DAC_NEUTRAL = 32768
_MAG_TABLE = [100.0 * (2 ** i) for i in range(16)]   # 100x .. ~3.3Mx
_FUNCTION_MODES = ["MAG", "MAG2", "LowMAG", "SAMAG", "DIFF"]
_STIGMATORS = ("objective", "condenser", "intermediate")
_APERTURES = {0: "none", 1: "CLA", 2: "OLA", 3: "HCA", 4: "SAA", 5: "ENTA", 6: "EDS"}
_SPOT_RANGE = (0, 7)

SIM_CALIBRATION = {
    "label": "AMADEUS simulator",
    "lenses": {"focus.fine": "focus.fine", "focus.coarse": "focus.coarse"},
    "deflectors": {"beam_shift": "beam_shift", "beam_tilt": "beam_tilt"},
}


def _dac(value, label: str) -> int:
    return as_int_in_range(value, label, DAC_RANGE[0], DAC_RANGE[1])


class SimColumnDriver(ColumnDriver, StageDriver):
    kind = "sim"

    def __init__(self, calibration=None):
        self.cal = (calibration or InstrumentCalibration()).with_defaults(SIM_CALIBRATION)
        self._lenses = {"focus.fine": 32768, "focus.coarse": 8000}
        self._mag_index = 5
        self._deflectors = {"beam_shift": [32768, 32768], "beam_tilt": [32768, 32768]}
        self._stig = {name: [32768, 32768] for name in _STIGMATORS}
        self._spot = 1
        self._alpha = 2
        self._blanked = False
        self._valves_open = True
        self._aperture = {"kind": 1, "sizes": {k: 0 for k in _APERTURES},
                          "position": [2048, 2048]}
        self._mode = 0            # 0=TEM 1=STEM
        self._function_mode = 0
        self._ht_v = 200000.0
        self._stage = {"x_m": 0.0, "y_m": 0.0, "z_m": 0.0,
                       "alpha_deg": 0.0, "beta_deg": 0.0}
        # Simulated travel time. 0 = instantaneous (default). >0 makes moves
        # run "in the background" and finish through stage_poll, the way the
        # JEOL driver's motion process does.
        self.move_time_s = 0.0
        self._moving_until = 0.0
        self._move_deadline = 0.0
        self._piezo = [0.0, 0.0]

    def describe(self) -> dict:
        return {"backend": self.kind, "kind": self.kind, "instrument": "AMADEUS simulator"}

    def capabilities(self, params: "dict | None" = None) -> dict:
        caps = super().capabilities(params)
        caps["features"] = ["piezo", "stage_speed", "alpha", "stigmator.intermediate"]
        caps["apertures"] = [name for kind, name in sorted(_APERTURES.items()) if kind]
        caps["instrument"] = "AMADEUS simulator"
        caps["function_modes"] = {"TEM": list(_FUNCTION_MODES), "STEM": list(_FUNCTION_MODES)}
        caps["stigmators"] = list(_STIGMATORS)
        caps["lenses"] = {role: {"vendor_name": role, "unit": "dac", "range": list(DAC_RANGE)}
                          for role in self._lenses}
        caps["deflectors"] = {role: {"vendor_name": role, "unit": "dac",
                                     "range": list(DAC_RANGE), "neutral": DAC_NEUTRAL}
                              for role in self._deflectors}
        caps["spot_size_range"] = list(_SPOT_RANGE)
        caps["magnification_by_value"] = ["TEM/*", "STEM/*"]
        return caps

    # -- focus ---------------------------------------------------------
    def _mode_name(self) -> str:
        return "STEM" if self._mode else "TEM"

    def get_focus(self, params: dict) -> dict:
        mode = self._mode_name()
        role = self.cal.focus_lens_role(mode)
        focus_dac = self._lenses[role]
        focus_nm = self.cal.dac_to_nm(focus_dac, mode)
        return {"focus_dac": focus_dac, "unit": "dac", "range": list(DAC_RANGE),
                "lens": role, "focus_nm": focus_nm, "calibrated": focus_nm is not None,
                "lenses": {r: device_value(v, "dac", DAC_RANGE, vendor_name=r)
                           for r, v in self._lenses.items()},
                "vendor": dict(self._lenses),
                "mode": mode, "operation_mode": self._operation_mode()}

    def set_focus(self, params: dict) -> dict:
        key = exactly_one(params, ("focus_nm", "focus_dac", "lens"), "set_focus")
        mode = self._mode_name()
        if key == "lens":
            role = params["lens"]
            if role not in self._lenses:
                raise ValueError(f"unknown lens role {role!r}; this instrument has "
                                 f"{', '.join(self._lenses)}")
            if params.get("value") is None:
                raise ValueError("set_focus: 'lens' needs a 'value'")
            self._lenses[role] = _dac(params["value"], f"{role} value")
        elif key == "focus_dac":
            self._lenses[self.cal.focus_lens_role(mode)] = _dac(params["focus_dac"],
                                                                "focus_dac")
        else:
            self._lenses[self.cal.focus_lens_role(mode)] = _dac(
                self.cal.nm_to_dac(float(params["focus_nm"]), mode), "focus dac")
        return self.get_focus({})

    def change_focus(self, params: dict) -> dict:
        key = exactly_one(params, ("delta_nm", "delta_dac"), "change_focus")
        mode = self._mode_name()
        if key == "delta_dac":
            delta = int(params["delta_dac"])
        else:
            delta = self.cal.delta_nm_to_dac(float(params["delta_nm"]), mode)
        role = self.cal.focus_lens_role(mode)
        self._lenses[role] = _dac(self._lenses[role] + delta, "focus dac")
        return self.get_focus({})

    # -- optics --------------------------------------------------------
    def get_magnification(self, params: dict) -> dict:
        return {"magnification": _MAG_TABLE[self._mag_index],
                "mag_index": self._mag_index, "mag_unit": "x",
                "mag_label": f"x{_MAG_TABLE[self._mag_index]:.0f}"}

    def set_magnification(self, params: dict) -> dict:
        key = exactly_one(params, ("magnification", "mag_index"), "set_magnification")
        if key == "magnification":
            value = float(params["magnification"])
            idx = min(range(len(_MAG_TABLE)), key=lambda i: abs(_MAG_TABLE[i] - value))
            if abs(_MAG_TABLE[idx] - value) > 0.02 * value:
                raise ValueError(f"magnification {value:g} is not in the table; nearest "
                                 f"is {_MAG_TABLE[idx]:g} (index {idx})")
        else:
            idx = as_index(params["mag_index"], "mag_index")
        if not (0 <= idx < len(_MAG_TABLE)):
            raise ValueError(f"mag_index must be 0-{len(_MAG_TABLE) - 1}, got {idx}")
        self._mag_index = idx
        return self.get_magnification({})

    def _vec(self, role: str) -> dict:
        x, y = self._deflectors[role]
        return {"x": x, "y": y, "unit": "dac", "range": list(DAC_RANGE),
                "neutral": DAC_NEUTRAL, "vendor": {"deflector": role}}

    def get_beam_shift(self, params: dict) -> dict:
        return self._vec("beam_shift")

    def set_beam_shift(self, params: dict) -> dict:
        self._deflectors["beam_shift"] = [_dac(params["x"], "x"), _dac(params["y"], "y")]
        return self._vec("beam_shift")

    def get_beam_tilt(self, params: dict) -> dict:
        return self._vec("beam_tilt")

    def set_beam_tilt(self, params: dict) -> dict:
        self._deflectors["beam_tilt"] = [_dac(params["x"], "x"), _dac(params["y"], "y")]
        return self._vec("beam_tilt")

    def get_stigmation(self, params: dict) -> dict:
        return stigmation_reply(self._stig, "dac", DAC_RANGE, DAC_NEUTRAL)

    def set_stigmation(self, params: dict) -> dict:
        name = name_from(params.get("stigmator"), _STIGMATORS, "stigmator")
        self._stig[name] = [_dac(params["x"], "x"), _dac(params["y"], "y")]
        return {"stigmator": name, "x": self._stig[name][0], "y": self._stig[name][1],
                "unit": "dac", "range": list(DAC_RANGE), "neutral": DAC_NEUTRAL}

    def get_spot_size(self, params: dict) -> dict:
        return {"spot_size": self._spot}

    def set_spot_size(self, params: dict) -> dict:
        spot = int(params["spot_size"])
        if not (_SPOT_RANGE[0] <= spot <= _SPOT_RANGE[1]):
            raise ValueError(f"spot_size must be {_SPOT_RANGE[0]}-{_SPOT_RANGE[1]}, got {spot}")
        self._spot = spot
        return self.get_spot_size({})

    def get_alpha(self, params: dict) -> dict:
        return {"alpha": self._alpha, "alpha_name": str(self._alpha)}

    def set_alpha(self, params: dict) -> dict:
        self._alpha = int(params["alpha"])
        return self.get_alpha({})

    # -- column state --------------------------------------------------
    def set_beam_blank(self, params: dict) -> dict:
        self._blanked = bool(params.get("blanked"))
        return {"blanked": self._blanked}

    def set_column_valves(self, params: dict) -> dict:
        self._valves_open = bool(params.get("open"))
        return {"open": self._valves_open}

    def _aperture_kind(self, kind) -> int:
        if isinstance(kind, str):
            for k, name in _APERTURES.items():
                if name.lower() == kind.strip().lower():
                    return k
            raise ValueError(f"unknown aperture {kind!r}")
        kind = int(kind)
        if kind not in _APERTURES:
            raise ValueError(f"Aperture kind index must be 0-6, got {kind}")
        return kind

    def get_aperture(self, params: dict) -> dict:
        if params.get("kind") is not None:
            self._aperture["kind"] = self._aperture_kind(params["kind"])
        kind = self._aperture["kind"]
        return {"kind": kind, "kind_name": _APERTURES.get(kind, str(kind)),
                "size": self._aperture["sizes"].get(kind, 0),
                "position": {"x": self._aperture["position"][0],
                             "y": self._aperture["position"][1]}}

    def set_aperture(self, params: dict) -> dict:
        kind = self._aperture_kind(params["kind"])
        self._aperture["kind"] = kind
        if params.get("size") is not None:
            self._aperture["sizes"][kind] = int(params["size"])
        if params.get("x") is not None and params.get("y") is not None:
            self._aperture["position"] = [int(params["x"]), int(params["y"])]
        return self.get_aperture({})

    def _operation_mode(self) -> str:
        if _FUNCTION_MODES[self._function_mode] == "DIFF":
            return "DIFFRACTION"
        return "SCANNING" if self._mode else "IMAGING"

    def get_instrument_mode(self, params: dict) -> dict:
        return {"mode": self._mode_name(),
                "function_mode_index": self._function_mode,
                "function_mode": _FUNCTION_MODES[self._function_mode],
                "operation_mode": self._operation_mode()}

    def set_instrument_mode(self, params: dict) -> dict:
        mode = name_from(params.get("mode"), ["TEM", "STEM"], "instrument mode")
        self._mode = 1 if mode == "STEM" else 0
        return self.get_instrument_mode({})

    def get_function_mode(self, params: dict) -> dict:
        return {"function_mode_index": self._function_mode,
                "function_mode": _FUNCTION_MODES[self._function_mode],
                "mode": self._mode_name()}

    def set_function_mode(self, params: dict) -> dict:
        name = name_from(params.get("mode"), _FUNCTION_MODES,
                         f"{self._mode_name()} function mode")
        self._function_mode = _FUNCTION_MODES.index(name)
        return self.get_function_mode({})

    def get_high_tension(self, params: dict) -> dict:
        return {"ht_v": self._ht_v, "high_tension_kV": self._ht_v / 1000.0,
                "range_v": [200000.0, 10.0]}

    # -- stage ---------------------------------------------------------
    def get_stage_position(self, params: dict) -> dict:
        return dict(self._stage)

    def _moving(self) -> bool:
        return time.monotonic() < self._moving_until

    def get_stage_status(self, params: dict) -> dict:
        moving = self._moving()
        return {"moving": moving, "limit_error": False,
                "axes": {k: int(moving and k in ("x", "y"))
                         for k in ("x", "y", "z", "alpha", "beta")},
                "motion": {"kind": "sim", "busy": None},
                "drive": "motor", "piezo_nm": list(self._piezo)}

    def stop_stage(self, params: dict) -> dict:
        self._moving_until = 0.0
        return self.get_stage_status({})

    def move_stage(self, params: dict) -> dict:
        provided = [k for k in self._stage if k in params and params[k] is not None]
        if not provided:
            raise ValueError("move_stage: no axes specified in params")
        if params.get("speed") not in (None, "slow", "normal", "fast"):
            raise ValueError(f"move_stage: bad speed {params['speed']!r}")
        drive = params.get("drive") or "motor"
        if drive not in ("motor", "piezo"):
            raise ValueError(f"move_stage: bad drive {drive!r}")
        if drive == "piezo" and set(provided) - {"x_m", "y_m"}:
            raise ValueError("move_stage: the piezo drive moves X/Y only")
        if self._moving():
            raise RuntimeError("the stage is still moving; wait or call stop_stage first")
        relative = params.get("mode", "absolute") == "relative"
        for key in provided:
            value = float(params[key])
            self._stage[key] = self._stage[key] + value if relative else value
        if self.move_time_s <= 0:
            return {**self._stage, "settled": True}
        self._moving_until = time.monotonic() + self.move_time_s
        self._move_deadline = time.monotonic() + float(params.get("timeout_s", 20.0))
        if not params.get("wait", True):
            return {**self._stage, "settled": False, "moving": True}
        return {"_continue": {"cmd": "stage_poll", "params": {}, "interval_s": 0.05}}

    def stage_poll(self, params: dict) -> dict:
        if not self._moving():
            return {**self._stage, "settled": True}
        if params.get("finalize") or time.monotonic() > self._move_deadline:
            return {**self._stage, "settled": False, "moving": True}
        return {"_continue": {"cmd": "stage_poll", "params": {}, "interval_s": 0.05}}

    # -- snapshot ------------------------------------------------------
    def get_microscope_state(self, params: dict) -> dict:
        focus = self.get_focus({})
        return {
            "microscope_name": "AMADEUS simulator",
            "unit": "dac",
            "high_tension_kV": self._ht_v / 1000.0,
            "magnification": _MAG_TABLE[self._mag_index],
            "mag_index": self._mag_index,
            "focus_nm": focus["focus_nm"],
            "focus_dac": focus["focus_dac"],
            "calibrated": focus["calibrated"],
            "spot_size": self._spot,
            "alpha": self._alpha,
            "beam_blanked": self._blanked,
            "column_valves_open": self._valves_open,
            "beam_shift": {"x": self._deflectors["beam_shift"][0],
                           "y": self._deflectors["beam_shift"][1]},
            "beam_tilt": {"x": self._deflectors["beam_tilt"][0],
                          "y": self._deflectors["beam_tilt"][1]},
            "stigmation": self.get_stigmation({})["stigmators"],
            "tem_stem_mode": self._mode_name(),
            "function_mode": _FUNCTION_MODES[self._function_mode],
            "operation_mode": self._operation_mode(),
            "stage": dict(self._stage),
        }
