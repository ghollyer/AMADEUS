"""MCP tools — stage control."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from client.transport.zmq_client import ZMQClient
from client.state.manager import StateManager
from client.state.models import StagePosition
from client.stage_moves import move_options as _move_options


def _update_stage_cache(state: StateManager, data: dict) -> None:
    if all(k in data for k in ("x_m", "y_m", "z_m")):
        state.microscope.stage = StagePosition.model_validate(data)


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
        speed: str | None = None,
        drive: str | None = None,
        approach: bool | None = None,
        wait: bool = True,
        timeout_s: float = 20.0,
    ) -> dict:
        """Move the stage by a relative offset. Axes left at 0 are not driven.

        Args:
            x_nm: X offset in nanometres.
            y_nm: Y offset in nanometres.
            z_nm: Z offset in nanometres.
            alpha_deg: Alpha tilt change in degrees.
            beta_deg: Beta tilt change in degrees.
            speed: "slow" | "normal" | "fast" for this move only (restored
                afterwards). Slow is gentler and more repeatable for short moves.
            drive: "motor" (default; coarse, +-100 um) or "piezo" (fine X/Y only,
                +-10 um total travel; best for sub-100 nm nudges). Restored to
                motor afterwards.
            approach: True = backlash-compensated X/Y move. It overshoots and then
                approaches the target from the calibrated direction. Use it when
                centring a feature precisely; reversing direction otherwise loses
                ~0.5 um on the F200. None = the instrument default.
            wait: wait for the stage to settle (default True).
            timeout_s: how long to wait. If the stage hasn't settled by then
                the reply has settled=false and the move keeps running; check
                get_stage_status, and stop_stage aborts it.
        """
        params: dict = {"mode": "relative"}
        for key, value, scale in (("x_m", x_nm, 1e-9), ("y_m", y_nm, 1e-9),
                                  ("z_m", z_nm, 1e-9), ("alpha_deg", alpha_deg, 1.0),
                                  ("beta_deg", beta_deg, 1.0)):
            if value:
                params[key] = value * scale
        if len(params) == 1:
            raise ValueError("move_stage_relative: every offset is zero")
        timeout_ms = _move_options(params, speed, drive, approach, wait, timeout_s)
        data = await zmq.send_command("move_stage", params, timeout_ms=timeout_ms)
        _update_stage_cache(state, data)
        return data

    @mcp.tool()
    async def move_stage_absolute(
        x_nm: float | None = None,
        y_nm: float | None = None,
        z_nm: float | None = None,
        alpha_deg: float | None = None,
        beta_deg: float | None = None,
        speed: str | None = None,
        drive: str | None = None,
        approach: bool | None = None,
        wait: bool = True,
        timeout_s: float = 20.0,
    ) -> dict:
        """Move the stage to an absolute position. Omit any axis to leave it unchanged.
        With drive="piezo", x_nm/y_nm are piezo positions (+-10 um), not motor ones.

        Args:
            x_nm: Target X position in nanometres.
            y_nm: Target Y position in nanometres.
            z_nm: Target Z position in nanometres.
            alpha_deg: Target alpha tilt in degrees.
            beta_deg: Target beta tilt in degrees.
            speed: "slow" | "normal" | "fast" for this move only (restored
                afterwards). Slow is gentler and more repeatable for short moves.
            drive: "motor" (default; coarse, +-100 um) or "piezo" (fine X/Y only,
                +-10 um total travel; best for sub-100 nm nudges). Restored to
                motor afterwards.
            approach: True = backlash-compensated X/Y move. It overshoots and then
                approaches the target from the calibrated direction. Use it when
                centring a feature precisely; reversing direction otherwise loses
                ~0.5 um on the F200. None = the instrument default.
            wait: wait for the stage to settle (default True).
            timeout_s: how long to wait. If the stage hasn't settled by then
                the reply has settled=false and the move keeps running; check
                get_stage_status, and stop_stage aborts it.
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
        timeout_ms = _move_options(params, speed, drive, approach, wait, timeout_s)
        data = await zmq.send_command("move_stage", params, timeout_ms=timeout_ms)
        _update_stage_cache(state, data)
        return data

    @mcp.tool()
    async def get_stage_status() -> dict:
        """Return whether the stage is moving (per axis, plus any background
        move still running), hardware-limit errors, the drive mode
        (motor/piezo), speed modes and piezo position."""
        return await zmq.send_command("get_stage_status")

    @mcp.tool()
    async def stop_stage() -> dict:
        """Immediately stop all stage drives, including a move still running
        from an earlier call. Sent on its own connection, so it never waits
        behind a move in progress; the operator pause doesn't block it."""
        return await zmq.send_command_out_of_band("stop_stage", timeout_ms=10_000)

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
    async def go_to_saved_location(name: str, approach: bool | None = None,
                                   timeout_s: float = 30.0) -> dict:
        """Move the stage to a previously saved location.

        Args:
            name: Label of the saved location.
            approach: backlash-compensated X/Y approach (see move_stage_absolute).
            timeout_s: how long to wait for the stage to settle.
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
        timeout_ms = _move_options(params, None, None, approach, True, timeout_s)
        data = await zmq.send_command("move_stage", params, timeout_ms=timeout_ms)
        _update_stage_cache(state, data)
        return data

    @mcp.tool()
    async def list_saved_locations() -> dict:
        """Return all saved stage locations."""
        return {
            name: pos.model_dump()
            for name, pos in state.saved_locations.items()
        }
