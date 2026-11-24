# Adding hardware to AMADEUS

AMADEUS talks to a microscope through **drivers**. Each driver connects one
vendor's software, such as JEOL PyJEM, Gatan Digital Micrograph or a simulator,
and fills one or more **slots**:

| Slot | What it covers | How many |
|---|---|---|
| `column` | focus, magnification, deflectors, stigmators, spot, alpha, modes, HT, blanking, valves, apertures | one driver |
| `stage` | position, moves, status, stop | one driver |
| `scan` | STEM scan unit and its detectors | devices from several drivers |
| `camera` | TEM cameras | devices from several drivers |
| `live` | live view, streamed to the laptop | several |
| `spectrometer` | EDS / EELS, spectrum imaging | several |
| `files` | data files on the instrument side | several |

The relay routes each command to the driver holding its slot
(`common/commands.py` lists which command belongs to which slot). In a
multi-device slot, a request picks its device with `params["device"]`. That is
either a device id (`"tvips/camera/0"`, meaning driver `tvips`, its local
device `camera/0`) or a **role** (`"camera:diffraction"`) that the config maps
to a device. The MCP tools, the web console and saved sequences only use
roles and neutral command names, so they don't change when the hardware does.

## The rules a driver follows

These are the conventions in `drivers/base.py`. `amadeus-driver-test` checks
them.

- **Operations are methods named like their wire command.** Each one takes the
  request's `params` dict and returns a JSON dict, or a `drivers.base.ImageResult`
  (raw bytes + shape + dtype) for pixels.
- **Never fake an operation.** Leave it un-overridden: it is then reported as
  missing (`capabilities()["commands"]` leaves it out), and a call gets the
  standard "not implemented" error.
- **Honest units.** The stage uses metres and degrees on the wire. Device
  values (lens, deflector, stigmator) are reported with their `unit` and
  `range`. A physical value such as `focus_nm` is added only when there is a
  measured calibration (`calibrated: true`).
