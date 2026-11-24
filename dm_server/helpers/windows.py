"""DM helpers for Digital Micrograph's document windows: list, select, close, save.

DM's Python ``Py_Window`` class can't bring a window to the front or report its
type, so everything here goes through DM-Script:

  CountDocumentWindows() / GetDocumentWindow(i)   all workspaces, z-order
  WindowGetType(w)        5 = image, 4 = script (incl. Python script windows)
  WindowSelect(w)         bring to front; switches workspace if needed
  WindowClose(w, verify)  verify=1 opens a modal "Save changes?" dialog

Facts verified on DM in the UTM dev VM (2026-11-09):

* Index 0 is the frontmost window. Indices shift whenever focus changes, so
  they are only good for the duration of one command. Image windows also carry
  a stable ``ImageDocumentGetID()``, the preferred handle.
* ``GetDocumentWindow`` spans every workspace. Windows in an inactive workspace
  report ``shown = 0``.
* ``WindowClose(w, 0)`` closes an image with unsaved changes **silently**,
  discarding it. We never pass ``verify = 1``: that dialog is modal and would
  block DM's main thread, the one the AMADEUS server runs on.
* ``ImageDocumentIsDirty`` matches DM's own "Save changes?" prompt: an image
  created by a script or an acquisition is dirty; a file opened from disk is
  clean. ``close_window`` refuses dirty images unless ``discard_unsaved``.
* ``WindowIsValid`` stays true after a close; only ``WindowIsOpen`` drops.

Script windows (type 4) are never closed. The AMADEUS server itself runs in
one, and closing a script window throws away its text without asking.

Both actions refuse while a live scan is running: live frames are read from
DM's front image, so changing the front window would redirect them.
"""

from __future__ import annotations

import os
import uuid
from pathlib import PureWindowsPath

from dm_server.helpers._dm_util import safe_exec_script, read_float, read_string
from dm_server.helpers import live as _live

TYPE_IMAGE = 5
TYPE_SCRIPT = 4
_TYPE_NAMES = {TYPE_IMAGE: "image", TYPE_SCRIPT: "script"}

_P = "__amadeus__:win:"

# Writes one tag subtree per window: win:<i>:<field>. Image-only fields are
# written only for image windows, and read back only when doc_id >= 0, so
# stale tags from an earlier, longer listing are never picked up.
_LIST_SCRIPT = f"""\
TagGroup tg = GetPersistentTagGroup()
string p = "{_P}"
number n = CountDocumentWindows()
TagGroupSetTagAsFloat(tg, p + "active_ws", WorkspaceGetActive())
number i
for (i = 0; i < n; i++) {{
    DocumentWindow w = GetDocumentWindow(i)
    string q = p + i + ":"
    number wtype = w.WindowGetType()
    number ws = w.WorkspaceFindByWindow()
    number t, l, b, r
    w.WindowGetFrameBounds(t, l, b, r)
    TagGroupSetTagAsString(tg, q + "title", w.WindowGetTitle())
    TagGroupSetTagAsFloat(tg, q + "type", wtype)
    TagGroupSetTagAsFloat(tg, q + "ws", ws)
    TagGroupSetTagAsString(tg, q + "ws_name", WorkspaceGetName(ws))
    TagGroupSetTagAsFloat(tg, q + "shown", w.WindowIsShown())
    TagGroupSetTagAsFloat(tg, q + "top", t)
    TagGroupSetTagAsFloat(tg, q + "left", l)
    TagGroupSetTagAsFloat(tg, q + "bottom", b)
    TagGroupSetTagAsFloat(tg, q + "right", r)
    number doc_id = -1
    if (wtype == {TYPE_IMAGE}) {{
        ImageDocument d = w.ImageWindowGetImageDocument()
        doc_id = d.ImageDocumentGetID()
        TagGroupSetTagAsString(tg, q + "doc_name", d.ImageDocumentGetName())
        TagGroupSetTagAsFloat(tg, q + "dirty", d.ImageDocumentIsDirty())
        TagGroupSetTagAsFloat(tg, q + "n_images", d.ImageDocumentCountImages())
        string path = ""
        if (d.ImageDocumentIsLinkedToFile()) {{
            path = d.ImageDocumentGetCurrentFile()
        }}
        TagGroupSetTagAsString(tg, q + "path", path)
    }}
    TagGroupSetTagAsFloat(tg, q + "doc_id", doc_id)
}}
TagGroupSetTagAsFloat(tg, p + "count", n)
"""


def _window_expr(win: dict) -> str:
    """DM-Script expression for *win*: by document ID if it's an image."""
    if win.get("doc_id") is not None:
        return f"GetImageDocumentByID({int(win['doc_id'])}).ImageDocumentGetWindow()"
    return f"GetDocumentWindow({int(win['index'])})"


