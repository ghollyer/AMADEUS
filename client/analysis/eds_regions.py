"""EDS SI analysis by region, using the DF image acquired with the SI.

Answers "what's in the particles, what's around them, what's background"
without hand-drawn ROIs:
  * :func:`region_masks`: particle / halo (a ring just outside) / far (well
    away from any particle), all from the SI's own DF image.
  * :func:`identify_peaks`: peaks in a region's summed spectrum, each with
    the nearest tabulated lines, including secondary lines (Kb, Lb, Lg, Mg,
    Ll) and Si-detector escape peaks.
  * :func:`edge_profile`: background-subtracted counts per pixel for each
    element vs distance from the particle edge. A shell element (Si, O)
    persists past the point where the core element (Au) drops to zero.
  * :func:`ratio_to_reference`: element ratio on particles after removing
    the far-field (off-particle) level. A ratio that changes with distance
    to the grid bar marks a spurious signal (Cu on 2026-11-12).

Cubes are numpy arrays with the energy axis at *energy_axis*. The other two
axes must match the DF image's (rows, cols).
"""

from __future__ import annotations

import numpy as np
from scipy import ndimage as ndi
from scipy.signal import find_peaks

from client.analysis.eds_si import EDS_LINES, extract_elemental_map

# Secondary lines worth recognising in a survey spectrum (keV).
EXTRA_LINES: dict[str, dict[str, float]] = {
    "Au": {"Lb": 11.442, "Lg": 13.381, "Mg": 2.409, "Ll": 8.494},
    "Pt": {"Lb": 11.071, "Lg": 12.942, "Mg": 2.333},
    "Ag": {"Lb": 3.151, "Lg": 3.348},
    "Pd": {"Lb": 2.990},
    "Cu": {"Kb": 8.905, "Ll": 0.811},
    "Fe": {"Kb": 7.058},
    "Co": {"Kb": 7.649},
    "Ni": {"Kb": 8.265},
    "Zn": {"Kb": 9.572},
    "Ti": {"Kb": 4.932},
    "Ga": {"Kb": 10.264},
    "Ge": {"Kb": 10.982},
    "Mo": {"Lb": 2.395},
    "W": {"Lb": 9.672},
    "Pb": {"Lb": 12.614},
}
SI_ESCAPE_KEV = 1.740      # Si Ka: escape peaks sit at parent - 1.74 keV


def all_lines() -> list[tuple[str, float]]:
    """("Au Ma", 2.122), ... for every tabulated line."""
    out = []
    for table in (EDS_LINES, EXTRA_LINES):
        for el, lines in table.items():
            out.extend((f"{el} {name}", float(e)) for name, e in lines.items())
    return sorted(out, key=lambda t: t[1])


def _spatial(cube: np.ndarray, energy_axis: int) -> np.ndarray:
    """Cube as (E, rows, cols)."""
    return np.moveaxis(np.asarray(cube), energy_axis, 0)


def region_masks(df: np.ndarray, threshold_frac: float = 0.4, halo_px: int = 2,
                 far_px: int = 4, smooth_px: float = 0.7) -> dict:
    """particle / halo / far boolean masks from the SI's DF image."""
    d = ndi.gaussian_filter(np.asarray(df, dtype=float), smooth_px)
    bg, pk = np.percentile(d, 10), np.percentile(d, 99)
    particle = d > bg + threshold_frac * (pk - bg)
    halo = ndi.binary_dilation(particle, iterations=halo_px) & ~particle
    far = ~ndi.binary_dilation(particle, iterations=far_px)
    return {"particle": particle, "halo": halo, "far": far}


def _find_peaks(s: np.ndarray, e: np.ndarray, prominence_sigma: float, min_keV: float):
    smooth = ndi.gaussian_filter1d(s, 1.5)
    band = (e > 0.5) & (e < 15)
    level = float(np.median(smooth[band])) if np.any(band) else 0.0
    noise = np.sqrt(max(level, 0.0) + 1.0)
    idx, _ = find_peaks(smooth, prominence=max(5.0, prominence_sigma * noise), distance=4)
    return smooth, [i for i in idx if min_keV < e[i] < e[-1] - 0.2]


