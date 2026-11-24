"""Template: copy this file to start a driver for a new vendor.

Steps (docs/ADDING_HARDWARE.md has the full guide):

1. Copy to ``drivers/<vendor>.py`` (or your own package), rename the class
   and ``kind``.
2. Keep the slot classes your hardware fills, drop the others. Each slot
   class lists its operations in ``drivers/base.py``.
3. Implement the operations the hardware supports. Leave the rest alone:
   an un-overridden operation is reported as missing, never faked.
4. Register the kind: add it to ``drivers/registry.py`` BUILTIN, or set
   ``"class": "yourpackage.module:YourDriver"`` in the config, or publish an
   ``amadeus.drivers`` entry point from your package.
5. Run the contract checks: ``amadeus-driver-test --kind <kind>`` (or
   ``python -m drivers.contract``), first against a fake of your vendor API,
   then read-only on the instrument with ``python -m drivers.smoke``.
6. Give it slots in ``amadeus_config.json``'s ``drivers`` section.

This template runs as is (kind ``template``): it reports one read-only
operation so the contract checks have something to look at.
"""

from __future__ import annotations

from drivers.base import ColumnDriver, ImageResult, ScanDriver  # noqa: F401


class TemplateDriver(ColumnDriver, ScanDriver):
    kind = "template"
    # Vendor-only operations: wire name -> method. Reached as "template.<name>".
    extras: dict = {}

    def __init__(self, calibration=None, **options):
        # *calibration* is the instrument section of the config
        # (drivers.calibration.InstrumentCalibration): lens and deflector
        # roles, focus calibration, stage settings. *options* come from the
        # driver's "options" in the config (addresses, serial ports, ...).
        # Connect to the vendor API here. Raise if it isn't available: the
        # relay reports the error and retries on the next request.
        self.cal = calibration
        self.options = options
        self._focus = 500

    def describe(self) -> dict:
        return {"backend": self.kind, "kind": self.kind,
                "instrument": "template (no hardware)"}

    def capabilities(self, params=None) -> dict:
        caps = super().capabilities(params)
        # Add the value lists your hardware has, e.g.:
        # caps["function_modes"] = {"TEM": ["MAG", "DIFF"]}
        # caps["stigmators"] = ["objective", "condenser"]
        # caps["lenses"] = {"focus.fine": {"vendor_name": "...", "unit": "...",
        #                                  "range": [lo, hi]}}
        caps["lenses"] = {"focus.fine": {"vendor_name": "template", "unit": "step",
                                         "range": [0, 1000]}}
        return caps

    # A read-only operation, in the reply format every driver uses: the
    # device value with its unit and range, the lens roles, vendor detail in
    # "vendor", and focus_nm only when there is a measured calibration.
    def get_focus(self, params: dict) -> dict:
        return {"focus_dac": self._focus, "unit": "step", "range": [0, 1000],
                "lens": "focus.fine", "focus_nm": None, "calibrated": False,
                "lenses": {"focus.fine": {"value": self._focus, "unit": "step",
                                          "range": [0, 1000]}},
                "vendor": {}, "mode": "TEM"}

    # An image operation returns an ImageResult (raw bytes, shape, dtype):
    # def stem_acquire(self, params: dict) -> ImageResult:
    #     payload, (h, w) = vendor.scan(...)
    #     return ImageResult({"image_meta": {"name": "...", "calibration":
    #                         {"x_scale": px, "y_scale": px, "units": "nm"}}},
    #                        payload, [h, w], "float32")
