"""MCP tools — EDS acquisition + analysis of stored SI cubes.

The acquisition tools (``eds_acquire_point``, ``eds_acquire_map``)
trigger DM and leave the data on the microscope side. The analysis
tools below operate on SI cubes that have already been pulled into the
local image store via ``fetch_dm_file`` (or saved by some other path).

The energy axis of an SI cube is identified by reading the per-axis
calibration stashed in the sidecar's ``tags`` dict at fetch time (see
``client/tools/files.py``).
"""

from __future__ import annotations

import numpy as np
from mcp.server.fastmcp import FastMCP

from client.analysis import eds_si as _eds_si
from client.state.manager import StateManager
from client.state.models import ImageCalibration
from client.transport.zmq_client import ZMQClient


def _load_si_cube(state: StateManager, image_id: str):
    """Return (cube, sidecar, energy_axis_index, energies_keV)."""
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
    return arr, sidecar, int(energy_axis), energies


def _spatial_calibration(sidecar, energy_axis: int) -> ImageCalibration:
    """Build an ImageCalibration for the two spatial axes of an SI cube."""
    axes = sidecar.tags.get("axes") or []
    spatial = [d for d in axes if int(d["index"]) != int(energy_axis)]
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
        with ``fetch_dm_file`` for client-side analysis.

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
        produce client-side elemental maps and quantifications, save the
        SI file in DM and pull it with ``fetch_dm_file``, then call
        ``eds_extract_elemental_map`` or ``eds_quantify_si``.

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
                (e.g. fetched via ``fetch_dm_file``).

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