def estimate_energy_calibration(peak_keV: list[float], heights: list[float],
                                search_offset_keV: float = 0.12, search_gain: float = 0.012,
                                sigma_keV: float = 0.012) -> dict:
    """Linear detector calibration error, ``observed = true * (1 + gain) +
    offset``, that best aligns the peaks with tabulated lines. Each peak
    scores ``sqrt(height) * exp(-(d / sigma)^2)`` against its nearest line,
    with a coarse grid then a fine one. On the F200 on 2026-11-12 the fit gave
    gain -0.4 %: C Ka read ~1 eV low, Au La ~40 eV low."""
    lines = np.array([v for _, v in all_lines()])
    obs = np.asarray(peak_keV, dtype=float)
    w = np.sqrt(np.maximum(np.asarray(heights, dtype=float), 0.0))

    def score(off, gain):
        true = (obs - off) / (1.0 + gain)
        d = np.min(np.abs(true[:, None] - lines[None, :]), axis=1)
        return float((w * np.exp(-(d / sigma_keV) ** 2)).sum()) - 1e-3 * (abs(off) + 10 * abs(gain))

    def search(offsets, gains):
        return max(((score(o, g), o, g) for g in gains for o in offsets))

    _, off, gain = search(np.arange(-search_offset_keV, search_offset_keV + 1e-9, 0.005),
                          np.arange(-search_gain, search_gain + 1e-12, 0.001))
    _, off, gain = search(np.arange(off - 0.005, off + 0.0051, 0.001),
                          np.arange(gain - 0.001, gain + 0.00101, 0.0001))
    return {"offset_keV": round(float(off), 3), "gain": round(float(gain), 4)}


def _subchannel(smooth: np.ndarray, e: np.ndarray, i: int) -> float:
    """Peak centre by parabola through the three channels around *i*."""
    if 0 < i < len(smooth) - 1:
        y0, y1, y2 = smooth[i - 1], smooth[i], smooth[i + 1]
        denom = y0 - 2 * y1 + y2
        if denom < 0:
            frac = 0.5 * (y0 - y2) / denom
            return float(e[i] + frac * (e[i + 1] - e[i]))
    return float(e[i])


_PRINCIPAL = ("Ka", "La", "Ma")


