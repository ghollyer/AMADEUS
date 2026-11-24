"""Tests for the live-scan frame stream (PUB/SUB) and StreamSubscriber.

The subscriber decode path is tested both directly (deterministic, no sockets)
and over a real PUB/SUB pair (integration). The latter accounts for the ZMQ
slow-joiner problem by publishing repeatedly until the frame lands.
"""

import asyncio
import json

import numpy as np
import pytest
import zmq

from client.transport.stream_subscriber import StreamSubscriber, stream_address_from

STREAM_PORT = 29877  # test port — avoid clashing with the live stream (9877)


def _make_frame(arr: np.ndarray, *, frame_index: int = 1, detector: str = "stem"):
    """Build a 3-part frame message exactly as the DM server publishes it."""
    meta = {
        "status": "ok",
        "image_meta": {
            "shape": list(arr.shape),
            "dtype": str(arr.dtype),
            "name": "AMADEUS Live",
            "calibration": {"x_scale": 0.1, "y_scale": 0.1, "units": "nm"},
        },
        "data": {"detector": detector, "frame_index": frame_index},
    }
    return [b"frame", json.dumps(meta).encode(), np.ascontiguousarray(arr).tobytes()]


# ---------------------------------------------------------------------------
# Address helper
# ---------------------------------------------------------------------------

def test_stream_address_from_swaps_port():
    assert stream_address_from("tcp://localhost:9876") == "tcp://localhost:9877"
    assert stream_address_from("tcp://10.0.0.5:9876", 9999) == "tcp://10.0.0.5:9999"


# ---------------------------------------------------------------------------
# Decode path — deterministic, no sockets
# ---------------------------------------------------------------------------

def test_ingest_decodes_frame(tmp_path):
    sub = StreamSubscriber("tcp://127.0.0.1:1", monitor_dir=tmp_path, preview_fps=1000)
    arr = np.arange(64, dtype=np.float32).reshape(8, 8)
    sub._ingest(_make_frame(arr, frame_index=3, detector="camera"))

    latest = sub.latest()
    assert latest is not None
    assert latest["frame_index"] == 3
    assert latest["meta"]["data"]["detector"] == "camera"
    np.testing.assert_array_equal(latest["array"], arr)
    # Preview written for the monitor GUI.
    assert (tmp_path / "live.npy").exists()


def test_ingest_ignores_malformed(tmp_path):
    sub = StreamSubscriber("tcp://127.0.0.1:1", monitor_dir=tmp_path, preview_fps=1000)
    arr = np.ones((4, 4), dtype=np.float32)
    sub._ingest(_make_frame(arr, frame_index=5))
    sub._ingest([b"frame", b"not-json", b""])     # bad meta
    sub._ingest([b"frame", b"{}"])                # wrong part count
    # Latest unchanged; bad messages dropped without raising.
    assert sub.latest()["frame_index"] == 5
    assert sub.stats()["frames_received"] == 1


# ---------------------------------------------------------------------------
# Integration — real PUB/SUB round trip
# ---------------------------------------------------------------------------

async def test_subscriber_receives_published_frame(tmp_path):
    ctx = zmq.Context()
    pub = ctx.socket(zmq.PUB)
    pub.bind(f"tcp://127.0.0.1:{STREAM_PORT}")

    sub = StreamSubscriber(
        f"tcp://127.0.0.1:{STREAM_PORT}",
        monitor_dir=tmp_path / "monitor",
        preview_fps=1000,
    )
    await sub.start()
    try:
        arr = np.arange(16, dtype=np.float32).reshape(4, 4)
        latest = None
        # Slow-joiner: keep publishing until the SUB connection is live.
        for _ in range(200):
            pub.send_multipart(_make_frame(arr, frame_index=7))
            await asyncio.sleep(0.02)
            latest = sub.latest()
            if latest is not None:
                break
        assert latest is not None, "subscriber never received a frame"
        np.testing.assert_array_equal(latest["array"], arr)
        assert latest["frame_index"] == 7
        assert sub.stats()["frames_received"] >= 1
    finally:
        await sub.stop()
        pub.close()
        ctx.term()