- **Names, not vendor numbers.** Enumerations are strings, and the valid
  values are listed in `capabilities()`: `function_modes`, `stigmators`,
  `lenses` (roles such as `focus.fine` -> the vendor's channel), `deflectors`.
  Vendor detail goes in a `"vendor"` sub-object.
- **Vendor-only operations are extras.** List them in `extras`. They are
  reached as `"<kind>.<name>"` on the wire, never as a bare name. The generic
  escape hatch is `vendor_call({"method", "args"})`, and you whitelist what it
  accepts (JEOL: one public TEM3 method; DM: `execute_script`).
- **Validate before calling the vendor.** Some vendor APIs silently ignore a
  bad argument (PyJEM's setters ignore non-ints, for example), so a typo
  becomes a no-op on the column. Check types and ranges, and raise.
- **Long operations don't block.** A stage move that takes seconds returns
  `{"_continue": {"cmd": "stage_poll", "params": {}, "interval_s": 0.1}}`, and
  the relay polls. If the vendor's call blocks (and holds the GIL, as online
  PyJEM's stage setters do), run it in a child process:
  `drivers/jeol/stage_motion.py` is the pattern.
- **No numpy in the driver.** Drivers run in vendor Python environments, which
  may be old (vjem310 is Python 3.10). Use the stdlib and `common/` only.

## Steps

1. **Start from the template.** Copy `drivers/template.py` to
   `drivers/<vendor>.py`, rename the class and `kind`, and keep the slot
   classes the hardware fills.
2. **Write a fake of the vendor API** for tests, a small Python module with the
   calls your driver makes. `tests/test_hw_backends.py` (fake PyJEM) and
   `tests/test_dm_helpers.py` (fake DigitalMicrograph) show the pattern.
3. **Implement the operations** against the fake until the contract passes:
   ```bash
   amadeus-driver-test --kind <kind>                     # reads
   amadeus-driver-test --kind <kind> --writes --acquire  # + round trips, one frame
   ```
   Add a test that runs `drivers.contract.check_driver` on it, like
   `test_jeol_driver_passes_the_contract_on_a_fake_pyjem`.
4. **Register the kind.** Add it to `BUILTIN` in `drivers/registry.py`. You
   can also skip that: set `"class": "package.module:Class"` on the driver in
   the config, or publish an `amadeus.drivers` entry point from your own
   package. AMADEUS core doesn't change either way.
5. **Configure it** in `amadeus_config.json` (below).
6. **First run on the instrument is read-only.** In the driver's own Python:
   ```bash
   "<driver python>" -m drivers.smoke --driver <name>
   ```
   It prints every value to compare with the vendor's console. It also
   records the result, and the launcher's Readiness list requires a pass
   before an online driver is trusted.
7. **Then** `amadeus-check --image` from the laptop, then the tools.

## Configuring drivers

The default `drivers` section is the classic pair: `hw` (JEOL, offline)
holding column + stage, and `dm` holding the acquisition slots. A file's own
`drivers` section replaces it as a whole. Each entry has:

| Field | Meaning |
|---|---|
| `kind` | `jeol`, `sim`, `dm`, `remote`, or your kind |
| `slots` | the slots this driver holds here |
| `host` | `in_process` (inside the relay), `process` (its own supervised process), or an address for `dm` / `remote` (a `dm` driver may leave it out: `ports.dm`) |
| `python` | for `process`: the interpreter to run it with (machine-local; not exported in the profile) |
| `offline` | JEOL: use PyJEM's offline simulator |
| `stream` | the driver's live-frame PUB address, republished by the relay |
| `options` | keyword arguments for the driver class |
| `class` | an explicit `module:Class` |
| `calibration` | a calibration file layered on the config's `instrument` section |

A JEOL column with DM cameras, a TVIPS diffraction camera and Bruker EDS:

```json
"drivers": {
  "jeol":   {"kind": "jeol", "host": "process", "slots": ["column", "stage"]},
  "gatan":  {"kind": "dm", "host": "tcp://127.0.0.1:9876",
             "stream": "tcp://127.0.0.1:9879",
             "slots": ["scan", "camera", "live", "files"]},
  "tvips":  {"kind": "emmenu", "host": "process", "python": "C:/tvips/python.exe",
             "slots": ["camera"]},
  "bruker": {"kind": "esprit", "host": "process", "slots": ["spectrometer"]}
},
"instrument": {
  "roles": {
    "detectors": {"HAADF": 1, "BF": 0},
    "cameras": {"imaging": "gatan/camera/0", "diffraction": "tvips/camera/0"},
    "spectrometers": {"eds": "bruker/spectrometer/0"}
  }
},
"exclusive": [["camera:imaging", "camera:diffraction"]],
"policy": {"stage_limits": {"z_m": [-2e-4, 2e-4], "alpha_deg": [-30, 30]},
           "confirm": ["set_column_valves", "set_instrument_mode"]}
```

- A role can name an index (`"HAADF": 1`), meaning that device on the first
  driver holding the slot, or a full device id.
- `exclusive` groups devices that can't be in use together, such as
  retractable cameras. Switching between them needs `confirmed: true`, so the
  agent asks the operator first.
- `policy.stage_limits` refuses stage targets outside the limits.
  `policy.confirm` lists commands that always need the operator's go-ahead.
  Both live in the relay, so every driver gets them.
- A microscope without Digital Micrograph has no `dm` driver. The DM-only tools
  (DM windows, `execute_dm_script`) are then not registered, and
  `stem_autofocus` skips DM window tidying.

## Where a driver runs

- `in_process`: on a dedicated worker thread inside the relay, which is the
  fastest option. A crash or deadlock in vendor code takes the relay down with
  it.
- `process`: `python -m drivers.host` in the driver's own interpreter, started
  and supervised by the relay. If it dies, the relay reports the driver as down
  (with the exit code and `logs/driver_<name>.log`), restarts it with backoff,
  and keeps serving the other drivers and the stop path. The operator can also
  restart it with the relay-local `restart_driver` command. The host exits by
  itself if the relay stops pinging it. Prefer `process` for real hardware once
  it has been tested on the instrument. For the derived JEOL driver, it is the
  launcher's "own process" checkbox (the `hw` driver's `host`).
- An address: a process you run yourself that speaks the AMADEUS wire protocol
  (`common/protocol.py`). `dm_server` is one.

## Support levels

`capabilities()["level"]` and the contract report how far a driver goes:

| Level | Meaning |
|---|---|
| 0 | nothing usable |
| 1 | reads instrument state |
| 2 | controls the column and/or stage |
| 3 | acquires images or spectra |
| 4 | live view and spectrum imaging |

A level-1 driver is already useful: its blocks show up in the web console, and
everything it can't do is greyed out there with the reason.

## Not yet covered

- Spectrum imaging across two drivers (a scan unit from one vendor and a
  spectrometer from another) needs a coordinator in the relay to arm, start and
  align both. It will be designed against a concrete second spectrometer.
- Live frames from several sources share the public stream port. Each frame's
  meta says which detector or camera produced it.
