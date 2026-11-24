"""Which driver serves a request.

Built once from the effective drivers (``common.config.effective_drivers``)
and the instrument's roles. The rules, in order:

1. ``vendor_call`` goes to ``params["driver"]`` (a driver name), or the one
   driver of ``params["kind"]``.
2. ``<kind>.<command>`` (``dm.list_windows``) goes to the driver of that
   kind, or ``params["driver"]`` when there are several.
3. A slot operation goes to the driver holding its slot. In a multi-device
   slot, ``params["device"]`` picks the driver: a device id
   ``"<driver>/<local id>"`` or a role (``"detector:HAADF"``) resolved through
   ``instrument.roles``. Without a device, the first driver holding the slot
   (config order) serves it.

The driver receives the *local* device id (``"camera/1"``). Unknown commands
and missing slots are errors that say what is configured, never a guess.
"""

from __future__ import annotations

from common.commands import COMMAND_SLOT, DEVICE_KIND_SLOT, SINGLE_DRIVER_SLOTS, SLOTS


class RoutingError(ValueError):
    pass


class RoutingTable:
    def __init__(self, drivers: dict, roles: "dict | None" = None):
        self.drivers = dict(drivers)                 # name -> spec (config order)
        self.roles = roles or {}
        self.holders = {slot: [] for slot in SLOTS}
        self.by_kind: dict = {}
        for name, spec in self.drivers.items():
            for slot in spec.get("slots", []):
                self.holders.setdefault(slot, []).append(name)
            self.by_kind.setdefault(spec.get("kind"), []).append(name)

    # ------------------------------------------------------------------
    def holder(self, slot: str) -> "str | None":
        names = self.holders.get(slot) or []
        return names[0] if names else None

    def summary(self) -> dict:
        return {slot: list(names) for slot, names in self.holders.items() if names}

    def _one_of_kind(self, kind: str, params: dict, cmd: str) -> str:
        wanted = params.pop("driver", None)
        names = self.by_kind.get(kind) or []
        if wanted is not None:
            if wanted not in self.drivers:
                raise RoutingError(f"no driver named {wanted!r} (have: "
                                   f"{', '.join(self.drivers) or 'none'})")
            return wanted
        if not names:
            raise RoutingError(f"{cmd!r} needs a {kind!r} driver, and this microscope "
                               f"has none (drivers: {self._describe()})")
        if len(names) > 1:
            raise RoutingError(f"{cmd!r}: several {kind!r} drivers ({', '.join(names)}); "
                               f"pass 'driver' to pick one")
        return names[0]

    def _describe(self) -> str:
        return ", ".join(f"{n} ({s.get('kind')})" for n, s in self.drivers.items()) or "none"

    # ------------------------------------------------------------------
    def resolve(self, cmd: str, params: "dict | None" = None) -> "tuple[str, str, dict]":
        """``(driver name, command, params)`` for one request."""
        params = dict(params or {})
        if cmd == "vendor_call":
            if params.get("driver") is not None:
                name = params.pop("driver")
                params.pop("kind", None)
                if name not in self.drivers:
                    raise RoutingError(f"no driver named {name!r} (have: {self._describe()})")
                return name, cmd, params
            kind = params.pop("kind", None)
            if not kind:
                raise RoutingError("vendor_call needs 'driver' (a driver name) or 'kind'")
            return self._one_of_kind(kind, params, cmd), cmd, params
        if "." in cmd:
            kind = cmd.split(".", 1)[0]
            return self._one_of_kind(kind, params, cmd), cmd, params
        slot = COMMAND_SLOT.get(cmd)
        if slot is None:
            raise RoutingError(f"unknown command {cmd!r}")
        holders = self.holders.get(slot) or []
        if not holders:
            raise RoutingError(
                f"{cmd!r} is a {slot} operation, and no driver on this microscope holds "
                f"the {slot} slot (drivers: {self._describe()})")
        device = params.get("device")
        if device not in (None, "") and slot not in SINGLE_DRIVER_SLOTS:
            name, local = self.resolve_device(device, slot)
            if name not in holders:
                raise RoutingError(f"device {device!r} belongs to driver {name!r}, which "
                                   f"doesn't hold the {slot} slot")
            params["device"] = local
            return name, cmd, params
        if device == "":
            params.pop("device")
        return holders[0], cmd, params

    def resolve_device(self, ref, slot: "str | None" = None) -> "tuple[str, str]":
        """``(driver name, local id)`` for a device id or a role.

        Accepted forms: ``"dm/camera/1"`` (full id), ``"camera:imaging"`` or
        ``"detector:HAADF"`` (roles from ``instrument.roles``), and
        ``"camera/1"`` (local id on the slot's first driver)."""
        text = str(ref).strip()
        head, _, rest = text.partition("/")
        if rest and head in self.drivers:
            return head, rest
        if ":" in text and "/" not in text.split(":", 1)[0]:
            kind, _, role = text.partition(":")
            table = self.roles.get(kind + "s") or {}
            if role not in table:
                raise RoutingError(f"this microscope has no {kind} role {role!r} "
                                   f"(known: {', '.join(table) or 'none'}; set them in "
                                   f"instrument.roles.{kind}s)")
            value = table[role]
            if isinstance(value, bool):
                raise RoutingError(f"role {text!r} must map to an index or a device id")
            if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
                owner_slot = DEVICE_KIND_SLOT.get(kind, slot)
                owner = self.holder(owner_slot) if owner_slot else None
                if owner is None:
                    raise RoutingError(f"role {text!r} -> {value}, but no driver holds "
                                       f"the {owner_slot} slot")
                return owner, f"{kind}/{int(value)}"
            return self.resolve_device(value, slot)
        if rest and head in DEVICE_KIND_SLOT:
            owner = self.holder(DEVICE_KIND_SLOT[head]) or (self.holder(slot) if slot else None)
            if owner is None:
                raise RoutingError(f"no driver holds the {DEVICE_KIND_SLOT[head]} slot "
                                   f"for device {text!r}")
            return owner, text
        raise RoutingError(f"can't resolve device {ref!r}: use '<driver>/<kind>/<n>', a "
                           f"role like 'detector:HAADF', or '<kind>/<n>'")

    def full_device_id(self, name: str, local) -> "str | None":
        if local in (None, ""):
            return None
        return f"{name}/{local}"
