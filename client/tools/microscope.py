"""MCP tools — microscope control (focus, mag, beam, stigmation, state)."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from client.transport.zmq_client import ZMQClient
from client.state.manager import StateManager
from client.state.models import MicroscopeState


def register(mcp: FastMCP, zmq: ZMQClient, state: StateManager) -> None:

    @mcp.tool()
    async def get_focus() -> dict:
        """Return the current defocus value from the microscope."""
        data = await zmq.send_command("get_focus")
        state.microscope.focus_nm = data.get("focus_nm")
        return data

    @mcp.tool()
    async def change_focus(delta: float) -> dict:
        """Apply a relative focus change.

        Args:
            delta: Focus change in nanometres (positive = more overfocus).
        """
        data = await zmq.send_command("change_focus", {"delta_nm": delta})
        state.microscope.focus_nm = data.get("focus_nm")
        return data

    @mcp.tool()
    async def set_focus(focus: float) -> dict:
        """Set the absolute defocus value.

        Args:
            focus: Target focus value in nanometres (relative to eucentric).
        """
        return await zmq.send_command("set_focus", {"focus_nm": focus})

    @mcp.tool()
    async def autofocus() -> dict:
        """Trigger the DM autofocus routine and return the resulting focus value."""
        data = await zmq.send_command("autofocus")
        state.microscope.focus_nm = data.get("focus_nm")
        return data

    @mcp.tool()
    async def get_magnification() -> dict:
        """Return the current magnification and mag table index."""
        data = await zmq.send_command("get_magnification")
        state.microscope.magnification = data.get("magnification")
        state.microscope.mag_index = data.get("mag_index")
        return data

    @mcp.tool()
    async def set_magnification(mag_index: int) -> dict:
        """Set the magnification by index into the instrument's magnification table.

        Args:
            mag_index: Index into the microscope magnification table.
        """
        data = await zmq.send_command("set_magnification", {"mag_index": mag_index})
        state.microscope.magnification = data.get("magnification")
        state.microscope.mag_index = data.get("mag_index")
        return data

    @mcp.tool()
    async def get_microscope_state() -> dict:
        """Return a full snapshot of common microscope parameters (HT, mag, focus, beam, stage)."""
        data = await zmq.send_command("get_microscope_state")
        # Update cached state
        ms = MicroscopeState.model_validate({**state.microscope.model_dump(), **data})
        state.microscope = ms
        return data

    @mcp.tool()
    async def set_beam_blank(blanked: bool) -> dict:
        """Blank or unblank the electron beam.

        Args:
            blanked: True to blank, False to unblank.
        """
        data = await zmq.send_command("set_beam_blank", {"blanked": blanked})
        state.microscope.beam_blanked = blanked
        return data

    @mcp.tool()
    async def set_column_valves(open: bool) -> dict:
        """Open or close the column valves.

        Args:
            open: True to open, False to close.
        """
        return await zmq.send_command("set_column_valves", {"open": open})

    @mcp.tool()
    async def get_beam_shift() -> dict:
        """Return the current beam shift (x, y in metres)."""
        return await zmq.send_command("get_beam_shift")

    @mcp.tool()
    async def set_beam_shift(x: float, y: float) -> dict:
        """Set the beam shift.

        Args:
            x: Beam shift x component (metres).
            y: Beam shift y component (metres).
        """
        return await zmq.send_command("set_beam_shift", {"x": x, "y": y})

    @mcp.tool()
    async def get_beam_tilt() -> dict:
        """Return the current beam tilt (x, y in radians)."""
        return await zmq.send_command("get_beam_tilt")

    @mcp.tool()
    async def set_beam_tilt(x: float, y: float) -> dict:
        """Set the beam tilt.

        Args:
            x: Beam tilt x component (radians).
            y: Beam tilt y component (radians).
        """
        return await zmq.send_command("set_beam_tilt", {"x": x, "y": y})

    @mcp.tool()
    async def get_stigmation() -> dict:
        """Return objective and condenser stigmation values."""
        return await zmq.send_command("get_stigmation")

    @mcp.tool()
    async def set_stigmation(mode: int, x: float, y: float) -> dict:
        """Set stigmation.

        Args:
            mode: 0 = objective, 1 = condenser.
            x: Stigmation x value.
            y: Stigmation y value.
        """
        return await zmq.send_command("set_stigmation", {"mode": mode, "x": x, "y": y})

    @mcp.tool()
    async def execute_dm_script(script: str) -> dict:
        """Execute an arbitrary DM-Script string on the microscope computer (escape hatch).

        Use this for operations not covered by other tools.

        Args:
            script: DM-Script source code.
        """
        return await zmq.send_command("execute_script", {"script": script})
