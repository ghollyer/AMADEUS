# AMADEUS — Claude Code Instructions

## Project Overview

AMADEUS (Addressable Microscope And Detector Experiment Unified Scripting) is a PhD research project for AI-driven automation of a JEOL F200 transmission electron microscope (TEM) fitted with Gatan Digital Micrograph (GMS).

The system is three processes over ZMQ:

- **`client/`**: MCP server on the control laptop. Holds all state. Exposes TEM control and analysis as tools to an AI agent via FastMCP. Connects to the relay.
- **AMADEUS server** (`relay/` + `drivers/`): one process in the **vjem310 conda env** (Python 3.10, Windows) on the microscope PC, and the only network listener there (command port 9875, live stream 9877). It routes every command to the *driver* holding its slot. On the F200 the JEOL driver (`drivers/jeol/`) drives the column and stage through JEOL's first-party PyJEM TEM3 API: focus, magnification, beam shift/tilt, stigmation, stage, apertures, valve, blanking, HT, spot, alpha, TEM/STEM mode. The DM driver forwards acquisition to DM, and the relay republishes DM's live frames.
- **`dm_server/`**: stateless ZMQ REP server *inside* Digital Micrograph, bound to **127.0.0.1** (REP 9876, live PUB 9879). Handles everything Gatan-side: cameras, DigiScan STEM acquisition, live-scan streaming, spectrum imaging, EDS, .dm3/.dm4 file access.

