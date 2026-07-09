# AMADEUS

Addressable Microscope And Detector Experiment Unified Scripting (AMADEUS) provides an MCP server for microscope automation workflows, plus client-side state management and image analysis utilities.

## Overview

AMADEUS is split into two runtime components:

- `client/`: runs on the control computer and exposes MCP tools for instrument control, state, mapping, planning, preferences, and analysis.
- `dm_server/`: runs in the microscope software Python environment and receives ZMQ commands from the client.

The two components communicate over ZMQ using JSON messages. Image-producing commands return JSON metadata plus raw NumPy array bytes.

Vendor documentation, local debugging notes, and instrument-specific reference material are intentionally not included in this repository.

## Architecture

AMADEUS separates agent-facing orchestration from microscope-side execution. The MCP server runs on the control computer, stores experiment state locally, and forwards hardware commands over ZMQ. The microscope-side server is intentionally stateless: it handles one command, calls the relevant helper, returns data or an image, and forgets the request.

```text
AI agent / MCP client
        |
        | stdio MCP
        v
client/server.py
FastMCP tool registry
        |
        +--> client/state/
        |    StateManager, image store, maps, plans, preferences
        |
        +--> client/analysis/
        |    FFT, segmentation, EDS SI analysis
        |
        +--> client/transport/zmq_client.py
             JSON / multipart image messages over ZMQ
                         |
                         v
                  dm_server/server.py
                  stateless command dispatcher
                         |
                         v
                  dm_server/helpers/
                  microscope, stage, camera, STEM, SI, EDS, files
                         |
                         v
                  microscope software Python environment
```

## System Connections

```text
Agent / control computer
------------------------
MCP client
amadeus-server
client state + analysis
        |
        | ZMQ over TCP
        | tcp://<VM IP here>:9876
        v
DM computer / microscope PC
---------------------------
microscope software Python environment
dm_server/server.py
dm_server/helpers/
        |
        | vendor microscope control APIs
        v
Microscope hardware
-------------------
stage, beam, detectors, camera, spectroscopy
```

## Requirements

- Python 3.11 or newer (client)
- `uv` (client)
- Python 3.10 or newer (Digitalmicrograph)
- Network access or ethernet bridge cable to Gatan

## Install

```bash
uv pip install -e ".[dev]"
```

## Run The MCP Server

For same-machine development or a local mock server:

```bash
amadeus-server --dm-address tcp://localhost:9876 --data-dir ./amadeus_data
```

For a microscope VM or remote host:

```bash
amadeus-server --dm-address tcp://<VM IP here>:9876 --data-dir ./amadeus_data
```

## Run The Microscope-Side Server

In Digitalmicrograph, open 'server.py'

```python
exec(open(r"C:\path\to\AMADEUS\dm_server\server.py").read())
```

The server listens on port `9876` by default.

## Manual GUI

`dm_gui.py` provides a local manual-control GUI for the microscope-side environment.

When launching it from a console where `__file__` is unavailable, set `AMADEUS_HOME` to the checkout directory first so imports resolve correctly.

```python
exec(open(r"C:\path\to\AMADEUS\dm_gui.py").read())
```

## MCP Commands

Microscope control:

- `get_focus`
- `change_focus`
- `set_focus`
- `autofocus`
- `get_magnification`
- `set_magnification`
- `get_microscope_state`
- `set_beam_blank`
- `set_column_valves`
- `get_beam_shift`
- `set_beam_shift`
- `get_beam_tilt`
- `set_beam_tilt`
- `get_stigmation`
- `set_stigmation`
- `execute_dm_script`

Stage:

- `get_stage_position`
- `move_stage_relative`
- `move_stage_absolute`
- `save_stage_location`
- `go_to_saved_location`
- `list_saved_locations`

Imaging and detectors:

- `take_stem_image`
- `take_camera_image`
- `get_stem_settings`
- `get_detectors`
- `move_beam_to`
- `get_beam_position`
- `list_cameras`
- `get_camera_info`

Spectrum imaging:

- `configure_si`
- `start_si`
- `stop_si`
- `get_si_status`

EDS:

- `eds_acquire_point`
- `eds_acquire_map`
- `eds_describe_si`
- `eds_extract_elemental_map`
- `eds_extract_multiple_maps`
- `eds_quantify_si`

File and image store:

- `list_remote_dm_files`
- `fetch_dm_file`
- `list_local_images`
- `inspect_image`

Client-side analysis:

- `compute_fft`
- `extract_lattice_spacings`
- `segment_image`
- `convert_pixel_to_stage`

Map:

- `add_map_object`
- `list_map_objects`
- `get_map_object`
- `mark_object_examined`
- `link_image_to_object`

Plan, preferences, and sample metadata:

- `get_experimental_plan`
- `set_experimental_plan`
- `get_operational_preferences`
- `set_operational_preferences`
- `get_sample_metadata`
- `set_sample_metadata`

Admin:

- `shutdown_dm_server`

## Tests

```bash
uv run --with pytest --with pytest-asyncio pytest
```

The tests run offline using mocks; they do not require a live microscope connection.

## Disclaimers

Not affiliated with or endorsed by Gatan Inc. DigitalMicrograph is a trademark of Gatan Inc.

This software is provided "AS IS", without warranty of any kind, express or implied. Use at your own risk.