"""Image utilities used while driving the microscope.

* :func:`sharpness`: a focus metric for comparing frames of a focus sweep
  of the same field. It picked OLf 32541-32601 on the F200, 2026-11-12.
* :func:`phase_correlation_shift`: the feature shift between two images of
  the same area, for stage calibration and drift checks.
* :func:`fit_stage_to_image`: least-squares stage->image Jacobian from
  images with recorded stage positions (``instrument.stage.image_mapping``).
* :func:`to_png` / :func:`overlay_png`: write images and two-colour
  overlays as PNG with only numpy + zlib (no Pillow dependency).
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np
from scipy import ndimage as ndi


# ---------------------------------------------------------------------------
# Focus metric
# ---------------------------------------------------------------------------

def sharpness(arr: np.ndarray) -> float:
    """99th-percentile Sobel gradient over the image's dynamic range.

    Scale-free, so frames with different brightness compare fairly. Only
    compare frames of the same field at the same magnification. Higher is
    sharper."""
    a = ndi.gaussian_filter(np.asarray(arr, dtype=float), 1.0)
    grad = np.hypot(ndi.sobel(a, 0), ndi.sobel(a, 1))
    span = float(np.percentile(a, 99.5) - np.percentile(a, 5))
    if span <= 0:
        return 0.0
    return float(np.percentile(grad, 99) / span)


def sweep_values(lo: int, hi: int, steps: int) -> list[int]:
    """*steps* evenly spaced integer DAC values from *lo* to *hi* inclusive,
    duplicates removed (a narrow range gives fewer)."""
    return sorted({int(round(v)) for v in np.linspace(lo, hi, max(int(steps), 2))})


def two_sharpest_range(points: list[tuple[int, float]]) -> tuple[int, int]:
    """(lo, hi) spanning the focus values of the two sharpest points
    (``(focus, sharpness)`` pairs): the next iteration's sweep range."""
    if len(points) < 2:
        raise ValueError("need at least two points")
    top = sorted(points, key=lambda p: -p[1])[:2]
    a, b = sorted(int(p[0]) for p in top)
    return a, b


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def _prep(arr: np.ndarray) -> np.ndarray:
    a = np.asarray(arr, dtype=float)
    a = np.clip(a, None, np.percentile(a, 99.0))       # tame saturated particles
    a = ndi.gaussian_filter(a, 2)
    a = a - ndi.gaussian_filter(a, 40)                  # remove slow background
    window = np.outer(np.hanning(a.shape[0]), np.hanning(a.shape[1]))
    return a * window


def phase_correlation_shift(reference: np.ndarray, moved: np.ndarray) -> dict:
    """Shift (px, x right / y down) that features moved from *reference* to
    *moved*, with a peak-to-mean confidence (above ~20 is reliable; noise
    gives ~5). Integer-pixel precision; images must have the same shape."""
    if reference.shape != moved.shape:
        raise ValueError(f"shape mismatch: {reference.shape} vs {moved.shape}")
    fa, fb = np.fft.fft2(_prep(reference)), np.fft.fft2(_prep(moved))
    cross = fa * np.conj(fb)
    cross /= np.abs(cross) + 1e-12
    corr = np.abs(np.fft.ifft2(cross))
    y, x = np.unravel_index(int(corr.argmax()), corr.shape)
    rows, cols = corr.shape
    if y > rows // 2:
        y -= rows
    if x > cols // 2:
        x -= cols
    return {"dx_px": int(-x), "dy_px": int(-y),
            "confidence": round(float(corr.max() / (corr.mean() + 1e-12)), 1)}


