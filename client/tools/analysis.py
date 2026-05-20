"""MCP tools — client-side image analysis (FFT, segmentation, coordinates)."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from client.state.manager import StateManager
from client.analysis import fft as _fft
from client.analysis import segmentation as _seg


def register(mcp: FastMCP, state: StateManager) -> None:

    @mcp.tool()
    async def compute_fft(image_id: str) -> dict:
        """Compute the power spectrum (FFT) of a stored image.

        The power spectrum is saved as a new image and its ID is returned.

        Args:
            image_id: ID of the source image (from take_stem_image or take_camera_image).

        Returns:
            power_spectrum_image_id, shape, and calibration of the FFT image.
        """
        arr, sidecar = state.images.load(image_id)
        ps = _fft.compute_power_spectrum(arr)
        ps_sidecar = state.images.save(
            ps,
            name=f"fft_{sidecar.name}",
            calibration=sidecar.calibration,
            tags={"source_image_id": image_id, "type": "power_spectrum"},
        )
        return {
            "power_spectrum_image_id": ps_sidecar.image_id,
            "shape": ps_sidecar.shape,
        }

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
        ps = _fft.compute_power_spectrum(arr)
        peaks = _fft.find_diffraction_peaks(
            ps,
            min_distance_px=min_distance_px,
            threshold_rel=threshold_rel,
            max_peaks=max_peaks,
        )
        cal = sidecar.calibration.x_scale  # nm/px
        spacings = _fft.extract_d_spacings(peaks, tuple(arr.shape[:2]), cal)
        return {"image_id": image_id, "peaks": spacings}

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
        """Convert pixel coordinates in an image to estimated stage coordinates.

        Uses the image calibration and the stage position recorded at acquisition
        time to estimate where on the specimen the pixel corresponds to.

        Args:
            image_id: ID of the image.
            px: Pixel column (x).
            py: Pixel row (y).

        Returns:
            Estimated stage position in metres (x_m, y_m).
        """
        _, sidecar = state.images.load(image_id)
        if sidecar.stage_position is None:
            raise ValueError("Image has no associated stage position.")
        rows, cols = sidecar.shape[:2]
        cal_x = sidecar.calibration.x_scale * 1e-9  # nm/px -> m/px
        cal_y = sidecar.calibration.y_scale * 1e-9
        dx_m = (px - cols / 2) * cal_x
        dy_m = (py - rows / 2) * cal_y
        stage = sidecar.stage_position
        return {
            "x_m": stage.x_m + dx_m,
            "y_m": stage.y_m + dy_m,
            "z_m": stage.z_m,
        }
