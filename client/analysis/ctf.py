"""Defocus (C1) and twofold astigmatism (A1) from Thon rings in an FFT.

Works on TEM images of a thin amorphous region (carbon film, an amorphous
shell or edge). Crystalline lattice fringes and STEM images don't carry Thon
rings, so the fit has nothing to lock onto there (low ``score``).

Model (CTFFIND convention, defocus ``df`` positive = underfocus)::

    chi(q, phi) = pi * lambda * q^2 * (df + A1 * cos(2 * (phi - phi_A)))
                  - pi / 2 * Cs * lambda^3 * q^4
    |CTF|^2     = sin^2(chi + psi),  psi = arcsin(amplitude contrast)

The image's power spectrum is averaged over overlapping tiles, its smooth
background is removed, and each radius is normalised so faint high-q rings
count. ``df`` is found first by a dense 1-D search on the rotational average,
then (df, A1, phi_A) by a coarse grid and Nelder-Mead on the 2-D spectrum,
maximising the correlation with ``-cos(2 (chi + psi))``.

|CTF|^2 hardly depends on the sign of df: only the Cs term breaks the
symmetry. The fit is repeated from the mirrored defocus and the sign is
reported as ambiguous unless one side wins clearly.
"""

from __future__ import annotations

import numpy as np
from scipy import ndimage as ndi
from scipy.optimize import minimize

_M0C2_EV = 510_998.95


def electron_wavelength_nm(voltage_kv: float) -> float:
    """Relativistic electron wavelength (nm). 200 kV -> 0.002508 nm."""
    v = float(voltage_kv) * 1e3
    return 1.226426 / np.sqrt(v * (1.0 + v / (2.0 * _M0C2_EV)))


def averaged_spectrum(image: np.ndarray, tile: int = 512) -> np.ndarray:
    """Zero-centred power spectrum averaged over half-overlapping, Hann-
    windowed tiles (Welch periodogram). Returns a (tile, tile) array."""
    a = np.asarray(image, dtype=np.float64)
    if a.ndim != 2:
        raise ValueError(f"need a 2-D image, got shape {a.shape}")
    n = 1 << int(np.floor(np.log2(min(a.shape))))
    tile = int(min(tile, n))
    if tile < 64:
        raise ValueError(f"image {a.shape} is too small for a CTF fit (need >= 64 px)")
    win = np.outer(np.hanning(tile), np.hanning(tile))
    step = tile // 2
    acc = np.zeros((tile, tile))
    count = 0
    for r in range(0, a.shape[0] - tile + 1, step):
        for c in range(0, a.shape[1] - tile + 1, step):
            t = a[r:r + tile, c:c + tile]
            acc += np.abs(np.fft.fft2((t - t.mean()) * win)) ** 2
            count += 1
    return np.fft.fftshift(acc / max(count, 1))


