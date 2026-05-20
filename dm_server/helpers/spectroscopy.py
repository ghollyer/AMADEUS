"""DM helpers — spectrum-image (SI) acquisition.

Supports all four SI modes the SIAcquire plug-in exposes:

  - "2D Array"     — rectangular XY raster (used for EDS maps)
  - "Line Scan"    — 1D scan along an arbitrary line on the survey
  - "Multi-Point"  — one or more discrete points (point spectra)
  - "Time Series"  — repeated acquisition at a single point (or 2D scan)

The per-pixel exposure field is configured through ``"Pixel Time"``. By
default, ``Signal 1`` and ``Scan`` are enabled; change the signal parameter for
deployments where EDS is wired to a different channel.

Survey-image protocol
---------------------
Every ``si_configure`` call begins with ``DSInvokeButton(3)`` +
``DSWaitUntilFinished()`` to acquire a fresh STEM image.  ``SISetMode`` then
picks that image up as the SI survey and assigns a default survey ROI.

This is required for the rest of AMADEUS to function: the survey image is the
coordinate frame in which ROIs, beam positions, and analysis results are
defined — if it is stale, pixel→stage mappings drift and downstream tools
operate on the wrong field of view.
"""

from dm_server.helpers._dm_util import exec_script, safe_exec_script, read_float, read_string

_TG = "GetPersistentTagGroup()"

VALID_SI_MODES = ("2D Array", "Line Scan", "Multi-Point", "Time Series")

# Mode-specific field name table. Keys are the kwargs callers pass in; values
# are the literal SI palette item names.
_MODE_FIELDS = {
    "2D Array": {
        "x_samples":     "2D Array, X samples",
        "y_samples":     "2D Array, Y samples",
        "sampling":      "2D Array, sampling",
        "sampling_lock": "2D Array, sampling lock",
    },
    "Line Scan": {
        "samples":          "LineScan, samples",
        "sampling":         "LineScan, sampling",
        "averaging":        "LineScan, averaging check",
        "averaging_points": "LineScan, averaging points",
        "averaging_angle":  "LineScan, averaging angle",
        "sampling_lock":    "LineScan, sampling lock",
    },
    "Multi-Point": {
        "points": "MultiPoint, points",
    },
    "Time Series": {
        "iterations":       "TimeSeries, iterations",
        "delay_time_s":     "TimeSeries, delay time",
        "scan_2d":          "TimeSeries, 2D scan check",
        "scan_2d_x_points": "TimeSeries, 2D scan X points",
        "scan_2d_y_points": "TimeSeries, 2D scan Y points",
    },
}

# Per-pixel exposure on this instrument lives in this field; this is the
# point most likely to differ on a future microscope, so keep it isolated.
PIXEL_TIME_FIELD = "Pixel Time"


def _fmt(val) -> str:
    """Format a Python value for embedding into a DM-Script literal."""
    if isinstance(val, bool):
        return "1" if val else "0"
    return repr(float(val)) if isinstance(val, float) else str(int(val))


def _build_configure_script(mode: str, params: dict, signal: int) -> str:
    """Compose the full configure-mode DM-Script (survey + mode + fields + signals)."""
    lines = [
        "// 1. Fresh survey image if one does not exist - required by AMADEUS for ROI/coord mapping",
        # Bind first, then test with ImageIsValid; some script parsers reject
        # applying the negation operator directly to an image value.
        # Braces also need to be on their own lines for this DM-Script build.
        "image __survey := GetFrontImage()",
        "if ( !ImageIsValid( __survey ) )",
        "{",
        "    DSInvokeButton(3)",
        "    DSWaitUntilFinished()",
        "}",
        "",
        "// 2. Enter SI mode (assigns the front image as survey + default ROI)",
        f'if ( !SISetMode("{mode}") )',
        f'    Throw("Could not set SI mode to {mode}")',
        "",
        "// 3. Common fields",
    ]
    if "pixel_time_s" in params:
        lines.append(f'SISetFieldValue("{PIXEL_TIME_FIELD}", {repr(float(params["pixel_time_s"]))})')
    if "passes" in params:
        lines.append(f'SISetFieldValue("Passes", {int(params["passes"])})')
    if "drift" in params:
        lines.append(f'SIInvokeButton("Drift", {1 if params["drift"] else 0})')

    lines.append("")
    lines.append(f"// 4. {mode} fields")
    for key, field_name in _MODE_FIELDS[mode].items():
        if key in params:
            lines.append(f'SISetFieldValue("{field_name}", {_fmt(params[key])})')

    lines.append("")
    lines.append("// 5. Enable scan + the requested signal channel")
    lines.append('SIInvokeButton("Scan", 1)')
    lines.append(f'SIInvokeButton("Signal {signal}", 1)')

    return "\n".join(lines)


