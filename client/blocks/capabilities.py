"""Capability manifests and the compatibility check for action blocks.

The idea in one line: a microscope *publishes* what it can do as a flat set of
strings, a block *declares* what it needs, and compatibility is set
containment. Nothing is typed against a particular instrument's config, so a
sequence written on one microscope runs on any other that has the same
capabilities, and a new vendor backend needs no client changes.

Capability vocabulary (namespaced strings):

=========================  ==========================================================
``cmd:<command>``          a relay command that actually works here: each driver's
                           implemented operations in the slots it holds, and its
                           vendor extras (``cmd:dm.list_windows``), when it is up
``cmd:vendor_call``        some driver accepts the generic vendor escape hatch
``driver:<kind>``          a driver of that kind is configured and up (``jeol``,
                           ``dm``, ``sim``, ...)
``mode:TEM`` / ``mode:STEM``  imaging modes the column has
``feature:<name>``         optional hardware/client features (``piezo``,
                           ``stage_speed``, ``backlash_approach``, ``scripting``)
``detector:any``           at least one DigiScan signal
``detector:<role>``        a named detector role (``HAADF``, ``BF``, ...)
``camera:any``             at least one camera
``camera:<role>``          a named camera role (``imaging``, ``diffraction``, ...)
``calib:focus.<mode>``     focus DAC<->nm slope measured for that mode
``calib:image_mapping.<mode>``  stage<->image Jacobian measured for that mode
``calib:eds_energy``       EDS detector energy calibration present
``tool:<name>``            the MCP tool is registered (scripting can remove some)
=========================  ==========================================================

Roles: the config's ``instrument.roles`` maps role names to this
microscope's devices, by index on the slot's first driver or by device id
(``{"detectors": {"HAADF": 1}, "cameras": {"imaging": 0, "diffraction":
"tvips/camera/0"}}``). Without a table, detector roles are guessed from the
scan unit's signal names (``HAADF``/``ADF``/``ABF``/``BF``/``DF``/``SE``),
and the first camera is ``imaging``.

The manifest comes from the relay's ``get_capabilities``: one entry per
driver under ``drivers``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from client.blocks import expr
from client.blocks.catalog import meta_for
from client.blocks.model import Ask, Call, Fail, ForEach, If, Pause, SetVar, While, \
    children_of
from common.roles import guess_detector_roles

def _guess_detector_roles(detectors: list[dict]) -> dict[str, int]:
    return guess_detector_roles(detectors)


@dataclass
class Capabilities:
    caps: set[str] = field(default_factory=set)
    roles: dict[str, dict[str, Any]] = field(default_factory=dict)
    sources: dict[str, Any] = field(default_factory=dict)   # raw manifest, for display
    notes: list[str] = field(default_factory=list)

    def has(self, cap: str) -> bool:
        return cap in self.caps

    def resolve_role(self, ref: str) -> Any:
        """``"detector:HAADF"`` -> this instrument's signal index."""
        kind, _, role = ref.partition(":")
        table = self.roles.get(kind + "s") or {}
        if role in table:
            return table[role]
        raise LookupError(f"this instrument has no {kind} role {role!r} "
                          f"(known: {', '.join(table) or 'none'})")

    def to_json(self) -> dict:
        return {"caps": sorted(self.caps), "roles": self.roles, "notes": self.notes,
                "sources": self.sources}

    @classmethod
    def from_manifest(cls, manifest: dict | None, tool_names: Iterable[str] = (),
                      allow_scripting: bool = True,
                      instrument: dict | None = None) -> "Capabilities":
        """Build from the relay's ``get_capabilities`` reply (``None`` if the
        relay is unreachable) plus what the client itself knows."""
        manifest = manifest or {}
        caps: set[str] = {f"tool:{t}" for t in tool_names}
        notes: list[str] = []
        if allow_scripting:
            caps.add("feature:scripting")

        drivers = manifest.get("drivers")
        detectors = cameras = None
        if isinstance(drivers, dict):
            detectors, cameras = cls._from_drivers(drivers, caps, notes)
        elif manifest:
            notes.append("the relay sent no driver manifest; instrument blocks are "
                         "greyed out")

        inst = instrument if instrument is not None else (manifest.get("instrument") or {})
        role_cfg = inst.get("roles") or {}
        det_roles = dict(role_cfg.get("detectors") or {})
        cam_roles = dict(role_cfg.get("cameras") or {})
        all_devices = set()
        if isinstance(drivers, dict):
            all_devices = {f"{n}/{d['id']}" for n, m in drivers.items() if m.get("ok")
                           for d in (m.get("devices") or [])}

        def known(value, indices: set) -> bool:
            if isinstance(value, str) and not value.isdigit():
                return not all_devices or value in all_devices    # a device id
            return int(value) in indices

        if detectors or any("/detector/" in d for d in all_devices):
            caps.add("detector:any")
            for role, index in _guess_detector_roles(detectors or []).items():
                det_roles.setdefault(role, index)
            indices = {int(d.get("index", -1)) for d in detectors or []}
            for role, value in list(det_roles.items()):
                if not known(value, indices):
                    notes.append(f"detector role {role} -> {value}, but the scan unit has "
                                 f"no such detector")
                    det_roles.pop(role)
        if cameras or any("/camera/" in d for d in all_devices):
            caps.add("camera:any")
            if cameras:
                cam_roles.setdefault("imaging", int(cameras[0].get("id", 0)))
            indices = {int(c.get("id", -1)) for c in cameras or []}
            for role, value in list(cam_roles.items()):
                if not known(value, indices):
                    notes.append(f"camera role {role} -> {value}, but there is no such "
                                 f"camera")
                    cam_roles.pop(role)
        caps.update(f"detector:{r}" for r in det_roles)
        caps.update(f"camera:{r}" for r in cam_roles)

        focus = ((inst.get("focus") or {}).get("by_mode")) or {}
        for mode, cal in focus.items():
            if cal and cal.get("dac_per_nm") is not None and cal.get("dac_at_zero") is not None:
                caps.add(f"calib:focus.{mode}")
        mapping = ((inst.get("stage") or {}).get("image_mapping")) or {}
        for mode, entry in mapping.items():
            if entry and entry.get("stage_to_image"):
                caps.add(f"calib:image_mapping.{mode}")
        if (inst.get("eds") or {}).get("energy_calibration"):
            caps.add("calib:eds_energy")

        return cls(caps=caps, roles={"detectors": det_roles, "cameras": cam_roles},
                   sources=manifest, notes=notes)

    @staticmethod
    def _from_drivers(drivers: dict, caps: set, notes: list) -> tuple:
        """Fold the per-driver manifest into *caps*. Returns the scan
        detectors and cameras (as index lists, for role guessing) of the
        first driver holding each slot."""
        detectors = cameras = None
        for name, man in drivers.items():
            kind = man.get("kind") or name
            if not man.get("ok"):
                notes.append(f"driver {name} ({kind}) unavailable: "
                             f"{man.get('error', 'unknown')}; its blocks are greyed out")
                continue
            caps.add(f"driver:{kind}")
            caps.update(f"cmd:{c}" for c in man.get("commands", []))
            caps.update(f"cmd:{c}" for c in man.get("extras", []))
            if man.get("vendor_call"):
                caps.update({"cmd:vendor_call", "feature:vendor_call"})
            caps.update(f"mode:{m}" for m in man.get("modes", []))
            caps.update(f"feature:{f}" for f in man.get("features", []))
            devices = man.get("devices") or []
            slots = set(man.get("slots") or [])
            if detectors is None and "scan" in slots:
                detectors = man.get("detectors")
                if detectors is None:
                    detectors = [{"index": int(d["id"].split("/")[-1]), "name": d.get("name")}
                                 for d in devices if d.get("id", "").startswith("detector/")]
            if cameras is None and "camera" in slots:
                cameras = man.get("cameras")
                if cameras is None:
                    cameras = [{"id": int(d["id"].split("/")[-1]), "name": d.get("name")}
                               for d in devices if d.get("id", "").startswith("camera/")]
        return detectors, cameras


