"""Device arguments for MCP tools: roles, device ids or indices.

A tool's ``detector`` / ``camera`` / ``spectrometer`` argument may be:

* a role name: ``"HAADF"`` or ``"detector:HAADF"`` (``instrument.roles``),
* a full device id: ``"dm/camera/1"`` (from ``get_capabilities`` devices),
* an integer index: that device on the slot's first driver (``"detector/1"``).

Everything goes to the relay as ``params["device"]``; the relay resolves it to
a driver and that driver's local device. Nothing here knows which vendor
serves the device.
"""

from __future__ import annotations


def device_params(value, kind: str) -> dict:
    """Request params selecting a *kind* device ({} = the default device)."""
    if value is None or value == "":
        return {}
    if isinstance(value, bool):
        raise ValueError(f"{kind} must be a role, a device id or an index, not {value!r}")
    if isinstance(value, int):
        return {"device": f"{kind}/{value}"}
    text = str(value).strip()
    if text.isdigit():
        return {"device": f"{kind}/{int(text)}"}
    if "/" in text or ":" in text:
        return {"device": text}
    return {"device": f"{kind}:{text}"}


def device_tag(params: dict, kind: str):
    """What to record in an image's tags for the device used."""
    return params.get("device")