def identify_peaks(spectrum: np.ndarray, energies_keV: np.ndarray, max_peaks: int = 15,
                   min_keV: float = 0.15, n_candidates: int = 3,
                   prominence_sigma: float = 3.0,
                   calibration: dict | None = None) -> dict:
    """Peaks in a summed spectrum, strongest first, with likely line IDs.

    The detector's linear calibration error (``{"offset_keV", "gain"}``) is
    estimated from the peaks (or pass *calibration*) and removed before
    matching. Candidates within 60 eV of a peak are ranked by line family:
    +1 for each of the element's lines that is present, -1.5 for each
    *principal* line (Ka/La/Ma) in range that is absent. So Au La beats W Lb
    when Au Ma and Au Lb are present and W La is absent. Each candidate lists
    its family's seen and missing lines.
    Returns ``{"calibration", "peaks"}``."""
    s = np.asarray(spectrum, dtype=float)
    e = np.asarray(energies_keV, dtype=float)
    smooth, idx = _find_peaks(s, e, prominence_sigma, min_keV)
    base = ndi.minimum_filter1d(ndi.gaussian_filter1d(s, 8), 40)
    idx = sorted(idx, key=lambda i: -smooth[i])[:max_peaks]
    centres = [_subchannel(smooth, e, i) for i in idx]
    if calibration is None:
        calibration = (estimate_energy_calibration(centres, [smooth[i] for i in idx])
                       if len(idx) >= 3 else {"offset_keV": 0.0, "gain": 0.0})
    off, gain = float(calibration["offset_keV"]), float(calibration["gain"])
    lines = all_lines()
    corrected = [float((c - off) / (1.0 + gain)) for c in centres]

    e_lo, e_hi = min_keV, float(e[-1]) - 0.2

    def family(element: str) -> dict:
        """Lines of *element* in range, split into seen/missing among the peaks."""
        seen, missing = [], []
        for name, v in lines:
            if name.split()[0] == element and e_lo < v < e_hi:
                if any(abs(c - v) < 0.03 for c in corrected):
                    seen.append(name)
                elif name.split()[1] in _PRINCIPAL:
                    missing.append(name)
        return {"seen": seen, "missing": missing,
                "score": len(seen) - 1.5 * len(missing)}

    peaks = []
    for i, c in zip(idx, corrected):
        lo, hi = max(0, i - 6), min(len(s), i + 7)
        near = [(n, v) for n, v in lines if abs(v - c) < 0.06] or \
            sorted(lines, key=lambda t: abs(t[1] - c))[:1]
        fams = {n: family(n.split()[0]) for n, _ in near}
        near.sort(key=lambda t: (-fams[t[0]]["score"], abs(t[1] - c)))
        entry = {"energy_keV": round(float(e[i]), 3),
                 "height": round(float(smooth[i]), 1),
                 "net_counts": round(float(s[lo:hi].sum() - base[lo:hi].sum()), 1),
                 "candidates": [{"line": n, "keV": v, "delta_keV": round(c - v, 3),
                                 "family_seen": fams[n]["seen"],
                                 "family_missing": fams[n]["missing"]}
                                for n, v in near[:n_candidates]]}
        # A Si escape peak needs its parent present and at least ~20x stronger.
        esc = [n for n, v in lines if abs((v - SI_ESCAPE_KEV) - c) < 0.03
               and any(abs(c2 - v) < 0.03 and smooth[i2] > 20 * smooth[i]
                       for i2, c2 in zip(idx, corrected))]
        if esc:
            entry["possible_escape_of"] = esc[:3]
        peaks.append(entry)
    return {"calibration": calibration, "peaks": peaks}


def region_spectra(cube: np.ndarray, energy_axis: int, masks: dict) -> dict:
    """Summed spectrum per region mask."""
    c = _spatial(cube, energy_axis)
    out = {}
    for name, m in masks.items():
        if m.shape != c.shape[1:]:
            raise ValueError(f"mask {name!r} {m.shape} doesn't match SI {c.shape[1:]}")
        out[name] = c[:, m].sum(axis=1).astype(float)
    return out


def element_maps(cube: np.ndarray, energy_axis: int, energies_keV: np.ndarray,
                 lines: dict[str, float], window_keV: float = 0.09,
                 energy_offset_keV: float = 0.0, energy_gain: float = 0.0) -> dict[str, np.ndarray]:
    """Background-subtracted net-count maps, {label: 2-D map}. Each line is
    integrated where the detector shows it: ``keV * (1 + gain) + offset``
    (take both from eds_region_spectra's calibration)."""
    maps = {}
    for label, kev in lines.items():
        res = extract_elemental_map(cube, energy_axis, np.asarray(energies_keV, dtype=float),
                                    float(kev) * (1.0 + energy_gain) + energy_offset_keV,
                                    window_keV=window_keV,
                                    bg_offset_keV=window_keV + 0.175, bg_width_keV=0.075,
                                    element=label)
        maps[label] = np.asarray(res.map, dtype=float)
    return maps


def edge_profile(maps: dict[str, np.ndarray], df: np.ndarray, px_nm: float,
                 bin_nm: float | None = None, inside_nm: float = 18.0,
                 outside_nm: float = 48.0, threshold_frac: float = 0.5) -> dict:
    """Mean net counts per pixel for each map, binned by signed distance from
    the particle edge (negative = inside), with the edge taken from the DF
    image."""
    d = ndi.gaussian_filter(np.asarray(df, dtype=float), 0.7)
    bg, pk = np.percentile(d, 10), np.percentile(d, 99)
    rod = d > bg + threshold_frac * (pk - bg)
    if not rod.any():
        raise ValueError("no particles in the DF image above the threshold")
    dist = ndi.distance_transform_edt(~rod) * px_nm - ndi.distance_transform_edt(rod) * px_nm
    step = float(bin_nm) if bin_nm else px_nm
    edges = np.arange(-inside_nm, outside_nm + 1e-9, step)
    bins = []
    for lo in edges:
        sel = (dist >= lo) & (dist < lo + step)
        if sel.sum() < 10:
            continue
        bins.append({"from_nm": round(float(lo), 2), "to_nm": round(float(lo + step), 2),
                     "n_px": int(sel.sum()),
                     **{k: round(float(m[sel].mean()), 4) for k, m in maps.items()}})
    return {"px_nm": px_nm, "bin_nm": step, "bins": bins,
            "note": "Pixel size and beam spread blur the edge by about one bin. Where a "
                    "shell element persists after the core element has reached ~0 gives "
                    "the shell thickness."}


