"""MCP tools for explicit task status and operator-visible activity."""

from __future__ import annotations

from datetime import datetime
from mcp.server.fastmcp import FastMCP
from client.state.manager import StateManager
from client.state.models import TaskStep
from client.state.session_events import SessionEventStore


def register(mcp: FastMCP, state: StateManager) -> None:
    events = SessionEventStore(state.data_dir)

    @mcp.tool()
    async def get_session_task() -> dict:
        """Return the current operator-visible task and progress."""
        return state.task.model_dump()

    @mcp.tool()
    async def update_session_task(objective: str | None = None,
                                  status: str | None = None,
                                  steps: list[str] | None = None,
                                  current_step: int | None = None,
                                  status_summary: str | None = None,
                                  blocking_condition: str | None = None) -> dict:
        """Update operator-visible task progress with a concise status summary."""
        kwargs = {"updated": datetime.utcnow()}
        for key, value in (("objective", objective), ("status", status),
                           ("current_step", current_step),
                           ("status_summary", status_summary),
                           ("blocking_condition", blocking_condition)):
            if value is not None:
                kwargs[key] = value
        if steps is not None:
            kwargs["steps"] = [TaskStep(label=s) for s in steps]
        task = state.set_task(**kwargs)
        events.append("task_update", actor="agent",
                      summary=status_summary or objective or "Task updated",
                      data=task.model_dump())
        return task.model_dump()

    @mcp.tool()
    async def report_agent_status(summary: str,
                                  category: str = "reasoning_summary") -> dict:
        """Publish a concise operator-visible activity/reasoning summary."""
        return events.append(category, actor="agent", summary=summary)

    @mcp.tool()
    async def list_session_activity(limit: int = 100) -> dict:
        """Return recent operator-visible session events."""
        return {"events": events.list(min(max(limit, 1), 500))}
