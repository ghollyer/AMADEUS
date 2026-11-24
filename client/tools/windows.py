"""MCP tools for Digital Micrograph's document windows: list, select, close, save.

Thin wrappers over the DM-side ``list_windows`` / ``select_window`` /
``close_window`` / ``save_window`` commands (``dm_server/helpers/windows.py``), where the safety
rules live: unsaved images are only closed with ``discard_unsaved``, script
windows are never closed, and nothing changes while a live scan is running.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from client.state.manager import StateManager
from client.tools.files import fetch_into_store
from client.transport.zmq_client import ZMQClient


def _target(doc_id: int | None, title: str | None, index: int | None) -> dict:
    return {"doc_id": doc_id, "title": title, "index": index}


def register(mcp: FastMCP, zmq: ZMQClient, state: StateManager) -> None:

    @mcp.tool()
    async def list_dm_windows() -> dict:
        """List the open windows in Digital Micrograph, frontmost first.

        Covers image windows and script windows in every DM workspace.
        Read-only.

        Returns:
            ``active_workspace_id``, ``count`` and ``windows``. Each window has
            ``index`` (z-order, 0 = front; it changes whenever another window
            comes forward), ``title`` (e.g. "A: HAADF 0012"), ``type``
            ("image", "script" or "other"), ``workspace_id``/``workspace_name``,
            ``shown`` (False if its workspace isn't active) and ``frame`` (px).
            Image windows also have ``doc_id``, a stable handle to pass to
            ``select_dm_window``/``close_dm_window``, plus ``doc_name``,
            ``unsaved`` (DM would ask "Save changes?"), ``file_path``
            (None if never saved) and ``n_images``.
        """
        return await zmq.send_command("dm.list_windows", {})

    @mcp.tool()
    async def select_dm_window(
        doc_id: int | None = None,
        title: str | None = None,
        index: int | None = None,
    ) -> dict:
        """Bring a DM window to the front (switching workspace if needed).

        The front image is what DM's own tools act on, so this sets the target
        for things like DM's menu commands. Refused while a live scan is
        running, because the live stream reads DM's front image.

        Give exactly one of:
            doc_id: Image document ID from ``list_dm_windows`` (preferred,
                stable).
            title: Window title ("A: name") or image name; must be unique.
            index: Position from a ``list_dm_windows`` call made just before.

        Returns:
            ``selected`` (the window), ``front`` (re-checked: True if it is
            now the frontmost window), ``active_workspace_id``.
        """
        return await zmq.send_command("dm.select_window", _target(doc_id, title, index))

    @mcp.tool()
    async def close_dm_window(
        doc_id: int | None = None,
        title: str | None = None,
        index: int | None = None,
        discard_unsaved: bool = False,
    ) -> dict:
        """Close a DM window, without any DM dialog.

        Images with unsaved changes (``unsaved: true`` in ``list_dm_windows``,
        e.g. a fresh acquisition that was never saved) are refused unless
        ``discard_unsaved`` is True, because closing them loses the data. Ask
        the operator before discarding anything. Script windows are never
        closed (the AMADEUS server runs in one). Refused while a live scan is
        running.

        Give exactly one of ``doc_id`` (preferred), ``title`` or ``index``,
        as for ``select_dm_window``.

        Args:
            discard_unsaved: Close even if the image has unsaved changes.

        Returns:
            ``closed`` (the window), ``discarded_unsaved``, ``remaining``
            (windows left open).
        """
        params = _target(doc_id, title, index)
        params["discard_unsaved"] = discard_unsaved
        return await zmq.send_command("dm.close_window", params)

    @mcp.tool()
    async def save_dm_window(
        path: str,
        doc_id: int | None = None,
        title: str | None = None,
        index: int | None = None,
        overwrite: bool = False,
        fetch: bool = False,
        name: str | None = None,
    ) -> dict:
        """Save a DM image document (e.g. a finished EDS SI, or its DF/survey
        image) to a .dm4 file on the microscope PC, with no DM dialog.

        Typical SI flow: list_dm_windows to find the SI's doc_id (the SI
        cube has n_images >= 1 and a 3-D shape; the DF image acquired with it
        is a separate window), then save_dm_window(path=..., doc_id=...,
        fetch=True) and pass the returned image_id to eds_map_elements.

        The whole document is saved, and afterwards it is linked to the file
        (no longer ``unsaved``), so close_dm_window closes it without
        discard_unsaved. An existing file is refused unless overwrite=True.

        Give exactly one of ``doc_id`` (preferred), ``title`` or ``index``,
        as for ``select_dm_window``.

        Args:
            path: Absolute path on the microscope PC, e.g.
                C:\\AMADEUS_data\\2026-11-19\\NP_C1_SI.dm4 (.dm4 added if
                missing; missing folders are created).
            overwrite: Replace an existing file.
            fetch: Also pull the saved file into the local image store
                (as fetch_remote_file) and return its image_id.
            name: Display name for the fetched image (default: file name).

        Returns:
            ``saved`` (the window), ``path`` (the file DM wrote),
            ``size_bytes``, ``unsaved`` (False after a good save) and, with
            fetch, ``fetched`` (image_id, shape, axes, energy_axis_index).
        """
        params = _target(doc_id, title, index)
        params.update({"path": path, "overwrite": overwrite})
        out = await zmq.send_command("dm.save_window", params)
        if fetch:
            out["fetched"] = await fetch_into_store(zmq, state, out["path"], name)
        return out
