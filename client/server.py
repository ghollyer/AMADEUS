"""AMADEUS MCP server — entry point for the control computer.

Run with:
    amadeus-server                          # everything from amadeus_config.json
    amadeus-server --amadeus-config /abs/path/amadeus_config.json
    amadeus-server --relay-address tcp://<microscope-pc>:9875 --data-dir /path/to/data
    amadeus-server --no-scripting           # no execute_dm_script / execute_tem3_call

Without a config file the defaults are tcp://localhost:9875 and ./amadeus_data.
Diagnostics go to stderr: stdout is the MCP stdio channel.

The server connects to the AMADEUS server (relay) on the microscope PC and
exposes all TEM control and analysis tools to an AI agent via MCP. The relay
runs the hardware backend itself and forwards everything else to the DM
server; it also republishes DM's live frames on the stream port.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from client.transport.zmq_client import ZMQClient
from client.transport.stream_subscriber import StreamSubscriber, stream_address_from
from client.state.manager import StateManager
from client.confirm import ChainConfirmer, ElicitConfirmer, MonitorConfirmer
from client.monitor import Monitor
from client.policy import PreferencePolicy
from client.tools import (
    microscope as _mic_tools,
    stem as _stem_tools,
    stage as _stage_tools,
    spectroscopy as _si_tools,
    eds as _eds_tools,
    files as _file_tools,
    analysis as _analysis_tools,
    image_analysis as _image_analysis_tools,
    focus as _focus_tools,
    map_tools as _map_tools,
    plan as _plan_tools,
    preferences as _pref_tools,
    admin as _admin_tools,
    live as _live_tools,
    session as _session_tools,
    windows as _window_tools,
)


def build_server(
    address: str,
    data_dir: Path,
    enable_monitor: bool = True,
    stream_address: str | None = None,
    timeout_ms: int = 30_000,
    instrument: dict | None = None,
    allow_scripting: bool = True,
    driver_kinds: set | None = None,
    mcp: FastMCP | None = None,
    confirmers: list | None = None,
) -> tuple[FastMCP, ZMQClient, StateManager, StreamSubscriber]:
    """*address* is the relay on the microscope PC. *instrument* is the
    config's ``instrument`` section (stage<->image mapping, etc.). *mcp* lets
    the web console pass its own FastMCP subclass (every call queued).
    *confirmers* go first in the operator-confirmation chain (the web
    console's dialog); amadeus-console and an MCP elicitation prompt follow."""
    mcp = mcp if mcp is not None else FastMCP("AMADEUS")
    zmq = ZMQClient(address, timeout_ms=timeout_ms)
    state = StateManager(data_dir)

    # Telemetry + operator pause gate. Attaching to the ZMQ client taps the
    # single chokepoint every instrument command flows through, so the monitor
    # GUI sees all traffic and can halt it without per-tool wiring.
    if enable_monitor:
        monitor = Monitor(state.data_dir / "monitor", state.images.root)
        zmq.set_monitor(monitor)
    # Operator confirmation (client/confirm.py): commands the relay or the
    # session preferences hold for a human's go-ahead are asked here, in the
    # first interface that can reach the operator, and declined otherwise.
    chain = list(confirmers or [])
    if enable_monitor:
        chain.append(MonitorConfirmer(state.data_dir / "monitor"))
    chain.append(ElicitConfirmer(mcp))
    zmq.set_confirmer(ChainConfirmer(chain))
    # Session preferences (beam blanking, post-move pause), also at the
    # chokepoint so every tool and the web queue get them.
    zmq.set_policy(PreferencePolicy(state))

    # Live-scan frame stream — receives PUB frames out-of-band and caches the
    # latest one for get_live_frame; writes a preview the console displays.
    # The relay republishes DM's frames on port 9877 of its own host, so the
    # default is "the relay address with the port swapped".
    stream_addr = stream_address or stream_address_from(address)
    subscriber = StreamSubscriber(stream_addr, monitor_dir=state.data_dir / "monitor")

    # Register tool groups
    # driver_kinds: the kinds in this microscope's config. Vendor-specific
    # tools (DM windows, execute_dm_script, execute_tem3_call) are registered
    # only where that vendor is present. None (no config) = all, as before.
    _mic_tools.register(mcp, zmq, state, allow_scripting=allow_scripting,
                        driver_kinds=driver_kinds)
    _stem_tools.register(mcp, zmq, state)
    _focus_tools.register(mcp, zmq, state)
    _stage_tools.register(mcp, zmq, state)
    _si_tools.register(mcp, zmq, state)
    _eds_tools.register(mcp, zmq, state)
    _file_tools.register(mcp, zmq, state)
    if driver_kinds is None or "dm" in driver_kinds:
        _window_tools.register(mcp, zmq, state)
    _analysis_tools.register(mcp, state, instrument=instrument)
    _image_analysis_tools.register(mcp, state, instrument=instrument)
    _map_tools.register(mcp, state)
    _plan_tools.register(mcp, state)
    _pref_tools.register(mcp, state, zmq)
    _admin_tools.register(mcp, zmq)
    _live_tools.register(mcp, zmq, state, subscriber)
    _session_tools.register(mcp, state)

    return mcp, zmq, state, subscriber


def _log(message: str) -> None:
    # stdout carries the MCP protocol; anything printed there corrupts it.
    print(message, file=sys.stderr, flush=True)


async def _startup_ping(address: str, endpoint: str) -> None:
    """Report reachability without delaying the MCP handshake. Uses its own
    short-lived socket so a timeout can't affect the main command socket."""
    try:
        async with ZMQClient(address, timeout_ms=5_000) as probe:
            py_ver = await probe.ping()
        _log(f"[amadeus] Connected to {endpoint} at {address}. "
             f"Python: {py_ver.splitlines()[0] if py_ver else '?'}")
    except Exception as exc:
        _log(f"[amadeus] Warning: could not ping {endpoint} at {address}: {exc}")
        _log("[amadeus] Client-side tools (analysis, map, plan) will still work. "
             "Run `amadeus-check` to diagnose the connection.")


def main() -> None:
    parser = argparse.ArgumentParser(description="AMADEUS MCP server")
    parser.add_argument(
        "--amadeus-config",
        default=None,
        help="unified amadeus_config.json (default: AMADEUS_CONFIG env var, "
             "then <repo>/amadeus_config.json)",
    )
    parser.add_argument(
        "--relay-address",
        default=None,
        help="ZMQ address of the instrument relay (default: from config, "
             "else tcp://localhost:9875)",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        type=Path,
        help="Directory for state JSON and image files (default: from config, "
             "else amadeus_data/)",
    )
    parser.add_argument(
        "--no-monitor",
        action="store_true",
        help="Disable the monitor telemetry/pause sidecar (no snapshot.json written).",
    )
    parser.add_argument(
        "--stream-address",
        default=None,
        help="ZMQ address of the DM live-frame PUB socket "
             "(default: from config, else the relay address with port 9877).",
    )
    parser.add_argument(
        "--timeout-s",
        default=None,
        type=float,
        help="per-command timeout in seconds (default: 30). Must cover the "
             "longest acquisition you run through MCP.",
    )
    parser.add_argument(
        "--no-scripting",
        action="store_true",
        help="don't register the scripting escape hatches (execute_dm_script, "
             "execute_tem3_call); overrides client.allow_scripting in the config",
    )
    args = parser.parse_args()

    from common import config as amadeus_config
    try:
        cfg = amadeus_config.load_or_none(args.amadeus_config)
    except amadeus_config.ConfigError as exc:
        parser.error(str(exc))
    settings = resolve_settings(args, cfg)
    if cfg is not None:
        _log(f"[amadeus] using config {cfg['_path']}")

    address = settings["address"]
    endpoint = "relay"

    mcp, zmq_client, _, subscriber = build_server(
        address, settings["data_dir"], enable_monitor=not args.no_monitor,
        stream_address=settings["stream_address"],
        timeout_ms=settings["timeout_ms"],
        instrument=(cfg or {}).get("instrument") or None,
        allow_scripting=settings["allow_scripting"],
        driver_kinds=driver_kinds_from(cfg),
    )
    if not settings["allow_scripting"]:
        _log("[amadeus] scripting tools disabled (execute_dm_script, execute_tem3_call)")

    async def _run() -> None:
        await zmq_client.connect()
        ping_task = asyncio.create_task(_startup_ping(address, endpoint))

        # Subscribe to the live-frame stream (idle until a scan starts).
        await subscriber.start()
        try:
            await mcp.run_stdio_async()
        finally:
            ping_task.cancel()
            await subscriber.stop()

    asyncio.run(_run())


def driver_kinds_from(cfg: dict | None) -> set | None:
    """Driver kinds in the config (``None`` without a config)."""
    if cfg is None:
        return None
    from common import config as amadeus_config
    return {spec.get("kind") for spec in amadeus_config.effective_drivers(cfg).values()}


def resolve_settings(args, cfg: dict | None) -> dict:
    """CLI flag > unified config > built-in default."""
    from common import config as amadeus_config
    address = args.relay_address
    stream = args.stream_address
    data_dir = args.data_dir
    if cfg is not None:
        address = address or amadeus_config.relay_address(cfg)
        # Only take the configured stream address if the command address also
        # came from the config. An explicit --relay-address keeps the old
        # "same host, port 9877" derivation.
        if stream is None and not args.relay_address:
            stream = amadeus_config.stream_address(cfg)
        data_dir = data_dir or amadeus_config.data_dir(cfg)
    timeout_s = args.timeout_s if args.timeout_s is not None else 30.0
    # --no-scripting can only turn scripting off, never back on over the config.
    allow_scripting = (not getattr(args, "no_scripting", False)
                       and bool((cfg or {}).get("client", {}).get("allow_scripting", True)))
    return {
        "address": address or "tcp://localhost:9875",
        "stream_address": stream,
        "data_dir": data_dir or Path("amadeus_data"),
        "timeout_ms": int(timeout_s * 1000),
        "allow_scripting": allow_scripting,
    }


if __name__ == "__main__":
    main()
