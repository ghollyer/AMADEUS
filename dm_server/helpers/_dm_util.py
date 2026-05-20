"""Shared DM API utilities used by all helper modules.

All helper modules that call DM API functions should import from here
rather than duplicating the tag I/O pattern.
"""

import DigitalMicrograph as DM

_PREFIX = "__amadeus__"


def exec_script(script: str) -> None:
    """Execute a DM-Script string via the Python bridge."""
    DM.ExecuteScriptString(script)


def safe_exec_script(script: str, expected_tags: list[str] | None = None) -> None:
    """Execute DM-Script with error detection and optional tag validation.

    1. Wraps DM.ExecuteScriptString in try/except — any Python-visible exception
       is re-raised as RuntimeError with the first 300 chars of the script for
       debugging context.
    2. If *expected_tags* is provided, each tag is checked in the persistent tag
       group after execution.  Missing tags indicate a silent DM-Script failure
       (e.g. a wrong function name or bad argument).  A RuntimeError listing the
       missing tags is raised, again with a script excerpt.

    Parameters
    ----------
    script : str
        DM-Script source to execute.
    expected_tags : list[str] | None
        Tag keys (without the ``__amadeus__:`` prefix) that the script should
        have written.  Pass ``None`` to skip tag validation.
    """
    excerpt = script[:300].strip()
    try:
        DM.ExecuteScriptString(script)
    except Exception as exc:
        raise RuntimeError(
            f"DM-Script execution failed: {exc}\nScript (first 300 chars):\n{excerpt}"
        ) from exc

    if expected_tags:
        tg = DM.GetPersistentTagGroup()
        missing = []
        for key in expected_tags:
            path = _tag_path(key)
            ok_f, _ = tg.GetTagAsFloat(path)
            ok_s, _ = tg.GetTagAsString(path)
            if not (ok_f or ok_s):
                missing.append(path)
        if missing:
            raise RuntimeError(
                f"DM-Script ran but did not write expected tags: {missing}\n"
                f"Script (first 300 chars):\n{excerpt}"
            )


def _tag_path(key: str) -> str:
    return f"{_PREFIX}:{key}"


def read_float(key: str) -> float:
    """Read a float from the persistent tag group (written by a DM-Script)."""
    ok, val = DM.GetPersistentTagGroup().GetTagAsFloat(_tag_path(key))
    if not ok:
        raise RuntimeError(f"DM tag not set: {_tag_path(key)!r} — check the DM-Script ran correctly")
    return float(val)


def read_string(key: str) -> str:
    """Read a string from the persistent tag group."""
    ok, val = DM.GetPersistentTagGroup().GetTagAsString(_tag_path(key))
    if not ok:
        raise RuntimeError(f"DM tag not set: {_tag_path(key)!r} — check the DM-Script ran correctly")
    return str(val)


def write_float(key: str, val: float) -> None:
    """Write a float to the persistent tag group from Python."""
    DM.GetPersistentTagGroup().SetTagAsFloat(_tag_path(key), float(val))


def tag_ref(key: str) -> str:
    """Return the full tag path for use inside DM-Script strings."""
    return _tag_path(key)
