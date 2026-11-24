"""Relay configuration, built from ``amadeus_config.json`` (``common/config.py``)
plus CLI overrides.

* ``drivers``: ``{name: spec}`` from ``common.config.effective_drivers``: each
  driver's kind, the slots it holds, and where it runs (``in_process``,
  ``process``, or an address).
* ``roles``, ``policy``, ``exclusive``, ``instrument``: device roles, the
  policy layer's rules, and the calibration passed to Python drivers.
* ``stream_listen``: where the relay republishes the drivers' live frames for
  the laptop (``None`` disables the proxy).

Which driver holds which slot (including the Gatan EM* fallback, where the DM
driver holds column + stage) is an operator decision made in the config. It
is deliberately *not* exposed as an MCP tool.
"""

from dataclasses import dataclass, field

from common.protocol import DEFAULT_PORTS


@dataclass
class RelayConfig:
    drivers: dict
    listen: str = f"tcp://*:{DEFAULT_PORTS['relay']}"
    timeout_ms: int = 30_000
    stream_listen: "str | None" = None
    roles: dict = field(default_factory=dict)
    policy: dict = field(default_factory=dict)
    exclusive: list = field(default_factory=list)
    instrument: dict = field(default_factory=dict)
    log_dir: "str | None" = None

    def __post_init__(self):
        if not isinstance(self.drivers, dict):
            raise ValueError("RelayConfig.drivers must be {name: spec}")
        for name, spec in self.drivers.items():
            if not spec.get("kind"):
                raise ValueError(f"driver {name!r} has no kind")
            spec.setdefault("host", "in_process")
            spec.setdefault("slots", [])

    def effective_drivers(self) -> dict:
        return self.drivers


def load_config(amadeus_cfg: "dict | None" = None, **overrides) -> RelayConfig:
    """Build a config: defaults < ``amadeus_cfg`` < keyword overrides (``None``
    overrides are ignored). Overrides: listen, timeout_ms, stream_listen,
    hw_mode (online/offline/sim), dm (DM address), calibration (a calibration
    file for the column driver).

    Without a config file the relay uses the built-in defaults: JEOL *offline*
    holding column + stage, DM on localhost, no stream proxy. ``--online``
    is needed to drive a real column."""
    from common import config as amadeus_config
    cfg = amadeus_cfg if amadeus_cfg is not None else amadeus_config.load_defaults()
    instrument = cfg.get("instrument") or {}
    drivers = amadeus_config.effective_drivers(cfg, hw_mode=overrides.get("hw_mode"),
                                               dm_address=overrides.get("dm"))
    if overrides.get("calibration"):
        name, _ = amadeus_config.hardware_driver({"drivers": drivers})
        if name is not None:
            drivers[name]["calibration"] = overrides["calibration"]
    data = {
        "drivers": drivers,
        "timeout_ms": cfg["relay"]["timeout_ms"],
        "roles": instrument.get("roles") or {},
        "policy": cfg.get("policy") or {},
        "exclusive": cfg.get("exclusive") or [],
        "instrument": instrument,
    }
    if amadeus_cfg is not None:
        data["listen"] = amadeus_config.relay_listen(cfg)
        data["stream_listen"] = amadeus_config.relay_stream_listen(cfg)
    for key in ("listen", "timeout_ms", "stream_listen"):
        if overrides.get(key) is not None:
            data[key] = overrides[key]
    return RelayConfig(**data)
