"""Bright-particle morphology for DF/HAADF-STEM images.

Grew out of the 2026-11-12 F200 survey of Au nanorods on carbon:
  * :func:`measure_rods`: length/width/aspect ratio per particle, plus a
    summary over *isolated* particles only (dense clusters merge touching
    rods into one blob, which would skew the statistics).
  * :func:`find_particle_groups`: where the clusters are in a low-mag
    survey, ignoring large bright regions such as grid bars.
  * :func:`edge_distance_profile`: mean intensity vs distance outside the
    particle edge. A weak plateau beyond the probe tail reveals a low-Z shell
    (the ~5-10 nm silica on those rods).

All assume bright particles on a darker background (DF/HAADF). Lengths are in
nm; pass ``nm_per_px`` from ``client.analysis.units.nm_per_px``.
"""

from __future__ import annotations

import numpy as np
from scipy import ndimage as ndi


def _levels(smoothed: np.ndarray, bg_pct: float = 50.0, peak_pct: float = 99.5):
    return float(np.percentile(smoothed, bg_pct)), float(np.percentile(smoothed, peak_pct))


def particle_mask(arr: np.ndarray, threshold_frac: float = 0.35, smooth_px: float = 1.5,
                  bg_pct: float = 50.0, peak_pct: float = 99.5) -> np.ndarray:
    """Binary mask of bright particles: above ``bg + frac * (peak - bg)``."""
    a = ndi.gaussian_filter(np.asarray(arr, dtype=float), smooth_px)
    bg, peak = _levels(a, bg_pct, peak_pct)
    mask = a > bg + threshold_frac * (peak - bg)
    mask = ndi.binary_fill_holes(mask)
    return ndi.binary_opening(mask, iterations=1)


def measure_rods(arr: np.ndarray, nm_per_px: float, threshold_frac: float = 0.35,
                 min_area_nm2: float = 80.0, min_fill: float = 0.85,
                 max_width_ratio: float = 1.4) -> dict:
    """Per-particle length/width from second moments, and isolated-rod stats.

    Length and width are ``sqrt(12 * eigenvalue)`` of the pixel covariance:
    exact for a filled rectangle, close for a capped rod. Width includes the
    probe blur, a nm or two at 0.5 nm/px. A particle counts as *isolated*
    when its area fills >= ``min_fill`` of the L x W box (touching pairs and
    crosses fill far less) and its width is within ``max_width_ratio`` of the
    median width. Particles touching the image border are dropped.
    """
    if nm_per_px <= 0:
        raise ValueError("nm_per_px must be positive")
    a = np.asarray(arr, dtype=float)
    smoothed = ndi.gaussian_filter(a, 1.5)
    bg, peak = _levels(smoothed)
    mask = particle_mask(a, threshold_frac)
    labels, _ = ndi.label(mask)
    rows, cols = a.shape
    particles = []
    for i, sl in enumerate(ndi.find_objects(labels), 1):
        if sl is None:
            continue
        if sl[0].start == 0 or sl[1].start == 0 or sl[0].stop == rows or sl[1].stop == cols:
            continue
        obj = labels[sl] == i
        area_px = int(obj.sum())
        area_nm2 = area_px * nm_per_px ** 2
        if area_nm2 < min_area_nm2 or area_px < 5:
            continue
        yy, xx = np.nonzero(obj)
        ev = np.sort(np.linalg.eigvalsh(np.cov(np.vstack([xx, yy]))))[::-1]
        length = float(np.sqrt(12 * ev[0]) * nm_per_px)
        width = float(np.sqrt(12 * max(ev[1], 1e-9)) * nm_per_px)
        particles.append({
            "centroid_px": [float(xx.mean() + sl[1].start), float(yy.mean() + sl[0].start)],
            "length_nm": round(length, 2),
            "width_nm": round(width, 2),
            "aspect_ratio": round(length / width, 3),
            "area_nm2": round(area_nm2, 1),
            "fill": round(area_nm2 / (length * width), 3),
            "rel_intensity": round(float((smoothed[sl][obj] - bg).mean() / (peak - bg + 1e-12)), 3),
        })
    widths = [p["width_nm"] for p in particles]
    med_w = float(np.median(widths)) if widths else 0.0
    for p in particles:
        p["isolated"] = bool(p["fill"] >= min_fill and p["width_nm"] < max_width_ratio * med_w)
    return {"nm_per_px": nm_per_px, "n_objects": len(particles),
            "isolated": summarize([p for p in particles if p["isolated"]]),
            "particles": particles}


