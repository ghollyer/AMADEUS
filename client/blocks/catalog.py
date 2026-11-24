"""The tool catalog: every MCP tool as a block type, with its requirements.

Names, descriptions and argument schemas come from FastMCP itself
(``mcp.list_tools()``), so they never drift from the code. What FastMCP can't
know lives in :data:`TOOL_META`:

* ``category``: palette group.
* ``requires``: capabilities (see :mod:`client.blocks.capabilities`) the tool
  needs. For instrument tools that is ``cmd:<relay command>`` for every
  command the tool sends, so a backend that lacks an operation greys out the
  block automatically.
* ``mode``: the imaging mode the tool needs at run time (a guard, not a
  capability: the mode can be switched, a missing operation can't).
* ``hardware``: True if it talks to the instrument (False = analysis/state).
* ``roles``: arguments that take a role (``detector``/``camera``) instead of
  an index, so the editor offers the instrument's role names.
* ``enums``: allowed values for string arguments FastMCP reports as ``str``.

``tests/test_blocks.py`` fails if a registered tool has no entry here, so a
new tool can't skip declaring its requirements.
"""

from __future__ import annotations

import re
from typing import Any

# --- helpers to keep the table short ------------------------------------------------

def _m(category, *cmds, mode=None, hardware=True, roles=None, enums=None, extra=()):
    return {"category": category,
            "requires": [f"cmd:{c}" for c in cmds] + list(extra),
            "mode": mode, "hardware": hardware,
            "roles": roles or {}, "enums": enums or {}}


def _a(category):
    """Analysis / client-state tool: needs nothing from the instrument."""
    return _m(category, hardware=False)


_SPEED = {"speed": ["slow", "normal", "fast"], "drive": ["motor", "piezo"]}

TOOL_META: dict[str, dict[str, Any]] = {
    # -- optics / column (hardware backend) --------------------------------------
    **{name: _m("Optics", name) for name in (
        "get_focus", "set_focus", "change_focus",
        "get_magnification", "set_magnification", "get_spot_size", "set_spot_size",
        "get_alpha", "set_alpha", "get_beam_shift", "set_beam_shift",
        "get_beam_tilt", "set_beam_tilt", "get_stigmation", "set_stigmation")},
    **{name: _m("Column", name) for name in (
        "get_instrument_mode", "get_function_mode", "set_function_mode",
        "get_high_tension", "get_microscope_state", "set_beam_blank",
        "set_column_valves", "get_aperture", "set_aperture")},
    "set_instrument_mode": _m("Column", "set_instrument_mode",
                              enums={"mode": ["TEM", "STEM"]}),
    "set_function_mode": _m("Column", "set_function_mode"),
    "set_stigmation": _m("Optics", "set_stigmation",
                         enums={"stigmator": ["objective", "condenser", "intermediate"]}),
    "autofocus": _m("Focus", "dm.autofocus"),
    # Window tidying (dm.list_windows / dm.close_window) is optional: the tool
    # skips it on a microscope without DM, so it is not a requirement.
    "stem_autofocus": _m("Focus", "get_instrument_mode", "get_focus", "set_focus",
                         "stem_acquire", mode="STEM", roles={"detector": "detector"},
                         enums={"lens": ["focus.fine", "focus.coarse"]}),
    # -- escape hatches ----------------------------------------------------------
    "execute_vendor_call": _m("Scripting", "vendor_call",
                              extra=["feature:scripting", "feature:vendor_call"]),
    "execute_dm_script": _m("Scripting", "dm.execute_script", extra=["feature:scripting"]),
    "execute_tem3_call": _m("Scripting", "jeol.tem3_call", extra=["feature:scripting"]),
    # -- acquisition (DM) --------------------------------------------------------
    "take_stem_image": _m("Acquire", "stem_acquire", "get_stage_position", mode="STEM",
                          roles={"detector": "detector"}, extra=["detector:any"],
                          enums={"mode": ["search", "preview", "capture"]}),
    "take_camera_image": _m("Acquire", "camera_acquire", "get_stage_position", mode="TEM",
                            roles={"camera": "camera"}, extra=["camera:any"],
                            enums={"processing": ["gain_normalised", "dark_subtracted",
                                                  "unprocessed"]}),
    "get_stem_settings": _m("Acquire", "get_stem_settings"),
    "get_detectors": _m("Acquire", "get_detectors"),
    "move_beam_to": _m("Acquire", "move_beam_to", mode="STEM"),
    "get_beam_position": _m("Acquire", "get_beam_position"),
    "list_cameras": _m("Acquire", "list_cameras"),
    "get_camera_info": _m("Acquire", "get_camera_info", roles={"camera": "camera"}),
    # -- live --------------------------------------------------------------------
    "start_live_scan": _m("Live", "live_start", roles={"device": "detector"},
                          enums={"detector": ["stem", "camera"],
                                 "processing": ["gain_normalised", "dark_subtracted",
                                                "unprocessed"]}),
    "stop_live_scan": _m("Live", "live_stop"),
    "get_live_status": _m("Live", "live_status"),
    "get_live_frame": _m("Live", hardware=False),
    # -- stage -------------------------------------------------------------------
    "get_stage_position": _m("Stage", "get_stage_position"),
    "move_stage_relative": _m("Stage", "move_stage", enums=_SPEED),
    "move_stage_absolute": _m("Stage", "move_stage", enums=_SPEED),
    "get_stage_status": _m("Stage", "get_stage_status"),
    "stop_stage": _m("Stage", "stop_stage"),
    "save_stage_location": _m("Stage", "get_stage_position"),
    "go_to_saved_location": _m("Stage", "move_stage"),
    "list_saved_locations": _a("Stage"),
    # -- spectroscopy ------------------------------------------------------------
    "configure_si": _m("Spectroscopy", "si_configure", mode="STEM",
                       enums={"kind": ["map", "line", "points", "time_series"]}),
    "start_si": _m("Spectroscopy", "si_start", mode="STEM"),
    "stop_si": _m("Spectroscopy", "si_stop"),
    "get_si_status": _m("Spectroscopy", "si_get_settings"),
    "eds_acquire_point": _m("Spectroscopy", "eds_acquire_point"),
    "eds_acquire_map": _m("Spectroscopy", "eds_acquire_map", mode="STEM"),
    **{name: _a("EDS analysis") for name in (
        "eds_describe_si", "eds_extract_elemental_map", "eds_extract_multiple_maps",
        "eds_quantify_si", "eds_region_spectra", "eds_edge_profile")},
    "eds_map_elements": _m("EDS analysis", hardware=False, extra=["calib:eds_energy"]),
    # -- files / windows ---------------------------------------------------------
    "download_remote_file": _m("Files", "read_file_chunk"),
    "list_remote_files": _m("Files", "list_files"),
    "fetch_remote_file": _m("Files", "load_file"),
    "list_local_images": _a("Files"),
    "inspect_image": _a("Files"),
    "list_dm_windows": _m("Windows", "dm.list_windows"),
    "select_dm_window": _m("Windows", "dm.select_window"),
    "close_dm_window": _m("Windows", "dm.close_window"),
    "save_dm_window": _m("Windows", "dm.save_window"),
    # -- analysis ----------------------------------------------------------------
    **{name: _a("Analysis") for name in (
        "compute_fft", "extract_lattice_spacings", "segment_image", "measure_nanorods",
        "find_particle_clusters", "edge_intensity_profile", "image_sharpness",
        "measure_image_shift", "fit_stage_image_mapping", "export_image_png",
        "export_overlay_png")},
    # Needs a measured stage<->image mapping for the image's mode; which mode
    # depends on the image, so this is checked at run time by the tool itself.
    "convert_pixel_to_stage": _a("Analysis"),
    # -- session / map / plan ----------------------------------------------------
    **{name: _a("Session") for name in (
        "add_map_object", "list_map_objects", "get_map_object", "mark_object_examined",
        "link_image_to_object", "get_experimental_plan", "set_experimental_plan",
        "get_operational_preferences", "set_operational_preferences",
        "get_sample_metadata", "set_sample_metadata", "get_session_task",
        "update_session_task", "report_agent_status", "list_session_activity")},
    # -- admin -------------------------------------------------------------------
    "backend_status": _m("Admin", hardware=False),
    "shutdown_dm_server": _m("Admin", "dm.shutdown"),
    "release_column_driver": _m("Admin"),
    # -- operator channel (web console only) -------------------------------------
    **{name: _a("Operator") for name in (
        "wait_for_operator", "ask_operator", "tell_operator")},
}