# --- requirements & compatibility -----------------------------------------------------

def tool_requirements(tool: str) -> list[str]:
    return [f"tool:{tool}", *meta_for(tool)["requires"]]


def _role_refs(value: Any) -> list[str]:
    if isinstance(value, dict):
        if set(value) == {"$role"}:
            return [str(value["$role"])]
        return [r for v in value.values() for r in _role_refs(v)]
    if isinstance(value, list):
        return [r for v in value for r in _role_refs(v)]
    return []


def _exprs_of(node) -> list[str]:
    out = []
    if isinstance(node, (If, While)):
        out.append(node.cond)
    elif isinstance(node, ForEach):
        out.append(node.items)
    elif isinstance(node, SetVar):
        out.append(node.value)
    elif isinstance(node, Call):
        def walk(v):
            if isinstance(v, dict):
                if set(v) == {"$expr"}:
                    out.append(v["$expr"])
                else:
                    for x in v.values():
                        walk(x)
            elif isinstance(v, list):
                for x in v:
                    walk(x)
        walk(node.args)
    return out


@dataclass
class Missing:
    cap: str
    path: str
    tool: str | None
    guarded: bool = False

    def to_json(self) -> dict:
        return {"cap": self.cap, "path": self.path, "tool": self.tool}


@dataclass
class Report:
    ok: bool
    missing: list[Missing]
    warnings: list[str]
    required: list[str]
    mode_guards: list[dict]

    def to_json(self) -> dict:
        return {"ok": self.ok, "missing": [m.to_json() for m in self.missing],
                "warnings": self.warnings, "required": self.required,
                "mode_guards": self.mode_guards}


