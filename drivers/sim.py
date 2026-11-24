"""SimDriver: the AMADEUS simulator filling every slot.

The column and stage come from ``drivers/sim_column.py``. On top,
this adds a STEM scan unit with two detectors, two cameras, an EDS
spectrometer and a small file store, all producing synthetic data. With it,
the whole stack (MCP tools, web console, sequences) runs end to end with no
Digital Micrograph and no vendor software. It is also the reference
implementation every other driver is checked against (``drivers/contract.py``).

The sample is a field of gold-like particles fixed in stage coordinates, so
moving the stage moves the image the way it would on a microscope, and the
field of view follows the magnification. No numpy (drivers run in vendor
Python environments): pixels are built with the ``array`` module.
"""

from __future__ import annotations

import array
import math
import random
import time

from drivers.base import (CameraDriver, FileDriver, ImageResult, ScanDriver,
                          SpectrometerDriver)
from drivers.sim_column import SimColumnDriver

_DETECTORS = [{"index": 0, "name": "HAADF"}, {"index": 1, "name": "BF"}]
_CAMERAS = [{"id": 0, "name": "Sim imaging camera", "width": 512, "height": 512},
            {"id": 1, "name": "Sim diffraction camera", "width": 256, "height": 256}]
_FIELD_NM_AT_1X = 2.0e7          # field of view (nm) x magnification
_CELL_NM = 400.0                 # particle grid cell in the sample
_MAX_PIXELS = 2048 * 2048


def _local_index(device, prefix: str, default: int) -> int:
    """``"camera/1"`` -> 1; ``None`` -> *default*."""
    if device is None or device == "":
        return default
    text = str(device)
    if "/" in text:
        kind, _, idx = text.partition("/")
        if kind != prefix:
            raise ValueError(f"device {device!r} is not a {prefix}")
        text = idx
    try:
        return int(text)
    except ValueError:
        raise ValueError(f"bad {prefix} device {device!r}")


