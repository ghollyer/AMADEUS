"""MCP tools — EDS acquisition + analysis of stored SI cubes.

The acquisition tools (``eds_acquire_point``, ``eds_acquire_map``)
trigger DM and leave the data on the microscope side. The analysis
tools below operate on SI cubes that have already been pulled into the
local image store via ``fetch_remote_file`` (or saved by some other path).

The energy axis of an SI cube is identified by reading the per-axis
calibration stashed in the sidecar's ``tags`` dict at fetch time (see
``client/tools/files.py``).
"""

from __future__ import annotations

import numpy as np
from mcp.server.fastmcp import FastMCP

from client.analysis import eds_regions as _regions
from client.analysis import eds_si as _eds_si
from client.analysis.units import nm_per_px
from client.state.manager import StateManager
from client.state.models import ImageCalibration
from client.transport.zmq_client import ZMQClient


def _load_si_cube(state: StateManager, image_id: str):
    """Return (cube, sidecar, energy numpy axis of cube, energies_keV)."""
    arr, sidecar = state.images.load(image_id)
    if arr.ndim != 3:
        raise ValueError(
            f"Image {image_id!r} is {arr.ndim}-D ({arr.shape}); EDS analysis "
            f"requires a 3-D SI cube."
        )
    axes = sidecar.tags.get("axes") or []
    energy_axis = sidecar.tags.get("energy_axis_index")
    if energy_axis is None:
        # Fall back to whichever axis declares eV/keV units.
        for d in axes:
            u = str(d.get("units", "")).strip().lower()
            if u in ("ev", "kev"):
                energy_axis = int(d["index"])
                break
    if energy_axis is None:
        raise ValueError(
            f"Image {image_id!r} has no energy-calibrated axis. The DM file "
            f"must be an SI cube (one axis with units of eV or keV)."
        )
    cal = next((d for d in axes if int(d["index"]) == int(energy_axis)), None)
    if cal is None:
        raise ValueError(
            f"Image {image_id!r} has energy_axis_index={energy_axis} but no "
            f"matching entry in the per-axis calibration table."
        )
    energies = _eds_si.energy_axis_values(
        n_channels=int(cal["size"]),
        origin=float(cal["origin"]),
        scale=float(cal["scale"]),
        units=str(cal["units"]),
    )
    return arr, sidecar, _numpy_axis(arr, int(energy_axis), int(cal["size"])), energies


def _numpy_axis(arr: np.ndarray, dm_index: int, size: int) -> int:
    """Map a DM dimension index to the numpy axis of *arr*.

    ``energy_axis_index`` and ``axes[*].index`` are DM dimension indices
    (dim 0 = x). DM's ``GetNumArray()`` returns them in reverse (C) order —
    an x, y, E cube arrives as numpy ``(E, y, x)`` — so DM dim *i* is numpy
    axis ``ndim - 1 - i``. Fall back to the unreversed index
    only if the reversed axis has the wrong length and that one fits.
    """
    reversed_axis = arr.ndim - 1 - dm_index
    if arr.shape[reversed_axis] == size:
        return reversed_axis
    if 0 <= dm_index < arr.ndim and arr.shape[dm_index] == size:
        return dm_index
    raise ValueError(
        f"energy axis (DM dim {dm_index}, {size} channels) matches no axis of "
        f"the stored array {arr.shape}"
    )


def _spatial_calibration(sidecar, energy_axis: int | None = None) -> ImageCalibration:
    """Build an ImageCalibration for the two spatial axes of an SI cube.

    The spatial axes are the non-energy entries of the DM ``axes`` table
    (DM order: x first, then y). *energy_axis* is unused — kept for callers.
    """
    axes = sidecar.tags.get("axes") or []
    spatial = [
        d for d in axes
        if str(d.get("units", "")).strip().lower() not in ("ev", "kev")
    ]
    if len(spatial) < 2:
        # Sidecar's top-level calibration already covers the spatial axes
        # for everything we acquired locally, so just reuse it.
        return sidecar.calibration
    return ImageCalibration(
        x_scale=float(spatial[0]["scale"]),
        y_scale=float(spatial[1]["scale"]),
        units=str(spatial[0]["units"] or "nm"),
    )