def ratio_to_reference(maps: dict[str, np.ndarray], masks: dict, numerator: str,
                       denominator: str) -> dict:
    """``(num_particle - num_far) / (den_particle - den_far)``, per-pixel means.
    Removing the far-field level separates a signal that rides on the
    particles from a uniform background."""
    p, f = masks["particle"], masks["far"]
    num = float(maps[numerator][p].mean() - maps[numerator][f].mean())
    den = float(maps[denominator][p].mean() - maps[denominator][f].mean())
    return {"numerator": numerator, "denominator": denominator,
            "particle_minus_far": {numerator: round(num, 4), denominator: round(den, 4)},
            "ratio": round(num / den, 4) if abs(den) > 1e-12 else None,
            "far_level": {numerator: round(float(maps[numerator][f].mean()), 4),
                          denominator: round(float(maps[denominator][f].mean()), 4)}}


# ---------------------------------------------------------------------------
# One-call element mapping (eds_map_elements)
# ---------------------------------------------------------------------------

def elements_from_peaks(peaks: list[dict], max_elements: int = 6,
                        exclude: list[str] | None = None) -> list[str]:
    """Elements behind the strongest peaks (each peak's best-ranked
    candidate), strongest first, skipping likely Si escape peaks."""
    skip = {e.strip() for e in (exclude or [])}
    out: list[str] = []
    for p in sorted(peaks, key=lambda p: -p["height"]):
        if p.get("possible_escape_of") or not p.get("candidates"):
            continue
        el = p["candidates"][0]["line"].split()[0]
        if el not in out and el not in skip:
            out.append(el)
        if len(out) >= max_elements:
            break
    return out


def line_net_counts(spectrum: np.ndarray, energies_keV: np.ndarray, line_keV: float,
                    window_keV: float = 0.09, energy_offset_keV: float = 0.0,
                    energy_gain: float = 0.0) -> tuple[float, float]:
    """(net counts, gross counts) of one line in a summed spectrum, with the
    same window and side-bands as :func:`element_maps`."""
    res = extract_elemental_map(np.asarray(spectrum, dtype=float)[:, None, None], 0,
                                np.asarray(energies_keV, dtype=float),
                                float(line_keV) * (1.0 + energy_gain) + energy_offset_keV,
                                window_keV=window_keV, bg_offset_keV=window_keV + 0.175,
                                bg_width_keV=0.075)
    return float(res.total_counts), float(res.total_counts + res.background_counts)


def resolve_line(spec: str, spectrum: np.ndarray, energies_keV: np.ndarray,
                 calibration: dict, window_keV: float = 0.09) -> tuple[str, float]:
    """``"Au Ma"`` -> itself; a bare ``"Au"`` -> whichever of its principal
    lines (Ka/La/Ma) in range has the most net counts in *spectrum*."""
    table = dict(all_lines())
    label = spec.strip()
    if label in table:
        return label, table[label]
    element = label.split()[0]
    e = np.asarray(energies_keV, dtype=float)
    lo, hi = max(0.15, float(e[0]) + 0.1), float(e[-1]) - 0.1
    options = [(f"{element} {ln}", table[f"{element} {ln}"]) for ln in _PRINCIPAL
               if f"{element} {ln}" in table and lo < table[f"{element} {ln}"] < hi]
    if not options:
        raise ValueError(f"no tabulated Ka/La/Ma line for {spec!r} inside the "
                         f"spectrum's range ({e[0]:.2f}-{e[-1]:.2f} keV); "
                         "give it as 'El Line'")
    off, gain = float(calibration.get("offset_keV", 0.0)), float(calibration.get("gain", 0.0))
    return max(options, key=lambda o: line_net_counts(spectrum, e, o[1], window_keV,
                                                      off, gain)[0])


