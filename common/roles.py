"""Default device roles, guessed from what the hardware reports.

Used by the relay (to resolve a role nobody configured) and by the client's
capability matching, so both agree. A configured role (``instrument.roles``)
always wins over a guess.

Stdlib only, Python 3.10.
"""

import re

DETECTOR_ROLES = ("HAADF", "ABF", "ADF", "BF", "DF", "SE")


def guess_detector_roles(detectors) -> dict:
    """``{"HAADF": 1, ...}`` from ``[{"index", "name"}]`` (signal names)."""
    roles = {}
    for det in detectors or []:
        name = str(det.get("name", "")).upper()
        tokens = set(re.split(r"[^A-Z]+", name))
        for role in DETECTOR_ROLES:
            if role in tokens and role not in roles:
                roles[role] = int(det.get("index", 0))
                break
    return roles


def guess_camera_roles(cameras) -> dict:
    """``{"imaging": <first camera id>}``."""
    cameras = list(cameras or [])
    return {"imaging": int(cameras[0].get("id", 0))} if cameras else {}
