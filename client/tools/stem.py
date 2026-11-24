"""MCP tools — STEM acquisition and detector control."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from client.devices import device_params, device_tag
from client.transport.zmq_client import ZMQClient
from client.state.manager import StateManager
from client.state.models import ImageCalibration, StagePosition

_STAGE_READ_TIMEOUT_MS = 5_000


async def _stage_at_acquisition(zmq: ZMQClient, state: StateManager):
    """(stage position, source) to store with a new image. Read fresh, since
    the client's cached position goes stale whenever a move timed out or the
    operator drove the stage. Falls back to the cache, tagged as such."""
    try:
        data = await zmq.send_command("get_stage_position",
                                      timeout_ms=_STAGE_READ_TIMEOUT_MS)
        pos = StagePosition.model_validate(data)
        state.microscope.stage = pos
        return pos, "read"
    except Exception:
        return state.microscope.stage, "cached"


async def _optics_at_acquisition(zmq: ZMQClient) -> dict:
    """Magnification, high tension and spot size to tag a new image with, so
    the image viewer (and later analysis) knows the conditions. Best effort:
    a backend without one of these reads just leaves that tag out."""
    tags: dict = {}
    reads = (("get_magnification", ("magnification", "mag_index", "mag_unit")),
             ("get_high_tension", ("high_tension_kV",)),
             ("get_spot_size", ("spot_size",)))
    for cmd, keys in reads:
        try:
            data = await zmq.send_command(cmd, timeout_ms=_STAGE_READ_TIMEOUT_MS)
        except Exception:
            continue
        if isinstance(data, dict):
            tags.update({k: data[k] for k in keys if data.get(k) is not None})
    return tags


def register(mcp: FastMCP, zmq: ZMQClient, state: StateManager) -> None:

    @mcp.tool()
    async def take_stem_image(
        mode: str = "capture",
        width: int = 512,
        height: int = 512,
        dwell_us: float = 2.0,
        detector: str | int | None = None,
        rotation: float = 0.0,
    ) -> dict:
        """Acquire a STEM image with the microscope's scan unit.

        Args:
            mode: "search" (continuous preview), "preview" (single medium-quality),
                  or "capture" (full-quality, default).
            width: Scan width in pixels.
            height: Scan height in pixels.
            dwell_us: Pixel dwell time in microseconds.
            detector: Which detector: a role ("HAADF", "BF"), a device id from
                get_detectors / backend capabilities ("dm/detector/1"), or an
                index. Default: the scan unit's default detector.
            rotation: Scan rotation in degrees.

        Returns:
            image_id and metadata for the acquired image.
        """
        params = {
            "mode": mode,
            "width": width,
            "height": height,
            "dwell_us": dwell_us,
            "rotation": rotation,
            **device_params(detector, "detector"),
        }
        stage, stage_source = await _stage_at_acquisition(zmq, state)
        optics = await _optics_at_acquisition(zmq)
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
            stage_position=stage,
            tags={"mode": mode, "imaging_mode": "STEM",
                  "signal_index": meta.get("data", {}).get("signal_index"),
                  "detector": meta.get("data", {}).get("detector")
                  or device_tag(params, "detector"),
                  "rotation": rotation, "dwell_us": dwell_us,
                  "width": width, "height": height,
                  "stage_position_source": stage_source, **optics},
        )
        return {"image_id": sidecar.image_id, "shape": sidecar.shape, "calibration": cal_data}

    @mcp.tool()
    async def take_camera_image(
        exposure_s: float = 1.0,
        binning: int = 1,
        processing: str = "gain_normalised",
        camera: str | int | None = None,
    ) -> dict:
        """Acquire a TEM camera image.

        Cameras can be retractable: switching to another camera may move it in
        (tens of seconds). If the cameras are an exclusive group, the operator
        is asked to confirm the switch; if they decline, don't retry.

        Args:
            exposure_s: Exposure time in seconds.
            binning: Pixel binning (1, 2, or 4).
            processing: "unprocessed", "dark_subtracted", or "gain_normalised".
            camera: Which camera: a role ("imaging", "diffraction"), a device id
                ("dm/camera/0", see list_cameras / backend capabilities), or an
                index. Pass it explicitly: the default is whatever camera the
                vendor software has active.

        Returns:
            image_id and metadata for the acquired image.
        """
        params = {"exposure_s": exposure_s, "binning": binning, "processing": processing,
                  **device_params(camera, "camera")}
        stage, stage_source = await _stage_at_acquisition(zmq, state)
        optics = await _optics_at_acquisition(zmq)
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
            stage_position=stage,
            tags={"exposure_s": exposure_s, "binning": binning,
                  "camera_id": meta.get("data", {}).get("camera_id"),
                  "camera": device_tag(params, "camera"),
                  "processing": processing,
                  "imaging_mode": "TEM", "stage_position_source": stage_source, **optics},
        )
        return {"image_id": sidecar.image_id, "shape": sidecar.shape, "calibration": cal_data}

    @mcp.tool()
    async def get_stem_settings() -> dict:
        """Return the scan unit's current detector and acquisition settings."""
        return await zmq.send_command("get_stem_settings")

    @mcp.tool()
    async def get_detectors() -> dict:
        """Return the scan unit's detectors (index and name). Roles such as
        "HAADF" map to these through the instrument config."""
        return await zmq.send_command("get_detectors")

    @mcp.tool()
    async def move_beam_to(x: float, y: float) -> dict:
        """Park the STEM beam at a fractional position in the scan field.

        Args:
            x: Fractional x position [0.0, 1.0].
            y: Fractional y position [0.0, 1.0].
        """
        return await zmq.send_command("move_beam_to", {"x": x, "y": y})

    @mcp.tool()
    async def get_beam_position() -> dict:
        """Return the current STEM beam position as fractions of the scan field."""
        return await zmq.send_command("get_beam_position")

    @mcp.tool()
    async def list_cameras() -> dict:
        """Return the cameras the acquisition software reports (id and name)."""
        return await zmq.send_command("list_cameras")

    @mcp.tool()
    async def get_camera_info(camera: str | int | None = None) -> dict:
        """Return size and pixel-size information for a camera.

        Args:
            camera: A role, device id or index (see take_camera_image).
                If omitted, uses the active camera.
        """
        params = device_params(camera, "camera")
        return await zmq.send_command("get_camera_info", params)
