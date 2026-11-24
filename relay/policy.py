"""Relay-side safety rules, the same for every driver.

Checked after routing and before a request reaches a driver, so a new driver
gets them without writing a line. From the config:

* ``policy.stage_limits``: soft limits on stage targets
  (``{"x_m": [min, max], ..., "alpha_deg": [min, max]}``). Relative moves are
  checked against the position read fresh from the stage driver.
* ``policy.confirm``: commands that need ``confirmed: true`` on the request.
* ``policy.confirm_vendor_writes`` (default true): escape-hatch calls
  (``vendor_call`` and ``<kind>.<name>_call`` extras such as
  ``jeol.tem3_call``) whose method is not a read (``Get*``/``Is*``) need
  ``confirmed: true`` too. They bypass every other rule here (a raw
  ``Stage3.SetX`` skips the stage limits), so a human looks at each write.
  ``dm.execute_script`` has no method to classify and stays ungated.
* ``exclusive``: groups of devices that can't be in use together, such as two
  retractable cameras (switching physically moves them, ~10-30 s on the
  F200). Using a different member of a group than the last one used needs
  ``confirmed: true``.

A confirmable refusal comes back with ``needs_confirmation: true``. The
client (``client/confirm.py``, at the ``ZMQClient`` chokepoint) then asks the
operator in whatever interface they are watching and, on Allow, resends with
``confirmed: true``. The client drops any ``confirmed`` a caller passes, so the
agent can't confirm for itself. The relay can't check who set the flag: any
process that talks to the relay directly is trusted. Stage limits are not
confirmable: a target outside them is refused, full stop.

The relay strips ``confirmed`` before forwarding: drivers never see it.
"""

from __future__ import annotations


_STAGE_AXES = ("x_m", "y_m", "z_m", "alpha_deg", "beta_deg")
# Commands that put a device to use (exclusive groups apply).
_DEVICE_USE = {"camera_acquire", "stem_acquire", "live_start", "get_camera_info",
               "si_start", "eds_acquire_point", "eds_acquire_map"}


class PolicyError(ValueError):
    def __init__(self, message: str, confirmable: bool = False):
        super().__init__(message)
        self.confirmable = confirmable


_READ_PREFIXES = ("Get", "Is", "get", "is")


def vendor_method(cmd: str, params: dict) -> "str | None":
    """The vendor method an escape-hatch call runs, or None if *cmd* isn't one."""
    if cmd != "vendor_call" and not ("." in cmd and cmd.endswith("_call")):
        return None
    method = params.get("method")
    return str(method) if method else None


def is_read_method(method: str) -> bool:
    return method.rsplit(".", 1)[-1].startswith(_READ_PREFIXES)


class Policy:
    def __init__(self, policy: "dict | None" = None, exclusive: "list | None" = None,
                 routing=None):
        policy = policy or {}
        self.limits = {axis: (float(lo), float(hi))
                       for axis, (lo, hi) in (policy.get("stage_limits") or {}).items()}
        self.confirm = set(policy.get("confirm") or [])
        self.confirm_vendor_writes = bool(policy.get("confirm_vendor_writes", True))
        self.routing = routing
        self._raw_groups = [list(g) for g in (exclusive or [])]
        self._groups: "list | None" = None
        self.active: dict = {}                # group index -> device id in use

    def describe(self) -> dict:
        return {"stage_limits": {a: list(v) for a, v in self.limits.items()},
                "confirm": sorted(self.confirm),
                "confirm_vendor_writes": self.confirm_vendor_writes,
                "exclusive": self._raw_groups,
                "active": {str(i): d for i, d in self.active.items()}}

    def _resolved_groups(self) -> list:
        """Groups as sets of full device ids (roles resolved once)."""
        if self._groups is None:
            groups = []
            for group in self._raw_groups:
                ids = set()
                for ref in group:
                    try:
                        name, local = self.routing.resolve_device(ref)
                        ids.add(f"{name}/{local}")
                    except Exception:
                        ids.add(str(ref))
                groups.append(ids)
            self._groups = groups
        return self._groups

    def _device_used(self, driver: str, cmd: str, params: dict) -> "str | None":
        if cmd not in _DEVICE_USE or not params.get("device"):
            return None
        return f"{driver}/{params['device']}"

    async def check(self, driver: str, cmd: str, params: dict, read_position=None) -> dict:
        """Raise :class:`PolicyError`, or return the params to forward
        (``confirmed`` removed)."""
        params = dict(params)
        confirmed = bool(params.pop("confirmed", False))
        if cmd in self.confirm and not confirmed:
            raise PolicyError(
                f"{cmd!r} needs the operator's go-ahead on this microscope "
                f"(policy.confirm).", confirmable=True)
        method = vendor_method(cmd, params)
        if (method is not None and self.confirm_vendor_writes and not confirmed
                and not is_read_method(method)):
            raise PolicyError(
                f"{cmd} {method}: a raw vendor write needs the operator's go-ahead "
                f"(policy.confirm_vendor_writes). It bypasses the other safety rules.",
                confirmable=True)
        if cmd == "move_stage" and self.limits:
            await self._check_stage(params, read_position)
        device = self._device_used(driver, cmd, params)
        if device is not None:
            for index, group in enumerate(self._resolved_groups()):
                if device not in group:
                    continue
                current = self.active.get(index)
                if current is not None and current != device and not confirmed:
                    raise PolicyError(
                        f"{device} can't be used while {current} is in use: they are an "
                        f"exclusive group (e.g. retractable cameras; switching physically "
                        f"moves them and takes tens of seconds).", confirmable=True)
                self.active[index] = device
        return params

    async def _check_stage(self, params: dict, read_position) -> None:
        relative = params.get("mode", "absolute") == "relative"
        current = None
        if relative and any(params.get(a) is not None and a in self.limits
                            for a in _STAGE_AXES):
            if read_position is None:
                raise PolicyError("stage limits are set, and the current position can't "
                                  "be read to check a relative move")
            current = await read_position()
        for axis in _STAGE_AXES:
            if params.get(axis) is None or axis not in self.limits:
                continue
            value = float(params[axis])
            target = (float(current[axis]) + value) if relative else value
            lo, hi = self.limits[axis]
            if not lo <= target <= hi:
                raise PolicyError(
                    f"stage {axis} target {target:g} is outside this microscope's limits "
                    f"[{lo:g}, {hi:g}] (policy.stage_limits). Nothing moved.")
