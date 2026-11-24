"""Image rendering and value formatting shared by the operator console.

numpy -> PPM -> Tk PhotoImage, stdlib + numpy only (no Pillow).
"""

from pathlib import Path

import numpy as np

_IMG_BOX = 440  # default max rendered image edge, pixels


# ---------------------------------------------------------------------------
# Image rendering (numpy -> PPM -> Tk PhotoImage; stdlib only)
# ---------------------------------------------------------------------------

def _to_2d(arr: np.ndarray) -> np.ndarray:
    a = np.squeeze(np.asarray(arr))
    while a.ndim > 2:               # collapse SI cubes / stacks to a 2D preview
        a = a.mean(axis=0)
    if a.ndim == 1:
        a = a[None, :]
    return a


def _normalize_u8(a: np.ndarray) -> np.ndarray:
    a = a.astype(np.float32)
    lo, hi = np.percentile(a, [1.0, 99.0])
    if hi <= lo:
        hi = lo + 1.0
    return (np.clip((a - lo) / (hi - lo), 0.0, 1.0) * 255).astype(np.uint8)


def _ppm_bytes(gray_u8: np.ndarray) -> bytes:
    h, w = gray_u8.shape
    rgb = np.repeat(gray_u8[:, :, None], 3, axis=2)
    return f"P6 {w} {h} 255\n".encode() + rgb.tobytes()


def render_ppm(npy_path: Path, out_path: Path, box: int = _IMG_BOX) -> tuple[int, int]:
    """Load *npy_path*, downsample/normalize, write a PPM to *out_path*.

    Returns the (width, height) of the written preview.
    """
    arr = np.load(npy_path)
    a = _to_2d(arr)
    h, w = a.shape
    stride = max(1, (max(h, w) + box - 1) // box)
    a = a[::stride, ::stride]
    out_path.write_bytes(_ppm_bytes(_normalize_u8(a)))
    return a.shape[1], a.shape[0]


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------


def fmt_mode(m):
    if not m:
        return "—"
    u = str(m).strip().upper()
    if u.startswith("SCAN"):
        return f"STEM ({str(m).strip()})"
    if u.startswith("IMAG"):
        return f"TEM ({str(m).strip()})"
    return str(m).strip()


def fmt_um(v):
    return f"{v * 1e6:.3f}" if isinstance(v, (int, float)) else "—"


def fmt_deg(v):
    return f"{v:.3f}" if isinstance(v, (int, float)) else "—"