def _normalised(ps: np.ndarray) -> np.ndarray:
    """Amplitude spectrum minus its smooth background, each radius scaled to
    unit RMS (so the decaying envelope doesn't let low q dominate)."""
    amp = np.sqrt(ps)
    n = amp.shape[0]
    box = max(7, n // 24) | 1
    resid = amp - ndi.uniform_filter(amp, box, mode="nearest")
    yy, xx = np.indices(amp.shape)
    r = np.hypot(yy - n // 2, xx - n // 2).astype(int)
    rms = np.sqrt(np.bincount(r.ravel(), (resid ** 2).ravel())
                  / np.maximum(np.bincount(r.ravel()), 1))
    rms = ndi.uniform_filter1d(rms, 5)
    return resid / (rms[r] + 1e-12)


def _significance(score: float, n_px: int) -> float:
    """Correlation in units of its noise level. Neighbouring FFT pixels of a
    Hann-windowed spectrum are correlated, so ~1 independent sample per 4 px.
    Noise-only spectra give < ~3 after the search; real rings give >> 5."""
    return float(score * np.sqrt(max(n_px, 1) / 4.0))


def _corr(model: np.ndarray, data_z: np.ndarray) -> float:
    m = model - model.mean()
    nrm = np.sqrt((m * m).sum())
    return float((m * data_z).sum() / nrm) if nrm > 0 else 0.0


def fit_ctf(image: np.ndarray, nm_per_px: float, voltage_kv: float = 200.0,
            cs_mm: float = 1.0, amplitude_contrast: float = 0.07,
            q_min_inv_nm: float | None = None, q_max_inv_nm: float | None = None,
            defocus_max_nm: float = 5000.0, tile: int = 512) -> dict:
    """Fit defocus and twofold astigmatism to the image's Thon rings.

    Returns ``defocus_nm`` (positive = underfocus), ``C1_nm`` (= -defocus,
    aberration convention: negative = underfocus), ``A1_nm`` (half the
    defocus difference between the two principal axes), ``A1_angle_deg``
    (axis of maximum underfocus, from image +x toward +y with y down),
    ``score`` (correlation of model and data), ``significance`` (score over
    its noise level) and ``reliable`` (significance >= 5 and >= 2 rings),
    ``sign_ambiguous``, ``n_rings`` and the q range used."""
    if nm_per_px <= 0:
        raise ValueError("nm_per_px must be positive")
    lam = electron_wavelength_nm(voltage_kv)
    cs_nm = float(cs_mm) * 1e6
    psi = float(np.arcsin(np.clip(amplitude_contrast, 0.0, 0.5)))

    ps = averaged_spectrum(image, tile)
    n = ps.shape[0]
    data = _normalised(ps)
    dq = 1.0 / (n * nm_per_px)                         # 1/nm per FFT pixel
    nyq = 0.5 / nm_per_px
    q_lo = float(q_min_inv_nm) if q_min_inv_nm else max(4 * dq, 0.02 * nyq)
    q_hi = float(q_max_inv_nm) if q_max_inv_nm else 0.8 * nyq
    q_hi = min(q_hi, (n // 2 - 2) * dq)
    if q_hi <= q_lo * 1.5:
        raise ValueError(f"q range [{q_lo:.3g}, {q_hi:.3g}] 1/nm is too narrow")

    yy, xx = np.indices((n, n))
    qy, qx = (yy - n // 2) * dq, (xx - n // 2) * dq
    q = np.hypot(qx, qy)
    half = yy <= n // 2                               # the spectrum is centrosymmetric

    def pixels(q_max):
        sel = half & (q >= q_lo) & (q <= q_max)
        dz = data[sel] - data[sel].mean()
        return q[sel], np.arctan2(qy[sel], qx[sel]), dz / (np.sqrt((dz * dz).sum()) + 1e-12)

    def chi(q2, df):
        return np.pi * lam * q2 * df - 0.5 * np.pi * cs_nm * lam ** 3 * q2 ** 2

    # 1) Dense 1-D defocus search on the rotational average.
    qs, _, d = pixels(q_hi)
    rbin = (qs / dq).astype(int)
    counts = np.bincount(rbin)
    keep = counts > 0
    w = np.sqrt(counts[keep])
    pz = (np.bincount(rbin, d) / np.maximum(counts, 1))[keep] * w
    pz -= pz.mean()
    qb2 = ((np.arange(len(counts)) + 0.5) * dq)[keep] ** 2
    step = 1.0 / (6 * lam * q_hi ** 2)                 # <= pi/3 phase change at q_hi
    grid = np.arange(-defocus_max_nm, defocus_max_nm + step, step)
    scores1 = np.empty(len(grid))
    for k in range(0, len(grid), 2048):               # chunked: bounded memory
        g = grid[k:k + 2048]
        m1 = -np.cos(2 * (chi(qb2[None, :], g[:, None]) + psi)) * w
        m1 -= m1.mean(axis=1, keepdims=True)
        scores1[k:k + 2048] = (m1 @ pz) / (np.linalg.norm(m1, axis=1)
                                           * np.linalg.norm(pz) + 1e-12)
    starts: list[float] = []
    for i in np.argsort(-scores1):                    # distinct local maxima, best first
        if all(abs(grid[i] - s) > 4 * step for s in starts):
            starts.append(float(grid[i]))
        if len(starts) == 3:
            break
    starts.append(-starts[0])

    # 2) Fit (df, A1, angle) in 2-D, only out to where the rings are still
    # resolved (period >= ~3 FFT pixels); beyond that they alias into noise.
    def q_resolved(df):
        return min(q_hi, max(2.5 * q_lo, 1.0 / (6 * lam * max(abs(df), 1.0) * dq)))

    def fit_from(s):
        q_fit = q_resolved(s * 1.25)
        qf, phi, d2 = pixels(q_fit)
        q2 = qf ** 2
        st = 1.0 / (6 * lam * q_fit ** 2)

        def corr(df, a1, th):
            return _corr(-np.cos(2 * (chi(q2, df + a1 * np.cos(2 * (phi - th))) + psi)), d2)

        best = (-np.inf, s, 0.0, 0.0)
        a1_grid = np.linspace(0, max(4 * st, 0.25 * abs(s)), 6)
        for df in (s - st, s, s + st):
            for a1 in a1_grid:
                for th in (np.deg2rad(np.arange(0, 180, 15)) if a1 > 0 else [0.0]):
                    sc = corr(df, a1, th)
                    if sc > best[0]:
                        best = (sc, df, a1, th)
        res = minimize(lambda p: -corr(p[0], abs(p[1]), p[2]),
                       x0=[best[1], max(best[2], st / 4), best[3]], method="Nelder-Mead",
                       options={"xatol": 0.02 * st, "fatol": 1e-5, "maxiter": 800})
        return (-float(res.fun), float(res.x[0]), abs(float(res.x[1])), float(res.x[2]),
                q_fit, int(qf.size))

    # Rank by significance, not raw correlation: a large-defocus start fits a
    # small q range, where a few hundred noise pixels can correlate by chance.
    fits = sorted((fit_from(s) for s in starts), key=lambda f: -_significance(f[0], f[5]))
    score, df, a1, th, q_fit, n_px = fits[0]
    mirrored = [f for f in fits if np.sign(f[1]) != np.sign(df)]
    alt = mirrored[0][0] if mirrored else -np.inf
    sign_ambiguous = bool(score - alt < max(0.01, 0.05 * abs(score)))

    angle = float(np.rad2deg(th)) % 180.0          # where df + A1 cos(...) peaks
    chi_max = abs(np.pi * lam * q_fit ** 2 * (abs(df) + a1))
    return {
        "defocus_nm": round(df, 2),
        "C1_nm": round(-df, 2),
        "A1_nm": round(a1, 2),
        "A1_angle_deg": round(angle, 1),
        "defocus_range_nm": [round(df - a1, 2), round(df + a1, 2)],
        "score": round(score, 4),
        "significance": round(_significance(score, n_px), 1),
        "reliable": bool(_significance(score, n_px) >= 5.0 and chi_max >= 2 * np.pi),
        "sign_ambiguous": sign_ambiguous,
        "score_mirrored_sign": round(alt, 4) if np.isfinite(alt) else None,
        "n_rings": int(chi_max // np.pi),
        "q_range_inv_nm": [round(q_lo, 4), round(q_fit, 4)],
        "wavelength_nm": round(lam, 6),
        "cs_mm": cs_mm,
        "tile_px": n,
        "n_px": n_px,
    }