CATEGORY_ORDER = ["Acquire", "Stage", "Optics", "Focus", "Column", "Live", "Spectroscopy",
                  "EDS analysis", "Analysis", "Files", "Windows", "Session", "Operator",
                  "Scripting", "Admin"]

_ARGS_HEADER = re.compile(r"^\s*Args:\s*$")
_ARG_LINE = re.compile(r"^\s{2,}(\w+)(?:\s*\([^)]*\))?:\s*(.*)$")


def parse_arg_docs(doc: str) -> tuple[str, dict[str, str]]:
    """Split a Google-style docstring into (summary, {arg: help})."""
    lines = (doc or "").splitlines()
    summary, args, current, in_args = [], {}, None, False
    for line in lines:
        if _ARGS_HEADER.match(line):
            in_args = True
            continue
        if in_args:
            if line.strip() and not line.startswith((" ", "\t")):
                in_args = False
            elif re.match(r"^\s*(Returns|Raises|Notes?|Example)s?:\s*$", line):
                in_args = False
                continue
        if in_args:
            m = _ARG_LINE.match(line)
            if m and (current is None or len(line) - len(line.lstrip())
                      <= args.get("_indent", 99)):
                current = m.group(1)
                args[current] = m.group(2).strip()
                args["_indent"] = len(line) - len(line.lstrip())
            elif current and line.strip():
                args[current] = (args[current] + " " + line.strip()).strip()
        elif not args:
            summary.append(line)
    args.pop("_indent", None)
    text = "\n".join(summary).strip()
    first_para = text.split("\n\n", 1)[0].replace("\n", " ").strip()
    return first_para, args


def meta_for(name: str) -> dict:
    return TOOL_META.get(name) or {"category": "Other", "requires": [f"tool:{name}"],
                                   "mode": None, "hardware": True, "roles": {}, "enums": {}}


def build_catalog(tools) -> list[dict]:
    """Catalog entries for the web UI from FastMCP ``Tool`` objects."""
    entries = []
    for tool in tools:
        meta = meta_for(tool.name)
        summary, arg_docs = parse_arg_docs(tool.description or "")
        schema = dict(tool.inputSchema or {})
        props = {k: dict(v) for k, v in (schema.get("properties") or {}).items()}
        for arg, values in meta["enums"].items():
            if arg in props:
                props[arg]["enum"] = values
        for arg, help_text in arg_docs.items():
            if arg in props and help_text:
                props[arg].setdefault("description", help_text)
        for arg, role in meta["roles"].items():
            if arg in props:
                props[arg]["x-role"] = role
        schema["properties"] = props
        entries.append({
            "name": tool.name, "summary": summary, "description": tool.description or "",
            "input_schema": schema, **meta,
        })
    order = {c: i for i, c in enumerate(CATEGORY_ORDER)}
    entries.sort(key=lambda e: (order.get(e["category"], 99), e["name"]))
    return entries
