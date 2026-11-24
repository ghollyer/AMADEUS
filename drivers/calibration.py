"""Per-instrument calibration and wiring, shared by every driver.

Lens, deflector and stigmator values are device units (a 16-bit DAC on JEOL).
AMADEUS reports them honestly and *optionally* converts focus to nanometres
when the operator has measured a calibration. Uncalibrated replies carry
``"calibrated": false`` and ``focus_nm: null``, never a made-up physical value.

The calibration lives in the ``instrument`` section of ``amadeus_config.json``,
so the instrument profile carries it. A driver's ``calibration`` file (config
``drivers.<name>.calibration``) is layered on top. Every key is optional.
Defaults come in two layers: the generic ones below, then the driver's own
(``with_defaults``; JEOL's set CLA1/CLA2, OLf/OLc and a nanometre stage).

Focus calibration procedure (per microscope, per HT, per mode):
  1. Park at zero displayed defocus, call ``get_focus``, record ``focus_dac``
     -> ``dac_at_zero``.
  2. Dial two known defocus values D1, D2 (nm) on the console, record the
     ``focus_dac`` readings raw1, raw2.
  3. ``dac_per_nm = (raw2 - raw1) / (D2 - D1)``; cross-check
     ``dac_at_zero ~= raw1 - D1 * dac_per_nm``. Record when, at which HT and
     how in ``"measured"``: restored profiles warn about stale calibrations.

Example (JEOL F200):

{
  "label": "JEOL F200",
  "stage_scale_to_m": 1e-9,
  "deflectors": {"beam_shift": "CLA1", "beam_tilt": "CLA2"},
  "lenses": {"focus.fine": "OLf", "focus.coarse": "OLc"},
  "magnifications": {"TEM": {"MAG": [1000, 1200, 1500]}},
  "focus": {
    "by_mode": {
      "TEM":  {"lens": "focus.fine", "dac_per_nm": 0.75, "dac_at_zero": 32768,
               "measured": {"date": "2026-11-12", "ht_kV": 200}},
      "STEM": {"lens": "focus.fine", "dac_per_nm": null, "dac_at_zero": null}
    }
  },
  "stage": {
    "motion": "auto",
    "backlash": {"enabled": false, "overshoot_nm": 1000, "approach": [1, 1]},
    "piezo_range_nm": 10000,
    "image_mapping": {
      "STEM": {"stage_to_image": [[-0.649, 0.50], [0.44, 0.83]],
               "scan_rotation_deg": 0.0, "measured": "2026-11-12 x12k/x25k DF"}
    }
  }
}

* ``stage_scale_to_m``: the vendor's stage unit in metres (JEOL: nm, 1e-9).
* ``deflectors`` / ``lenses``: role -> the vendor's channel. Tools and
  sequences only ever name the role.
* ``magnifications``: per imaging mode and function mode (``"*"`` = any), the
  mag selector table, for ``set_magnification`` by value.
* ``stage.motion``: where blocking stage drive calls run. ``"process"`` = a
  child process (``drivers/jeol/stage_motion.py``), so a vendor setter that
  blocks until the stage settles can't freeze the server;
  ``"inline"`` = the driver's thread; ``"auto"`` = process online, inline
  for an offline simulator.
* ``stage.backlash``: a fixed-direction approach. An X/Y motor move first goes
  ``overshoot_nm`` short of the target on the far side, then drives to the
  target in the ``approach`` direction (+1/-1 per axis). The F200 lost
  0.5-0.7 um on direction reversals.
* ``stage.image_mapping``: used client-side by ``convert_pixel_to_stage``.
  ``stage_to_image`` is the 2x2 Jacobian J: moving the stage by d shifts
  image features by J.d (image x right, y down). One entry per imaging mode.
"""

import copy
import json

DEFAULT_CALIBRATION = {
    "label": "uncalibrated instrument",
    # Vendor stage unit in metres (a driver whose stage reports metres keeps 1).
    "stage_scale_to_m": 1.0,
    "deflectors": {},
    "lenses": {},
    "magnifications": {},
    "focus": {"by_mode": {}},
    "stage": {
        # "auto" | "process" | "inline" — see the module docstring.
        "motion": "auto",
        "backlash": {"enabled": False, "overshoot_nm": 1000.0, "approach": [1, 1]},
        "piezo_range_nm": 10000.0,
        "image_mapping": {},
    },
}


