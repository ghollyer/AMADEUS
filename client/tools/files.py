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

import hashlib
import os
import time
from pathlib import Path, PureWindowsPath

from mcp.server.fastmcp import FastMCP

from client.state.manager import StateManager
from client.state.models import ImageCalibration
from client.transport.zmq_client import ZMQClient

DEFAULT_CHUNK_BYTES = 32 * 1024 * 1024


def _unique_path(dest_dir: Path, name: str) -> Path:
    """``dest_dir/name``, or ``name (1).ext``… if that already exists."""
    target = dest_dir / name
    stem, suffix = target.stem, target.suffix
    n = 1
    while target.exists():
        target = dest_dir / f"{stem} ({n}){suffix}"
        n += 1
    return target


async def download_file(
    zmq: ZMQClient,
    remote_path: str,
    dest_dir: Path,
    *,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
) -> dict:
    """Copy a file from the microscope computer byte-for-byte.

    Streams ``read_file_chunk`` replies into ``<name>.part``, verifying each
    chunk's SHA-256 and that the remote file's size/mtime don't change
    mid-transfer, then renames it into place (never overwriting) and sets
    its mtime to the remote one. A failed transfer leaves no partial file.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = PureWindowsPath(remote_path).name
    part = dest_dir / f".{name}.{os.getpid()}.part"
    whole = hashlib.sha256()
    offset = 0
    size = mtime = None
    t0 = time.monotonic()
    try:
        with open(part, "wb") as fh:
            while True:
                meta, arr = await zmq.send_command_image(
                    "read_file_chunk",
                    {"path": remote_path, "offset": offset, "length": chunk_bytes},
                )
                d = meta.get("data", {})
                if size is None:
                    size, mtime = d["size_bytes"], d["mtime"]
                elif (d["size_bytes"], d["mtime"]) != (size, mtime):
                    raise RuntimeError(
                        f"{remote_path} changed on the microscope computer during "
                        "the transfer (still being written?); try again")
                chunk = arr.tobytes()
                if d["offset"] != offset or len(chunk) != d["length"]:
                    raise RuntimeError(
                        f"chunk at offset {offset}: got {len(chunk)} bytes at "
                        f"offset {d['offset']}, expected {d['length']}")
                if hashlib.sha256(chunk).hexdigest() != d["sha256"]:
                    raise RuntimeError(f"chunk at offset {offset} failed its SHA-256 check")
                fh.write(chunk)
                whole.update(chunk)
                offset += len(chunk)
                if d["eof"] or not chunk:
                    break
        if offset != size:
            raise RuntimeError(f"received {offset} bytes, file is {size} bytes")
        target = _unique_path(dest_dir, name)
        os.replace(part, target)
    finally:
        if part.exists():
            part.unlink()
    os.utime(target, (mtime, mtime))
    elapsed = time.monotonic() - t0
    return {
        "local_path":  str(target),
        "remote_path": remote_path,
        "size_bytes":  size,
        "sha256":      whole.hexdigest(),
        "seconds":     round(elapsed, 3),
        "mb_per_s":    round(size / 1e6 / elapsed, 1) if elapsed > 0 else None,
    }


async def fetch_into_store(zmq: ZMQClient, state: StateManager, path: str,
                           name: str | None = None) -> dict:
    """Load a DM file on the microscope PC and save it in the local image
    store (shared by ``fetch_remote_file`` and ``save_dm_window(fetch=True)``)."""
    meta, arr = await zmq.send_command_image("load_file", {"path": path})
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


def register(mcp: FastMCP, zmq: ZMQClient, state: StateManager) -> None:

    @mcp.tool()
    async def download_remote_file(path: str) -> dict:
        """Copy a file from the microscope computer to this computer, unchanged.

        Unlike ``fetch_remote_file`` (which extracts the array + calibration for
        analysis), this transfers the original bytes — a .dm4 keeps its full
        DM tag tree (microscope info, acquisition parameters, annotations)
        and opens in DM / HyperSpy / rosettasciio. Any file type works.
        Every chunk is SHA-256 verified; an existing local file is never
        overwritten (a " (1)" suffix is added instead).

        Args:
            path: Absolute path of the file on the microscope computer
                (see ``list_remote_files``).

        Returns:
            ``local_path``, ``size_bytes``, ``sha256``, ``seconds``,
            ``mb_per_s``.
        """
        return await download_file(zmq, path, state.data_dir / "files")

    @mcp.tool()
    async def list_remote_files(
        directory: str,
        patterns: list[str] | None = None,
        recursive: bool = False,
    ) -> dict:
        """List data files in a directory on the instrument side (the driver
        holding the files slot: DM on the F200).

        Args:
            directory: Absolute path on the microscope computer.
            patterns: Glob patterns to match, case-insensitive. The default
                depends on the driver (DM: ``["*.dm3", "*.dm4"]``).
            recursive: If True, walk subdirectories.

        Returns:
            ``directory``, ``count``, and a ``files`` list sorted by
            mtime (newest first). Each entry has ``name``, ``path``,
            ``size_bytes``, ``mtime_iso``.
        """
        return await zmq.send_command("list_files", {
            "directory": directory,
            "patterns": patterns,
            "recursive": recursive,
        })

    @mcp.tool()
    async def fetch_remote_file(path: str, name: str | None = None) -> dict:
        """Pull an instrument-side data file (DM: .dm3/.dm4) into the local
        image store.

        The files driver opens it, its full array and per-axis calibration
        are transferred over the ZMQ bridge, and the result is saved as a
        standard image (.npy + .json sidecar) that any analysis tool can
        address by ``image_id``.

        Args:
            path: Absolute path of the file on the microscope computer.
            name: Optional display name. Defaults to the file's basename.

        Returns:
            ``image_id``, ``name``, ``shape``, ``dtype``, ``ndim``,
            ``axes`` (per-dimension origin/scale/units), and
            ``energy_axis_index`` (the index of the energy-calibrated
            dimension, or None for plain images).
        """
        return await fetch_into_store(zmq, state, path, name)

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