def _enumerate() -> dict:
    """Run the listing script and read the result back from the tags."""
    safe_exec_script(_LIST_SCRIPT, expected_tags=["win:count"])
    n = int(read_float("win:count"))
    windows = []
    for i in range(n):
        k = f"win:{i}:"
        code = int(read_float(k + "type"))
        doc_id = int(read_float(k + "doc_id"))
        win = {
            "index":          i,
            "title":          read_string(k + "title"),
            "type":           _TYPE_NAMES.get(code, "other"),
            "type_code":      code,
            "workspace_id":   int(read_float(k + "ws")),
            "workspace_name": read_string(k + "ws_name"),
            "shown":          bool(read_float(k + "shown")),
            "frame": {side: int(read_float(k + side))
                      for side in ("top", "left", "bottom", "right")},
            "doc_id":         None,
        }
        if doc_id >= 0:
            path = read_string(k + "path")
            win.update({
                "doc_id":    doc_id,
                "doc_name":  read_string(k + "doc_name"),
                "unsaved":   bool(read_float(k + "dirty")),
                "file_path": path or None,
                "n_images":  int(read_float(k + "n_images")),
            })
        windows.append(win)
    return {
        "active_workspace_id": int(read_float("win:active_ws")),
        "count":               n,
        "windows":             windows,
    }


def _resolve(windows: list[dict], params: dict) -> dict:
    """Pick the window named by exactly one of doc_id / title / index."""
    given = [k for k in ("doc_id", "title", "index") if params.get(k) is not None]
    if len(given) != 1:
        raise ValueError("give exactly one of 'doc_id', 'title' or 'index' "
                         f"(got {given or 'none'})")
    key = given[0]

    if key == "doc_id":
        doc_id = int(params["doc_id"])
        for w in windows:
            if w["doc_id"] == doc_id:
                return w
        raise ValueError(f"no open image window has doc_id {doc_id}")

    if key == "index":
        index = int(params["index"])
        if not 0 <= index < len(windows):
            raise ValueError(f"index {index} out of range: {len(windows)} window(s) open")
        return windows[index]

    # Title: exact match on the window title ("A: name") or the image name,
    # then the same case-insensitively. Ambiguity is an error, not a guess.
    title = str(params["title"])
    for fold in (False, True):
        norm = (lambda s: s.casefold()) if fold else (lambda s: s)
        hits = [w for w in windows
                if norm(title) in (norm(w["title"]), norm(w.get("doc_name") or ""))]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            listing = ", ".join(f"[{w['index']}] {w['title']!r} doc_id={w['doc_id']}"
                                for w in hits)
            raise ValueError(f"title {title!r} matches {len(hits)} windows ({listing}); "
                             "use doc_id or index")
    raise ValueError(f"no window titled {title!r}")


def _refuse_during_live(action: str) -> None:
    if _live._SESSION is not None:
        raise RuntimeError(
            f"cannot {action} a window while a live scan is running: live frames "
            "come from DM's front image. Stop the live scan first.")


def _summary(win: dict) -> dict:
    keys = ("index", "title", "type", "doc_id", "doc_name", "workspace_id")
    return {k: win[k] for k in keys if k in win}


def list_windows(params: dict) -> dict:
    """List DM's document windows (image and script windows), front first.

    Returns
    -------
    {
        "active_workspace_id": int,
        "count": int,
        "windows": [{
            "index": int,            # z-order, 0 = frontmost; changes on select
            "title": str,            # e.g. "A: HAADF 0012"
            "type": "image" | "script" | "other",
            "type_code": int,
            "workspace_id": int, "workspace_name": str,
            "shown": bool,           # False when in an inactive workspace
            "frame": {"top", "left", "bottom", "right"},   # px
            "doc_id": int | None,    # stable handle (image windows only)
            # image windows only:
            "doc_name": str, "unsaved": bool, "file_path": str | None,
            "n_images": int,
        }, ...]
    }
    """
    return _enumerate()


def select_window(params: dict) -> dict:
    """Bring a window to the front (switching workspace if needed).

    Parameters
    ----------
    Exactly one of ``doc_id`` (int), ``title`` (str: the window title or the
    image name), or ``index`` (int, from a listing made just before).

    Returns
    -------
    {"selected": {...}, "front": bool, "active_workspace_id": int}
    ``front`` is re-checked after the select, not assumed.
    """
    _refuse_during_live("select")
    target = _resolve(_enumerate()["windows"], params)
    safe_exec_script(f"WindowSelect({_window_expr(target)})\n")
    after = _enumerate()
    front = after["windows"][0] if after["windows"] else None
    if target["doc_id"] is not None:
        is_front = front is not None and front["doc_id"] == target["doc_id"]
    else:
        is_front = front is not None and front["title"] == target["title"]
    return {
        "selected":            _summary(front if is_front else target),
        "front":               is_front,
        "active_workspace_id": after["active_workspace_id"],
    }


