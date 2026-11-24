"""MCP tools — microscope column control.

The relay routes these to whichever driver holds the column slot on this
microscope (JEOL PyJEM, the simulator, the DM fallback, another vendor). The
tools are vendor-neutral: lenses and stigmators by role or name, modes by
name, and every device value comes back with its ``unit`` and ``range`` (DAC
0-65535 on JEOL; other vendors differ), so read before you write. Focus is
also given in nanometres when the instrument has a measured calibration
(``calibrated: true``).
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from client.transport.zmq_client import ZMQClient
from client.state.manager import StateManager
from client.state.models import MicroscopeState


def register(mcp: FastMCP, zmq: ZMQClient, state: StateManager,
             allow_scripting: bool = True, driver_kinds: "set[str] | None" = None) -> None:
    """*allow_scripting* False leaves out the escape hatches
    (``execute_vendor_call``, ``execute_dm_script``, ``execute_tem3_call``): the
    agent then can't call vendor APIs by name, only the dedicated tools.

    *driver_kinds* is the set of driver kinds in this microscope's config.
    Vendor-specific tools are registered only for kinds that are present;
    ``None`` (no config) registers them all, as before."""

    # ------------------------------------------------------------------
    # Focus
    # ------------------------------------------------------------------
    @mcp.tool()
    async def get_focus() -> dict:
        """Return the current focus: focus_dac (the lens that carries focus in
        this mode, with unit and range), every focus lens by role under
        "lenses" (e.g. "focus.fine", "focus.coarse"), and focus_nm with
        calibrated=true when the instrument has a DAC<->nm calibration."""
        data = await zmq.send_command("get_focus")
        state.microscope.focus_nm = data.get("focus_nm")
        state.microscope.focus_dac = data.get("focus_dac")
        return data

    @mcp.tool()
    async def set_focus(
        focus_nm: float | None = None,
        focus_dac: int | None = None,
        lens: str | None = None,
        value: int | None = None,
    ) -> dict:
        """Set absolute focus. Give one of: focus_nm, focus_dac, or lens + value.

        Args:
            focus_nm: Target defocus in nanometres; needs a measured focus
                calibration on the instrument (errors otherwise).
            focus_dac: Device value for the lens that carries focus in the
                current mode (see get_focus for its range).
            lens: A focus lens role from get_focus ("focus.fine",
                "focus.coarse"); give its device value in value. Coarse
                steps are large: prefer focus.fine for focusing.
            value: The device value for lens (get_focus gives the range).
        """
        params: dict = {}
        if focus_nm is not None:
            params["focus_nm"] = focus_nm
        if focus_dac is not None:
            params["focus_dac"] = focus_dac
        if lens is not None:
            params["lens"] = lens
            if value is not None:
                params["value"] = value
        data = await zmq.send_command("set_focus", params)
        state.microscope.focus_nm = data.get("focus_nm")
        state.microscope.focus_dac = data.get("focus_dac")
        return data

    @mcp.tool()
    async def change_focus(
        delta_nm: float | None = None,
        delta_dac: int | None = None,
    ) -> dict:
        """Apply a relative focus change. Provide exactly one argument.

        Args:
            delta_nm: Focus change in nanometres (requires calibration;
                positive = more overfocus).
            delta_dac: Focus change in raw DAC counts (always available).
        """
        params: dict = {}
        if delta_nm is not None:
            params["delta_nm"] = delta_nm
        if delta_dac is not None:
            params["delta_dac"] = delta_dac
        data = await zmq.send_command("change_focus", params)
        state.microscope.focus_nm = data.get("focus_nm")
        state.microscope.focus_dac = data.get("focus_dac")
        return data

    @mcp.tool()
    async def autofocus() -> dict:
        """Trigger Digital Micrograph's own autofocus routine (only on
        microscopes with DM, and often unavailable there). In STEM, prefer
        stem_autofocus, which sweeps the focus lens and scores sharpness itself."""
        data = await zmq.send_command("dm.autofocus")
        state.microscope.focus_nm = data.get("focus_nm")
        return data

    # ------------------------------------------------------------------
    # Magnification / illumination
    # ------------------------------------------------------------------
    @mcp.tool()
    async def get_magnification() -> dict:
        """Return the current magnification value and mag table index."""
        data = await zmq.send_command("get_magnification")
        state.microscope.magnification = data.get("magnification")
        state.microscope.mag_index = data.get("mag_index")
        return data

    @mcp.tool()
    async def set_magnification(magnification: float | None = None,
                                mag_index: int | None = None) -> dict:
        """Set the magnification. Give one of the two.

        Args:
            magnification: The value (e.g. 25000). Needs the instrument's
                magnification table for the current mode (capabilities list
                which modes have one); otherwise use mag_index.
            mag_index: Index into the current mode's magnification table.
        """
        params = {}
        if magnification is not None:
            params["magnification"] = magnification
        if mag_index is not None:
            params["mag_index"] = mag_index
        data = await zmq.send_command("set_magnification", params)
        state.microscope.magnification = data.get("magnification")
        state.microscope.mag_index = data.get("mag_index")
        return data

    @mcp.tool()
    async def get_spot_size() -> dict:
        """Return the current spot size index (the range is in the backend
        capabilities; on JEOL 0-7, smaller = brighter probe)."""
        data = await zmq.send_command("get_spot_size")
        state.microscope.spot_size = data.get("spot_size")
        return data

    @mcp.tool()
    async def set_spot_size(spot_size: int) -> dict:
        """Set the spot size index.

        Args:
            spot_size: Spot size index (see get_spot_size for the range).
        """
        data = await zmq.send_command("set_spot_size", {"spot_size": spot_size})
        state.microscope.spot_size = data.get("spot_size")
        return data

    @mcp.tool()
    async def get_alpha() -> dict:
        """Return the current alpha (convergence-angle) selector index.
        Only on instruments that have one (JEOL: feature "alpha")."""
        data = await zmq.send_command("get_alpha")
        state.microscope.alpha = data.get("alpha")
        return data

    @mcp.tool()
    async def set_alpha(alpha: int) -> dict:
        """Set the alpha (convergence-angle) selector.

        Args:
            alpha: Alpha selector index (0-8 on most JEOL instruments).
        """
        data = await zmq.send_command("set_alpha", {"alpha": alpha})
        state.microscope.alpha = data.get("alpha")
        return data

    # ------------------------------------------------------------------
    # Modes / high tension
    # ------------------------------------------------------------------
    @mcp.tool()
    async def get_instrument_mode() -> dict:
        """Return the TEM/STEM mode and the imaging function mode
        (e.g. MAG, LowMAG, SAMAG, DIFF)."""
        data = await zmq.send_command("get_instrument_mode")
        state.microscope.tem_stem_mode = data.get("mode")
        state.microscope.function_mode = data.get("function_mode")
        state.microscope.operation_mode = data.get("operation_mode")
        return data

    @mcp.tool()
    async def set_instrument_mode(mode: str) -> dict:
        """Switch between TEM and STEM mode. This is a large column state
        change — lenses and detectors reconfigure; re-check focus afterwards.

        Args:
            mode: "TEM" or "STEM".
        """
        data = await zmq.send_command("set_instrument_mode", {"mode": mode})
        state.microscope.tem_stem_mode = data.get("mode")
        state.microscope.operation_mode = data.get("operation_mode")
        return data

    @mcp.tool()
    async def get_function_mode() -> dict:
        """Return the imaging function mode name (and index). The names this
        instrument has are in the backend capabilities ("function_modes")."""
        data = await zmq.send_command("get_function_mode")
        state.microscope.function_mode = data.get("function_mode")
        return data

    @mcp.tool()
    async def set_function_mode(mode: str) -> dict:
        """Set the imaging function mode, by name.

        Args:
            mode: A mode name from the backend capabilities "function_modes"
                for the current TEM/STEM mode (JEOL TEM: MAG, MAG2, LowMAG,
                SAMAG, DIFF; STEM: Align, SM-LMAG, SM-MAG, AMAG, uuDIFF,
                Rocking).
        """
        data = await zmq.send_command("set_function_mode", {"mode": mode})
        state.microscope.function_mode = data.get("function_mode")
        return data

    @mcp.tool()
    async def get_high_tension() -> dict:
        """Return the accelerating voltage (volts and kV)."""
        data = await zmq.send_command("get_high_tension")
        state.microscope.high_tension_kV = data.get("high_tension_kV")
        return data

    # ------------------------------------------------------------------
    # Full snapshot
    # ------------------------------------------------------------------
    @mcp.tool()
    async def get_microscope_state() -> dict:
        """Return a full snapshot of microscope parameters (HT, mag, focus,
        spot, beam, stigmation, modes, stage). Partial read failures are
        reported in an "errors" field instead of failing the call."""
        data = await zmq.send_command("get_microscope_state")
        ms = MicroscopeState.model_validate({**state.microscope.model_dump(), **{
            k: v for k, v in data.items()
            if k in MicroscopeState.model_fields and v is not None
        }})
        state.microscope = ms
        return data

    # ------------------------------------------------------------------
    # Beam blank / valves / apertures
    # ------------------------------------------------------------------
    @mcp.tool()
    async def set_beam_blank(blanked: bool) -> dict:
        """Blank or unblank the electron beam.

        Args:
            blanked: True to blank, False to unblank.
        """
        data = await zmq.send_command("set_beam_blank", {"blanked": blanked})
        state.microscope.beam_blanked = blanked
        return data

    @mcp.tool()
    async def set_column_valves(open: bool) -> dict:
        """Open or close the column/gun beam valve.

        Args:
            open: True to open, False to close.
        """
        data = await zmq.send_command("set_column_valves", {"open": open})
        state.microscope.column_valves_open = data.get("open", open)
        return data

    @mcp.tool()
    async def get_aperture(kind: str | None = None) -> dict:
        """Return the selected aperture (kind, hole number, position).

        Args:
            kind: Optionally select an aperture first — one of
                "CLA", "OLA", "HCA", "SAA", "ENTA", "EDS" (or index 0-6).
        """
        params = {} if kind is None else {"kind": kind}
        data = await zmq.send_command("get_aperture", params)
        state.microscope.aperture_kind = data.get("kind_name")
        state.microscope.aperture_size = data.get("size")
        return data

    @mcp.tool()
    async def set_aperture(
        kind: str,
        size: int | None = None,
        x: int | None = None,
        y: int | None = None,
    ) -> dict:
        """Select an aperture and optionally set its hole and position.

        Args:
            kind: Aperture: "CLA" (condenser), "OLA" (objective), "HCA",
                "SAA" (selected area), "ENTA", "EDS" — or index 0-6.
            size: Hole number (0 = open/retracted, 1-4 = holes, largest first).
            x: Aperture position x (0-4095), for fine centering.
            y: Aperture position y (0-4095).
        """
        params: dict = {"kind": kind}
        if size is not None:
            params["size"] = size
        if x is not None:
            params["x"] = x
        if y is not None:
            params["y"] = y
        data = await zmq.send_command("set_aperture", params)
        state.microscope.aperture_kind = data.get("kind_name")
        state.microscope.aperture_size = data.get("size")
        return data

    # ------------------------------------------------------------------
    # Deflectors / stigmators (device units — DAC on JEOL)
    # ------------------------------------------------------------------
    @mcp.tool()
    async def get_beam_shift() -> dict:
        """Return the beam shift: x, y in device units, with unit, range and
        neutral (on JEOL a DAC 0-65535 with 32768 neutral)."""
        return await zmq.send_command("get_beam_shift")

    @mcp.tool()
    async def set_beam_shift(x: int, y: int) -> dict:
        """Set the beam shift, in the device units get_beam_shift reports.

        Args:
            x: Beam shift x (within get_beam_shift's range).
            y: Beam shift y.
        """
        return await zmq.send_command("set_beam_shift", {"x": x, "y": y})

    @mcp.tool()
    async def get_beam_tilt() -> dict:
        """Return the beam tilt: x, y in device units, with unit, range and neutral."""
        return await zmq.send_command("get_beam_tilt")

    @mcp.tool()
    async def set_beam_tilt(x: int, y: int) -> dict:
        """Set the beam tilt, in the device units get_beam_tilt reports.

        Args:
            x: Beam tilt x (within get_beam_tilt's range).
            y: Beam tilt y.
        """
        return await zmq.send_command("set_beam_tilt", {"x": x, "y": y})

    @mcp.tool()
    async def get_stigmation() -> dict:
        """Return every stigmator's x, y under "stigmators" (objective,
        condenser, and intermediate where the instrument has one), with unit
        and range."""
        return await zmq.send_command("get_stigmation")

    @mcp.tool()
    async def set_stigmation(stigmator: str, x: int, y: int) -> dict:
        """Set a stigmator, in the device units get_stigmation reports.

        Args:
            stigmator: "objective", "condenser" or "intermediate" (get_stigmation
                and the backend capabilities list which this instrument has).
            x: Stigmation x (within get_stigmation's range).
            y: Stigmation y.
        """
        return await zmq.send_command("set_stigmation",
                                      {"stigmator": stigmator, "x": x, "y": y})

    # ------------------------------------------------------------------
    # Escape hatches
    # ------------------------------------------------------------------
    if not allow_scripting:
        return

    def has(kind: str) -> bool:
        return driver_kinds is None or kind in driver_kinds

    @mcp.tool()
    async def execute_vendor_call(method: str, args: list | dict | None = None,
                                  driver: str | None = None,
                                  kind: str | None = None) -> dict:
        """Escape hatch: one named call into a vendor API, through its driver.
        Each driver whitelists what it accepts; no arbitrary code. Use only
        for operations no dedicated tool covers.

        Args:
            method: The vendor call. JEOL: "<TEM3 class>.<method>", e.g.
                "EOS3.GetSpotSize". DM: "execute_script" with args
                {"script": ...}.
            args: Arguments for the call.
            driver: The driver name (backend_status lists them), or
            kind: the driver kind ("jeol", "dm", ...) when there is one of it.
        """
        params: dict = {"method": method, "args": args if args is not None else []}
        if driver:
            params["driver"] = driver
        elif kind:
            params["kind"] = kind
        else:
            raise ValueError("pass driver (a name from backend_status) or kind")
        return await zmq.send_command("vendor_call", params)

    if has("dm"):
        @mcp.tool()
        async def execute_dm_script(script: str) -> dict:
            """Execute an arbitrary DM-Script string in Digital Micrograph
            (escape hatch for Gatan-side operations not covered by other tools).

            Args:
                script: DM-Script source code.
            """
            return await zmq.send_command("dm.execute_script", {"script": script})

    if not has("jeol"):
        return

    @mcp.tool()
    async def execute_tem3_call(
        class_name: str, method: str, args: list | None = None
    ) -> dict:
        """Call a single PyJEM TEM3 method by name on the hardware backend
        (escape hatch for JEOL operations not covered by other tools; no
        arbitrary code — one public method per call).

        Args:
            class_name: One of Stage3, EOS3, Lens3, Def3, Apt3, HT3, FEG3,
                Detector3.
            method: Public method name, e.g. "GetPos", "SetOLf".
            args: Positional arguments for the method.
        """
        return await zmq.send_command("jeol.tem3_call", {
            "class_name": class_name, "method": method, "args": args or [],
        })