def summarize(particles: list[dict]) -> dict:
    """Mean/SD/median/IQR/range of length, width and aspect ratio."""
    if not particles:
        return {"n": 0}
    out = {"n": len(particles)}
    for key in ("length_nm", "width_nm", "aspect_ratio"):
        v = np.array([p[key] for p in particles], dtype=float)
        out[key] = {"mean": round(float(v.mean()), 2), "sd": round(float(v.std()), 2),
                    "median": round(float(np.median(v)), 2),
                    "iqr": [round(float(np.percentile(v, 25)), 2),
                            round(float(np.percentile(v, 75)), 2)],
                    "min": round(float(v.min()), 2), "max": round(float(v.max()), 2)}
    ar = np.array([p["aspect_ratio"] for p in particles], dtype=float)
    out["fraction_aspect_below_2_5"] = round(float((ar < 2.5).mean()), 3)
    return out


def find_particle_groups(arr: np.ndarray, nm_per_px: float, join_px: int = 6,
                         threshold_frac: float = 0.25, min_bright_px: int = 4,
                         exclude_large_bright: bool = True,
                         large_bright_nm: float = 1000.0) -> list[dict]:
    """Clusters of bright particles in a survey image, largest first.

    Particles within ``join_px`` of each other merge into one group. Bright
    regions more than ``large_bright_nm`` across (grid bars, thick
    contamination) are masked out when ``exclude_large_bright``. Offsets are from the image centre, in nm,
    x right and y down.
    """
    a = ndi.gaussian_filter(np.asarray(arr, dtype=float), 1.0)
    med = float(np.median(a))
    hi = float(np.percentile(a, 99.99))
    bright = a > med + threshold_frac * (hi - med)
    if exclude_large_bright:
        # Regions more than large_bright_nm across (grid bars, thick debris).
        # Distance transforms keep this cheap at any magnification.
        radius_px = max(6.0, large_bright_nm / 2.0 / nm_per_px)
        lit = a > med + 0.15 * (hi - med)
        core = ndi.distance_transform_edt(lit) > radius_px
        if core.any():
            # The whole connected bright region holding a core (its bright
            # rim included), plus a small margin; clusters near but not
            # touching the bar survive.
            lit_labels, _ = ndi.label(lit)
            ids = np.unique(lit_labels[core])
            large = np.isin(lit_labels, ids[ids > 0])
            bright &= ~ndi.binary_dilation(large, iterations=5)
    labels, _ = ndi.label(ndi.binary_dilation(bright, iterations=max(1, int(join_px))))
    rows, cols = a.shape
    groups = []
    for k, sl in enumerate(ndi.find_objects(labels), 1):
        if sl is None:
            continue
        member = bright[sl] & (labels[sl] == k)
        n = int(member.sum())
        if n < min_bright_px:
            continue
        cy, cx = ndi.center_of_mass(member)
        cx += sl[1].start
        cy += sl[0].start
        groups.append({
            "centroid_px": [round(float(cx), 1), round(float(cy), 1)],
            "offset_nm": [round((cx - cols / 2) * nm_per_px, 1),
                          round((cy - rows / 2) * nm_per_px, 1)],
            "extent_nm": [round((sl[1].stop - sl[1].start) * nm_per_px, 1),
                          round((sl[0].stop - sl[0].start) * nm_per_px, 1)],
            "bright_px": n,
        })
    return sorted(groups, key=lambda g: -g["bright_px"])


def edge_distance_profile(arr: np.ndarray, nm_per_px: float, step_nm: float | None = None,
                          max_nm: float = 30.0, threshold_frac: float = 0.5) -> dict:
    """Median intensity in shells at increasing distance outside the particle
    edge, as a fraction of (particle - background).

    The first few nm are the probe tail. A plateau of a few % beyond it, out
    to some distance, is a low-Z coating, and where it falls to background
    gives its thickness. Neighbouring particles and carbon-film steps
    contaminate the far bins, so compare with an off-particle area.
    """
    a = ndi.gaussian_filter(np.asarray(arr, dtype=float), 1.0)
    bg = float(np.percentile(a, 3))
    peak = float(np.percentile(a, 99))
    rod = ndi.binary_opening(a > bg + threshold_frac * (peak - bg), iterations=2)
    if not rod.any():
        raise ValueError("no particles found above the threshold")
    dist = ndi.distance_transform_edt(~rod) * nm_per_px
    step = float(step_nm) if step_nm else max(1.5, 2 * nm_per_px)
    bins = []
    lo = 0.0
    while lo < max_nm:
        sel = (dist > lo) & (dist <= lo + step)
        if sel.sum() >= 10:
            bins.append({"from_nm": round(lo, 2), "to_nm": round(lo + step, 2),
                         "fraction": round(float((np.median(a[sel]) - bg) / (peak - bg)), 4),
                         "n_px": int(sel.sum())})
        lo += step
    return {"nm_per_px": nm_per_px, "step_nm": step, "bins": bins}
