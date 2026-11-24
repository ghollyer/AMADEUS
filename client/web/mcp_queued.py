"""FastMCP subclass whose tool calls go through the web console's queue.

The agent's MCP tool calls (over ``/mcp``) become ``agent`` blocks in the
queue, so the operator sees them, can pause before them and can remove them,
just like their own blocks. FastMCP's low-level handler is bound to
``self.call_tool`` at construction, so overriding it here is enough.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

from client.tools.operator_channel import OPERATOR_TOOLS


class QueuedFastMCP(FastMCP):
    queue = None      # BlockQueue, attached by the web app once its loop is running
    channel = None    # OperatorChannel

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        if self.queue is None or name in OPERATOR_TOOLS:
            result = await super().call_tool(name, arguments)
        else:
            tool = self._tool_manager.get_tool(name)
            if tool is None:
                return await super().call_tool(name, arguments)   # FastMCP's own error
            raw = await self.queue.submit_call(name, arguments, origin="agent")
            result = tool.fn_metadata.convert_result(raw)
        if self.channel is not None and name != "wait_for_operator":
            unread = self.channel.drain_unread()
            if unread:
                note = TextContent(type="text", text="operator_message: " + "\n".join(
                    e.text for e in unread) + "\n(The operator typed this in the web "
                    "console while you were working. Take it into account now.)")
                result = _append(result, note)
        return result

    async def run_raw(self, name: str, arguments: dict[str, Any]) -> Any:
        """Run a tool and return its Python result (no MCP conversion). Used
        by the queue to execute blocks."""
        return await self._tool_manager.call_tool(name, arguments, convert_result=False)


def _append(result, note: TextContent):
    if isinstance(result, CallToolResult):
        result.content = [*result.content, note]
        return result
    if isinstance(result, tuple) and len(result) == 2:
        content, structured = result
        return [*content, note], structured
    if isinstance(result, (list, tuple)):
        return [*result, note]
    return result
