"""MCP tools — experimental plan CRUD."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from client.state.manager import StateManager


def register(mcp: FastMCP, state: StateManager) -> None:

    @mcp.tool()
    async def get_experimental_plan() -> dict:
        """Return the current experimental plan."""
        return state.plan.model_dump()

    @mcp.tool()
    async def set_experimental_plan(
        objective: str | None = None,
        techniques: list[str] | None = None,
        target_magnifications: list[float] | None = None,
        target_particle_count: int | None = None,
        notes: str | None = None,
    ) -> dict:
        """Set or update the experimental plan.

        Args:
            objective: Plain-language description of the experiment goal.
            techniques: List of techniques to use (e.g. ["HAADF", "EELS", "EDX"]).
            target_magnifications: List of magnification values (×) to acquire at.
            target_particle_count: How many particles to examine.
            notes: Free-text notes.
        """
        kwargs = {}
        if objective is not None:
            kwargs["objective"] = objective
        if techniques is not None:
            kwargs["techniques"] = techniques
        if target_magnifications is not None:
            kwargs["target_magnifications"] = target_magnifications
        if target_particle_count is not None:
            kwargs["target_particle_count"] = target_particle_count
        if notes is not None:
            kwargs["notes"] = notes
        plan = state.set_plan(**kwargs)
        return plan.model_dump()
