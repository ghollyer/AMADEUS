"""Driver kinds: how to build each one.

A *kind* names an implementation (``jeol``, ``sim``, ``dm``). A config's
``drivers`` section gives each driver a *name* and a kind, so one microscope
can run two drivers of different kinds side by side.

Python drivers are built here, in whatever process hosts them (the relay for
``host: in_process``, a ``drivers.host`` process otherwise). ``dm`` and
``remote`` are not built: they are processes of their own, and the relay
talks to them over ZMQ (``dm`` through the adapter in ``drivers/dm.py``).

Out-of-tree drivers: a settings entry ``"class": "package.module:ClassName"``
builds that class, and an installed package can register kinds under the
``amadeus.drivers`` entry-point group. Either way, nothing in AMADEUS core
changes to add a microscope.
"""

from __future__ import annotations

import importlib

from drivers.calibration import InstrumentCalibration

BUILTIN = {
    "jeol": "drivers.jeol:JeolDriver",
    "sim": "drivers.sim:SimDriver",
    "template": "drivers.template:TemplateDriver",
}
# Kinds that are separate processes the relay connects to, never built here.
REMOTE_KINDS = frozenset({"dm", "remote"})
ENTRY_POINT_GROUP = "amadeus.drivers"


def _load(target: str):
    module, _, attr = target.partition(":")
    if not attr:
        raise ValueError(f"driver class must be 'module:ClassName', got {target!r}")
    return getattr(importlib.import_module(module), attr)


def _entry_point(kind: str):
    try:
        from importlib.metadata import entry_points
        for ep in entry_points(group=ENTRY_POINT_GROUP):
            if ep.name == kind:
                return ep.load()
    except Exception:
        return None
    return None


def driver_class(kind: str, target: "str | None" = None):
    """The class for *kind* (or the explicit ``module:Class`` *target*)."""
    if target:
        return _load(target)
    if kind in BUILTIN:
        return _load(BUILTIN[kind])
    cls = _entry_point(kind)
    if cls is None:
        raise ValueError(f"unknown driver kind {kind!r} (built in: "
                         f"{', '.join(sorted(BUILTIN))}; remote: "
                         f"{', '.join(sorted(REMOTE_KINDS))}; or set 'class')")
    return cls


def build_driver(kind: str, settings: "dict | None" = None):
    """Instantiate a Python driver.

    *settings*: ``offline`` (JEOL), ``calibration`` (file path),
    ``instrument`` (the config's calibration section), ``options`` (keyword
    arguments for the driver class) and ``class`` (an explicit class)."""
    settings = dict(settings or {})
    if kind in REMOTE_KINDS:
        raise ValueError(f"the {kind!r} driver is a separate process; the relay "
                         f"connects to it, nothing to build")
    calibration = InstrumentCalibration.load(settings.get("calibration"),
                                             settings.get("instrument"))
    cls = driver_class(kind, settings.get("class"))
    options = dict(settings.get("options") or {})
    if kind == "jeol":
        return cls(offline=bool(settings.get("offline", False)), calibration=calibration,
                   **options)
    try:
        return cls(calibration=calibration, **options)
    except TypeError:
        return cls(**options)
