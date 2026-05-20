"""MCP tools — operational preferences and sample metadata CRUD."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from client.state.manager import StateManager


def register(mcp: FastMCP, state: StateManager) -> None:

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
        max_beam_shift_nm: float | None = None,
    ) -> dict:
        """Update operational preferences.

        Args:
            blank_between_acquisitions: Whether to blank the beam between image acquisitions.
            post_move_pause_s: Seconds to wait after a stage move before acquiring.
            max_beam_shift_nm: Safety limit on beam shift magnitude in nanometres.
        """
        kwargs = {}
        if blank_between_acquisitions is not None:
            kwargs["blank_between_acquisitions"] = blank_between_acquisitions
        if post_move_pause_s is not None:
            kwargs["post_move_pause_s"] = post_move_pause_s
        if max_beam_shift_nm is not None:
            kwargs["max_beam_shift_nm"] = max_beam_shift_nm
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
