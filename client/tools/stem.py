"""MCP tools — STEM acquisition and detector control."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from client.transport.zmq_client import ZMQClient
from client.state.manager import StateManager
from client.state.models import ImageCalibration


def register(mcp: FastMCP, zmq: ZMQClient, state: StateManager) -> None:

    @mcp.tool()
    async def take_stem_image(
        mode: str = "capture",
        width: int = 512,
        height: int = 512,
        dwell_us: float = 2.0,
        signal_index: int = 0,
        rotation: float = 0.0,
    ) -> dict:
        """Acquire a STEM image using DigiScan.

        Args:
            mode: "search" (continuous preview), "preview" (single medium-quality),
                  or "capture" (full-quality, default).
            width: Scan width in pixels.
            height: Scan height in pixels.
            dwell_us: Pixel dwell time in microseconds.
            signal_index: DigiScan detector signal index (0 = first detector).
            rotation: Scan rotation in degrees.

        Returns:
            image_id and metadata for the acquired image.
        """
        params = {
            "mode": mode,
            "width": width,
            "height": height,
            "dwell_us": dwell_us,
            "signal_index": signal_index,
            "rotation": rotation,
        }
        meta, arr = await zmq.send_command_image("stem_acquire", params)
        image_meta = meta.get("image_meta", {})
        cal_data = image_meta.get("calibration", {})
        calibration = ImageCalibration(
            x_scale=cal_data.get("x_scale", 1.0),
            y_scale=cal_data.get("y_scale", 1.0),
            units=cal_data.get("units", "nm"),
        )
        sidecar = state.images.save(
            arr,
            name=image_meta.get("name", f"stem_{mode}"),
            calibration=calibration,
            stage_position=state.microscope.stage,
            tags={"mode": mode, "signal_index": signal_index},
        )
        return {"image_id": sidecar.image_id, "shape": sidecar.shape, "calibration": cal_data}

    @mcp.tool()
    async def take_camera_image(
        exposure_s: float = 1.0,
        binning: int = 1,
        processing: str = "gain_normalised",
    ) -> dict:
        """Acquire a TEM camera image.

        Args:
            exposure_s: Exposure time in seconds.
            binning: Pixel binning (1, 2, or 4).
            processing: "unprocessed", "dark_subtracted", or "gain_normalised".

        Returns:
            image_id and metadata for the acquired image.
        """
        params = {"exposure_s": exposure_s, "binning": binning, "processing": processing}
        meta, arr = await zmq.send_command_image("camera_acquire", params)
        image_meta = meta.get("image_meta", {})
        cal_data = image_meta.get("calibration", {})
        calibration = ImageCalibration(
            x_scale=cal_data.get("x_scale", 1.0),
            y_scale=cal_data.get("y_scale", 1.0),
            units=cal_data.get("units", "nm"),
        )
        sidecar = state.images.save(
            arr,
            name=image_meta.get("name", "camera_image"),
            calibration=calibration,
            stage_position=state.microscope.stage,
            tags={"exposure_s": exposure_s, "binning": binning},
        )
        return {"image_id": sidecar.image_id, "shape": sidecar.shape, "calibration": cal_data}

    @mcp.tool()
    async def get_stem_settings() -> dict:
        """Return current DigiScan detector and acquisition settings."""
        return await zmq.send_command("get_stem_settings")

    @mcp.tool()
    async def get_detectors() -> dict:
        """Return a list of available DigiScan detector signals."""
        return await zmq.send_command("get_detectors")

    @mcp.tool()
    async def move_beam_to(x: float, y: float) -> dict:
        """Move the STEM beam to a fractional DigiScan position.

        Args:
            x: Fractional x position [0.0, 1.0].
            y: Fractional y position [0.0, 1.0].
        """
        return await zmq.send_command("move_beam_to", {"x": x, "y": y})

    @mcp.tool()
    async def get_beam_position() -> dict:
        """Return the current STEM beam position in DigiScan fractional coordinates."""
        return await zmq.send_command("get_beam_position")

    @mcp.tool()
    async def list_cameras() -> dict:
        """Return a list of all cameras available in Digital Micrograph."""
        return await zmq.send_command("list_cameras")

    @mcp.tool()
    async def get_camera_info(camera_id: int | None = None) -> dict:
        """Return size and pixel-size information for a camera.

        Args:
            camera_id: Camera ID to query.  If omitted, uses the active camera.
        """
        params = {} if camera_id is None else {"camera_id": camera_id}
        return await zmq.send_command("get_camera_info", params)