def register(mcp: FastMCP, zmq: ZMQClient, state: StateManager) -> None:

    # ---------------------------------------------------------------
    # Acquisition (data stays in DM)
    # ---------------------------------------------------------------

    @mcp.tool()
    async def eds_acquire_point(exposure_s: float = 30.0) -> dict:
        """Acquire an EDS point spectrum at the current beam/stage position.

        The spectrum is collected by the EDS detector and stored in DM.
        Data stays in Digital Micrograph — use DM's EDS analysis tools to
        process the resulting spectrum, or save the file and pull it
        with ``fetch_remote_file`` for client-side analysis.

        Args:
            exposure_s: Live-time in seconds (default 30 s).
        """
        return await zmq.send_command("eds_acquire_point", {"exposure_s": exposure_s})

    @mcp.tool()
    async def eds_acquire_map(
        x_pixels: int = 64,
        y_pixels: int = 64,
        exposure_s: float = 0.05,
    ) -> dict:
        """Acquire an EDS map (spectrum image) over the current scan area.

        The map is collected as a spectrum image and stored in DM. To
        produce client-side elemental maps, find the SI with
        ``list_dm_windows``, save and pull it with
        ``save_dm_window(path=..., doc_id=..., fetch=True)``, then call
        ``eds_map_elements``.

        Args:
            x_pixels: Number of scan pixels in X (default 64).
            y_pixels: Number of scan pixels in Y (default 64).
            exposure_s: Per-pixel live-time in seconds (default 0.05 s).
        """
        return await zmq.send_command("eds_acquire_map", {
            "x_pixels":   x_pixels,
            "y_pixels":   y_pixels,
            "exposure_s": exposure_s,
        })

    # ---------------------------------------------------------------
    # Analysis of stored SI cubes
    # ---------------------------------------------------------------

    @mcp.tool()
    async def eds_describe_si(image_id: str) -> dict:
        """Summarise the energy axis of a stored EDS SI cube.

        Args:
            image_id: ID of a 3-D SI cube already in the image store
                (e.g. fetched via ``fetch_remote_file``).

        Returns:
            ``shape``, ``energy_axis_index``, ``n_channels``,
            ``energy_min_keV``, ``energy_max_keV``, ``dispersion_eV``,
            and ``spatial_shape`` (the 2-D map dimensions).
        """
        _, sidecar, energy_axis, energies = _load_si_cube(state, image_id)
        spatial_shape = [s for i, s in enumerate(sidecar.shape) if i != energy_axis]
        dispersion_eV = float(np.median(np.diff(energies)) * 1000.0)
        return {
            "image_id":          image_id,
            "shape":             sidecar.shape,
            "energy_axis_index": energy_axis,
            "spatial_shape":     spatial_shape,
            "n_channels":        int(energies.shape[0]),
            "energy_min_keV":    float(energies.min()),
            "energy_max_keV":    float(energies.max()),
            "dispersion_eV":     dispersion_eV,
        }

    @mcp.tool()
    async def eds_extract_elemental_map(
        image_id: str,
        element: str,
        line: str = "Ka",
        line_keV: float | None = None,
        window_keV: float = 0.150,
        bg_offset_keV: float = 0.300,
        bg_width_keV: float = 0.150,
    ) -> dict:
        """Produce a background-subtracted elemental map from a stored SI cube.

        Integrates counts inside ``[line_keV ± window_keV]`` at every
        pixel and subtracts a linear background fit to two side-bands
        offset by ``bg_offset_keV`` on either side of the line. The
        resulting 2-D map is saved as a new image in the local store.

        Args:
            image_id: ID of the source SI cube.
            element: Element symbol (e.g. ``"Fe"``).
            line: Which characteristic line — ``"Ka"`` (default),
                ``"La"``, or ``"Ma"``. Ignored if ``line_keV`` is given.
            line_keV: Override the line energy directly. Use this for
                elements not in the built-in table.
            window_keV: Half-width of the integration window.
            bg_offset_keV: Distance from the line to the centre of each
                background side-band.
            bg_width_keV: Half-width of each background side-band.

        Returns:
            ``map_image_id``, ``element``, ``line``, ``line_keV``,
            ``total_counts``, and ``background_counts``.
        """
        cube, sidecar, energy_axis, energies = _load_si_cube(state, image_id)
        if line_keV is None:
            line_keV = _eds_si.lookup_line_keV(element, line)

        result = _eds_si.extract_elemental_map(
            cube,
            energy_axis=energy_axis,
            energies_keV=energies,
            line_keV=line_keV,
            window_keV=window_keV,
            bg_offset_keV=bg_offset_keV,
            bg_width_keV=bg_width_keV,
            element=element,
            line=line,
        )

        map_sidecar = state.images.save(
            result.map,
            name=f"eds_map_{element}_{line}_{sidecar.name}",
            calibration=_spatial_calibration(sidecar, energy_axis),
            stage_position=sidecar.stage_position,
            tags={
                "type":             "eds_elemental_map",
                "source_image_id":  image_id,
                "element":          element,
                "line":             line,
                "line_keV":         result.line_keV,
                "window_keV":       result.window_keV,
                "bg_offset_keV":    bg_offset_keV,
                "bg_width_keV":     bg_width_keV,
            },
        )
        return {
            "map_image_id":      map_sidecar.image_id,
            "element":           element,
            "line":              line,
            "line_keV":          result.line_keV,
            "shape":             list(result.map.shape),
            "total_counts":      result.total_counts,
            "background_counts": result.background_counts,
        }

    @mcp.tool()
    async def eds_extract_multiple_maps(
        image_id: str,
        elements: list[str],
        line: str = "Ka",
        window_keV: float = 0.150,
    ) -> dict:
        """Convenience wrapper — extract one map per element.

        Args:
            image_id: ID of the source SI cube.
            elements: List of element symbols (e.g. ``["Si", "O", "Fe"]``).
            line: Line to use for all elements (default "Ka"). Per-element
                overrides require separate ``eds_extract_elemental_map``
                calls.
            window_keV: Half-width of the integration window.

        Returns:
            ``maps``: list of one result dict per element, with the same
            schema as ``eds_extract_elemental_map``.
        """
        maps = []
        for element in elements:
            line_keV = _eds_si.lookup_line_keV(element, line)
            cube, sidecar, energy_axis, energies = _load_si_cube(state, image_id)
            result = _eds_si.extract_elemental_map(
                cube,
                energy_axis=energy_axis,
                energies_keV=energies,
                line_keV=line_keV,
                window_keV=window_keV,
                element=element,
                line=line,
            )
            map_sidecar = state.images.save(
                result.map,
                name=f"eds_map_{element}_{line}_{sidecar.name}",
                calibration=_spatial_calibration(sidecar, energy_axis),
                stage_position=sidecar.stage_position,
                tags={
                    "type":            "eds_elemental_map",
                    "source_image_id": image_id,
                    "element":         element,
                    "line":            line,
                    "line_keV":        result.line_keV,
                    "window_keV":      result.window_keV,
                },
            )
            maps.append({
                "map_image_id":      map_sidecar.image_id,
                "element":           element,
                "line":              line,
                "line_keV":          result.line_keV,
                "total_counts":      result.total_counts,
                "background_counts": result.background_counts,
            })
        return {"image_id": image_id, "maps": maps}

    @mcp.tool()
    async def eds_quantify_si(
        image_id: str,
        elements: list[str],
        line: str = "Ka",
        window_keV: float = 0.150,
        k_factors: dict[str, float] | None = None,
    ) -> dict:
        """Cliff-Lorimer quantification over a stored SI cube.

        Sums each element's net counts across the whole map (after
        background subtraction) and applies the closed Cliff-Lorimer
        normalisation to produce weight fractions that sum to 1.

        Args:
            image_id: ID of the source SI cube.
            elements: Elements to include in the quant.
            line: Characteristic line for all elements.
            window_keV: Half-width of the integration window.
            k_factors: Per-element k-factors (relative to Si Kα). Missing
                entries fall back to the built-in approximate table.

        Returns:
            ``intensities`` (raw net counts per element) and
            ``weight_fractions`` (sum to 1 by construction).
        """
        cube, sidecar, energy_axis, energies = _load_si_cube(state, image_id)
        intensities: dict[str, float] = {}
        line_energies: dict[str, float] = {}
        for element in elements:
            line_keV = _eds_si.lookup_line_keV(element, line)
            result = _eds_si.extract_elemental_map(
                cube,
                energy_axis=energy_axis,
                energies_keV=energies,
                line_keV=line_keV,
                window_keV=window_keV,
                element=element,
                line=line,
            )
            intensities[element] = result.total_counts
            line_energies[element] = result.line_keV

        fractions = _eds_si.cliff_lorimer_quantify(intensities, k_factors)
        return {
            "image_id":         image_id,
            "line":             line,
            "line_energies":    line_energies,
            "intensities":      intensities,
            "weight_fractions": fractions,
        }

    # ------------------------------------------------------------------
    # Region analysis against the SI's own DF image
    # ------------------------------------------------------------------
    def _cube_and_df(si_image_id: str, df_image_id: str):
        cube, sidecar, energy_axis, energies = _load_si_cube(state, si_image_id)
        df, _ = state.images.load(df_image_id)
        spatial = tuple(np.delete(np.array(cube.shape), energy_axis))
        if df.shape != spatial:
            raise ValueError(f"DF image {df.shape} doesn't match the SI's spatial "
                             f"shape {spatial}; use the DF image acquired with this SI")
        return cube, sidecar, energy_axis, energies, df

    @mcp.tool()
    async def eds_region_spectra(si_image_id: str, df_image_id: str,
                                 max_peaks: int = 12) -> dict:
        """Identify the peaks on the particles, in a ring just outside them, and
        far from them. The regions come from the DF image acquired with the SI.

        Use it as the first look at an SI. Elements peaked on "particle" are
        the particles; elements enriched in "halo" relative to "far" (per
        pixel) point to a coating; elements uniform everywhere are support or
        system peaks (C film, Cu grid). Each peak lists its nearest
        tabulated lines, including secondary lines and Si escape peaks. A
        detector offset of tens of eV is normal, so match the whole line
        family, not one peak.

        Args:
            si_image_id: SI cube in the local store (fetch_remote_file).
            df_image_id: The DF image saved with that SI (same 2-D size).
            max_peaks: Peaks to report per region.
        """
        cube, _, energy_axis, energies, df = _cube_and_df(si_image_id, df_image_id)
        masks = _regions.region_masks(df)
        spectra = _regions.region_spectra(cube, energy_axis, masks)
        # One detector calibration for the whole SI, from the all-pixel spectrum.
        calibration = _regions.identify_peaks(sum(spectra.values()), energies,
                                              max_peaks)["calibration"]
        out = {}
        for name, spec in spectra.items():
            n_px = int(masks[name].sum())
            out[name] = {"n_px": n_px, "total_counts": float(spec.sum()),
                         "counts_per_px": round(float(spec.sum()) / max(n_px, 1), 3),
                         "peaks": _regions.identify_peaks(spec, energies, max_peaks,
                                                          calibration=calibration)["peaks"]}
        return {"si_image_id": si_image_id, "detector_calibration": calibration,
                "note": "detector_calibration: observed keV = tabulated * (1 + gain) "
                        "+ offset_keV. Pass both to eds_edge_profile.",
                "regions": out}

    @mcp.tool()
    async def eds_edge_profile(si_image_id: str, df_image_id: str, lines: list[str],
                               energy_offset_keV: float = 0.0, energy_gain: float = 0.0,
                               bin_nm: float | None = None,
                               ratio: list[str] | None = None,
                               save_maps: bool = False) -> dict:
        """Element counts vs distance from the particle edge, which shows core
        vs shell. Optionally gives an element ratio after removing the
        far-field level.

        The edge comes from the DF image. Negative distances are inside the
        particles. A shell element (e.g. Si, O around Au) stays positive past
        the point where the core element has dropped to zero. ratio=["Cu Ka",
        "Au Ma"] gives (on-particle minus far) Cu/Au. Compare it across SIs
        taken at different distances from the grid bar: if the ratio changes,
        the signal is spurious (grid fluorescence), not alloyed.

        Args:
            si_image_id: SI cube in the local store.
            df_image_id: The DF image saved with that SI.
            lines: Lines as "El Line" (e.g. "Au Ma", "Si Ka", "O Ka") or a bare
                element (its strongest usable line is chosen).
            energy_offset_keV, energy_gain: Detector calibration error, with
                observed keV = tabulated * (1 + gain) + offset. Take both from
                eds_region_spectra's detector_calibration.
            bin_nm: Distance bin (default one SI pixel).
            ratio: Optional [numerator, denominator] line labels.
            save_maps: Also save each net-count map to the image store.
        """
        cube, sidecar, energy_axis, energies, df = _cube_and_df(si_image_id, df_image_id)
        table = dict(_regions.all_lines())
        resolved = {}
        for spec in lines:
            label = spec.strip()
            if label not in table:
                element = label.split()[0]
                for line in ("Ka", "La", "Ma"):
                    kev = table.get(f"{element} {line}")
                    if kev is not None and kev <= 12.0:
                        label = f"{element} {line}"
                        break
                else:
                    raise ValueError(f"no tabulated line for {spec!r}")
            resolved[label] = table[label]
        maps = _regions.element_maps(cube, energy_axis, energies, resolved,
                                     energy_offset_keV=energy_offset_keV,
                                     energy_gain=energy_gain)
        cal = _spatial_calibration(sidecar, energy_axis)
        px_nm = nm_per_px(cal.x_scale, cal.units)
        out = {"si_image_id": si_image_id, "lines_keV": resolved,
               **_regions.edge_profile(maps, df, px_nm, bin_nm)}
        if ratio:
            if len(ratio) != 2 or any(r not in maps for r in ratio):
                raise ValueError(f"ratio must name two of {sorted(maps)}")
            out["ratio"] = _regions.ratio_to_reference(
                maps, _regions.region_masks(df), ratio[0], ratio[1])
        if save_maps:
            out["map_image_ids"] = {}
            for label, m in maps.items():
                saved = state.images.save(
                    m.astype(np.float32), name=f"eds_net_{label.replace(' ', '_')}_{sidecar.name}",
                    calibration=cal, stage_position=sidecar.stage_position,
                    tags={"type": "eds_elemental_map", "source_image_id": si_image_id,
                          "line_label": label, "line_keV": resolved[label],
                          "energy_offset_keV": energy_offset_keV,
                          "energy_gain": energy_gain})
                out["map_image_ids"][label] = saved.image_id
        return out


    # ------------------------------------------------------------------
    # One-call element mapping
    # ------------------------------------------------------------------
    @mcp.tool()
    async def eds_map_elements(
        si_image_id: str,
        elements: list[str] | None = None,
        exclude: list[str] | None = None,
        df_image_id: str | None = None,
        max_elements: int = 6,
        window_keV: float = 0.09,
        smooth_px: float = 1.0,
        overlay: list[str] | None = None,
        export_png: bool = True,
    ) -> dict:
        """Elemental maps of an SI in one call: finds the elements, picks each
        one's best line, corrects the detector calibration, and saves
        background-subtracted maps plus PNGs. No scripting needed.

        Steps: sum the SI spectrum, identify its peaks (with the detector's
        gain/offset error fitted, as eds_region_spectra does), choose the
        elements (or use *elements*), map each one's strongest line
        (Ka/La/Ma), subtract a background interpolated from side-bands placed
        clear of every other peak, and report how
        significant each element is, where it sits, and which elements
        co-locate. Look at the PNGs (Read the path) to see the maps.

        Args:
            si_image_id: SI cube in the local store (save_dm_window with
                fetch=True, or fetch_remote_file).
            elements: Elements or lines to map, e.g. ["Au", "Si", "O"] or
                ["Au Ma", "Si Ka"]. A bare element gets its strongest line.
                Default: the elements behind the strongest peaks.
            exclude: Elements to leave out of the automatic choice (e.g. ["Cu"]
                for the grid).
            df_image_id: Optional DF/HAADF image saved with the SI (same 2-D
                size): adds per-element counts on particles / in a ring just
                outside them / far away.
            max_elements: Most elements to pick automatically.
            window_keV: Half-width of each line's integration window.
            smooth_px: Gaussian smoothing for the PNGs and correlations only
                (the saved maps are unsmoothed).
            overlay: Up to 3 elements/lines for an RGB overlay PNG (red,
                green, blue). Default: the 3 most significant detected
                elements other than C and Cu (film and grid).
            export_png: Write PNGs to <data_dir>/exports/.

        Returns:
            detector_calibration; per element: line, line_keV, net_counts,
            significance (net / sqrt(gross); >= 5 = detected), counts per
            pixel, map_image_id, png; spatial correlations between maps;
            the overlay PNG; peaks no mapped element explains; warnings
            (overlapping lines, weak elements).
        """
        from client.analysis import imaging as _img
        from client.tools.image_analysis import _export_path
        from scipy import ndimage as _ndi

        cube, sidecar, energy_axis, energies = _load_si_cube(state, si_image_id)
        total = _regions._spatial(cube, energy_axis).sum(axis=(1, 2)).astype(float)
        ident = _regions.identify_peaks(total, energies, max_peaks=20)
        cal_e = ident["calibration"]
        off, gain = float(cal_e["offset_keV"]), float(cal_e["gain"])

        specs = list(elements) if elements else _regions.elements_from_peaks(
            ident["peaks"], max_elements, exclude)
        if not specs:
            raise ValueError("no peaks found in the SI's summed spectrum; is it empty?")
        lines: dict[str, float] = {}
        for spec in specs:
            label, kev = _regions.resolve_line(spec, total, energies, cal_e, window_keV)
            lines[label] = kev

        # Side-bands clear of every other detected peak (in detector keV).
        peak_obs = [0.0] + [p["energy_keV"] for p in ident["peaks"]]   # 0 = noise peak
        e_lo, e_hi = float(energies[0]), float(energies[-1])
        maps: dict[str, np.ndarray] = {}
        line_info: dict[str, dict] = {}
        for label, kev in lines.items():
            obs = kev * (1.0 + gain) + off
            left, right = _regions.clean_sidebands(
                obs, window_keV, [p for p in peak_obs if abs(p - obs) > 0.06], e_lo, e_hi)
            nm = _regions.net_line_map(cube, energy_axis, energies, obs, window_keV,
                                       left, right)
            maps[label] = nm["net"]
            line_info[label] = {"observed_keV": round(obs, 4),
                                "background_bands_keV": [left, right], **nm}
        spatial_cal = _spatial_calibration(sidecar, energy_axis)
        masks = None
        if df_image_id:
            df, _ = state.images.load(df_image_id)
            shape = next(iter(maps.values())).shape
            if df.shape != shape:
                raise ValueError(f"DF image {df.shape} doesn't match the SI's spatial "
                                 f"shape {shape}; use the DF image acquired with this SI")
            masks = _regions.region_masks(df)

        warnings = _regions.overlapping_lines(lines)
        results: dict[str, dict] = {}
        n_px = int(next(iter(maps.values())).size)
        for label, m in maps.items():
            info = line_info[label]
            net, gross = float(m.sum()), info["gross_total"]
            sig = net / np.sqrt(gross) if gross > 0 else 0.0
            saved = state.images.save(
                m.astype(np.float32),
                name=f"eds_net_{label.replace(' ', '_')}_{sidecar.name}",
                calibration=spatial_cal, stage_position=sidecar.stage_position,
                tags={"type": "eds_elemental_map", "source_image_id": si_image_id,
                      "line_label": label, "line_keV": lines[label],
                      "window_keV": window_keV, "energy_offset_keV": off,
                      "energy_gain": gain})
            if info["background_bands_keV"] == [None, None]:
                warnings.append(f"{label}: no clean background band nearby; the map "
                                "is gross counts")
            entry = {"line_keV": lines[label],
                     "background_bands_keV": info["background_bands_keV"],
                     "net_counts": round(net, 1),
                     "significance": round(float(sig), 1), "detected": bool(sig >= 5),
                     "net_counts_per_px": round(net / n_px, 4),
                     "map_image_id": saved.image_id}
            if masks is not None:
                far = float(m[masks["far"]].mean()) if masks["far"].any() else 0.0
                entry["per_px"] = {k: round(float(m[v].mean()), 4) if v.any() else None
                                   for k, v in masks.items()}
                entry["particle_minus_far"] = round(
                    (entry["per_px"]["particle"] or 0.0) - far, 4)
            if export_png:
                sm = _ndi.gaussian_filter(m.astype(float), smooth_px) if smooth_px else m
                entry["png"] = str(_img.to_png(
                    sm, _export_path(state, f"eds_{label}_{sidecar.name}")))
            if not entry["detected"]:
                warnings.append(f"{label} is weak (significance {entry['significance']}); "
                                "its map is mostly noise")
            results[label] = entry

        out: dict = {
            "si_image_id": si_image_id,
            "detector_calibration": cal_e,
            "spatial_calibration": {"scale": spatial_cal.x_scale,
                                    "units": spatial_cal.units},
            "elements": results,
            "correlations": _regions.map_correlations(maps, smooth_px or 0.0),
        }
        if masks is not None:
            out["region_px"] = {k: int(v.sum()) for k, v in masks.items()}

        # RGB overlay.
        if export_png:
            if overlay:
                chosen = []
                for spec in overlay[:3]:
                    hit = next((k for k in maps if k == spec.strip()
                                or k.split()[0] == spec.strip()), None)
                    if hit is None:
                        raise ValueError(f"overlay {spec!r} is not among the mapped "
                                         f"lines {sorted(maps)}")
                    chosen.append(hit)
            else:
                ranked = sorted((k for k in maps if results[k]["detected"]
                                 and k.split()[0] not in ("C", "Cu")),
                                key=lambda k: -results[k]["significance"])
                chosen = ranked[:3]
            if len(chosen) >= 2:
                path = _img.overlay_png(
                    maps[chosen[0]], maps[chosen[1]],
                    _export_path(state, f"eds_overlay_{sidecar.name}"),
                    blue=maps[chosen[2]] if len(chosen) > 2 else None,
                    smooth_px=smooth_px or 0.0, upscale=max(1, 400 // max(
                        next(iter(maps.values())).shape)))
                out["overlay"] = {"png": str(path),
                                  "channels": dict(zip(("red", "green", "blue"), chosen))}

        mapped = {k.split()[0] for k in maps}
        unexplained = [{"energy_keV": p["energy_keV"], "height": p["height"],
                        "best_guess": p["candidates"][0]["line"] if p["candidates"] else None}
                       for p in ident["peaks"]
                       if p["candidates"] and p["candidates"][0]["line"].split()[0] not in mapped
                       and not p.get("possible_escape_of")]
        if unexplained:
            out["unmapped_peaks"] = unexplained
        if warnings:
            out["warnings"] = warnings
        return out
