"""The driver interfaces: one root class and one class per slot.

A *driver* connects AMADEUS to one vendor's software. It fills one or more
*slots* (column, stage, scan, camera, live, spectrometer, files) by
subclassing the matching slot classes below and overriding the operations
the hardware supports. The relay routes each command to the driver holding
its slot (``common/commands.py``), so the MCP tools, the wire protocol and the
sequences don't change from one microscope to the next.

Conventions every driver follows:

* Each operation is a method named exactly like its wire command. It takes
  the request's ``params`` dict and returns a JSON-serialisable dict, or an
  :class:`ImageResult` for operations that return pixels.
* An operation the hardware can't do stays un-overridden: it raises
  ``NotImplementedError``, the dispatcher turns that into a structured error,
  and :meth:`Driver.capabilities` leaves it out. Never fake an operation.
* Stage positions cross the wire in metres (``x_m``/``y_m``/``z_m``) and
  degrees (``alpha_deg``/``beta_deg``).
* Lens, deflector and stigmator values are honest *device units*. Replies say
  which (``"unit": "dac"``) and give the valid ``"range"``, so a client never
  has to know the vendor's scale. Physical units (``focus_nm``) are added only
  when the instrument has a measured calibration (``"calibrated": true``).
* Booleans are booleans, enumerations are strings (``"DIFF"``, not ``4``), and
  the driver lists the valid values in :meth:`Driver.capabilities`.
* Raw vendor detail goes in a ``"vendor"`` sub-object, never in top-level keys.
* Multi-device slots (scan, camera, live, spectrometer, files) receive
  ``params["device"]``: the driver-local part of a device id (``"camera/1"``),
  already resolved from a role by the relay. Absent means the default device.
* Vendor-only operations are *extras*: list them in :attr:`Driver.extras` and
  they are reachable on the wire as ``"<kind>.<name>"``. The generic escape
  hatch is :meth:`Driver.vendor_call`.

Stdlib only and Python 3.10-compatible: drivers run in vendor Python
environments (vjem310 for PyJEM). No numpy: image payloads are raw bytes.
"""

from __future__ import annotations

from common.commands import SLOT_COMMANDS

# Support levels, reported per driver and by ``amadeus-driver-test``.
LEVELS = {
    0: "nothing usable",
    1: "reads instrument state",
    2: "controls the column and/or stage",
    3: "acquires images or spectra",
    4: "live view and spectrum imaging",
}


class ImageResult:
    """An operation's reply that carries pixels.

    *payload* is the raw C-contiguous array bytes, *shape* and *dtype* (a
    numpy dtype string such as ``"float32"``) describe them, and *data* is the
    JSON metadata. ``data["image_meta"]`` (name, calibration, ...) is merged
    into the image meta frame, exactly as ``dm_server`` does."""

    __slots__ = ("data", "payload", "shape", "dtype")

    def __init__(self, data: dict, payload: bytes, shape, dtype: str):
        self.data = dict(data or {})
        self.payload = bytes(payload)
        self.shape = [int(n) for n in shape]
        self.dtype = str(dtype)
        itemsize = {"uint8": 1, "int8": 1, "uint16": 2, "int16": 2, "float32": 4,
                    "int32": 4, "uint32": 4, "float64": 8, "int64": 8,
                    "complex64": 8}.get(self.dtype)
        count = 1
        for n in self.shape:
            count *= n
        if itemsize is not None and count * itemsize != len(self.payload):
            raise ValueError(f"image payload is {len(self.payload)} bytes; shape "
                             f"{self.shape} of {self.dtype} needs {count * itemsize}")


