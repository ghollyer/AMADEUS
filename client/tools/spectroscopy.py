"""MCP tools — spectrum image (SI) acquisition control."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from client.transport.zmq_client import ZMQClient
from client.state.manager import StateManager


def register(mcp: FastMCP, zmq: ZMQClient, state: StateManager) -> None:

    @mcp.tool()
    async def configure_si(
        mode: str = "2D Array",
        pixel_time_s: float | None = 0.05,
        signal: int = 1,
        # 2D Array
        x_samples: int | None = None,
        y_samples: int | None = None,
        sampling: float | None = None,
        sampling_lock: bool | None = None,
        # Line Scan
        samples: int | None = None,
        averaging: bool | None = None,
        averaging_points: int | None = None,
        averaging_angle: float | None = None,
        # Multi-Point
        points: int | None = None,
        # Time Series
        iterations: int | None = None,
        delay_time_s: float | None = None,
        scan_2d: bool | None = None,
        scan_2d_x_points: int | None = None,
        scan_2d_y_points: int | None = None,
        # Advanced
        passes: int | None = None,
        drift: bool | None = None,
    ) -> dict:
        """Configure a spectrum-image (SI) acquisition for the chosen mode.

        Grabs a fresh survey image (DSInvokeButton(3)), enters the SI mode
        (which assigns the survey + default ROI), applies the requested
        field values, and enables Scan + the chosen Signal channel. Does
        not trigger Capture — call ``start_si`` for that.

        Args:
            mode: One of "2D Array", "Line Scan", "Multi-Point", "Time Series".
            pixel_time_s: Per-pixel exposure (the "Pixel Time" palette field).
            signal: Signal channel to enable (1-5). Defaults to 1.
            x_samples, y_samples, sampling, sampling_lock: 2D Array fields.
            samples, averaging, averaging_points, averaging_angle: Line Scan fields.
            points: Multi-Point field (number of points).
            iterations, delay_time_s, scan_2d, scan_2d_x_points, scan_2d_y_points:
                Time Series fields.
            passes: Number of acquisition passes.
            drift: Toggle spatial drift correction.
        """
        args: dict = {"mode": mode, "signal": signal}
        if pixel_time_s is not None:
            args["pixel_time_s"] = pixel_time_s
        for name, val in [
            ("x_samples", x_samples), ("y_samples", y_samples),
            ("sampling", sampling), ("sampling_lock", sampling_lock),
            ("samples", samples), ("averaging", averaging),
            ("averaging_points", averaging_points),
            ("averaging_angle", averaging_angle),
            ("points", points),
            ("iterations", iterations), ("delay_time_s", delay_time_s),
            ("scan_2d", scan_2d),
            ("scan_2d_x_points", scan_2d_x_points),
            ("scan_2d_y_points", scan_2d_y_points),
            ("passes", passes), ("drift", drift),
        ]:
            if val is not None:
                args[name] = val
        return await zmq.send_command("si_configure", args)

    @mcp.tool()
    async def start_si() -> dict:
        """Start the configured SI acquisition (triggers the Capture button)."""
        return await zmq.send_command("si_start")

    @mcp.tool()
    async def stop_si() -> dict:
        """Stop the current SI acquisition."""
        return await zmq.send_command("si_stop")

    @mcp.tool()
    async def get_si_status() -> dict:
        """Return the SI mode and whether acquisition is active or paused."""
        return await zmq.send_command("si_get_settings")