def si_configure(params: dict) -> dict:
    """Configure an SI acquisition for the chosen mode.

    Grabs a fresh survey image, sets the SI mode, applies common +
    mode-specific field values, and enables the Scan and Signal buttons.
    Does *not* trigger Capture — call ``si_start`` for that, optionally
    after manual ROI adjustment in DM.

    Common parameters
    -----------------
    mode : str
        One of "2D Array", "Line Scan", "Multi-Point", "Time Series".
    pixel_time_s : float, optional
        Per-pixel exposure ("Pixel Time" palette field).
    signal : int, default 1
        Signal channel to enable (1-5). Defaults to 1.
    passes : int, optional
        Number of acquisition passes.
    drift : bool, optional
        Toggle spatial drift correction.

    Mode-specific parameters
    ------------------------
    "2D Array": ``x_samples``, ``y_samples`` (int); ``sampling`` (float, nm/px);
                ``sampling_lock`` (bool).
    "Line Scan": ``samples`` (int); ``sampling`` (float, nm/px);
                ``sampling_lock``, ``averaging`` (bool);
                ``averaging_points`` (int); ``averaging_angle`` (float).
    "Multi-Point": ``points`` (int).
    "Time Series": ``iterations`` (int); ``delay_time_s`` (float);
                ``scan_2d`` (bool); ``scan_2d_x_points``, ``scan_2d_y_points`` (int).
    """
    mode = params.get("mode", "2D Array")
    if mode not in VALID_SI_MODES:
        raise ValueError(
            f"Unknown SI mode {mode!r}. Valid modes: {list(VALID_SI_MODES)}"
        )

    signal = int(params.get("signal", 1))
    if not 1 <= signal <= 5:
        raise ValueError(f"signal must be in 1-5, got {signal}")

    exec_script(_build_configure_script(mode, params, signal))

    applied_mode_params = {
        key: params[key] for key in _MODE_FIELDS[mode] if key in params
    }
    result = {
        "configured":  True,
        "mode":        mode,
        "signal":      signal,
        "mode_params": applied_mode_params,
    }
    for key in ("pixel_time_s", "passes", "drift"):
        if key in params:
            result[key] = params[key]
    return result


def si_start(params: dict) -> dict:
    """Trigger the SI palette's Capture button.

    Assumes ``si_configure`` has been called.  Survey image is *not*
    re-acquired here so any manual ROI adjustment made between configure
    and start is preserved.

    Refuses to start (returns ``started=False`` plus a ``warning`` string)
    if the column valves are closed or the microscope is not in SCANNING
    operation mode — DM itself pops a warning in that state and the
    acquisition does nothing useful.
    """
    # Probe valves + operation mode before touching the SI palette.  Both
    # functions are well-supported on every GMS build we target; the result
    # is read back through the same persistent-tag channel as the rest of the
    # helper.
    safe_exec_script(
        f"""\
        number valves = EMGetColumnValvesOpen()
        string opmode = EMGetOperationMode()
        TagGroup tg = {_TG}
        TagGroupSetTagAsFloat(tg,  "__amadeus__:si:pre:valves", valves)
        TagGroupSetTagAsString(tg, "__amadeus__:si:pre:opmode", opmode)
        """,
        expected_tags=["si:pre:valves", "si:pre:opmode"],
    )

    valves_open = bool(read_float("si:pre:valves"))
    # Some GMS builds append whitespace/newline to the mode string; tolerate
    # that and match case-insensitively against the documented values.
    opmode = read_string("si:pre:opmode").strip()
    in_scanning = opmode.upper().startswith("SCANNING")

    if not valves_open or not in_scanning:
        reasons = []
        if not valves_open:
            reasons.append("column valves are closed")
        if not in_scanning:
            reasons.append(f"operation mode is {opmode!r} (need SCANNING)")
        return {
            "started":        False,
            "warning":        "; ".join(reasons),
            "valves_open":    valves_open,
            "operation_mode": opmode,
        }

    # Keep braces on their own lines for compatibility with stricter script
    # parsers. Throw on the failure path; fall through to Capture otherwise.
    exec_script("""\
DSWaitUntilFinished()
image survey := SIGetSurveyImage()
if ( !ImageIsValid( survey ) )
    Throw("Survey image not found")
SIInvokeButton("Capture", 1)
""")
    return {
        "started":        True,
        "valves_open":    valves_open,
        "operation_mode": opmode,
    }


def si_stop(params: dict) -> dict:
    """Stop the current SI acquisition."""
    exec_script('SIInvokeButton("Capture", 0)')
    return {"stopped": True}


def si_get_settings(params: dict) -> dict:
    """Return current SI mode and acquisition state.

    Returns
    -------
    {"is_active": bool, "is_paused": bool, "mode": str}
    """
    exec_script(f"""\
        number active = SIIsAcquisitionActive()
        number paused = SIIsAcquisitionPaused()
        string mode = SIGetMode()
        TagGroup tg = {_TG}
        TagGroupSetTagAsFloat(tg,  "__amadeus__:si:active", active)
        TagGroupSetTagAsFloat(tg,  "__amadeus__:si:paused", paused)
        TagGroupSetTagAsString(tg, "__amadeus__:si:mode",   mode)
    """)
    return {
        "is_active": bool(read_float("si:active")),
        "is_paused": bool(read_float("si:paused")),
        "mode":      read_string("si:mode"),
    }
