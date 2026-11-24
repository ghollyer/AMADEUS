"""Small helpers every driver uses to validate requests and shape replies.

Vendor APIs often accept bad arguments silently (PyJEM's setters ignore
non-ints), so a driver validates before it calls the vendor, and raises.
Replies follow the conventions in ``drivers/base.py``: a device value carries
its ``unit`` and ``range``; enumerations are names.

Stdlib only, Python 3.10.
"""

from __future__ import annotations


def exactly_one(params: dict, keys: tuple, cmd: str) -> str:
    """The single key of *keys* present in *params*, else ``ValueError``.

    Silently preferring one argument and dropping another would turn an
    ambiguous request into a surprise on the column."""
    given = [k for k in keys if params.get(k) is not None]
    if len(given) != 1:
        raise ValueError(f"{cmd} needs exactly one of: {', '.join(keys)}"
                         + (f" (got {', '.join(given)})" if given else ""))
    return given[0]


def as_int_in_range(value, label: str, lo: int, hi: int) -> int:
    """An integer device value within ``[lo, hi]`` (floats are rounded)."""
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a number, got {value!r}")
    try:
        ivalue = int(round(float(value)))
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number, got {value!r}")
    if not lo <= ivalue <= hi:
        raise ValueError(f"{label}={ivalue} outside the range {lo}-{hi}")
    return ivalue


def as_index(value, label: str) -> int:
    """A non-negative integer selector index (bools and fractions rejected)."""
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer, got {value!r}")
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be an integer, got {value!r}")
    if f != int(f):
        raise ValueError(f"{label} must be an integer, got {value!r}")
    if f < 0:
        raise ValueError(f"{label} must be >= 0, got {int(f)}")
    return int(f)


def device_value(value, unit: str, value_range, **extra) -> dict:
    """``{"value", "unit", "range", ...}``: the neutral form of a device value."""
    return {"value": value, "unit": unit, "range": list(value_range), **extra}


def name_from(value, names, label: str) -> str:
    """*value* matched (case-insensitively) against *names*, else ``ValueError``."""
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a name, one of {', '.join(names)}; got {value!r}")
    lookup = {n.lower(): n for n in names}
    key = value.strip().lower()
    if key not in lookup:
        raise ValueError(f"unknown {label} {value!r}; expected one of {', '.join(names)}")
    return lookup[key]


def stigmation_reply(values: dict, unit: str, value_range, neutral=None) -> dict:
    """get_stigmation reply: ``{"stigmators": {name: {"x", "y"}}, "unit", "range"}``."""
    out = {"stigmators": {name: {"x": int(x), "y": int(y)} for name, (x, y) in values.items()},
           "unit": unit, "range": list(value_range)}
    if neutral is not None:
        out["neutral"] = neutral
    return out


def jsonable(value):
    """*value* made JSON-safe (unknown objects become their repr)."""
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    return repr(value)
