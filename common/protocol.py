"""ZMQ wire-protocol helpers shared by the relay (AMADEUS server) and tests.

Wire format (identical to ``dm_server/server.py``):

Request:  JSON  {"cmd": "...", "params": {}, "request_id": "uuid"}
Response: JSON  {"status": "ok", "data": {...}, "request_id": "uuid"}
          or, for image responses, a multipart message:
            frame 1: JSON meta with "has_image": true and "image_meta"
            frame 2: raw C-contiguous numpy bytes
Error:    JSON  {"status": "error", "error": "description", "request_id": "uuid"}

Live frames (``LiveFrame``): a driver that streams publishes each frame on its
PUB socket (its ``stream`` address in the config) as three frames:

    1. topic   b"frame"
    2. meta    JSON {"status": "ok",
                     "image_meta": {"shape": [h, w], "dtype": "float32",
                                    "name": str, "calibration": {"x_scale",
                                    "y_scale", "units"}},
                     "data": {"detector": "stem"|"camera", "frame_index": int,
                              "signal_index"/"camera_id"/"device": which one}}
    3. pixels  raw C-contiguous bytes

The relay republishes every driver's stream unchanged on the public stream
port (XSUB -> XPUB), and ``client/transport/stream_subscriber.py`` decodes it.
``dm_server`` is the reference publisher.
"""

import json
import uuid

# Default ports across the deployment. Only "relay" and "stream" are reached
# from the laptop; both belong to the relay process. "dm" and "dm_stream" are
# the DM server's sockets, bound to 127.0.0.1 and reached only by the relay.
DEFAULT_PORTS = {
    "relay": 9875,      # relay front door (MCP client connects here)
    "dm": 9876,         # dm_server REP inside GMS (internal)
    "stream": 9877,     # relay's public live-frame PUB (proxied from DM)
    "dm_stream": 9879,  # dm_server live-frame PUB (internal)
}


# Topic of every live frame (see "Live frames" above).
LIVE_TOPIC = b"frame"


def make_request(cmd: str, params: "dict | None" = None,
                 request_id: "str | None" = None) -> bytes:
    """Encode a request exactly the way ``ZMQClient`` does."""
    return json.dumps({
        "cmd": cmd,
        "params": params or {},
        "request_id": request_id or str(uuid.uuid4()),
    }).encode()


def ok_response(data: dict, request_id: str = "") -> dict:
    return {"status": "ok", "data": data, "request_id": request_id}


def error_response(message: str, request_id: str = "") -> dict:
    return {"status": "error", "error": message, "request_id": request_id}


def parse_request(raw: bytes) -> dict:
    """Decode a request; raises ``ValueError`` on malformed JSON."""
    try:
        request = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON parse error: {exc}") from exc
    if not isinstance(request, dict):
        raise ValueError("request must be a JSON object")
    return request
