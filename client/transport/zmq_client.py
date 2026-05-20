"""Async ZMQ REQ client for communicating with the DM server.

Usage
-----
from client.transport.zmq_client import ZMQClient

async with ZMQClient("tcp://microscope-pc:9876") as client:
    data = await client.send_command("get_focus")
    meta, arr = await client.send_command_image("stem_acquire", {"mode": "capture"})
"""

import asyncio
import json
import uuid
from typing import Any

import numpy as np
import zmq
import zmq.asyncio


class ZMQClient:
    """Async ZMQ REQ socket that talks to the DM server."""

    def __init__(self, address: str = "tcp://localhost:9876", timeout_ms: int = 30_000):
        self.address = address
        self.timeout_ms = timeout_ms
        self._context: zmq.asyncio.Context | None = None
        self._socket: zmq.asyncio.Socket | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self._context = zmq.asyncio.Context()
        self._socket = self._context.socket(zmq.REQ)
        self._socket.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
        self._socket.setsockopt(zmq.SNDTIMEO, self.timeout_ms)
        self._socket.connect(self.address)

    async def disconnect(self) -> None:
        if self._socket:
            self._socket.close()
            self._socket = None
        if self._context:
            self._context.term()
            self._context = None

    async def __aenter__(self) -> "ZMQClient":
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.disconnect()

    def _build_request(self, cmd: str, params: dict) -> bytes:
        return json.dumps({
            "cmd": cmd,
            "params": params,
            "request_id": str(uuid.uuid4()),
        }).encode()

    async def send_command(self, cmd: str, params: dict | None = None) -> dict[str, Any]:
        """Send a command and receive a plain JSON response.

        Returns
        -------
        dict
            The ``data`` field from the server response.

        Raises
        ------
        RuntimeError
            If the server returns status != "ok".
        """
        async with self._lock:
            await self._socket.send(self._build_request(cmd, params or {}))
            raw = await self._socket.recv()

        reply = json.loads(raw)
        if reply.get("status") != "ok":
            raise RuntimeError(reply.get("error", "Unknown error from DM server"))
        return reply.get("data", {})

    async def send_command_image(
        self, cmd: str, params: dict | None = None
    ) -> tuple[dict[str, Any], np.ndarray]:
        """Send a command and receive a multipart response (JSON + image bytes).

        Returns
        -------
        (metadata_dict, numpy_array)
            metadata_dict — the full server reply (image_meta + data fields)
            numpy_array   — reconstructed image array
        """
        async with self._lock:
            await self._socket.send(self._build_request(cmd, params or {}))
            meta_raw = await self._socket.recv()
            img_raw = await self._socket.recv()

        meta = json.loads(meta_raw)
        if meta.get("status") != "ok":
            raise RuntimeError(meta.get("error", "Unknown error from DM server"))

        image_meta = meta.get("image_meta", {})
        shape = image_meta["shape"]
        dtype = np.dtype(image_meta["dtype"])
        arr = np.frombuffer(img_raw, dtype=dtype).reshape(shape).copy()
        return meta, arr

    async def ping(self) -> str:
        """Ping the DM server. Returns the Python version string."""
        data = await self.send_command("ping")
        return data.get("python_version", "")