class Driver:
    """Root of every driver. Subclass it together with the slot classes."""

    kind = "base"
    # Vendor-only operations: wire name (without the "<kind>." prefix) ->
    # method name. Reachable as "<kind>.<name>" and listed in capabilities.
    extras: dict = {}

    def describe(self) -> dict:
        """Identity info merged into ping replies."""
        return {"backend": self.kind, "kind": self.kind}

    def close(self) -> None:
        """Release vendor resources (helper processes, connections)."""

    def devices(self) -> list:
        """Devices in multi-device slots: ``[{"id": "camera/0", "slot":
        "camera", "name": "OneView"}, ...]``. ``id`` is local to this driver;
        the relay prefixes the driver name. Must not move hardware."""
        return []

    def vendor_call(self, params: dict) -> dict:
        """Escape hatch: one named vendor API call, ``{"method": str, "args":
        list}``. Drivers whitelist what is callable. No arbitrary code."""
        raise NotImplementedError

    # -- introspection ---------------------------------------------------
    @classmethod
    def operations(cls) -> dict:
        """``{command: slot}`` for every slot operation this class overrides
        (a method still raising the slot class's ``NotImplementedError`` is
        left out), so a partial driver is described honestly."""
        out = {}
        for slot_cls in _SLOT_CLASSES:
            if not issubclass(cls, slot_cls):
                continue
            for name in SLOT_COMMANDS[slot_cls.slot]:
                base_fn = slot_cls.__dict__.get(name)
                if base_fn is not None and getattr(cls, name, base_fn) is not base_fn:
                    out[name] = slot_cls.slot
        return out

    def capabilities(self, params: "dict | None" = None) -> dict:
        """What this driver can do. Never touches the vendor API except
        through :meth:`devices`.

        Subclasses extend it with ``modes`` (imaging modes), ``features``
        (optional abilities such as ``"piezo"``), value lists for their
        enumerations (``function_modes``, ``stigmators``, ``lenses``, ...) and
        ``unreliable_reads`` (``{command: why}`` for reads known to fail on
        some instruments: the contract and smoke test report them as WARN)."""
        ops = self.operations()
        slots = sorted({slot for slot in ops.values()})
        try:
            devices = self.devices()
        except Exception as exc:              # never fail capabilities over a probe
            devices = []
            device_error = f"{type(exc).__name__}: {exc}"
        else:
            device_error = None
        caps = {
            "backend": self.kind, "kind": self.kind,
            "commands": sorted(ops),
            "slots": slots,
            "extras": sorted(f"{self.kind}.{name}" for name in self.extras),
            "vendor_call": _overrides(type(self), "vendor_call", Driver),
            "modes": ["TEM", "STEM"] if "column" in slots else [],
            "features": [],
            "devices": devices,
        }
        if device_error:
            caps["devices_error"] = device_error
        caps["level"] = support_level(caps["commands"])
        return caps


def _overrides(cls, name: str, base) -> bool:
    return getattr(cls, name) is not getattr(base, name)


def support_level(commands) -> int:
    """0-4 from the commands a driver implements (see :data:`LEVELS`)."""
    cmds = set(commands)
    if cmds & {"live_start", "si_start", "si_configure"}:
        return 4
    if cmds & {"stem_acquire", "camera_acquire", "eds_acquire_point", "eds_acquire_map"}:
        return 3
    if cmds & {"set_focus", "change_focus", "set_magnification", "move_stage",
               "set_beam_shift", "set_stigmation"}:
        return 2
    if any(c.startswith("get_") or c in ("list_cameras", "list_files") for c in cmds):
        return 1
    return 0


# ---------------------------------------------------------------------------
# Slot classes. Method names are the wire commands in common/commands.py
# (tests/test_drivers.py checks the two stay identical).
# ---------------------------------------------------------------------------

class ColumnDriver(Driver):
    """Optics and column state."""
    slot = "column"

    # focus: get_focus returns focus_dac (+ unit/range), focus_nm/calibrated,
    # and per-role lens values under "lenses" ({"focus.fine": {...}}).
    # set_focus takes exactly one of focus_nm, focus_dac, or lens + dac.
    def get_focus(self, params: dict) -> dict: raise NotImplementedError
    def set_focus(self, params: dict) -> dict: raise NotImplementedError
    def change_focus(self, params: dict) -> dict: raise NotImplementedError
    # magnification: by "magnification" (value) or "mag_index".
    def get_magnification(self, params: dict) -> dict: raise NotImplementedError
    def set_magnification(self, params: dict) -> dict: raise NotImplementedError
    def get_beam_shift(self, params: dict) -> dict: raise NotImplementedError
    def set_beam_shift(self, params: dict) -> dict: raise NotImplementedError
    def get_beam_tilt(self, params: dict) -> dict: raise NotImplementedError
    def set_beam_tilt(self, params: dict) -> dict: raise NotImplementedError
    # stigmation: set by "stigmator" name (listed in capabilities).
    def get_stigmation(self, params: dict) -> dict: raise NotImplementedError
    def set_stigmation(self, params: dict) -> dict: raise NotImplementedError
    def get_spot_size(self, params: dict) -> dict: raise NotImplementedError
    def set_spot_size(self, params: dict) -> dict: raise NotImplementedError
    def get_alpha(self, params: dict) -> dict: raise NotImplementedError
    def set_alpha(self, params: dict) -> dict: raise NotImplementedError
    def set_beam_blank(self, params: dict) -> dict: raise NotImplementedError
    def set_column_valves(self, params: dict) -> dict: raise NotImplementedError
    def get_aperture(self, params: dict) -> dict: raise NotImplementedError
    def set_aperture(self, params: dict) -> dict: raise NotImplementedError
    def get_instrument_mode(self, params: dict) -> dict: raise NotImplementedError
    def set_instrument_mode(self, params: dict) -> dict: raise NotImplementedError
    # function mode: by name (listed in capabilities "function_modes").
    def get_function_mode(self, params: dict) -> dict: raise NotImplementedError
    def set_function_mode(self, params: dict) -> dict: raise NotImplementedError
    def get_high_tension(self, params: dict) -> dict: raise NotImplementedError
    def get_microscope_state(self, params: dict) -> dict: raise NotImplementedError


