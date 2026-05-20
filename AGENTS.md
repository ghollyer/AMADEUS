# AMADEUS Agent Notes

## Project Overview

AMADEUS (Addressable Microscope And Detector Experiment Unified Scripting) provides an MCP interface for microscope automation and analysis.

The system has two sides that communicate over ZMQ:

- `client/`: MCP server on the control computer. It owns application state, image storage, maps, plans, preferences, and client-side analysis tools.
- `dm_server/`: ZMQ REP server intended to run inside the microscope software environment. It dispatches instrument commands through helper modules and returns data or image arrays.

## Repository Layout

```text
AMADEUS/
├── pyproject.toml
├── client/
│   ├── server.py
│   ├── transport/zmq_client.py
│   ├── state/
│   ├── tools/
│   └── analysis/
├── dm_server/
│   ├── server.py
│   └── helpers/
├── tests/
└── dm_gui.py
```

## Development Commands

```bash
uv pip install -e ".[dev]"
uv run --with pytest --with pytest-asyncio pytest
amadeus-server --dm-address tcp://<VM IP here>:9876 --data-dir ./amadeus_data
```

Use `tcp://localhost:9876` for local mock or same-machine development. Use `tcp://<VM IP here>:9876` when connecting across machines.

## Architecture Rules

- Keep `dm_server/` stateless. Each request should be handled independently and return one response.
- Keep durable state in `client/state/` through `StateManager` and `ImageStore`.
- MCP tools should be registered through `register(mcp, ...)` functions. Avoid module-level state for tool behavior.
- Use `ZMQClient.send_command()` for data-only commands and `ZMQClient.send_command_image()` for commands that return arrays.
- The dispatcher should continue returning structured JSON errors for unimplemented instrument capabilities.

## Adding Commands

1. Add or update a helper in `dm_server/helpers/`.
2. Register the command in `dm_server/server.py`.
3. Add the client-facing MCP tool in `client/tools/`.
4. Register new tool modules in `client/server.py`.
5. Add focused tests for helper behavior and ZMQ round trips.

## Testing Notes

- The test suite is offline and mocks the microscope software module.
- Keep the microscope module mock installed before importing helper modules in `tests/test_dm_helpers.py`.
- `tests/test_zmq_protocol.py` uses an in-process mock ZMQ server on port `29876`.

## Repository Hygiene

- Do not commit vendor documentation, instrument-specific notes, local logs, virtual environments, caches, or generated build metadata.
- Do not commit real IP addresses, hostnames, credentials, access keys, or local mapped drive paths. Use placeholders such as `<VM IP here>`.
- Keep comments technical and concise. Remove debugging notes once the behavior is covered by tests.