def fit_stage_to_image(stage_deltas_m: list, image_shifts_m: list) -> dict:
    """Least-squares J with ``image_shift = J @ stage_delta``.

    Needs >= 2 non-parallel stage moves. Returns J, the per-move residuals (m)
    and the RMS residual. Backlash shows up as large residuals on moves that
    reversed direction; approach every move from the same side."""
    d = np.asarray(stage_deltas_m, dtype=float)
    s = np.asarray(image_shifts_m, dtype=float)
    if d.ndim != 2 or d.shape[1] != 2 or d.shape != s.shape:
        raise ValueError("need matching lists of (x, y) stage deltas and image shifts")
    if d.shape[0] < 2 or np.linalg.matrix_rank(d) < 2:
        raise ValueError("need at least two non-parallel stage moves")
    jt, *_ = np.linalg.lstsq(d, s, rcond=None)          # s = d @ J^T
    j = jt.T
    resid = s - d @ jt
    return {"stage_to_image": [[round(float(v), 4) for v in row] for row in j],
            "residuals_m": [[float(v) for v in r] for r in resid],
            "rms_residual_m": float(np.sqrt((resid ** 2).sum(axis=1).mean())),
            "n_moves": int(d.shape[0])}


# ---------------------------------------------------------------------------
# PNG output
# ---------------------------------------------------------------------------

def stretch(arr: np.ndarray, mode: str = "auto") -> np.ndarray:
    """Map to 0..1 for display.

    "auto": 0.5-99.8 percentile linear stretch.
    "low": saturates the particles to show faint material (shells, residue,
    support film), as the shell checks did.
    "log": log of the intensity above the minimum."""
    a = np.asarray(arr, dtype=float)
    if a.ndim == 3:                       # e.g. an SI cube: sum the spectral axis
        a = a.sum(axis=int(np.argmax(a.shape)))
    if mode == "low":
        b = a - np.percentile(a, 1)
        return np.clip(b / (np.percentile(b, 60) * 4 + 1e-12), 0, 1) ** 0.6
    if mode == "log":
        b = np.log1p(np.clip(a - a.min(), 0, None))
        a = b
    lo, hi = np.percentile(a, [0.5, 99.8])
    return np.clip((a - lo) / (hi - lo + 1e-12), 0, 1)


def _write_png(path: Path, rgb_or_gray: np.ndarray) -> Path:
    img = np.ascontiguousarray(rgb_or_gray, dtype=np.uint8)
    if img.ndim == 2:
        color_type = 0
    elif img.ndim == 3 and img.shape[2] == 3:
        color_type = 2
    else:
        raise ValueError(f"PNG needs HxW or HxWx3, got {img.shape}")
    h, w = img.shape[:2]
    raw = b"".join(b"\x00" + img[r].tobytes() for r in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, color_type, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    return path


def _downsample(a: np.ndarray, max_px: int | None) -> np.ndarray:
    if not max_px or max(a.shape[:2]) <= max_px:
        return a
    step = int(np.ceil(max(a.shape[:2]) / max_px))
    return a[::step, ::step]


def to_png(arr: np.ndarray, path, mode: str = "auto", max_px: int | None = 1024) -> Path:
    """Write *arr* as an 8-bit greyscale PNG."""
    return _write_png(Path(path), _downsample(stretch(arr, mode), max_px) * 255)


def overlay_png(red: np.ndarray, green: np.ndarray, path, blue: np.ndarray | None = None,
                smooth_px: float = 1.0, upscale: int = 1) -> Path:
    """Two- or three-colour overlay (e.g. Au red / Si green elemental maps).
    Each channel is smoothed and stretched 2-99.5 % independently."""
    def norm(x):
        x = ndi.gaussian_filter(np.asarray(x, dtype=float), smooth_px)
        lo, hi = np.percentile(x, [2, 99.5])
        return np.clip((x - lo) / (hi - lo + 1e-12), 0, 1)
    r, g = norm(red), norm(green)
    if r.shape != g.shape:
        raise ValueError(f"channel shapes differ: {r.shape} vs {g.shape}")
    b = norm(blue) if blue is not None else np.zeros_like(r)
    rgb = np.dstack([r, g, b]) * 255
    if upscale > 1:
        rgb = np.repeat(np.repeat(rgb, upscale, axis=0), upscale, axis=1)
    return _write_png(Path(path), rgb)
