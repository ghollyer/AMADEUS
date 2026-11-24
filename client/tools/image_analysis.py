"""MCP tools: particle morphology, focus/registration helpers, PNG export.

These started as one-off scripts during the 2026-11-12 F200 nanorod survey
and are now part of the client. All work on images already in the local
store; none talk to the instrument.
"""

from __future__ import annotations

import re
from datetime import datetime

from mcp.server.fastmcp import FastMCP

from client.analysis import imaging as _img
from client.analysis import particles as _particles
from client.analysis import stage_mapping as _map
from client.analysis.units import nm_per_px
from client.state.manager import StateManager

_MAX_LISTED = 200


def _nm_px(sidecar) -> float:
    cal = sidecar.calibration
    return nm_per_px(cal.x_scale, cal.units)          # raises if uncalibrated


def _export_path(state: StateManager, stem: str):
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem)[:80] or "image"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return state.data_dir / "exports" / f"{stamp}_{safe}.png"


def register(mcp: FastMCP, state: StateManager, instrument: dict | None = None) -> None:
    """*instrument* is the config's ``instrument`` section (stage mapping)."""

    @mcp.tool()
    async def measure_nanorods(image_id: str, threshold_frac: float = 0.35,
                               include_particles: bool = False) -> dict:
        """Length, width and aspect ratio of bright particles in a DF/HAADF image.

        The summary covers *isolated* particles only. In dense clusters,
        touching rods merge into one blob, so those are excluded (filled area
        < 85 % of the L x W box, or width > 1.4x the median). Particles on
        the image border are dropped. Width includes ~1-2 nm of probe blur.
        Best at <= 1.5 nm/px (x150k or higher for ~20 nm rods). Pool several
        images for a distribution.

        Args:
            image_id: DF/HAADF image (bright particles, dark background).
            threshold_frac: Edge threshold between background and particle
                level (0.35 suits DF; raise it if particles merge).
            include_particles: Also return every particle (up to 200).
        """
        arr, sidecar = state.images.load(image_id)
        result = _particles.measure_rods(arr, _nm_px(sidecar), threshold_frac)
        particles = result.pop("particles")
        if include_particles:
            result["particles"] = particles[:_MAX_LISTED]
        result["image_id"] = image_id
        return result

    @mcp.tool()
    async def find_particle_clusters(image_id: str, join_px: int = 6,
                                     max_clusters: int = 20) -> dict:
        """Locate clusters of bright particles in a low-mag survey image.

        Grid bars and other large bright regions are excluded. Each cluster
        has its pixel centroid, offset from the image centre (nm), extent, a
        size measure (bright_px), and the stage position that centres it.
        That position comes from the same Jacobian logic as
        convert_pixel_to_stage, with its warnings.

        Args:
            image_id: Survey image (e.g. x12k-x25k DF).
            join_px: Particles closer than this merge into one cluster.
            max_clusters: Largest N clusters to return.
        """
        arr, sidecar = state.images.load(image_id)
        groups = _particles.find_particle_groups(arr, _nm_px(sidecar), join_px=join_px)
        groups = groups[:max(1, int(max_clusters))]
        warnings = set()
        if sidecar.stage_position is not None:
            for g in groups:
                target = _map.pixel_to_stage(sidecar, *g["centroid_px"], instrument)
                g["stage_target_m"] = {"x_m": target["x_m"], "y_m": target["y_m"]}
                warnings.update(target.get("warnings", []))
        out = {"image_id": image_id, "n_clusters": len(groups), "clusters": groups}
        if warnings:
            out["warnings"] = sorted(warnings)
        return out

    @mcp.tool()
    async def edge_intensity_profile(image_id: str, max_nm: float = 30.0,
                                     step_nm: float | None = None) -> dict:
        """Intensity vs distance outside the particle edge, as a fraction of
        the particle signal.

        Use it to detect and size a low-Z shell (oxide, silica, ligand) around
        high-Z particles in DF. The first ~3-5 nm is the probe tail. A
        plateau of a few % beyond it is shell material, and where it falls
        to background gives the shell thickness. Use x400k or higher;
        export_image_png with stretch="low" shows the same thing visually.
        Confirm the composition with eds_edge_profile.

        Args:
            image_id: DF/HAADF image.
            max_nm: Furthest distance from the edge to report.
            step_nm: Bin width (default max(1.5 nm, 2 px)).
        """
        arr, sidecar = state.images.load(image_id)
        return {"image_id": image_id,
                **_particles.edge_distance_profile(arr, _nm_px(sidecar), step_nm, max_nm)}

    @mcp.tool()
    async def image_sharpness(image_ids: list[str]) -> dict:
        """Focus metric for frames of a focus sweep, sharpest first.

        Only compare frames of the same field at the same magnification and
        scan settings. Small 512^2 previews at a few us are enough. Note each
        frame's focus value yourself when you take it.

        Args:
            image_ids: Images to compare.
        """
        rows = []
        for iid in image_ids:
            arr, _ = state.images.load(iid)
            rows.append({"image_id": iid, "sharpness": round(_img.sharpness(arr), 4)})
        rows.sort(key=lambda r: -r["sharpness"])
        return {"ranked": rows, "sharpest": rows[0]["image_id"] if rows else None}

    @mcp.tool()
    async def measure_image_shift(reference_image_id: str, moved_image_id: str) -> dict:
        """How far features moved between two images of the same area
        (phase correlation). Use it for drift checks and stage calibration.

        Returns the shift in px and in nm (x right, y down), a confidence
        (reliable above ~20), and the stage move recorded between the two
        acquisitions. Both images must have the same size and calibration.

        Args:
            reference_image_id: First image.
            moved_image_id: Second image (after a stage move, or later).
        """
        a, sa = state.images.load(reference_image_id)
        b, sb = state.images.load(moved_image_id)
        shift = _img.phase_correlation_shift(a, b)
        px = _nm_px(sa)
        out = {**shift, "dx_nm": round(shift["dx_px"] * px, 1),
               "dy_nm": round(shift["dy_px"] * px, 1), "nm_per_px": px}
        if sa.stage_position is not None and sb.stage_position is not None:
            out["stage_delta_nm"] = [
                round((sb.stage_position.x_m - sa.stage_position.x_m) * 1e9, 1),
                round((sb.stage_position.y_m - sa.stage_position.y_m) * 1e9, 1)]
        return out

    @mcp.tool()
    async def fit_stage_image_mapping(image_ids: list[str]) -> dict:
        """Fit the stage->image Jacobian from images of one area at
        different stage positions.

        The first image is the reference. Each later image adds one (stage
        move, feature shift) pair from the recorded stage positions and phase
        correlation. You need >= 2 non-parallel moves, e.g. a pure +X and a
        pure +Y move of a few um at x12k-x25k, approached from the same
        direction (backlash otherwise shows up as large residuals). The
        result is the config entry instrument.stage.image_mapping.<MODE>;
        the operator decides whether to save it. Nothing is written here.

        Args:
            image_ids: Reference first, then the moved images (same size/mag).
        """
        if len(image_ids) < 3:
            raise ValueError("need a reference image plus at least two moved images")
        ref, sref = state.images.load(image_ids[0])
        if sref.stage_position is None:
            raise ValueError("reference image has no stage position")
        px_m = _nm_px(sref) * 1e-9
        deltas, shifts, pairs = [], [], []
        for iid in image_ids[1:]:
            arr, sc = state.images.load(iid)
            if sc.stage_position is None:
                raise ValueError(f"image {iid} has no stage position")
            sh = _img.phase_correlation_shift(ref, arr)
            d = [sc.stage_position.x_m - sref.stage_position.x_m,
                 sc.stage_position.y_m - sref.stage_position.y_m]
            deltas.append(d)
            shifts.append([sh["dx_px"] * px_m, sh["dy_px"] * px_m])
            pairs.append({"image_id": iid, "stage_delta_nm": [round(v * 1e9, 1) for v in d],
                          "shift_nm": [round(v * 1e9, 1) for v in shifts[-1]],
                          "confidence": sh["confidence"]})
        fit = _img.fit_stage_to_image(deltas, shifts)
        mode = _map.image_mode(sref.tags)
        return {"mode": mode, "pairs": pairs, **fit,
                "config_entry": {"stage": {"image_mapping": {str(mode): {
                    "stage_to_image": fit["stage_to_image"],
                    "scan_rotation_deg": sref.tags.get("rotation", 0.0),
                    "measured": f"{datetime.now():%Y-%m-%d} fit_stage_image_mapping, "
                                f"{fit['n_moves']} moves, rms {fit['rms_residual_m'] * 1e9:.0f} nm"}}}}}

    @mcp.tool()
    async def export_image_png(image_id: str, stretch: str = "auto",
                               max_px: int = 1024) -> dict:
        """Write a stored image as a PNG so it can be viewed, with a choice of
        contrast stretch. SI cubes are summed over energy.

        Args:
            image_id: Image to export.
            stretch: "auto" (0.5-99.8 % linear), "low" (saturates particles
                to show faint shells, residue and support film) or "log".
            max_px: Downsample so the longest side is at most this.
        """
        if stretch not in ("auto", "low", "log"):
            raise ValueError("stretch must be auto, low or log")
        arr, sidecar = state.images.load(image_id)
        path = _img.to_png(arr, _export_path(state, f"{sidecar.name}_{stretch}"), stretch,
                           max_px)
        return {"path": str(path), "image_id": image_id, "stretch": stretch}

    @mcp.tool()
    async def export_overlay_png(red_image_id: str, green_image_id: str,
                                 blue_image_id: str | None = None, upscale: int = 4) -> dict:
        """Colour overlay of two or three same-size maps (e.g. EDS Au map red,
        Si map green) as a PNG. Each channel is stretched independently.

        Args:
            red_image_id: Map shown in red.
            green_image_id: Map shown in green.
            blue_image_id: Optional map shown in blue.
            upscale: Integer pixel replication (SI maps are often 100x100).
        """
        r, sr = state.images.load(red_image_id)
        g, sg = state.images.load(green_image_id)
        b = state.images.load(blue_image_id)[0] if blue_image_id else None
        path = _img.overlay_png(r, g, _export_path(state, f"overlay_{sr.name}_{sg.name}"),
                                blue=b, upscale=max(1, int(upscale)))
        return {"path": str(path)}

