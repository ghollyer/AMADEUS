"""MCP tools to browse and pull DM files from the microscope computer.

These tools wrap the DM-side ``list_dm_files`` and ``load_dm_file``
commands. Fetched arrays land in the client ImageStore alongside images
acquired live, so all the downstream analysis tools (FFT, segmentation,
EDS SI extraction, etc.) can address them by ``image_id``.

The full per-axis calibration from the DM file (origin, scale, units for
every dimension) is preserved in the sidecar's ``tags`` dict — the
top-level ImageCalibration model only carries the primary x/y spatial
scaling.
"""

from __future__ import annotations

from pathlib import PureWindowsPath

from mcp.server.fastmcp import FastMCP

from client.state.manager import StateManager
from client.state.models import ImageCalibration
from client.transport.zmq_client import ZMQClient


def register(mcp: FastMCP, zmq: ZMQClient, state: StateManager) -> None:

    @mcp.tool()
    async def list_remote_dm_files(
        directory: str,
        patterns: list[str] | None = None,
        recursive: bool = False,
    ) -> dict:
        """List DM image files in a directory on the microscope computer.

        Args:
            directory: Absolute path on the microscope computer.
            patterns: Glob patterns to match, case-insensitive. Defaults
                to ``["*.dm3", "*.dm4"]``.
            recursive: If True, walk subdirectories.

        Returns:
            ``directory``, ``count``, and a ``files`` list sorted by
            mtime (newest first). Each entry has ``name``, ``path``,
            ``size_bytes``, ``mtime_iso``.
        """
        return await zmq.send_command("list_dm_files", {
            "directory": directory,
            "patterns": patterns,
            "recursive": recursive,
        })

    @mcp.tool()
    async def fetch_dm_file(path: str, name: str | None = None) -> dict:
        """Pull a .dm3/.dm4 file into the local image store.

        The file is opened on the DM side, its full numpy array and
        per-axis calibration are transferred over the ZMQ bridge, and
        the result is saved as a standard image (.npy + .json sidecar)
        that any analysis tool can address by ``image_id``.

        Args:
            path: Absolute path of the file on the microscope computer.
            name: Optional display name. Defaults to the file's basename.

        Returns:
            ``image_id``, ``name``, ``shape``, ``dtype``, ``ndim``,
            ``axes`` (per-dimension origin/scale/units), and
            ``energy_axis_index`` (the index of the energy-calibrated
            dimension, or None for plain images).
        """
        meta, arr = await zmq.send_command_image("load_dm_file", {"path": path})
        image_meta = meta.get("image_meta", {})
        data = meta.get("data", {})

        display_name = name or data.get("name") or PureWindowsPath(path).name

        cal_info = image_meta.get("calibration") or {}
        calibration = ImageCalibration(
            x_scale=float(cal_info.get("x_scale", 1.0)),
            y_scale=float(cal_info.get("y_scale", 1.0)),
            units=str(cal_info.get("units", "nm")),
        )

        tags = {
            "type":              "dm_file",
            "source_path":       data.get("source_path", path),
            "ndim":              data.get("ndim", arr.ndim),
            "axes":              data.get("axes", []),
            "energy_axis_index": data.get("energy_axis_index"),
            "intensity":         data.get("intensity", {}),
        }
        sidecar = state.images.save(
            arr,
            name=display_name,
            calibration=calibration,
            tags=tags,
        )
        return {
            "image_id":           sidecar.image_id,
            "name":               sidecar.name,
            "shape":              sidecar.shape,
            "dtype":              sidecar.dtype,
            "ndim":               tags["ndim"],
            "axes":               tags["axes"],
            "energy_axis_index":  tags["energy_axis_index"],
        }

    @mcp.tool()
    async def list_local_images() -> dict:
        """List all images currently stored locally by AMADEUS.

        Returns:
            ``images``: list of ``image_id``, ``name``, ``shape``,
            ``dtype``, and any ``type`` tag (e.g. ``dm_file``,
            ``power_spectrum``, ``label_map``).
        """
        ids = state.images.list_ids()
        out = []
        for iid in ids:
            sc = state.images.load_sidecar(iid)
            out.append({
                "image_id": sc.image_id,
                "name":     sc.name,
                "shape":    sc.shape,
                "dtype":    sc.dtype,
                "type":     sc.tags.get("type"),
            })
        return {"count": len(out), "images": out}

    @mcp.tool()
    async def inspect_image(image_id: str) -> dict:
        """Return full sidecar metadata for a stored image.

        Use this to discover an image's dimensions, calibration, source
        file path, energy-axis index (for SI cubes), and any other tags
        captured at fetch / acquisition time.

        Args:
            image_id: The image identifier.
        """
        sc = state.images.load_sidecar(image_id)
        return sc.model_dump(mode="json")
