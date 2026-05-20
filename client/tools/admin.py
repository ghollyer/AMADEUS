"""MCP tools — DM server lifecycle (shutdown / module unload)."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from client.transport.zmq_client import ZMQClient


def register(mcp: FastMCP, zmq: ZMQClient) -> None:

    @mcp.tool()
    async def shutdown_dm_server() -> dict:
        """Cleanly shut down the DM-side ZMQ server and unload its helper modules.

        Sends the ``shutdown`` command to the DM server. The DM side acks,
        closes its REP socket, terminates the ZMQ context, and removes every
        ``dm_server.*`` entry from ``sys.modules`` so the next ``exec`` of
        ``dm_server/server.py`` in the DM Python console reimports helper
        code from source.

        Use this after editing helpers on the microscope computer, to pick
        up changes without restarting Digital Micrograph (which has no
        other way to reset its embedded Python interpreter).

        Microscope-control tools will fail with ZMQ errors until the DM
        server is restarted manually inside DM.
        """
        return await zmq.send_command("shutdown")
