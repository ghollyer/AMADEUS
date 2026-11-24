"""Shared DM API utilities used by all helper modules.

All helper modules that call DM API functions should import from here
rather than duplicating the tag I/O pattern.
"""

import DigitalMicrograph as DM

_PREFIX = "__amadeus__"

# When a DM-Script error's message contains non-ASCII text
# (``µm``, accented names), DM's Python bridge raises a RuntimeError with NO
# args, so the text is lost. Inside DM-Script, try/catch + GetExceptionString()
# still sees it (as ANSI bytes). So every script runs inside this wrapper,
# which stores the message in a tag for Python to raise, decoded.
#
# Function and class definitions aren't allowed inside a block, so such
# scripts fail to *compile* when wrapped. Runtime errors are caught in DM, so
# only a compile error can escape the wrapper, and then nothing has run: it is
# safe to run the original script unwrapped, which also reports a genuine
# syntax error with its true line numbers. (All verified in DM, 2026-11-09.)
_ERR_KEY = "script_error"
WRAP_HEAD = "try {\n"
WRAP_TAIL = (
    "\n} catch {\n"
    f'    TagGroupSetTagAsString(GetPersistentTagGroup(), "{_PREFIX}:{_ERR_KEY}", '
    "GetExceptionString());\n"
    "    break;\n"
    "}\n"
)
_DROPPED = ("DM-Script raised an error but DM's Python bridge dropped its message "
            "(it contained non-ASCII text)")


def _run_unwrapped(script: str) -> None:
    try:
        DM.ExecuteScriptString(script)
    except RuntimeError as exc:
        if not str(exc):
            raise RuntimeError(_DROPPED) from exc
        raise


def exec_script(script: str) -> None:
    """Execute a DM-Script string via the Python bridge.

    DM-Script errors are raised as ``RuntimeError`` with their message
    intact, including non-ASCII text (see ``WRAP_HEAD``).
    """
    tg = DM.GetPersistentTagGroup()
    err_path = _tag_path(_ERR_KEY)
    tg.SetTagAsString(err_path, "")
    try:
        DM.ExecuteScriptString(WRAP_HEAD + script + WRAP_TAIL)
    except Exception:
        # Compile error only (see above): nothing ran, so run it as written.
        _run_unwrapped(script)
        return
    ok, msg = tg.GetTagAsString(err_path)
    if ok and msg:
        raise RuntimeError(dm_text(msg))


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
        exec_script(script)
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
    return dm_text(val)


def dm_text(val) -> str:
    """Coerce a DM string return value to ``str``.

    DM hands back non-ASCII strings (e.g. calibration units ``µm``) as
    ``bytes`` in the Windows ANSI code page; ``str()`` on those yields the
    repr ``"b'\\xb5m'"``.  Decode UTF-8 first, then fall back to cp1252.
    """
    if isinstance(val, (bytes, bytearray)):
        try:
            return bytes(val).decode("utf-8")
        except UnicodeDecodeError:
            return bytes(val).decode("cp1252", errors="replace")
    return str(val)


def write_float(key: str, val: float) -> None:
    """Write a float to the persistent tag group from Python."""
    DM.GetPersistentTagGroup().SetTagAsFloat(_tag_path(key), float(val))


def tag_ref(key: str) -> str:
    """Return the full tag path for use inside DM-Script strings."""
    return _tag_path(key)
