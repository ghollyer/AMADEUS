"""The Gatan Digital Micrograph driver, relay side.

``dm_server`` runs inside DM (a separate process with its own embedded
Python) and keeps its original command names and parameters: it is
self-contained, and must not import anything from AMADEUS. This adapter is
the other half. It describes what DM provides in the vendor-neutral
vocabulary, and translates neutral requests into ``dm_server`` requests:

* neutral command names -> DM names (``list_files`` -> ``list_dm_files``),
* ``dm.<extra>`` -> the bare DM command (``dm.list_windows`` -> ``list_windows``),
* ``device`` ids -> DM indices (``"camera/1"`` -> ``camera_id=1``,
  ``"detector/0"`` -> ``signal_index=0``).

DM also implements a fallback hardware path (Gatan ``EM*`` scripting). A config
that gives this driver the ``column``/``stage`` slots routes hardware there
(the wizard's "DM EM* scripting (fallback)" choice).
"""

from __future__ import annotations

KIND = "dm"

# Neutral name -> dm_server name, where they differ.
_TO_DM = {"list_files": "list_dm_files", "load_file": "load_dm_file"}

# What dm_server serves, per slot (neutral names).
SLOT_COMMANDS = {
    "scan": ["stem_acquire", "get_stem_settings", "set_stem_settings", "get_detectors",
             "set_detector", "move_beam_to", "get_beam_position"],
    "camera": ["camera_acquire", "get_camera_info", "list_cameras"],
    "live": ["live_start", "live_stop", "live_status"],
    "spectrometer": ["si_configure", "si_start", "si_stop", "si_get_settings",
                     "eds_acquire_point", "eds_acquire_map"],
    "files": ["list_files", "load_file", "read_file_chunk"],
    # The EM* hardware fallback path. get/set_aperture exist in dm_server but
    # raise NotImplementedError, so they are left out.
    "column": ["get_focus", "set_focus", "change_focus", "get_magnification",
               "set_magnification", "get_beam_shift", "set_beam_shift",
               "get_beam_tilt", "set_beam_tilt", "get_stigmation", "set_stigmation",
               "get_microscope_state", "set_beam_blank", "set_column_valves"],
    "stage": ["get_stage_position", "move_stage"],
}

# Neutral spectrum-image kinds -> DM's SI mode names.
SI_MODES = {"map": "2D Array", "line": "Line Scan", "points": "Multi-Point",
            "time_series": "Time Series"}

# DM-only operations, reachable as "dm.<name>".
EXTRAS = ("execute_script", "autofocus", "list_windows", "select_window",
          "close_window", "save_window", "shutdown")


def commands_for(slots) -> list:
    out = set()
    for slot in slots:
        out.update(SLOT_COMMANDS.get(slot, ()))
    return sorted(out)


def translate(cmd: str, params: dict) -> "tuple[str, dict]":
    """Neutral request -> the ``dm_server`` request."""
    params = dict(params or {})
    if cmd.startswith(KIND + "."):
        cmd = cmd[len(KIND) + 1:]
    if cmd == "vendor_call":
        method = str(params.get("method") or "")
        if method != "execute_script":
            raise ValueError("the dm driver's vendor_call supports method "
                             "'execute_script' (args: {'script': ...})")
        args = params.get("args") or {}
        return "execute_script", {"script": args.get("script", "") if isinstance(args, dict)
                                  else (args[0] if args else "")}
    cmd = _TO_DM.get(cmd, cmd)
    if cmd == "si_configure" and "kind" in params:
        kind = params.pop("kind")
        if kind not in SI_MODES:
            raise ValueError(f"spectrum image kind must be one of {', '.join(SI_MODES)}")
        params.setdefault("mode", SI_MODES[kind])
    device = params.pop("device", None)
    if device not in (None, ""):
        kind, _, index = str(device).partition("/")
        try:
            value = int(index)
        except ValueError:
            raise ValueError(f"DM device ids are 'detector/<n>', 'camera/<n>' or "
                             f"'spectrometer/<n>'; got {device!r}")
        if kind == "detector":
            params.setdefault("signal_index", value)
        elif kind == "camera":
            params.setdefault("camera_id", value)
        elif kind == "spectrometer":
            params.setdefault("signal", value)
        else:
            raise ValueError(f"DM has no {kind!r} devices (device {device!r})")
    return cmd, params


def devices_from_probe(cameras, detectors) -> list:
    """Device list from ``list_cameras`` / ``get_detectors`` replies."""
    out = []
    for det in detectors or []:
        out.append({"id": f"detector/{int(det.get('index', 0))}", "slot": "scan",
                    "name": det.get("name")})
    for cam in cameras or []:
        out.append({"id": f"camera/{int(cam.get('id', 0))}", "slot": "camera",
                    "name": cam.get("name")})
    return out
