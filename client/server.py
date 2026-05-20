"""AMADEUS MCP server — entry point for the control computer.

Run with:
    amadeus-server                          # default DM address tcp://localhost:9876
    amadeus-server --dm-address tcp://<VM IP here>:9876
    amadeus-server --data-dir /path/to/data

The server connects to the DM ZMQ server on startup and exposes all TEM
control and analysis tools to an AI agent via MCP.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from client.transport.zmq_client import ZMQClient
from client.state.manager import StateManager
from client.tools import (
    microscope as _mic_tools,
    stem as _stem_tools,
    stage as _stage_tools,
    spectroscopy as _si_tools,
    eds as _eds_tools,
    files as _file_tools,
    analysis as _analysis_tools,
    map_tools as _map_tools,
    plan as _plan_tools,
    preferences as _pref_tools,
    admin as _admin_tools,
)


def build_server(dm_address: str, data_dir: Path) -> tuple[FastMCP, ZMQClient, StateManager]:
    mcp = FastMCP("AMADEUS")
    zmq = ZMQClient(dm_address)
    state = StateManager(data_dir)

    # Register tool groups
    _mic_tools.register(mcp, zmq, state)
    _stem_tools.register(mcp, zmq, state)
    _stage_tools.register(mcp, zmq, state)
    _si_tools.register(mcp, zmq, state)
    _eds_tools.register(mcp, zmq, state)
    _file_tools.register(mcp, zmq, state)
    _analysis_tools.register(mcp, state)
    _map_tools.register(mcp, state)
    _plan_tools.register(mcp, state)
    _pref_tools.register(mcp, state)
    _admin_tools.register(mcp, zmq)

    return mcp, zmq, state


def main() -> None:
    parser = argparse.ArgumentParser(description="AMADEUS MCP server")
    parser.add_argument(
        "--dm-address",
        default="tcp://localhost:9876",
        help="ZMQ address of the DM server (default: tcp://localhost:9876)",
    )
    parser.add_argument(
        "--data-dir",
        default="amadeus_data",
        type=Path,
        help="Directory for state JSON and image files (default: amadeus_data/)",
    )
    args = parser.parse_args()

    mcp, zmq_client, _ = build_server(args.dm_address, args.data_dir)

    async def _run() -> None:
        await zmq_client.connect()
        try:
            # Verify DM connection
            py_ver = await zmq_client.ping()
            print(f"[amadeus] Connected to DM server. DM Python: {py_ver.splitlines()[0]}")
        except Exception as exc:
            print(f"[amadeus] Warning: could not ping DM server: {exc}")
            print("[amadeus] Client-side tools (analysis, map, plan) will still work.")

        await mcp.run_stdio_async()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
