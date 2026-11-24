"""Async ZMQ SUB subscriber for the DM server's live-scan frame stream.

The DM server publishes live-scan frames on a PUB socket, and the relay
republishes them on its public stream port (default 9877),
out-of-band from the synchronous REQ/REP command channel. This subscriber runs
a background asyncio task that receives those frames and:

* keeps the **latest full-resolution frame in memory** — read instantly by the
  ``get_live_frame`` MCP tool with no DM round-trip, and
* writes a **rate-capped preview** (``<monitor-dir>/live.npy`` + ``live.json``)
  so the file-polling monitor GUI can display the live view without opening its
  own socket.

Frame wire format (3-part, mirrors the REQ/REP image protocol):
    frame 1: topic  b"frame"
    frame 2: JSON   {"status", "image_meta": {shape, dtype, calibration, ...}, "data": {...}}
    frame 3: raw C-contiguous numpy bytes
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import numpy as np
import zmq
import zmq.asyncio


def stream_address_from(command_address: str, stream_port: int = 9877) -> str:
    """Derive the stream address from the command address by swapping the port.

    ``tcp://host:9876`` -> ``tcp://host:9877``. Falls back to appending the port
    if the address has no parseable ``:port`` suffix.
    """
    head, sep, _port = command_address.rpartition(":")
    if sep and head:
        return f"{head}:{stream_port}"
    return f"{command_address}:{stream_port}"


class StreamSubscriber:
    """Receives live-scan frames and exposes the latest one."""

    def __init__(
        self,
        address: str,
        *,
        monitor_dir: Path | str | None = None,
        preview_fps: float = 8.0,
        topic: bytes = b"frame",
    ):
        self.address = address
        self.topic = topic
        self._monitor_dir = Path(monitor_dir) if monitor_dir else None
        if self._monitor_dir is not None:
            self._monitor_dir.mkdir(parents=True, exist_ok=True)
        self._preview_min_interval = 1.0 / preview_fps if preview_fps > 0 else 0.0

        self._context: zmq.asyncio.Context | None = None
        self._socket: zmq.asyncio.Socket | None = None
        self._task: asyncio.Task | None = None

        self._latest: dict[str, Any] | None = None
        self._frames_received = 0
        self._last_received_t: float | None = None
        self._last_preview_t = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._task is not None:
            return
        self._context = zmq.asyncio.Context()
        self._socket = self._context.socket(zmq.SUB)
        self._socket.setsockopt(zmq.SUBSCRIBE, self.topic)
        self._socket.setsockopt(zmq.RCVHWM, 4)  # drop stale frames under load
        self._socket.connect(self.address)
        self._task = asyncio.create_task(self._run(), name="amadeus-stream-sub")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        if self._context is not None:
            self._context.term()
            self._context = None

    # ------------------------------------------------------------------
    # Receive loop
    # ------------------------------------------------------------------
    async def _run(self) -> None:
        assert self._socket is not None
        while True:
            try:
                parts = await self._socket.recv_multipart()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Socket closing or a malformed message — keep the loop alive.
                await asyncio.sleep(0.05)
                continue
            self._ingest(parts)

    def _ingest(self, parts: list[bytes]) -> None:
        if len(parts) != 3:
            return  # not a well-formed frame message
        _topic, meta_raw, img_raw = parts
        try:
            meta = json.loads(meta_raw)
            image_meta = meta["image_meta"]
            shape = image_meta["shape"]
            dtype = np.dtype(image_meta["dtype"])
            arr = np.frombuffer(img_raw, dtype=dtype).reshape(shape).copy()
        except (json.JSONDecodeError, KeyError, ValueError):
            return

        self._frames_received += 1
        self._last_received_t = time.monotonic()
        self._latest = {
            "meta": meta,
            "array": arr,
            "received_at": self._last_received_t,
            "frame_index": meta.get("data", {}).get("frame_index"),
        }
        self._maybe_write_preview(arr, meta)

    def _maybe_write_preview(self, arr: np.ndarray, meta: dict) -> None:
        if self._monitor_dir is None:
            return
        now = time.monotonic()
        if now - self._last_preview_t < self._preview_min_interval:
            return
        self._last_preview_t = now
        try:
            # np.save appends ".npy" unless the name already ends in it, so the
            # temp file must end in .npy too (else os.replace can't find it).
            npy_tmp = self._monitor_dir / "live.tmp.npy"
            np.save(npy_tmp, arr)
            os.replace(npy_tmp, self._monitor_dir / "live.npy")
            json_tmp = self._monitor_dir / "live.json.tmp"
            json_tmp.write_text(
                json.dumps({
                    "frame_index": meta.get("data", {}).get("frame_index"),
                    "detector": meta.get("data", {}).get("detector"),
                    "image_meta": meta.get("image_meta", {}),
                }, default=str),
                encoding="utf-8",
            )
            os.replace(json_tmp, self._monitor_dir / "live.json")
        except OSError:
            # Preview is best-effort; never disturb the receive loop.
            pass

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------
    def latest(self) -> dict[str, Any] | None:
        """The most recent frame: ``{meta, array, received_at, frame_index}``."""
        return self._latest

    def stats(self) -> dict[str, Any]:
        age = (time.monotonic() - self._last_received_t
               if self._last_received_t is not None else None)
        return {
            "frames_received": self._frames_received,
            "last_frame_age_s": round(age, 3) if age is not None else None,
            "address": self.address,
            "running": self._task is not None,
        }
