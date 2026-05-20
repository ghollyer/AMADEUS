"""MCP tools — stage control."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from client.transport.zmq_client import ZMQClient
from client.state.manager import StateManager
from client.state.models import StagePosition


def register(mcp: FastMCP, zmq: ZMQClient, state: StateManager) -> None:

    @mcp.tool()
    async def get_stage_position() -> dict:
        """Return the current stage position (x_m, y_m, z_m in metres; multiply by 1e6 for µm; alpha_deg, beta_deg in degrees)."""
        data = await zmq.send_command("get_stage_position")
        state.microscope.stage = StagePosition.model_validate(data)
        return data

    @mcp.tool()
    async def move_stage_relative(
        x_nm: float = 0.0,
        y_nm: float = 0.0,
        z_nm: float = 0.0,
        alpha_deg: float = 0.0,
        beta_deg: float = 0.0,
    ) -> dict:
        """Move the stage by a relative offset.

        Args:
            x_nm: X offset in nanometres.
            y_nm: Y offset in nanometres.
            z_nm: Z offset in nanometres.
            alpha_deg: Alpha tilt change in degrees.
            beta_deg: Beta tilt change in degrees.
        """
        params = {
            "mode": "relative",
            "x_m": x_nm * 1e-9,
            "y_m": y_nm * 1e-9,
            "z_m": z_nm * 1e-9,
            "alpha_deg": alpha_deg,
            "beta_deg": beta_deg,
        }
        data = await zmq.send_command("move_stage", params)
        state.microscope.stage = StagePosition.model_validate(data)
        return data

    @mcp.tool()
    async def move_stage_absolute(
        x_nm: float | None = None,
        y_nm: float | None = None,
        z_nm: float | None = None,
        alpha_deg: float | None = None,
        beta_deg: float | None = None,
    ) -> dict:
        """Move the stage to an absolute position. Omit any axis to leave it unchanged.

        Args:
            x_nm: Target X position in nanometres.
            y_nm: Target Y position in nanometres.
            z_nm: Target Z position in nanometres.
            alpha_deg: Target alpha tilt in degrees.
            beta_deg: Target beta tilt in degrees.
        """
        params: dict = {"mode": "absolute"}
        if x_nm is not None:
            params["x_m"] = x_nm * 1e-9
        if y_nm is not None:
            params["y_m"] = y_nm * 1e-9
        if z_nm is not None:
            params["z_m"] = z_nm * 1e-9
        if alpha_deg is not None:
            params["alpha_deg"] = alpha_deg
        if beta_deg is not None:
            params["beta_deg"] = beta_deg
        data = await zmq.send_command("move_stage", params)
        state.microscope.stage = StagePosition.model_validate(data)
        return data

    @mcp.tool()
    async def save_stage_location(name: str) -> dict:
        """Save the current stage position under a named label for later recall.

        Args:
            name: Label for this location (e.g. "grid_centre", "particle_1").
        """
        data = await zmq.send_command("get_stage_position")
        pos = StagePosition.model_validate(data)
        state.save_location(name, pos)
        return {"saved": name, "position": data}

    @mcp.tool()
    async def go_to_saved_location(name: str) -> dict:
        """Move the stage to a previously saved location.

        Args:
            name: Label of the saved location.
        """
        pos = state.get_location(name)
        if pos is None:
            raise ValueError(f"No saved location named {name!r}")
        params = {
            "mode": "absolute",
            "x_m": pos.x_m,
            "y_m": pos.y_m,
            "z_m": pos.z_m,
            "alpha_deg": pos.alpha_deg,
            "beta_deg": pos.beta_deg,
        }
        data = await zmq.send_command("move_stage", params)
        state.microscope.stage = StagePosition.model_validate(data)
        return data

    @mcp.tool()
    async def list_saved_locations() -> dict:
        """Return all saved stage locations."""
        return {
            name: pos.model_dump()
            for name, pos in state.saved_locations.items()
        }