def close_window(params: dict) -> dict:
    """Close a window without any DM dialog.

    Parameters
    ----------
    Exactly one of ``doc_id`` / ``title`` / ``index`` (as ``select_window``).
    params["discard_unsaved"] : bool, default False
        Required to close an image with unsaved changes (an acquisition or a
        script-made image that was never saved, or an edited file). Without
        it such a close is refused, because the data would be lost.

    Script windows are never closed.

    Returns
    -------
    {"closed": {...}, "discarded_unsaved": bool, "remaining": int}
    """
    _refuse_during_live("close")
    before = _enumerate()
    target = _resolve(before["windows"], params)
    if target["type_code"] == TYPE_SCRIPT:
        raise ValueError(
            f"refusing to close script window {target['title']!r}: the AMADEUS "
            "server runs in a script window, and closing one discards its text")
    unsaved = bool(target.get("unsaved"))
    if unsaved and not params.get("discard_unsaved"):
        where = target.get("file_path") or "never saved"
        raise ValueError(
            f"{target['title']!r} (doc_id {target['doc_id']}) has unsaved changes "
            f"({where}); closing it loses them. Save it first, or pass "
            "discard_unsaved=true to close it anyway.")

    # verify=0: never put up DM's modal save dialog (it would hang the server).
    safe_exec_script(f"WindowClose({_window_expr(target)}, 0)\n")

    after = _enumerate()
    if target["doc_id"] is not None:
        still_open = any(w["doc_id"] == target["doc_id"] for w in after["windows"])
    else:
        still_open = after["count"] >= before["count"]
    if still_open:
        raise RuntimeError(f"DM did not close {target['title']!r}")
    return {
        "closed":            _summary(target),
        "discarded_unsaved": unsaved,
        "remaining":         after["count"],
    }


def _save_target_path(path: str) -> tuple[str, str]:
    """(path without extension for DM, the .dm4 file DM will write)."""
    p = str(path or "").strip()
    if not p:
        raise ValueError("give 'path': the .dm4 file to write on the microscope PC")
    if '"' in p or "\n" in p:
        raise ValueError(f"path {p!r} contains a quote or newline")
    if not (PureWindowsPath(p).is_absolute() or os.path.isabs(p)):
        raise ValueError(f"path {p!r} must be absolute (e.g. C:\\AMADEUS_data\\...\\name.dm4)")
    stem, ext = os.path.splitext(p)
    if ext.lower() not in ("", ".dm4"):
        raise ValueError(f"only .dm4 is supported (DM's 'Gatan Format'), got {ext!r}")
    return stem, stem + ".dm4"


def save_window(params: dict) -> dict:
    """Save an image document (e.g. a finished SI) to a .dm4 file.

    Parameters
    ----------
    Exactly one of ``doc_id`` / ``title`` / ``index`` (as ``select_window``).
    params["path"] : str
        Absolute .dm4 path on the microscope PC (the extension is optional).
        Missing folders are created.
    params["overwrite"] : bool, default False
        Replace an existing file. Without it an existing file is refused.

    Uses Gatan's documented no-prompt save (SetCurrentFileSaveFormat +
    SetCurrentFile + ImageDocumentSave(0)), so no dialog can block DM's main
    thread, and the document is then linked to the file and no longer
    unsaved. The whole document is saved (all of an SI's images). The file
    is checked on disk afterwards rather than assumed.

    Returns
    -------
    {"saved": {...}, "path": str, "size_bytes": int, "unsaved": bool}
    """
    stem, target_file = _save_target_path(params.get("path"))
    target = _resolve(_enumerate()["windows"], params)
    if target.get("doc_id") is None:
        raise ValueError(f"{target['title']!r} is not an image window; only image "
                         "documents can be saved")
    if os.path.exists(target_file) and not params.get("overwrite"):
        raise ValueError(f"{target_file} already exists; pass overwrite=true to replace it")
    parent = os.path.dirname(target_file)
    if parent:
        os.makedirs(parent, exist_ok=True)

    # A fresh tag key per call, so a stale tag from an earlier save can't make
    # a failed script look successful.
    key = f"win:saved:{uuid.uuid4().hex[:12]}"
    dm_stem = stem.replace("\\", "/")
    script = (
        f"ImageDocument d = GetImageDocumentByID({int(target['doc_id'])})\n"
        f"d.ImageDocumentSetCurrentFileSaveFormat(\"Gatan Format\")\n"
        f"d.ImageDocumentSetCurrentFile(\"{dm_stem}\")\n"
        f"d.ImageDocumentSave(0)\n"
        f"TagGroupSetTagAsString(GetPersistentTagGroup(), \"__amadeus__:{key}\", "
        f"d.ImageDocumentGetCurrentFile())\n"
    )
    safe_exec_script(script, expected_tags=[key])
    reported = read_string(key)
    written = next((f for f in (reported, target_file) if f and os.path.isfile(f)), None)
    if written is None:
        raise RuntimeError(f"DM reported saving {target['title']!r} to {reported!r}, "
                           f"but no file is at {target_file}")
    after = next((w for w in _enumerate()["windows"] if w["doc_id"] == target["doc_id"]),
                 target)
    return {
        "saved":      _summary(target),
        "path":       written,
        "size_bytes": os.path.getsize(written),
        "unsaved":    bool(after.get("unsaved")),
    }
