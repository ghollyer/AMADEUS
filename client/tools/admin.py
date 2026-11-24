"""MCP tools — instrument-side server lifecycle and health."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from client.transport.zmq_client import ZMQClient


def register(mcp: FastMCP, zmq: ZMQClient) -> None:

    @mcp.tool()
    async def backend_status() -> dict:
        """Report AMADEUS server health: every driver on this microscope
        (kind, where it runs, which slots it holds, whether it answers, and
        for a driver in its own process its pid and restart count), the
        routing of slots to drivers, and the relay's safety policy.
        """
        return await zmq.send_command("backend_status")

    @mcp.tool()
    async def shutdown_dm_server() -> dict:
        """Cleanly shut down the DM-side ZMQ server and unload its helper modules.

        The DM side acks, closes its sockets, and removes every ``dm_server.*``
        entry from ``sys.modules`` so the next ``exec`` of ``dm_server/server.py``
        in the DM Python console reimports helper code from source. Use after
        editing helpers on the microscope computer — DM has no other way to
        reset its embedded Python interpreter short of restarting GMS.

        Camera/scan/spectroscopy tools will fail until the DM server is
        restarted manually inside DM.

        KNOWN ISSUE: on the F200 this does not actually
        stop the DM server. Do not rely on it to stop the server or to load
        new helper code — the operator must restart Digital Micrograph.
        """
        return await zmq.send_command("dm.shutdown")

    @mcp.tool()
    async def release_column_driver() -> dict:
        """Release the driver holding the column (e.g. the PyJEM connection).

        The next column or stage tool call rebuilds it automatically, so this
        is a reset, not a permanent stop. The AMADEUS server and the other
        drivers keep running; the operator stops the server from the launcher.
        """
        return await zmq.send_command("shutdown_hw")