**Division of labour:** anything that controls microscope *hardware* goes through PyJEM (JEOL's first-party API); anything involving cameras, detector signals, scan patterns/image acquisition, spectroscopy, or data files goes through DM.

**Drivers (AMADEUS 1.0, config schema v4, `docs/ADDING_HARDWARE.md`):** JEOL and DM are two *drivers*. A driver fills *slots* (`column`, `stage`, `scan`, `camera`, `live`, `spectrometer`, `files`). The relay routes each command to the driver holding its slot, and picks a device in multi-device slots from `params["device"]` (a device id `"<driver>/<kind>/<n>"`, a local id `"<kind>/<n>"`, or a role like `"detector:HAADF"`). The config's `drivers` section is the only description of the hardware; the default is `hw` (JEOL, offline: column + stage) and `dm` (the acquisition slots). There are no pre-1.0 compatibility shims: old wire names, deprecated tool arguments and reply keys, and the `hw_server` package are gone.

## Repository Layout

```
AMADEUS/
├── pyproject.toml                  # dependencies, scripts, pytest config
├── amadeus_config.example.json     # unified deployment config template (IPs, ports, paths, modes)
├── AMADEUS.bat                     # Windows double-click: first-run setup, then the launcher
├── dm_gui.py                       # in-DM manual control GUI (calls dm_server.helpers directly)
├── operator/                       # locked-down agent workspace (--no-scripting, deny-list)
├── docs/ADDING_HARDWARE.md         # how to add a vendor driver (slots, rules, config, tests)
├── docs/VM_DEV.md                  # trying DM-side changes in a Windows VM (offline GMS)
├── drivers/                        # vendor drivers (Py3.10, stdlib + common, no numpy)
│   ├── base.py                     # Driver + one interface per slot, ImageResult, levels
│   ├── values.py                   # validation + neutral reply helpers (device_value, ...)
│   ├── calibration.py              # InstrumentCalibration (generic + per-driver defaults):
│   │                               #   lens/deflector roles, focus DAC↔nm, mag tables, stage
│   ├── jeol/                       # JEOL PyJEM TEM3 driver (online / offline)
│   │   ├── driver.py               #   JeolDriver: column + stage, JEOL_CALIBRATION defaults
│   │   └── stage_motion.py         #   stage drive calls in a child process
│   ├── sim_column.py               # SimColumnDriver: simulated column + stage
│   ├── dispatch.py                 # request -> driver call -> JSON / image frames
│   ├── registry.py                 # kind -> class (built-in, "class", entry points)
│   ├── host.py                     # one driver in its own process (python -m drivers.host)
│   ├── dm.py                       # relay-side DM adapter (names, devices -> DM indices)
│   ├── sim.py                      # SimDriver: every slot, synthetic sample, no DM needed
│   ├── contract.py                 # amadeus-driver-test: driver vs the interfaces
│   ├── smoke.py                    # read-only first check of a configured driver
│   └── template.py                 # starting point for a new vendor
├── scripts/
│   ├── pyjem_stage_probe.py        # MOVES the stage: does SetXRel block / hold the GIL?
│   ├── start_amadeus_dm.py         # run INSIDE DM; reads ports from the config
│   ├── write_mcp_config.py         # register the MCP server (no IPs in the entry)
│   └── find_vjem310.bat            # locates vjem310 python for AMADEUS.bat
├── common/                         # shared by relay + drivers + tests (Py3.10, stdlib only)
│   ├── protocol.py                 # request/response builders, default ports
│   ├── commands.py                 # SLOT_COMMANDS: command -> slot (what routing uses)
│   ├── roles.py                    # detector/camera role guesses from the hardware
│   ├── config.py                   # amadeus_config.json loader/validator + derived addresses
│   ├── netutil.py                  # adapter/IP listing, TCP probe, ping
│   └── dmscript_lint.py            # offline DM-Script syntax checker (tests + CLI)
├── setup_wizard/                   # Tk, stdlib + common only, Py3.10 (runs under vjem310)
│   ├── wizard.py                   # setup pages + `amadeus-setup --pc` (laptop fetches config)
│   ├── launcher.py                 # AMADEUS.bat: server start/stop, health, Settings
│   ├── netdiag.py                  # read-only Windows firewall/listener doctor (+ opt-in fix)
│   ├── readiness.py                # post-install checklist shown by the launcher
│   └── mcp_config.py               # MCP host entry generation
├── relay/                          # the AMADEUS server process
│   ├── server.py                   # ROUTER front door, routing, continuations, stream
│   │                               #   proxy, get_config / get_capabilities / restart_driver
│   ├── links.py                    # RemoteLink, InProcessLink, HostedLink (supervised)
│   ├── routing.py                  # RoutingTable: slots, devices, roles, extras, aliases
│   ├── policy.py                   # stage limits, confirm gates, exclusive device groups
│   ├── recovery.py                 # request-id dedupe, read-back after a timeout
│   └── config.py                   # RelayConfig from the unified config + CLI
├── client/
│   ├── server.py                   # FastMCP entry point (amadeus-server)
│   ├── diagnostics.py              # amadeus-check: layered connection checks
│   ├── operator_console.py         # amadeus-console (+ --read-only passive monitor)
│   ├── web/                        # amadeus-web: browser console, action queue, HTTP /mcp
│   │   ├── app.py  queue.py  agent_channel.py  mcp_queued.py
│   │   └── static/                 # no-build UI (Preact/htm, dockview, Blockly vendored)
│   ├── blocks/                     # action blocks: model, expr, catalog (TOOL_META),
│   │                               #   capabilities, executor (vendor-neutral)
│   ├── gui/                        # console parts: bridge.py, control_panel.py, render.py
│   ├── monitor.py                  # server-side telemetry snapshot + pause gate
│   ├── policy.py                   # session preferences enforced (blanking, post-move pause)
│   ├── devices.py                  # detector/camera args: role / device id / index
│   ├── transport/
│   │   ├── zmq_client.py           # async ZMQ REQ client (command channel)
│   │   └── stream_subscriber.py    # async ZMQ SUB — live-scan frame stream
│   ├── state/                      # Pydantic models, StateManager, ImageStore
│   ├── tools/                      # MCP tool handlers (one file per domain)
│   └── analysis/                   # FFT, segmentation, EDS SI, stage_mapping (pixel→stage),
│                                   #   particles (rods/clusters/shell), imaging (focus metric,
│                                   #   registration, Jacobian fit, PNG), eds_regions
├── dm_server/
│   ├── server.py                   # ZMQ REP dispatcher (9876) + PUB live stream (9879), 127.0.0.1
│   └── helpers/                    # camera, digiscan, live, spectroscopy, eds, files, windows
│                                   # (+ microscope.py, stage.py — DM fallback path only)
├── tests/
│   ├── test_jeol_driver.py         # JEOL driver (fake PyJEM), column/stage sim, dispatch
│   ├── test_relay.py               # routing, passthrough, fallback toggle, timeouts,
│   │                               #   in-process hw thread, stream proxy, get_config
│   ├── test_zmq_protocol.py        # client ↔ mock DM server round trips
│   ├── test_dm_helpers.py          # DM helper unit tests (mocked DM)
    └── test_state / test_stream / test_analysis / test_monitor / test_eds_si
```

## Running

Normal setup is GUI-driven. On the microscope PC, `AMADEUS.bat` runs first-run setup and then the launcher, which starts the AMADEUS server (`python -m relay`). Run `scripts/start_amadeus_dm.py` inside DM. On the laptop, `amadeus-setup --pc <ip>` fetches the config from the running server, then `amadeus-check` verifies. Every entry point reads the config (`--amadeus-config` → `AMADEUS_CONFIG` → `<repo>/amadeus_config.json` → defaults), and **CLI flags beat the config**.

```bash
# Install on the control computer (use uv — pip is not available on this machine)
uv pip install -e ".[dev]"

# Microscope PC, inside DM (File > Open, Execute):
#   scripts\start_amadeus_dm.py

# Microscope PC, vjem310 conda env: the AMADEUS server
python -m relay                              # from amadeus_config.json; no config = JEOL offline
python -m relay --online | --offline | --sim # column driver override (--dm <addr> too)

# Control computer:
amadeus-setup --pc <microscope-pc>           # fetch config + register MCP server
amadeus-server --relay-address tcp://<microscope-pc>:9875 --data-dir ./data
amadeus-web                                  # browser console on http://127.0.0.1:8765/ (+ /mcp)

# Run tests (all offline — no DM, no PyJEM, no instrument)
uv run --with pytest --with pytest-asyncio pytest

# Drivers
amadeus-driver-test --kind sim --writes --acquire  # contract checks (drivers/contract.py)
python -m drivers.smoke --driver hw                # read-only, on the instrument; Readiness needs it
python -m relay --sim                              # sim column/stage + DM; a "drivers" section
                                                   #   with {"kind": "sim", "slots": [...all]} needs no DM
```

**First time on a new instrument:** run `python -m drivers.smoke --driver hw` in the vjem310 env (read-only) and confirm the stage readout matches the console before trusting closed-loop moves. Readiness requires it for every online driver.

## Key Architectural Rules

### Deployment config (`common/config.py`)
- One JSON file, `amadeus_config.json` (git-ignored; template `amadeus_config.example.json`), holds every IP/port/path/mode for **both** machines. New settings go there, not into new per-script constants or hand-edited files.
- Precedence everywhere: CLI flag > config file > default. With no config file, entry points use the built-in defaults (the relay: JEOL *offline*; `--online` drives a real column). Use `load_or_none()` in entry points.
- The instrument calibration lives in the config's `instrument` section (format: `drivers/calibration.py`); a driver's `calibration` file path, if set, layers on top. The **instrument profile** (`export_profile` / `load_profile` / `restore_profile` in `common/config.py`; launcher **Export profile…**, first-run **Restore**, `python -m setup_wizard --export/--restore`) is the config minus `MACHINE_LOCAL` paths, with any calibration file folded in. Anything new that a reinstall would need belongs in the config, so the profile carries it. Machine-local values must be re-detectable and listed in `MACHINE_LOCAL`.
- `setup_wizard/readiness.py` is the post-install checklist (pure `readiness()` + `gather()`). `python -m drivers.smoke --driver <name>` records `logs/driver_smoke_<name>.json`, which readiness uses to require a passing read-only check per online driver, install and interpreter.
- Ports: `relay` 9875 and `stream` 9877 are public (relay process); `dm` 9876 and `dm_stream` 9879 are DM-internal (loopback). Schema `version` 4; `migrate()` upgrades v1 files (which had `ports.hw` and a public `dm_stream`) and v2/v3 files (their `hw_server` section and `relay.hardware_backend` become a `drivers` section, and focus `lens: "olf"/"olc"` becomes `focus.fine`/`focus.coarse`). Laptops get the config via the relay-local `get_config` command. Don't reintroduce hand-copying.
- `drivers` (`{name: {kind, slots, host, python, offline, stream, options, class, calibration}}`) is replaced as a whole by the file's section, never merged with the default. `effective_drivers()` fills defaults (a `dm` driver without `host`/`stream` uses `ports.dm`/`ports.dm_stream`; a hosted JEOL driver uses `vjem310_python`) and applies `--online/--offline/--sim`/`--dm`. The wizard edits it through `set_hardware()`; `hardware_driver()`, `hardware_mode()`, `is_online()` read it. Also `exclusive` (device groups) and `policy` (`stage_limits`, `confirm`). `drivers.*.python` is machine-local (stripped on export). The profile records `provenance` (AMADEUS version, driver kinds); `load_profile` returns `warnings` for a version mismatch and for calibrations without a `measured` date or older than 180 days.
- `dm_server` still must not import `common/`: `scripts/start_amadeus_dm.py` reads the JSON with plain `json` and passes ports to `run_server()`.
- `setup_wizard/` is Py3.10, stdlib + `common/` only (it runs under vjem310). The firewall one-click fix stays opt-in (`experimental.firewall_autofix`, default false) and must only *add* a rule.
- `client.allow_scripting` (default true) / `amadeus-server --no-scripting` leaves out the MCP escape hatches `execute_dm_script` and `execute_tem3_call`. The flag can only turn scripting off. This is enforced only in the MCP server: the relay still accepts `execute_script` because the console's live-scan buttons and `amadeus-check` use it. The locked-down agent setup is `operator/` (its `.mcp.json` passes `--no-scripting`, and its `.claude/settings.json` denies those tools plus Bash/Write/Edit/web/other MCP servers). Run `claude` from there to operate the instrument.
- The MCP server must not print to stdout (it is the stdio channel) and must not block the MCP handshake on instrument reachability.

### Units policy (decided — do not regress)
Hardware values are **honest device units**: lens/deflector/stigmator state is the raw 16-bit DAC (0–65535, 32768 = deflector neutral); stage is metres/degrees on the wire (nm/degrees on the JEOL side); HT is volts. Focus *additionally* reports `focus_nm` + `calibrated: true` only when the calibration (`drivers/calibration.py`) has a measured DAC↔nm slope for the current mode. Never invent physical units.

### Relay (`relay/`)
- Routing is by slot (`common/commands.py` `SLOT_COMMANDS`, `relay/routing.py`): a slot operation goes to the driver holding its slot; in multi-device slots `params["device"]` (id or role, resolved via `instrument.roles`) picks the driver and the driver gets the *local* id (`"camera/1"`). Vendor-only commands are `"<kind>.<cmd>"` (`dm.list_windows`, `jeol.tem3_call`); `vendor_call` takes `driver` or `kind`. **Unknown commands are an error**, including pre-1.0 names (`execute_script`, `tem3_call`, `list_dm_files`, bare `shutdown`). A new DM command must be added to a slot in `common/commands.py` + `drivers/dm.py`, or as a DM extra (`test_every_dm_server_command_is_reachable_through_the_dm_driver` checks).
- Which driver holds column/stage is an **operator decision** made in the `drivers` section (giving the DM driver `column`/`stage` = the Gatan EM* fallback; the wizard's routing choice). It is deliberately NOT exposed as an MCP tool; do not add one.
- The policy layer (`relay/policy.py`) runs after routing for every driver.
  - `policy.stage_limits` are hard: a target outside them is refused.
  - These need `confirmed: true`, which the relay strips before forwarding:
    - `policy.confirm` commands;
    - switching inside an `exclusive` device group;
    - with `policy.confirm_vendor_writes` (default true), raw vendor calls whose method isn't a `Get*`/`Is*` read (`jeol.tem3_call`, `vendor_call`). `dm.execute_script` stays ungated.
  - Such a refusal is *confirmable*: the reply carries `needs_confirmation: true`.
- Recovery (`relay/recovery.py`): a re-sent `request_id` gets the first reply (the command runs once); after a timeout the relay reads the affected state back (if the driver is free) and puts it in the error (`state_after`).
- Drivers run `in_process` (`InProcessLink`, one worker thread), in their own supervised process (`HostedLink`: `python -m drivers.host` in the driver's `python`; restart with backoff, `DriverDown` errors while down, the relay-local `restart_driver`; the host exits if the relay stops pinging), or at an address (`RemoteLink`; DM through the `drivers/dm.py` translator).
- Replies are forwarded frame-for-frame (multipart image responses pass through untouched).
- The relay **proxies the live stream**: XSUB on DM's loopback PUB (9879) → XPUB on the public stream port (9877), frames untouched. DM never needs inbound network access; only the vjem310 python.exe does.
- An in-process driver (e.g. the default `"hw"`) runs on **one dedicated worker thread**: every vendor call, and the driver's construction, happens there. Never call it from the event loop or another thread. A remote `"hw"` entry in `RelayConfig.backends` overrides it (the tests use this); `RelayConfig.drivers` takes an explicit driver map.
- A backend timeout returns a structured JSON error (a REQ backend's socket is rebuilt); the other backend is unaffected. The error names the command, and for the hardware worker it says whether the call is still running or is queued behind another command. `shutdown_hw` releases the in-process backend (calling its `close()`); the next call rebuilds it.
- **Per-request timeout:** a request may carry top-level `"timeout_ms"`. `ZMQClient.send_command(..., timeout_ms=)` sends it only when a call passes one explicitly. The relay then uses it instead of the backend default and answers at 90 % of it, so the client gets a reply, not a dead socket.
- **Continuations:** a reply whose `data` has `"_continue": {"cmd", "params", "interval_s"}` is followed by the relay. It re-issues that command, each poll a separate short request so other commands interleave, until the reply has no `_continue`. When the budget runs out it does a last poll with `params.finalize=true`. Waiting `move_stage` → `stage_poll` uses this.

### Drivers (`drivers/`)
- Must stay **Python 3.10-compatible** (vjem310 env) and depend only on `common/` (+ the vendor API). No numpy: image payloads are raw bytes (`ImageResult`). `tests/test_config.py` parses `drivers/` as Python 3.10 too.
- `JeolDriver` and `SimColumnDriver` subclass `ColumnDriver` + `StageDriver` from `drivers/base.py`; `SimDriver` adds every acquisition slot. Each driver layers its own calibration defaults with `InstrumentCalibration.with_defaults()` (JEOL: CLA1/CLA2, OLf/OLc, nm stage); the generic defaults have no vendor names. A new manufacturer = a driver (`drivers/template.py`, `docs/ADDING_HARDWARE.md`) registered in `drivers/registry.py` (or via `"class"` / an `amadeus.drivers` entry point). The relay, wire protocol, MCP tools and sequences do not change. It must pass `drivers/contract.py` (`amadeus-driver-test`); add a test like `test_jeol_driver_passes_the_contract_on_a_fake_pyjem`.
- **Neutral wire forms:** device values come with `unit` and `range`; focus by lens *role* (`set_focus` takes `focus_nm`, `focus_dac`, or `lens` + `value`; roles map to vendor lenses via `instrument.lenses`, default `focus.fine`→OLf, `focus.coarse`→OLc); stigmators by name (`stigmator`); function modes by name; `set_magnification` by `magnification` (needs `instrument.magnifications` tables) or `mag_index`; vendor detail in `"vendor"`. Stigmators, function modes and the TEM/STEM mode are names only (no indices). `get_stigmation` returns `{"stigmators": {name: {x, y}}, "unit", "range", "neutral"}`; `get_microscope_state.stigmation` is the same per-name dict. Capabilities list the vocabularies (`function_modes`, `stigmators`, `lenses`, `deflectors`, `spot_size_range`, `magnification_by_value`).
- PyJEM setters silently ignore non-int values — the adapter validates and raises instead; keep it that way.
- **Stage drive calls never run on the hardware worker when online.** Online PyJEM's `Stage3.Set*` setters block until the stage settles and appear to hold the GIL, which froze the whole server. `drivers/jeol/stage_motion.py` runs them in a spawned child process with its own `Stage3` (`instrument.stage.motion`: `auto` = process online / inline offline). A waiting `move_stage` returns `_continue`, and `stage_poll` reports settled once the child's call has returned *and* `GetStatus()` is at rest. The reply carries `settled: bool`, and `settled: false, moving: true` means the move is still running after `timeout_s`. One move at a time; `stop_stage` works mid-move. Keep `relay/__main__.py` spawn-safe (`if __name__ == "__main__"`).
- `move_stage` options: `speed` slow/normal/fast (`SetSpeedMode`) and `drive` motor/piezo (`SelDrvMode`; piezo = fine X/Y, ±`piezo_range_nm`) apply to that move only and are restored afterwards. `approach` (or `instrument.stage.backlash.enabled`) overshoots by `overshoot_nm` and approaches X/Y from the `approach` direction, because the F200 loses 0.5–0.7 µm on reversals.
- `jeol.tem3_call` (and `vendor_call` with `method: "Class.Method"`) is the JEOL escape hatch: one public TEM3 method per call, no arbitrary code.

### DM server (`dm_server/`)
- **Stateless** — one command in, one response out. The one exception is `helpers/live.py` (`_SESSION` holds the running live-scan handle; frames publish on PUB 9879 between commands via the cooperative single-threaded poller).
- Binds **127.0.0.1** by default: only the relay talks to it.
- `helpers/microscope.py` and `helpers/stage.py` are the **fallback hardware path** — kept working, routed to only when the config gives the DM driver the `column`/`stage` slots. Don't delete them; don't extend them either — new hardware features go in the drivers.
- The dispatcher converts `NotImplementedError` to a JSON error, never a crash. Do not change this behaviour.
- `dm_server` stays self-contained (no imports from `common/`) — it is exec'd inside DM and is the reference implementation of the wire protocol.
- Use `exec_script()` for fire-and-forget DM-Script; `safe_exec_script(script, expected_tags=[...])` when the script writes output tags.
- DM returns non-ASCII strings (e.g. units `µm`) as **bytes** — pass every DM-returned string through `_dm_util.dm_text()`, never `str()`. DM's Python bridge drops the message of any DM-Script error containing non-ASCII text; `_dm_util.exec_script` works around it by running every script inside a DM-Script `try`/`catch` (re-running unwrapped only if the wrapper fails to compile, e.g. function/class definitions). Always go through `exec_script`/`safe_exec_script`, never `DM.ExecuteScriptString` directly.
- **On the F200 the DM server can't be stopped** (`shutdown_dm_server` doesn't work there), so loading new `dm_server/` code means restarting Digital Micrograph, then re-running `start_amadeus_dm.py`. In the dev VM it *does* work: `shutdown_dm_server`, then Execute `start_amadeus_dm.py` again, loads new code with no DM restart. The F200-specific cause is still unknown.
- DM-Script needs a `;` (or newline) before a `}` on the same line: `if (x) { a = 1; }`, never `{ a = 1 }`. Every script a test sends to the mocked DM is run through `common/dmscript_lint.py` (in `test_dm_helpers.py`'s autouse fixture), so new helpers get checked as long as a test calls them. Standalone: `python -m common.dmscript_lint file.s`.

### Client (`client/`)
- **Stateful** — all instrument state, image history, maps, plans, preferences live here. `StateManager` persists to `amadeus_state.json`; images are `.npy` + `.json` sidecars via `ImageStore`.
- MCP tools are registered via `register(mcp, zmq, state)` functions — one module per domain. No global state or module-level singletons.
- Vendor-specific tools are registered only when the config has that driver kind (`build_server(driver_kinds=...)`, from `driver_kinds_from(cfg)`): `execute_dm_script` and the DM window tools need a `dm` driver, `execute_tem3_call` a `jeol` one. `execute_vendor_call` is generic. All three go away with `--no-scripting`. Tool device arguments (`detector`, `camera`, `device`) take a role, a device id or an index, always sent as `params["device"]` (`client/devices.py`). The generic file tools are `list_remote_files` / `fetch_remote_file`; `release_column_driver` resets the column driver.
- Session preferences are enforced at the ZMQ chokepoint (`client/policy.py`, `ZMQClient.set_policy`):
  - `blank_between_acquisitions` (opt-in): unblank before and blank after synchronous acquisitions only, and never undo an operator's blank;
  - `post_move_pause_s`: a pause after a settled move;
  - `max_beam_shift`: a `set_beam_shift` further than this from neutral needs confirmation.

  `stop_stage` out of band bypasses it.
- **Operator confirmation (`client/confirm.py`): the agent never confirms.**
  - `ZMQClient` drops any caller's `confirmed`. On a `needs_confirmation` refusal (relay) or `NeedsConfirmation` (preferences), it asks its confirmer, then resends once with `confirmed: true` on Allow, or raises `OperatorDeclined`.
  - `build_server()` chains the confirmers, and the first one that can reach an operator answers:
    1. the browser dialog (amadeus-web's `WebConfirmer`, in the terminal pane plus a header banner);
    2. `MonitorConfirmer`, a dialog in `amadeus-console` including `--read-only`. It works through `monitor/console.json` (heartbeat), `confirm_request.json` and `confirm_reply.json`. Answering one is not an instrument command;
    3. `ElicitConfirmer`, an MCP elicitation prompt in the agent's host when the client supports it.
  - If no confirmer can reach an operator, the command is declined.
  - The Tk console's own commands prompt inline; buttons it already OK'd count as confirmed.
  - `set_operational_preferences` asks before loosening a safety setting (raising `max_beam_shift`, turning blanking off).
- Particle/EDS survey tools (`client/tools/image_analysis.py` + `eds.py`): `measure_nanorods` (isolated particles only), `find_particle_clusters` (skips grid bars, gives Jacobian stage targets), `edge_intensity_profile` / `eds_edge_profile` (shell detection), `eds_region_spectra` (particle/halo/far peak ID with a fitted detector gain+offset and line-family ranking), `image_sharpness` (focus sweeps), `measure_image_shift` / `fit_stage_image_mapping` (stage calibration; the fit only *returns* a config entry), `export_image_png` / `export_overlay_png` (written to `<data_dir>/exports/`, no Pillow dependency). They were validated against the 2026-11-12 F200 Au-nanorod data.
- `eds_map_elements` (`client/tools/eds.py`) is the one-call path from an SI to elemental maps: it finds the elements, picks each one's line, corrects the detector gain/offset, places background bands clear of other peaks (`eds_regions.clean_sidebands` / `net_line_map`, unclipped), and saves maps, PNGs and an RGB overlay. Not yet run on F200 data.
- `stem_autofocus` (`client/tools/focus.py`) sweeps a focus lens role (default `focus.fine`, OLf on JEOL) within the range `get_focus` reports, scores fast 512² frames with `imaging.sharpness`, and narrows the range to the two sharpest each iteration. It restores the starting focus on any error and closes only the DM frame windows it opened (skipped without a DM driver). Arguments `focus_min`/`focus_max`/`half_range` and results `start`/`best`/`final` are in the lens's device units, with `lens`. The older `autofocus` tool calls DM's routine (`dm.autofocus`), which is a stub.
- `compute_fft(fit_ctf=True)` fits C1 (defocus) and A1 (twofold astigmatism) to Thon rings (`client/analysis/ctf.py`). Use it on TEM images of amorphous material. It reports `reliable` and `sign_ambiguous`, because |CTF|² only fixes the sign of the defocus through Cs.
- The monitor/pause gate attaches to `ZMQClient` — the single chokepoint all instrument traffic flows through. Keep it that way.
- **Stop must never queue behind a move.** `ZMQClient` serialises requests on one REQ socket, so a waiting `move_stage` holds it. `stop_stage` (the MCP tool and the console's STOP STAGE) goes through `send_command_out_of_band()` on a fresh socket. That path still runs the monitor hooks, and `stop_stage` is exempt from the operator pause. Move options (speed/drive/approach/wait) are built by `client/stage_moves.py` for both the MCP tools and the console's Stage tab (Motion panel + status readouts).
- **Pixel→stage uses the measured Jacobian** in `instrument.stage.image_mapping.<STEM|TEM>.stage_to_image` (`client/analysis/stage_mapping.py`; image x right / y down; features shift by J·d for a stage move d). The F200's stage axes are rotated and differently scaled relative to the scan. Never add a pixel offset straight onto the stage position. The client gets the `instrument` section from the config (`build_server(instrument=...)`). Acquisitions read the stage position fresh and tag `imaging_mode`/`rotation`.
- **Image calibration is stored in DM's own units** (`ImageCalibration.units`; the F200 OneView is µm, uncalibrated images are `""`). Anything needing a physical length must use `client/analysis/units.nm_per_px()`, which converts and refuses uncalibrated images. Never assume nm/px — that was a 1000× bug in pixel→stage and lattice spacings.
- The client talks only to the relay (no direct-DM mode). One laptop GUI: `amadeus-console`. Manual control lives in its Instrument tab (`client/gui/control_panel.py`); `--read-only` is the passive monitor and must never send instrument commands. `dm_gui.py` (in-DM) is separate and kept.

### Web console and action blocks (`client/web/`, `client/blocks/`)
- `amadeus-web` owns the instrument connection, the same way `amadeus-server` does (`build_server(mcp=QueuedFastMCP)`). It serves the UI, REST `/api/*`, SSE `/events` and the MCP server over streamable HTTP at `/mcp`, on **127.0.0.1 only**. The stdio `amadeus-server` is unchanged. `operator/` has an `amadeus-web` HTTP entry (enable it instead of `amadeus`).
- **Every tool call is a queue block.** Agent MCP calls, manual blocks and sequence steps all run FIFO, one at a time, through `BlockQueue`. Pause/step/abort are checked *between* blocks. Never interrupt a running call from the queue. Stop goes out of band (`/api/stop_stage`). The operator-channel tools (`wait_for_operator`, `ask_operator`, `tell_operator`) bypass the queue. Browser instructions reach the agent through `wait_for_operator`, or appended to its next tool result as `operator_message:`.
- **Sequences** (`client/blocks/model.py`, saved as `<data_dir>/sequences/*.amseq.json`) are the only program format. Blockly is only an editor and converts to and from it. Argument values may be `{"$expr": ...}` (safe AST-whitelist evaluator, `expr.py`: never `eval`) or `{"$role": "detector:HAADF"}`. Loops are always bounded (`max_iter`).
- **Portability is capability matching, not config typing.** The relay-local `get_capabilities` returns one manifest per driver (`drivers`: commands in its slots, extras, devices, level), plus `devices`, `routing`, `roles`, `policy` and the `instrument` config. `Driver.capabilities()` derives `commands` from the methods a driver actually overrides. The client turns this into strings (`cmd:*`, `mode:*`, `feature:*`, `detector:<role>`, `camera:<role>`, `calib:*`, `tool:*`). A block's requirements come from `TOOL_META` in `client/blocks/catalog.py`. **Every new MCP tool needs a `TOOL_META` entry** (`test_blocks.py` enforces it): list each relay command it sends as `cmd:<name>`, its imaging `mode` (a run-time guard, not a capability), and any role arguments. Branches guarded by `has("cap")` don't count as requirements. Sequences use roles, not indices: `instrument.roles.{detectors,cameras}` maps role names to this microscope's indices, and detector roles are otherwise guessed from the scan unit's signal names (`common/roles.py`, by the client and the relay alike).
- Acquisitions tag `magnification`/`mag_index`/`high_tension_kV`/`spot_size` (best effort, read before the acquisition) for the image viewer.
- Front-end libraries are vendored and pinned in `client/web/static/vendor/` (see `VERSIONS.md`). No build step and no CDN at run time.

### ZMQ Protocol
```
Request  (client → relay → backend):  JSON {"cmd": "...", "params": {}, "request_id": "uuid"}
Response:                             JSON {"status": "ok", "data": {...}, "request_id": "uuid"}
Image response (multipart, DM only):  frame 1 meta JSON, frame 2 raw numpy bytes
Error:                                JSON {"status": "error", "error": "description"}
```
- `ZMQClient.send_command()` for data-only commands; `ZMQClient.send_command_image()` for numpy-returning commands.

## PyJEM — Critical Facts

- `Stage3` drive control: `SelDrvMode(0 motor | 1 piezo)`; `GetPiezoPosi()`; `SetSpeedMode(kind, xy, z, txy)` / `GetSpeedMode(kind)` with 0 slow / 1 normal / 2 fast (PyJEM ≥ 1.3; `...Ex` variants add rotation). Piezo setters take ±10000 nm. `GetStatus()`: 0 rest, 1 moving, 2 hardware limit. Also `Setf1OverRate` (drive frequency) and `SetAccelAndDclrRate` (Z/tilt keystone), not used.
- Online `TEM3` is a **compiled Windows .pyd**, per-Python-version — it only runs in the vjem310 env on the microscope PC. `PyJEM.offline.TEM3` is a pure-Python simulator with the identical API (use `--offline`).
- All lens/deflector values are DAC ints 0–65535. `Stage3.GetPos()` returns nm (docstring ±100000.0 nm) and degrees; setters are `SetX`/`SetXRel` etc.; **there is no wait call** — poll `GetStatus()`.
- Deflector roles: CLA1 = beam shift, CLA2 = beam tilt (JEOL convention; swappable in the calibration file if the F200 differs).
- The "column valve" on a FEG is `FEG3.SetBeamValve` (V1 gun valve).
- Apertures: `Apt3.SelectKind(1..6 = CLA/OLA/HCA/SAA/ENTA/EDS)`, then `SetSize`/`SetPosition`.
- `EOS3.GetMagValue()` returns `[value, unit, label]`; it can fail off-instrument (Windows-path resource) — the adapter degrades to index-only.
- STEM is called "ASID": `EOS3.GetTemStemMode()` 0=TEM 1=STEM; `SelectTemStem` switches (large state change).

## DM Python API — Critical Facts

- **EM\*, Camera\*, SI\* functions are DM-Script only** — call via `DM.ExecuteScriptString`, read results back through `GetPersistentTagGroup()`. **DS_\* (DigiScan) functions are direct Python callables.**
- `DM.GetApplicationVersion()` does not exist — use `sys.version`.
- Call `EMUpdateCalibrationState()` before reading calibrated values; `EMWaitUntilReady()` after stage/mag changes (fallback path only).
- Offline/viewer DM installs have no hardware functions — stubs raise `NotImplementedError` there. Expected.
- **Windows** (`dm_server/helpers/windows.py`; verified in the dev VM): `GetDocumentWindow(i)` covers every workspace in z-order (0 = front, so indices shift on every select). Image windows have a stable `ImageDocumentGetID()`. `WindowSelect` switches workspace itself. `WindowClose(w, 0)` **silently discards** unsaved images; `WindowClose(w, 1)` opens a modal dialog that would hang the server's main thread, so never use it. `ImageDocumentIsDirty` matches DM's own "Save changes?" prompt (acquisitions and script-made images are dirty; files opened from disk are clean). `WindowIsValid` stays true after a close; use `WindowIsOpen`. The window tools never close script windows (the server runs in one), close dirty images only with `discard_unsaved`, and refuse while a live scan runs. `save_window` (MCP `save_dm_window`, optional `fetch=True`) saves an image document to `.dm4` with Gatan's no-prompt save (`ImageDocumentSetCurrentFileSaveFormat("Gatan Format")` + `SetCurrentFile` + `ImageDocumentSave(0)`). The save links the document to the file, refuses an existing file without `overwrite`, and checks the file on disk afterwards. It replaces the `execute_dm_script` save recipe; not yet run in DM.
- Gatan's scripting docs ship with GMS (not included here): `Scripting_EMControl.md`, `Scripting_DigiScan.md`, `Scripting_Camera.md`, `Scripting_SIAcquisition.md`, `PythonReference.md`.

## Working on the F200

- Back each bug fix with a regression test that fails on the old code.
- The PC runs a manually copied checkout at `C:\AMADEUS` — a repo change isn't live until copied. What each part needs after copying: `dm_server/` → restart DM + re-run `start_amadeus_dm.py`; `relay/`, `drivers/`, `common/` → Stop/Start in the launcher; `client/` (laptop) → `/mcp` reconnect.
- Network: direct cable, PC `10.77.77.1` / laptop `10.77.77.2` (`docs/ETHERNET_SETUP.md`).
- Both cameras are retractable: **switching cameras physically moves them** (OneView ≈ 10 s, Metro ≈ 20 s to insert/withdraw — a swap ≈ the 30 s timeout). `CameraPrepareForAcquire` does this implicitly. Ask the operator before switching; pass `camera` explicitly (DM's active camera can change under you). Metro acquisition doesn't work yet — don't probe exposures on it (direct detector).
- Read-only DM probes: `execute_dm_script` returns only `executed`, so report values with `Throw("AMADEUS-PROBE ...")` and read the error text.
- Test leftovers in the laptop session state (saved locations, map objects) can't be deleted via MCP; prefix test entries `mcp_test`.

## Developing against DM in a VM

Before going to the microscope, try DM-side changes in a Windows VM with an offline GMS + vjem310 (no hardware). The runbook is **`docs/VM_DEV.md`**. Essentials:
- Full stack:
  - Copy only the latest code to `C:\AMADEUS` and verify it by hash. No `.git`, docs or instrument configs.
  - Start the relay `--offline --listen tcp://127.0.0.1:9875` through WMI, so it outlives SSH.
  - Start DM, then run `start_amadeus_dm.py` in it on a background thread.
  - Open an SSH tunnel to host port 39875, and run `amadeus-check` with a config pointing there.
- DM doesn't repaint while a script holds its main thread. If DM disappears, someone probably closed it; just restart it.

## Adding a New Command

**A new operation** (any slot):
1. Add the name to its slot in `common/commands.py` `SLOT_COMMANDS` and a method (raising `NotImplementedError`) to that slot's class in `drivers/base.py` (`test_slot_classes_match_the_wire_command_tables` keeps them identical).
2. Implement it in every driver that can do it: column/stage in `drivers/jeol/driver.py` **and** `drivers/sim_column.py`; acquisition in `drivers/sim.py` **and** the DM side (helper in `dm_server/helpers/<module>.py`, registered in `dm_server/server.py`'s `COMMANDS`, listed in `drivers/dm.py` `SLOT_COMMANDS`, plus a name translation there if DM's name differs).
3. Add the MCP tool in `client/tools/<module>.py` and its `TOOL_META` entry in `client/blocks/catalog.py` listing every command it sends (`test_tools_send_only_the_commands_their_tool_meta_declares` checks it). Capabilities pick up the new method automatically.
4. Tests: driver behaviour in `test_jeol_driver.py` (the parametrised sim test picks up column/stage commands automatically; add params to `SIM_PARAMS` if needed), DM helpers in `test_dm_helpers.py`, and the contract still passing.

**A vendor-only operation:** a method listed in the driver's `extras` (wire name `"<kind>.<name>"`), or for DM a helper + `drivers/dm.py` `EXTRAS`. Its tool is registered only when that driver kind is configured.

## Testing

```bash
uv run --with pytest --with pytest-asyncio pytest -v
```

- All tests run fully offline — no DM, no PyJEM, no instrument, no relay running.
- `test_dm_helpers.py` mocks `DigitalMicrograph` in `sys.modules` before helper imports; `test_jeol_driver.py` does the same for `PyJEM`. Both mocks must stay at the top of their files.
- `test_zmq_protocol.py` uses port **29876** (19876 is occupied by Zotero on the dev machine); `test_relay.py` uses **299xx** ports (up to 29918); `test_multivendor.py` uses **29920–29939**; `test_zmq_recovery.py` uses **29881–29883**.
- `test_multivendor.py` covers drivers, routing, devices/roles, policy, dedupe/read-back, preferences, a hosted driver killed and restarted, and the contract on sim/template/JEOL.
- The motion-process tests spawn a real child that imports `tests/fake_tem3_motion.py` (blocking fake setters), configured through the `AMADEUS_FAKE_STAGE_*` env vars.
- `test_config.py` asserts every file that runs in vjem310 parses as Python 3.10; `test_setup_wizard.py` tests wizard/launcher/doctor logic without opening Tk windows.
- Full-stack e2e on a dev machine: `python -m relay --sim` + `amadeus-server` / `amadeus-check` — no instrument needed (dm reports down without a DM or mock). With a config whose `drivers` section has one sim driver holding every slot, the whole stack (images included) runs with no DM at all. With a real (offline) DM: the UTM VM, `docs/VM_DEV.md`.

## Dependencies

Managed via `uv`. Key packages: `mcp>=1.2.0`, `pyzmq>=25.0`, `pydantic>=2.0`, `numpy`, `scipy`.
The vjem310 env needs only `pyzmq` installed into it (PyJEM ships with the env). `common/`, `relay/`, `drivers/`, `setup_wizard/` must stay Python 3.10-compatible.
