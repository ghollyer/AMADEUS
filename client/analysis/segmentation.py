"""Particle segmentation — threshold, morphology, metrics."""

from __future__ import annotations

import numpy as np
from scipy import ndimage


def segment_particles(
    array: np.ndarray,
    method: str = "otsu",
    min_size_px: int = 10,
    max_size_px: int | None = None,
    fill_holes: bool = True,
) -> tuple[np.ndarray, list[dict]]:
    """Segment particles in *array* and return a label map + metrics.

    Parameters
    ----------
    array : ndarray
        2-D float image.
    method : str
        "otsu"     — Otsu's threshold (default)
        "mean"     — threshold at image mean
        "fixed"    — threshold at ``threshold`` param (see below, not used here)
    min_size_px : int
        Remove objects smaller than this many pixels.
    max_size_px : int or None
        Remove objects larger than this many pixels (None = no limit).
    fill_holes : bool
        Fill holes in binary mask before labelling.

    Returns
    -------
    (label_map, metrics_list)
        label_map    — int32 array; 0 = background, N = particle N
        metrics_list — list of dicts, one per particle:
            {
                "label": int,
                "area_px": int,
                "centroid_row": float,
                "centroid_col": float,
                "bbox": [row_min, col_min, row_max, col_max],
                "equiv_diameter_px": float,
            }
    """
    img = array.astype(np.float64)

    # Threshold
    if method == "otsu":
        threshold = _otsu_threshold(img)
    elif method == "mean":
        threshold = img.mean()
    else:
        raise ValueError(f"Unknown segmentation method: {method!r}")

    binary = img > threshold

    if fill_holes:
        binary = ndimage.binary_fill_holes(binary)

    # Label
    labels, n_objects = ndimage.label(binary)

    # Filter by size
    sizes = ndimage.sum(binary, labels, range(1, n_objects + 1))
    keep = set()
    for lbl, sz in enumerate(sizes, start=1):
        if sz < min_size_px:
            continue
        if max_size_px is not None and sz > max_size_px:
            continue
        keep.add(lbl)

    mask_keep = np.isin(labels, list(keep))
    labels_filtered, _ = ndimage.label(mask_keep)

    # Metrics
    metrics = []
    n_final = labels_filtered.max()
    for lbl in range(1, n_final + 1):
        obj_mask = labels_filtered == lbl
        area = int(obj_mask.sum())
        rows, cols = np.where(obj_mask)
        centroid_r = float(rows.mean())
        centroid_c = float(cols.mean())
        bbox = [int(rows.min()), int(cols.min()), int(rows.max()), int(cols.max())]
        equiv_diam = float(np.sqrt(4 * area / np.pi))
        metrics.append({
            "label": lbl,
            "area_px": area,
            "centroid_row": centroid_r,
            "centroid_col": centroid_c,
            "bbox": bbox,
            "equiv_diameter_px": equiv_diam,
        })

    return labels_filtered.astype(np.int32), metrics


def _otsu_threshold(img: np.ndarray) -> float:
    """Compute Otsu's threshold for a float image."""
    hist, bin_edges = np.histogram(img.ravel(), bins=256)
    bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
    hist = hist.astype(np.float64)
    total = hist.sum()
    if total == 0:
        return 0.0

    w0 = np.cumsum(hist) / total
    w1 = 1.0 - w0

    mu0 = np.cumsum(hist * bin_centers) / (np.cumsum(hist) + 1e-12)
    mu_total = (hist * bin_centers).sum() / total
    mu1 = np.where(w1 > 0, (mu_total - w0 * mu0) / (w1 + 1e-12), 0.0)

    sigma_b = w0 * w1 * (mu0 - mu1) ** 2
    idx = int(np.argmax(sigma_b))
    return float(bin_centers[idx])
