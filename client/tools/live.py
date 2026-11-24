"""MCP tools — continuous live scan.

Control (start/stop/status) goes over the normal REQ/REP channel; pixels stream
out-of-band on the PUB/SUB channel and are cached in memory by the
:class:`~client.transport.stream_subscriber.StreamSubscriber`. ``get_live_frame``
reads that in-memory frame and persists it to the ImageStore — no DM round-trip,
so it never stalls or interrupts the running scan.
"""

from __future__ import annotations

import time

from mcp.server.fastmcp import FastMCP

from client.devices import device_params
from client.transport.zmq_client import ZMQClient
from client.transport.stream_subscriber import StreamSubscriber
from client.state.manager import StateManager
from client.state.models import ImageCalibration


def register(
    mcp: FastMCP, zmq: ZMQClient, state: StateManager, subscriber: StreamSubscriber
) -> None:

    @mcp.tool()
    async def start_live_scan(
        detector: str = "stem",
        width: int = 512,
        height: int = 512,
        dwell_us: float = 2.0,
        rotation: float = 0.0,
        exposure_s: float = 0.1,
        binning: int = 1,
        processing: str = "gain_normalised",
        interval_ms: int = 100,
        device: str | int | None = None,
    ) -> dict:
        """Start a continuous live scan streamed over the network.

        Frames stream to the client in the background; call ``get_live_frame``
        to grab the current frame, and ``stop_live_scan`` when finished. STEM
        and camera live modes are mutually exclusive (starting one stops the
        other).

        Args:
            detector: "stem" (scan raster) or "camera" (TEM camera).
            width: STEM scan width in pixels.
            height: STEM scan height in pixels.
            dwell_us: STEM pixel dwell time in microseconds.
            rotation: STEM scan rotation in degrees.
            exposure_s: Camera exposure time in seconds.
            binning: Camera pixel binning.
            processing: Camera processing: "unprocessed", "dark_subtracted",
                or "gain_normalised".
            interval_ms: How often the server samples and publishes a frame.
            device: Which STEM detector or camera to stream: a role ("HAADF",
                "imaging"), a device id ("dm/camera/0") or an index. Default:
                the vendor software's active detector/camera.

        Returns:
            Server status for the started scan.
        """
        params = {
            "detector": detector,
            "width": width,
            "height": height,
            "dwell_us": dwell_us,
            "rotation": rotation,
            "exposure_s": exposure_s,
            "binning": binning,
            "processing": processing,
            "interval_ms": interval_ms,
        }
        kind = "camera" if detector == "camera" else "detector"
        params.update(device_params(device, kind))
        return await zmq.send_command("live_start", params)

    @mcp.tool()
    async def stop_live_scan() -> dict:
        """Stop the running live scan and free the detector for other commands."""
        return await zmq.send_command("live_stop")

    @mcp.tool()
    async def get_live_status() -> dict:
        """Return the live-scan status (server state + client stream stats)."""
        data = await zmq.send_command("live_status")
        return {**data, "subscriber": subscriber.stats()}

    @mcp.tool()
    async def get_live_frame() -> dict:
        """Save the most recently streamed live frame and return its image_id.

        Reads the latest frame from the in-memory stream cache (no DM round-trip)
        and writes it to the ImageStore so the analysis tools can use it.

        Returns:
            image_id, shape, frame_index, age_s and calibration; or
            ``{"available": False}`` if no frame has arrived yet.
        """
        latest = subscriber.latest()
        if latest is None:
            return {"available": False,
                    "message": "No live frame received yet. Is a live scan running?"}

        arr = latest["array"]
        meta = latest["meta"]
        image_meta = meta.get("image_meta", {})
        cal_data = image_meta.get("calibration", {})
        detector = meta.get("data", {}).get("detector", "live")
        frame_index = latest.get("frame_index")
        calibration = ImageCalibration(
            x_scale=cal_data.get("x_scale", 1.0),
            y_scale=cal_data.get("y_scale", 1.0),
            units=cal_data.get("units", "nm"),
        )
        sidecar = state.images.save(
            arr,
            name=image_meta.get("name", f"live_{detector}"),
            calibration=calibration,
            stage_position=state.microscope.stage,
            tags={"source": "live", "detector": detector, "frame_index": frame_index},
        )
        age = latest.get("received_at")
        age_s = round(time.monotonic() - age, 3) if age is not None else None
        return {
            "available": True,
            "image_id": sidecar.image_id,
            "shape": sidecar.shape,
            "frame_index": frame_index,
            "age_s": age_s,
            "calibration": cal_data,
        }