class StageDriver(Driver):
    """Specimen stage. Positions in metres and degrees."""
    slot = "stage"

    def get_stage_position(self, params: dict) -> dict: raise NotImplementedError

    def move_stage(self, params: dict) -> dict:
        """``mode`` absolute|relative, any of x_m/y_m/z_m/alpha_deg/beta_deg,
        ``wait``/``timeout_s``, and *intent* options a driver may support:
        ``speed`` slow|normal|fast, ``drive`` motor|piezo (fine), ``approach``
        (backlash-compensated). A long move may return ``{"_continue":
        {"cmd": "stage_poll", "params": {}, "interval_s": s}}``; the relay
        then polls ``stage_poll`` and the driver stays free between polls."""
        raise NotImplementedError

    def get_stage_status(self, params: dict) -> dict: raise NotImplementedError
    def stop_stage(self, params: dict) -> dict: raise NotImplementedError
    def stage_poll(self, params: dict) -> dict: raise NotImplementedError


class ScanDriver(Driver):
    """STEM scan unit and its detectors (devices ``detector/<n>``)."""
    slot = "scan"

    def stem_acquire(self, params: dict) -> "ImageResult": raise NotImplementedError
    def get_stem_settings(self, params: dict) -> dict: raise NotImplementedError
    def set_stem_settings(self, params: dict) -> dict: raise NotImplementedError
    def get_detectors(self, params: dict) -> dict: raise NotImplementedError
    def set_detector(self, params: dict) -> dict: raise NotImplementedError
    def move_beam_to(self, params: dict) -> dict: raise NotImplementedError
    def get_beam_position(self, params: dict) -> dict: raise NotImplementedError


class CameraDriver(Driver):
    """TEM cameras (devices ``camera/<n>``)."""
    slot = "camera"

    def camera_acquire(self, params: dict) -> "ImageResult": raise NotImplementedError
    def get_camera_info(self, params: dict) -> dict: raise NotImplementedError
    def list_cameras(self, params: dict) -> dict: raise NotImplementedError


class LiveDriver(Driver):
    """Live view. Frames are published on the driver's stream address in the
    ``LiveFrame`` format (``common/protocol.py``)."""
    slot = "live"

    def live_start(self, params: dict) -> dict: raise NotImplementedError
    def live_stop(self, params: dict) -> dict: raise NotImplementedError
    def live_status(self, params: dict) -> dict: raise NotImplementedError


class SpectrometerDriver(Driver):
    """EDS / EELS detectors and spectrum imaging (devices ``spectrometer/<n>``)."""
    slot = "spectrometer"

    def si_configure(self, params: dict) -> dict: raise NotImplementedError
    def si_start(self, params: dict) -> dict: raise NotImplementedError
    def si_stop(self, params: dict) -> dict: raise NotImplementedError
    def si_get_settings(self, params: dict) -> dict: raise NotImplementedError
    def eds_acquire_point(self, params: dict) -> dict: raise NotImplementedError
    def eds_acquire_map(self, params: dict) -> dict: raise NotImplementedError


class FileDriver(Driver):
    """Data files on the instrument side."""
    slot = "files"

    def list_files(self, params: dict) -> dict: raise NotImplementedError
    def load_file(self, params: dict) -> "ImageResult": raise NotImplementedError
    def read_file_chunk(self, params: dict) -> "ImageResult": raise NotImplementedError


_SLOT_CLASSES = (ColumnDriver, StageDriver, ScanDriver, CameraDriver, LiveDriver,
                 SpectrometerDriver, FileDriver)
SLOT_CLASSES = {cls.slot: cls for cls in _SLOT_CLASSES}