class InstrumentCalibration:
    """Loaded calibration with unit-conversion helpers."""

    def __init__(self, data: "dict | None" = None, defaults: "dict | None" = None):
        self._user = copy.deepcopy(data or {})
        self._defaults = copy.deepcopy(defaults or {})
        self._data = copy.deepcopy(DEFAULT_CALIBRATION)
        _deep_update(self._data, self._defaults)
        _deep_update(self._data, self._user)

    def with_defaults(self, defaults: dict) -> "InstrumentCalibration":
        """The same calibration with a driver's own defaults underneath (the
        config still wins): ``JeolDriver`` passes CLA1/CLA2, OLf/OLc, nm stage."""
        merged = copy.deepcopy(self._defaults)
        _deep_update(merged, defaults)
        return InstrumentCalibration(self._user, merged)

    @classmethod
    def load(cls, path: "str | None", inline: "dict | None" = None) -> "InstrumentCalibration":
        """Defaults < *inline* (the config's ``instrument`` section) < the
        JSON file at *path*."""
        data = copy.deepcopy(inline or {})
        if path:
            with open(path, "r", encoding="utf-8") as f:
                _deep_update(data, json.load(f))
        return cls(data)

    def as_dict(self) -> dict:
        return copy.deepcopy(self._data)

    @property
    def label(self) -> str:
        return self._data["label"]

    @property
    def stage_scale_to_m(self) -> float:
        return float(self._data["stage_scale_to_m"])

    # ------------------------------------------------------------------
    # Lenses and magnification
    # ------------------------------------------------------------------
    def lens_roles(self) -> dict:
        """``{"focus.fine": "OLf", ...}``."""
        return dict(self._data.get("lenses") or {})

    def lens_vendor(self, role: str) -> str:
        """Vendor lens channel for a role (``"focus.fine"`` -> ``"OLf"``)."""
        roles = self.lens_roles()
        if role not in roles:
            raise ValueError(f"unknown lens role {role!r}; this instrument has "
                             f"{', '.join(sorted(roles)) or 'none'}")
        return roles[role]

    def focus_lens_role(self, mode: str) -> str:
        """The lens role that carries focus in *mode* (default focus.fine)."""
        return self.focus_lens(mode)

    def focus_lens_vendor(self, mode: str) -> str:
        return self.lens_vendor(self.focus_lens_role(mode))

    def mag_tables(self) -> list:
        """``["TEM/MAG", ...]``: the modes with a magnification table."""
        out = []
        for mode, by_fm in (self._data.get("magnifications") or {}).items():
            for fm, table in (by_fm or {}).items():
                if table:
                    out.append(f"{mode}/{fm}")
        return out

    def mag_table(self, mode: str, function_mode: "str | None") -> tuple:
        """``(label, [values])`` for this mode, or ``(label, [])``."""
        by_fm = (self._data.get("magnifications") or {}).get(mode) or {}
        label = f"{mode}/{function_mode}"
        table = by_fm.get(function_mode or "") or by_fm.get("*") or []
        return label, [float(v) for v in table]

    def deflector_for(self, role: str) -> str:
        """The vendor deflector for a role ("beam_shift" -> JEOL "CLA1")."""
        deflectors = self._data.get("deflectors") or {}
        if role not in deflectors:
            raise ValueError(f"no deflector configured for {role!r} (instrument.deflectors)")
        return deflectors[role]

    # ------------------------------------------------------------------
    # Stage
    # ------------------------------------------------------------------
    @property
    def stage(self) -> dict:
        return copy.deepcopy(self._data["stage"])

    def stage_motion(self, offline: bool) -> str:
        """Resolved motion mode: "process" or "inline"."""
        mode = self._data["stage"].get("motion", "auto")
        if mode == "auto":
            return "inline" if offline else "process"
        if mode not in ("process", "inline"):
            raise ValueError(f"stage.motion must be auto, process or inline; got {mode!r}")
        return mode

    # ------------------------------------------------------------------
    # Focus
    # ------------------------------------------------------------------
    def focus_cal(self, mode: str) -> "dict | None":
        """The focus calibration sub-dict for "TEM"/"STEM", or None if that
        mode has no measured slope/offset."""
        cal = self._data["focus"]["by_mode"].get(mode)
        if not cal:
            return None
        if cal.get("dac_per_nm") is None or cal.get("dac_at_zero") is None:
            return None
        return cal

    def focus_lens(self, mode: str) -> str:
        """The lens role that carries focus in this mode."""
        cal = self._data["focus"]["by_mode"].get(mode) or {}
        return cal.get("lens", "focus.fine")

    def dac_to_nm(self, dac: float, mode: str) -> "float | None":
        cal = self.focus_cal(mode)
        if cal is None:
            return None
        return (dac - cal["dac_at_zero"]) / cal["dac_per_nm"]

    def nm_to_dac(self, nm: float, mode: str) -> int:
        cal = self.focus_cal(mode)
        if cal is None:
            raise RuntimeError(
                f"Focus is not calibrated for mode {mode!r} on this instrument. "
                f"Set focus by device value (focus_dac) instead, or add dac_per_nm / "
                f"dac_at_zero to instrument.focus (see drivers/calibration.py)."
            )
        return int(round(nm * cal["dac_per_nm"] + cal["dac_at_zero"]))

    def delta_nm_to_dac(self, delta_nm: float, mode: str) -> int:
        cal = self.focus_cal(mode)
        if cal is None:
            raise RuntimeError(
                f"Focus is not calibrated for mode {mode!r}; use delta_dac instead."
            )
        return int(round(delta_nm * cal["dac_per_nm"]))


def _deep_update(base: dict, extra: dict) -> None:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
