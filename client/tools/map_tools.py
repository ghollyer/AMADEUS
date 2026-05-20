"""MCP tools — specimen map CRUD (stage locations + examined state)."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from client.state.manager import StateManager
from client.state.models import MapObject, StagePosition


def register(mcp: FastMCP, state: StateManager) -> None:

    @mcp.tool()
    async def add_map_object(
        label: str,
        x_nm: float,
        y_nm: float,
        z_nm: float = 0.0,
        notes: str = "",
    ) -> dict:
        """Add a new object to the specimen map at the given stage position.

        Args:
            label: Human-readable label (e.g. "particle_1", "region_A").
            x_nm: Stage X position in nanometres.
            y_nm: Stage Y position in nanometres.
            z_nm: Stage Z position in nanometres.
            notes: Optional free-text notes.
        """
        obj = MapObject(
            label=label,
            position=StagePosition(x_m=x_nm * 1e-9, y_m=y_nm * 1e-9, z_m=z_nm * 1e-9),
            notes=notes,
        )
        state.add_map_object(obj)
        return obj.model_dump()

    @mcp.tool()
    async def list_map_objects(examined: bool | None = None) -> dict:
        """List all objects in the specimen map.

        Args:
            examined: If True, return only examined objects. If False, only unexamined.
                      If None (default), return all.
        """
        objs = state.list_map_objects()
        if examined is not None:
            objs = [o for o in objs if o.examined == examined]
        return {"objects": [o.model_dump() for o in objs]}

    @mcp.tool()
    async def get_map_object(object_id: str) -> dict:
        """Return details for a specific map object.

        Args:
            object_id: UUID of the map object.
        """
        obj = state.get_map_object(object_id)
        if obj is None:
            raise ValueError(f"Map object {object_id!r} not found.")
        return obj.model_dump()

    @mcp.tool()
    async def mark_object_examined(object_id: str, examined: bool = True) -> dict:
        """Mark a map object as examined (or unexamined).

        Args:
            object_id: UUID of the map object.
            examined: True to mark as examined, False to reset.
        """
        obj = state.update_map_object(object_id, examined=examined)
        return obj.model_dump()

    @mcp.tool()
    async def link_image_to_object(object_id: str, image_id: str) -> dict:
        """Associate a stored image with a map object.

        Args:
            object_id: UUID of the map object.
            image_id: ID of the image to link.
        """
        obj = state.get_map_object(object_id)
        if obj is None:
            raise ValueError(f"Map object {object_id!r} not found.")
        updated_ids = obj.image_ids + [image_id]
        obj = state.update_map_object(object_id, image_ids=updated_ids)
        return obj.model_dump()
