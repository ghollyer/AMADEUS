"""Command names and the slots they belong to: what the relay routes on.

A *driver* fills one or more *slots*; the relay sends each command to the
driver holding the command's slot (``relay/routing.py``). Command names are
vendor-neutral operations. Vendor-only commands are namespaced on the wire
as ``"<driver kind>.<command>"`` (``dm.list_windows``, ``jeol.tem3_call``),
and the generic escape hatch is ``vendor_call``. Anything else is an error.

Stdlib only, Python 3.10.
"""

# Handled by the relay itself, never forwarded.
RELAY_LOCAL_COMMANDS = frozenset({
    "ping",
    "backend_status",
    "get_config",       # the config, so laptops needn't copy it by hand
    "get_capabilities", # capability manifest: every driver + devices + calibration
    "restart_driver",   # operator: restart a driver running in its own process
    "shutdown_relay",
})

# Rewritten by the relay to "shutdown" for the driver holding the column
# (releases it; it is rebuilt on the next request).
HW_SHUTDOWN_COMMAND = "shutdown_hw"


# ``column`` and ``stage`` are held by at most one driver; the acquisition
# slots can hold devices from several drivers, and a request picks one with
# ``params["device"]``: a device id ``"<driver>/<kind>/<n>"``, a local id
# ``"<kind>/<n>"`` (on the slot's first driver), or a role such as
# ``"detector:HAADF"``.

SLOT_COMMANDS = {
    "column": frozenset({
        "get_focus", "set_focus", "change_focus",
        "get_magnification", "set_magnification",
        "get_beam_shift", "set_beam_shift", "get_beam_tilt", "set_beam_tilt",
        "get_stigmation", "set_stigmation",
        "get_spot_size", "set_spot_size", "get_alpha", "set_alpha",
        "set_beam_blank", "set_column_valves", "get_aperture", "set_aperture",
        "get_instrument_mode", "set_instrument_mode",
        "get_function_mode", "set_function_mode",
        "get_high_tension", "get_microscope_state",
    }),
    "stage": frozenset({
        "get_stage_position", "move_stage", "get_stage_status", "stop_stage",
        "stage_poll",
    }),
    "scan": frozenset({
        "stem_acquire", "get_stem_settings", "set_stem_settings",
        "get_detectors", "set_detector", "move_beam_to", "get_beam_position",
    }),
    "camera": frozenset({"camera_acquire", "get_camera_info", "list_cameras"}),
    "live": frozenset({"live_start", "live_stop", "live_status"}),
    "spectrometer": frozenset({
        "si_configure", "si_start", "si_stop", "si_get_settings",
        "eds_acquire_point", "eds_acquire_map",
    }),
    "files": frozenset({"list_files", "load_file", "read_file_chunk"}),
}

SLOTS = tuple(SLOT_COMMANDS)
# Slots one driver owns outright (one column, one stage per microscope).
SINGLE_DRIVER_SLOTS = frozenset({"column", "stage"})
ACQUISITION_SLOTS = ("scan", "camera", "live", "spectrometer", "files")

# command -> slot
COMMAND_SLOT = {cmd: slot for slot, cmds in SLOT_COMMANDS.items() for cmd in cmds}

# Device kinds a role or device id can name, and the slot that serves them.
DEVICE_KIND_SLOT = {"detector": "scan", "camera": "camera",
                    "spectrometer": "spectrometer"}

# Generic driver commands every driver answers (not slot operations).
DRIVER_COMMANDS = frozenset({
    "ping", "get_driver_capabilities", "list_devices", "vendor_call",
})

# Driver kinds and what each is.
DRIVER_KINDS = {
    "jeol": "JEOL column + stage through PyJEM TEM3 (online or JEOL's offline simulator)",
    "sim": "AMADEUS simulator: every slot, synthetic images, no vendor software",
    "dm": "Gatan Digital Micrograph (dm_server, a separate process inside DM)",
    "remote": "any process speaking the AMADEUS driver protocol at an address",
}
