"""MCP tools — client-side image analysis (FFT, segmentation, coordinates)."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from client.state.manager import StateManager
from client.analysis import ctf as _ctf
from client.analysis import fft as _fft
from client.analysis import segmentation as _seg
from client.analysis import stage_mapping as _map
from client.analysis.units import nm_per_px


def register(mcp: FastMCP, state: StateManager, instrument: dict | None = None) -> None:
    """*instrument* is the config's ``instrument`` section. Its
    ``stage.image_mapping`` drives ``convert_pixel_to_stage``."""

    @mcp.tool()
    async def compute_fft(
        image_id: str,
        fit_ctf: bool = False,
        voltage_kv: float = 200.0,
        cs_mm: float = 1.0,
        q_min_inv_nm: float | None = None,
        q_max_inv_nm: float | None = None,
        defocus_max_nm: float = 5000.0,
    ) -> dict:
        """Compute the power spectrum (FFT) of a stored image, and optionally
        fit defocus (C1) and twofold astigmatism (A1) to its Thon rings.

        The power spectrum is saved as a new image and its ID is returned.

        The C1/A1 fit needs a spatially calibrated TEM image that includes
        thin amorphous material (carbon film, an amorphous shell or edge);
        crystalline-only areas and STEM images have no Thon rings, and give
        reliable=false. Use a few hundred nm of defocus for clear rings.
        To correct astigmatism, change one stigmator axis, refit, and watch
        A1_nm fall; the angle tells you which direction to push.

        Args:
            image_id: ID of the source image (from take_stem_image or take_camera_image).
            fit_ctf: Also fit C1 and A1 (default False).
            voltage_kv: Accelerating voltage for the electron wavelength (F200: 200).
            cs_mm: Spherical aberration of the objective (mm). Only affects the
                defocus sign and high-q rings; check your pole piece.
            q_min_inv_nm, q_max_inv_nm: Fit range in 1/nm (default from ~2 %
                to 80 % of Nyquist, cut where the rings get too dense to resolve).
            defocus_max_nm: Search defocus within +/- this.

        Returns:
            power_spectrum_image_id and shape. With fit_ctf, also ``ctf``:
            defocus_nm (positive = underfocus), C1_nm (= -defocus_nm,
            aberration convention), A1_nm (half the defocus difference between
            the principal axes), A1_angle_deg (most-underfocused axis, from
            image +x toward +y, y down), score, significance, reliable,
            sign_ambiguous (|CTF|^2 barely depends on the defocus sign; only Cs
            breaks it, so trust |C1| and check the sign by stepping focus),
            n_rings and the q range used.
        """
        arr, sidecar = state.images.load(image_id)
        ps = _fft.compute_power_spectrum(arr)
        ps_sidecar = state.images.save(
            ps,
            name=f"fft_{sidecar.name}",
            calibration=sidecar.calibration,
            tags={"source_image_id": image_id, "type": "power_spectrum"},
        )
        out = {
            "power_spectrum_image_id": ps_sidecar.image_id,
            "shape": ps_sidecar.shape,
        }
        if fit_ctf:
            cal = sidecar.calibration
            nm_px = nm_per_px(cal.x_scale, cal.units)   # raises if uncalibrated
            fit = _ctf.fit_ctf(arr, nm_px, voltage_kv=voltage_kv, cs_mm=cs_mm,
                               q_min_inv_nm=q_min_inv_nm, q_max_inv_nm=q_max_inv_nm,
                               defocus_max_nm=defocus_max_nm)
            if not fit["reliable"]:
                fit["warning"] = ("no clear Thon rings: the fit is not reliable. Image "
                                  "thin amorphous material in TEM, with a few hundred "
                                  "nm of defocus.")
            out["ctf"] = {"nm_per_px": nm_px, **fit}
        return out

    @mcp.tool()
    async def extract_lattice_spacings(
        image_id: str,
        min_distance_px: int = 5,
        threshold_rel: float = 0.3,
        max_peaks: int = 20,
    ) -> dict:
        """Find diffraction peaks in a stored image and return d-spacings.

        Computes the FFT internally; does not require a separate compute_fft call.

        Args:
            image_id: ID of the source image.
            min_distance_px: Minimum separation between peaks in pixels.
            threshold_rel: Only keep peaks above this fraction of the maximum intensity.
            max_peaks: Maximum number of peaks to return.

        Returns:
            List of peaks with q (1/nm) and d-spacing (nm and Angstrom).
        """
        arr, sidecar = state.images.load(image_id)
        cal = sidecar.calibration
        nm_px = nm_per_px(cal.x_scale, cal.units)   # raises if uncalibrated
        ps = _fft.compute_power_spectrum(arr)
        peaks = _fft.find_diffraction_peaks(
            ps,
            min_distance_px=min_distance_px,
            threshold_rel=threshold_rel,
            max_peaks=max_peaks,
        )
        spacings = _fft.extract_d_spacings(peaks, tuple(arr.shape[:2]), nm_px)
        return {"image_id": image_id, "nm_per_px": nm_px,
                "min_resolvable_d_nm": 2 * nm_px, "peaks": spacings}

    @mcp.tool()
    async def segment_image(
        image_id: str,
        method: str = "otsu",
        min_size_px: int = 10,
        max_size_px: int | None = None,
        fill_holes: bool = True,
    ) -> dict:
        """Segment particles in a stored image.

        Args:
            image_id: ID of the source image.
            method: Threshold method — "otsu" (default) or "mean".
            min_size_px: Discard objects smaller than this in pixels.
            max_size_px: Discard objects larger than this in pixels (None = no limit).
            fill_holes: Fill holes in binary mask before labelling.

        Returns:
            label_map_image_id, particle count, and per-particle metrics.
        """
        arr, sidecar = state.images.load(image_id)
        label_map, metrics = _seg.segment_particles(
            arr,
            method=method,
            min_size_px=min_size_px,
            max_size_px=max_size_px,
            fill_holes=fill_holes,
        )
        label_sidecar = state.images.save(
            label_map,
            name=f"labels_{sidecar.name}",
            calibration=sidecar.calibration,
            tags={"source_image_id": image_id, "type": "label_map"},
        )
        return {
            "label_map_image_id": label_sidecar.image_id,
            "particle_count": len(metrics),
            "particles": metrics,
        }

    @mcp.tool()
    async def convert_pixel_to_stage(
        image_id: str, px: float, py: float
    ) -> dict:
        """Stage position that puts pixel (px, py) of an image at the image centre.

        Uses the stage position recorded when the image was acquired, the
        image's spatial calibration, and the instrument's measured
        stage->image Jacobian for the image's mode (STEM/TEM). The F200's
        stage axes are rotated and scaled relative to the scan, so a plain
        pixel offset would land the stage far off target. Without a measured
        Jacobian it falls back to the plain offset and says so.

        Accuracy is limited by stage backlash (~0.5 um on direction
        reversals). For precise centring, move with approach=True, then
        re-image and repeat with the new image.

        Args:
            image_id: ID of the image.
            px: Pixel column (x).
            py: Pixel row (y).

        Returns:
            x_m, y_m, z_m: target stage position in metres (pass x_m*1e9 /
            y_m*1e9 as x_nm / y_nm to move_stage_absolute). Also: the move
            (dx_m, dy_m), the mapping used, and any warnings.
        """
        _, sidecar = state.images.load(image_id)
        return _map.pixel_to_stage(sidecar, px, py, instrument)

