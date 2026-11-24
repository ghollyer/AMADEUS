"""MCP tools — operational preferences and sample metadata CRUD."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from client.state.manager import StateManager


def register(mcp: FastMCP, state: StateManager, zmq=None) -> None:
    """*zmq* (the ZMQClient) supplies the operator confirmer: loosening a
    safety preference needs the operator's go-ahead."""

    # ------------------------------------------------------------------
    # Operational preferences
    # ------------------------------------------------------------------

    @mcp.tool()
    async def get_operational_preferences() -> dict:
        """Return current operational preferences (scan presets, blanking policy, etc.)."""
        return state.preferences.model_dump()

    @mcp.tool()
    async def set_operational_preferences(
        blank_between_acquisitions: bool | None = None,
        post_move_pause_s: float | None = None,
        max_beam_shift: float | None = None,
    ) -> dict:
        """Update operational preferences.

        Args:
            blank_between_acquisitions: Whether to blank the beam between image acquisitions.
            post_move_pause_s: Seconds to wait after a stage move before acquiring.
            max_beam_shift: Limit on beam-shift excursions from neutral, in
                the device units get_beam_shift reports. A set_beam_shift
                beyond it needs the operator's confirmation.

        Loosening a safety setting (raising max_beam_shift, turning
        blank_between_acquisitions off) asks the operator first. If they
        decline, nothing changes.
        """
        current = state.preferences
        loosened = []
        if max_beam_shift is not None and max_beam_shift > current.max_beam_shift:
            loosened.append(f"max_beam_shift {current.max_beam_shift:g} -> {max_beam_shift:g}")
        if blank_between_acquisitions is False and current.blank_between_acquisitions:
            loosened.append("blank_between_acquisitions on -> off")
        if loosened:
            if zmq is None:
                raise RuntimeError("loosening a safety preference needs the operator's "
                                   "go-ahead, and no operator interface is attached")
            await zmq.confirm("set_operational_preferences",
                              {"changes": loosened},
                              "this loosens a safety setting: " + "; ".join(loosened) + ".")
        kwargs = {}
        if blank_between_acquisitions is not None:
            kwargs["blank_between_acquisitions"] = blank_between_acquisitions
        if post_move_pause_s is not None:
            kwargs["post_move_pause_s"] = post_move_pause_s
        if max_beam_shift is not None:
            kwargs["max_beam_shift"] = max_beam_shift
        prefs = state.set_preferences(**kwargs)
        return prefs.model_dump()

    # ------------------------------------------------------------------
    # Sample metadata
    # ------------------------------------------------------------------

    @mcp.tool()
    async def get_sample_metadata() -> dict:
        """Return the current sample metadata."""
        return state.sample.model_dump()

    @mcp.tool()
    async def set_sample_metadata(
        name: str | None = None,
        elements: list[str] | None = None,
        phases: list[str] | None = None,
        grid_type: str | None = None,
        notes: str | None = None,
    ) -> dict:
        """Set or update sample metadata.

        Args:
            name: Sample name or identifier.
            elements: List of elemental symbols present (e.g. ["Fe", "O", "Ni"]).
            phases: Expected crystal phases (e.g. ["Fe3O4", "FeO"]).
            grid_type: TEM grid description (e.g. "lacey carbon", "holey SiN 50nm").
            notes: Free-text notes about the sample.
        """
        kwargs = {}
        if name is not None:
            kwargs["name"] = name
        if elements is not None:
            kwargs["elements"] = elements
        if phases is not None:
            kwargs["phases"] = phases
        if grid_type is not None:
            kwargs["grid_type"] = grid_type
        if notes is not None:
            kwargs["notes"] = notes
        sample = state.set_sample(**kwargs)
        return sample.model_dump()