class SimDriver(SimColumnDriver, ScanDriver, CameraDriver, SpectrometerDriver, FileDriver):
    kind = "sim"

    def __init__(self, calibration=None, image_size: int = 256, seed: int = 7):
        super().__init__(calibration=calibration)
        self.image_size = int(image_size)
        self.seed = int(seed)
        self._stem = {"width": self.image_size, "height": self.image_size,
                      "dwell_us": 2.0, "rotation": 0.0, "signal_index": 0}
        self._beam = {"x": 0.5, "y": 0.5}
        self._si = {"configured": False, "running": False, "settings": {}}
        self._files: dict = {}           # path -> (meta, payload, shape)

    def describe(self) -> dict:
        return {"backend": self.kind, "kind": self.kind,
                "instrument": "AMADEUS simulator (all slots)"}

    def devices(self) -> list:
        out = [{"id": f"detector/{d['index']}", "slot": "scan", "name": d["name"]}
               for d in _DETECTORS]
        out += [{"id": f"camera/{c['id']}", "slot": "camera", "name": c["name"]}
                for c in _CAMERAS]
        out.append({"id": "spectrometer/0", "slot": "spectrometer", "name": "Sim EDS"})
        return out

    # ------------------------------------------------------------------
    # The synthetic sample
    # ------------------------------------------------------------------
    def _field_nm(self) -> float:
        mag = self.get_magnification({})["magnification"] or 1.0
        return _FIELD_NM_AT_1X / float(mag)

    def _particles(self, x0: float, y0: float, x1: float, y1: float) -> list:
        """Particles (cx, cy, r) in nm that touch the box, from a seeded grid."""
        out = []
        for gx in range(int(math.floor(x0 / _CELL_NM)) - 1, int(math.floor(x1 / _CELL_NM)) + 2):
            for gy in range(int(math.floor(y0 / _CELL_NM)) - 1,
                            int(math.floor(y1 / _CELL_NM)) + 2):
                rng = random.Random(hash((self.seed, gx, gy)) & 0xFFFFFFFF)
                if rng.random() < 0.45:
                    continue
                r = 20.0 + rng.random() * 60.0
                out.append(((gx + rng.random()) * _CELL_NM, (gy + rng.random()) * _CELL_NM, r))
        return out

    def _render(self, width: int, height: int, bright: bool, noise: float) -> tuple:
        """(float32 bytes, nm per px). Particles bright (HAADF) or dark (BF)."""
        if width * height > _MAX_PIXELS:
            raise ValueError(f"{width}x{height} is larger than the simulator allows")
        field = self._field_nm()
        px = field / width
        stage = self.get_stage_position({})
        cx, cy = stage["x_m"] * 1e9, stage["y_m"] * 1e9
        x0, y0 = cx - field / 2, cy - px * height / 2
        defocus = abs(self._lenses["focus.fine"] - 32768) / 400.0    # blur in px
        values = array.array("f", [0.0]) * (width * height)
        for (pxc, pyc, r) in self._particles(x0, y0, x0 + field, y0 + px * height):
            u0, v0 = (pxc - x0) / px, (pyc - y0) / px
            rp = r / px + defocus
            for v in range(max(0, int(v0 - rp - 2)), min(height, int(v0 + rp + 3))):
                row = v * width
                for u in range(max(0, int(u0 - rp - 2)), min(width, int(u0 + rp + 3))):
                    d = math.hypot(u - u0, v - v0)
                    if d < rp + 1.5:
                        edge = 1.0 if d <= rp - 1 else max(0.0, (rp + 1.5 - d) / 2.5)
                        values[row + u] += edge
        rng = random.Random(int(time.monotonic() * 1000) ^ self.seed)
        base, gain = (0.05, 1.0) if bright else (1.0, -0.7)
        for i in range(width * height):
            values[i] = base + gain * min(values[i], 1.0) + noise * (rng.random() - 0.5)
        return values.tobytes(), px

    def _image(self, width: int, height: int, bright: bool, noise: float,
               name: str, tags: dict) -> ImageResult:
        payload, px = self._render(width, height, bright, noise)
        cal = {"x_scale": px, "y_scale": px, "units": "nm"}
        data = {**tags, "image_meta": {"name": name, "calibration": cal}}
        path = f"sim://acquisitions/{len(self._files) + 1:04d}_{name}.sim"
        self._files[path] = (dict(data), payload, [height, width])
        return ImageResult(data, payload, [height, width], "float32")

    # ------------------------------------------------------------------
    # Scan
    # ------------------------------------------------------------------
    def get_detectors(self, params: dict) -> dict:
        return {"detectors": [dict(d) for d in _DETECTORS]}

    def set_detector(self, params: dict) -> dict:
        index = _local_index(params.get("device"), "detector", 0)
        if not 0 <= index < len(_DETECTORS):
            raise ValueError(f"no detector {index}")
        self._stem["signal_index"] = index
        return {"signal_index": index, "name": _DETECTORS[index]["name"]}

    def get_stem_settings(self, params: dict) -> dict:
        return dict(self._stem)

    def set_stem_settings(self, params: dict) -> dict:
        for key in ("width", "height", "dwell_us", "rotation"):
            if params.get(key) is not None:
                self._stem[key] = type(self._stem[key])(params[key])
        return dict(self._stem)

    def stem_acquire(self, params: dict) -> ImageResult:
        if self._mode != 1:
            raise RuntimeError("the simulated column is in TEM mode; switch to STEM first")
        index = _local_index(params.get("device"), "detector",
                             self._stem["signal_index"])
        if not 0 <= index < len(_DETECTORS):
            raise ValueError(f"no detector {index}")
        width = int(params.get("width", self._stem["width"]))
        height = int(params.get("height", self._stem["height"]))
        dwell = float(params.get("dwell_us", self._stem["dwell_us"]))
        noise = 0.6 / math.sqrt(max(dwell, 0.05))
        name = f"STEM {_DETECTORS[index]['name']}"
        return self._image(width, height, index == 0, noise, name, {
            "mode": params.get("mode", "capture"), "width": width, "height": height,
            "dwell_us": dwell, "rotation": float(params.get("rotation", 0.0)),
            "signal_index": index, "detector": _DETECTORS[index]["name"]})

    def move_beam_to(self, params: dict) -> dict:
        x, y = float(params["x"]), float(params["y"])
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise ValueError("beam position is a fraction of the scan, 0-1")
        self._beam = {"x": x, "y": y}
        return dict(self._beam)

    def get_beam_position(self, params: dict) -> dict:
        return dict(self._beam)

    # ------------------------------------------------------------------
    # Cameras
    # ------------------------------------------------------------------
    def list_cameras(self, params: dict) -> dict:
        return {"cameras": [{"id": c["id"], "name": c["name"]} for c in _CAMERAS]}

    def _camera(self, params: dict) -> dict:
        cam_id = _local_index(params.get("device"), "camera", 0)
        for cam in _CAMERAS:
            if cam["id"] == cam_id:
                return cam
        raise ValueError(f"no camera {cam_id}")

    def get_camera_info(self, params: dict) -> dict:
        cam = self._camera(params)
        return {"camera_id": cam["id"], "name": cam["name"],
                "width": cam["width"], "height": cam["height"], "inserted": True}

    def camera_acquire(self, params: dict) -> ImageResult:
        cam = self._camera(params)
        binning = max(1, int(params.get("binning", 1)))
        exposure = float(params.get("exposure_s", 1.0))
        if exposure <= 0:
            raise ValueError("exposure_s must be positive")
        width, height = cam["width"] // binning, cam["height"] // binning
        noise = 0.4 / math.sqrt(exposure * 10.0)
        return self._image(width, height, False, noise, cam["name"], {
            "camera_id": cam["id"], "exposure_s": exposure, "binning": binning,
            "processing": params.get("processing", "gain_normalised")})

    # ------------------------------------------------------------------
    # Spectrometer
    # ------------------------------------------------------------------
    def si_configure(self, params: dict) -> dict:
        self._si = {"configured": True, "running": False, "settings": dict(params)}
        return {"configured": True, **self._si["settings"]}

    def si_start(self, params: dict) -> dict:
        if not self._si["configured"]:
            raise RuntimeError("configure the spectrum image first (si_configure)")
        self._si["running"] = True
        return {"started": True}

    def si_stop(self, params: dict) -> dict:
        self._si["running"] = False
        return {"stopped": True}

    def si_get_settings(self, params: dict) -> dict:
        return {"running": self._si["running"], **self._si["settings"]}

    def eds_acquire_point(self, params: dict) -> dict:
        exposure = float(params.get("exposure_s", 30.0))
        rng = random.Random(self.seed)
        channels = 2048
        spectrum = [rng.random() * 3 for _ in range(channels)]
        for energy_ev, height in ((2120.0, 400.0), (9711.0, 250.0), (1740.0, 120.0)):
            ch = int(energy_ev / 10.0)
            for d in range(-8, 9):
                if 0 <= ch + d < channels:
                    spectrum[ch + d] += height * math.exp(-(d * d) / 12.0) * exposure / 30.0
        return {"acquired": True, "mode": "Multi-Point", "exposure_s": exposure,
                "eV_per_channel": 10.0, "energy_offset_eV": 0.0,
                "spectrum": [round(v, 2) for v in spectrum],
                "beam_position": dict(self._beam)}

    def eds_acquire_map(self, params: dict) -> dict:
        return {"acquired": True, "mode": "2D Array",
                "x_pixels": int(params.get("x_pixels", 64)),
                "y_pixels": int(params.get("y_pixels", 64)),
                "exposure_s": float(params.get("exposure_s", 0.05)),
                "note": "simulated: the map stays on the simulator; no cube is produced"}

    # ------------------------------------------------------------------
    # Files: the acquisitions made in this session
    # ------------------------------------------------------------------
    def list_files(self, params: dict) -> dict:
        directory = params.get("directory") or "sim://acquisitions"
        entries = [{"name": p.rsplit("/", 1)[-1], "path": p, "size": len(v[1])}
                   for p, v in sorted(self._files.items()) if p.startswith(directory)]
        return {"directory": directory, "files": entries, "count": len(entries)}

    def load_file(self, params: dict) -> ImageResult:
        path = params.get("path")
        if path not in self._files:
            raise FileNotFoundError(f"no such simulated file {path!r}")
        data, payload, shape = self._files[path]
        return ImageResult({**data, "path": path}, payload, shape, "float32")