def map_correlations(maps: dict[str, np.ndarray], smooth_px: float = 1.0) -> dict:
    """Pearson correlation between every pair of (smoothed) maps. Positive =
    the elements sit together, ~0 = unrelated, negative = they avoid each
    other (e.g. core vs shell)."""
    keys = list(maps)
    flat = {k: ndi.gaussian_filter(np.asarray(maps[k], dtype=float), smooth_px).ravel()
            for k in keys}
    out: dict = {}
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            x, y = flat[a], flat[b]
            if x.std() == 0 or y.std() == 0:
                r = None
            else:
                r = round(float(np.corrcoef(x, y)[0, 1]), 3)
            out[f"{a} / {b}"] = r
    return out


def overlapping_lines(lines: dict[str, float], tolerance_keV: float = 0.12) -> list[str]:
    """Pairs of chosen lines too close to separate at EDS resolution."""
    items = sorted(lines.items(), key=lambda t: t[1])
    return [f"{a} ({ea:.3f} keV) and {b} ({eb:.3f} keV) overlap; their maps mix"
            for (a, ea), (b, eb) in zip(items, items[1:]) if eb - ea < tolerance_keV]


def clean_sidebands(center_keV: float, window_keV: float, avoid_keV: list[float],
                    e_lo: float, e_hi: float, band_keV: float = 0.075,
                    clearance_keV: float = 0.12, reach_keV: float = 0.7
                    ) -> tuple[float | None, float | None]:
    """Background side-band centres (left, right) nearest the line that stay
    *clearance_keV* clear of every other peak in *avoid_keV* and inside the
    axis; None for a side with no clean spot within *reach_keV*. Fixed
    offsets don't work in a crowded spectrum: O Ka's left band lands on C Ka."""
    def ok(c: float) -> bool:
        return (e_lo <= c - band_keV and c + band_keV <= e_hi
                and all(abs(c - p) >= band_keV + clearance_keV for p in avoid_keV))

    out = []
    for sign in (-1.0, 1.0):
        d, found = window_keV + band_keV + 0.02, None
        while d <= reach_keV:
            if ok(center_keV + sign * d):
                found = round(center_keV + sign * d, 4)
                break
            d += 0.01
        out.append(found)
    return out[0], out[1]


def net_line_map(cube: np.ndarray, energy_axis: int, energies_keV: np.ndarray,
                 center_keV: float, window_keV: float, left_keV: float | None,
                 right_keV: float | None, band_keV: float = 0.075) -> dict:
    """Net counts per pixel in ``center +/- window`` minus a background
    interpolated linearly between the side-bands (one band = flat; none =
    no subtraction). Not clipped at zero, so noise averages out instead of
    biasing weak maps upward. *center_keV* is in detector (observed) keV."""
    c = _spatial(cube, energy_axis)
    e = np.asarray(energies_keV, dtype=float)
    win = (e >= center_keV - window_keV) & (e <= center_keV + window_keV)
    if not win.any():
        raise ValueError(f"window {center_keV:.3f} +/- {window_keV} keV is off the axis")
    gross = c[win].astype(float).sum(axis=0)
    pts = []
    for b in (left_keV, right_keV):
        if b is not None:
            m = (e >= b - band_keV) & (e <= b + band_keV)
            if m.any():
                pts.append((b, c[m].astype(float).mean(axis=0)))
    n = int(win.sum())
    if len(pts) == 2:
        (x0, y0), (x1, y1) = pts
        bg = (y0 + (y1 - y0) * (center_keV - x0) / (x1 - x0)) * n
    elif pts:
        bg = pts[0][1] * n
    else:
        bg = np.zeros_like(gross)
    return {"net": gross - bg, "gross_total": float(gross.sum()),
            "background_total": float(bg.sum())}