def requirements(body: list, assumed: frozenset = frozenset(), path: str = "") -> \
        list[tuple[str, str, str | None]]:
    """(cap, block path, tool) for every hard requirement under *body*.

    A branch guarded by ``has("cap")`` does not require ``cap``: generic
    sequences test for optional features and fall back."""
    out = []
    for i, node in enumerate(body):
        here = f"{path}/{i}:{node.type}"
        if isinstance(node, Call):
            here = f"{path}/{i}:{node.tool}"
            for cap in tool_requirements(node.tool) + list(node.requires) \
                    + _role_refs(node.args):
                if cap not in assumed:
                    out.append((cap, here, node.tool))
        if isinstance(node, If):
            guarded = assumed | frozenset(expr.has_calls(node.cond))
            out += requirements(node.then, guarded, here + "/then")
            out += requirements(node.orelse, assumed, here + "/else")
            continue
        if isinstance(node, While):
            guarded = assumed | frozenset(expr.has_calls(node.cond))
            out += requirements(node.body, guarded, here + "/body")
            continue
        for slot, kids in children_of(node):
            out += requirements(kids, assumed, f"{here}/{slot}")
    return out


def _walk(body: list, path: str = ""):
    for i, node in enumerate(body):
        here = f"{path}/{i}:{getattr(node, 'tool', None) or node.type}"
        yield here, node
        for slot, kids in children_of(node):
            yield from _walk(kids, f"{here}/{slot}")


def check(seq, caps: Capabilities) -> Report:
    """Static compatibility of *seq* (a :class:`Sequence` or a block list)
    with *caps*. Run when a sequence is loaded and before it is queued."""
    body = seq.body if hasattr(seq, "body") else list(seq)
    declared = list(getattr(seq, "requires", []) or [])
    reqs = requirements(body) + [(c, "/", None) for c in declared]
    missing = [Missing(cap, path, tool) for cap, path, tool in reqs if not caps.has(cap)]
    warnings: list[str] = []
    guards = []
    for here, node in _walk(body):
        for source in _exprs_of(node):
            try:
                expr.check(source)
            except expr.ExprError as exc:
                missing.append(Missing("valid-expression", here, None))
                warnings.append(f"{here}: {exc}")
        if isinstance(node, Call):
            mode = meta_for(node.tool).get("mode")
            if mode:
                guards.append({"path": here, "tool": node.tool, "mode": mode,
                               "auto_switch": node.ensure_mode})
                if not caps.has(f"mode:{mode}") and caps.caps:
                    missing.append(Missing(f"mode:{mode}", here, node.tool))
            for ref in _role_refs(node.args):
                kind = ref.partition(":")[0]
                if kind not in ("detector", "camera"):
                    warnings.append(f"{here}: unknown role kind in {ref!r}")
    if any(isinstance(n, (Pause, Ask)) for _, n in _walk(body)):
        warnings.append("this sequence waits for the operator at least once")
    if not any(isinstance(n, (Call, Fail)) for _, n in _walk(body)):
        warnings.append("this sequence has no tool calls")
    seen = set()
    unique = []
    for m in missing:
        key = (m.cap, m.path)
        if key not in seen:
            seen.add(key)
            unique.append(m)
    required = sorted({c for c, _, _ in reqs})
    return Report(ok=not unique, missing=unique, warnings=warnings,
                  required=required, mode_guards=guards)
