"""Append-only, cross-process session activity log."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class SessionEventStore:
    def __init__(self, data_dir: Path | str):
        self.path = Path(data_dir) / "session_events.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event_type: str, *, actor: str, summary: str,
               data: dict[str, Any] | None = None) -> dict[str, Any]:
        event = {"id": str(uuid.uuid4()),
                 "timestamp": datetime.now(timezone.utc).isoformat(),
                 "type": event_type, "actor": actor, "summary": summary,
                 "data": data or {}}
        line = json.dumps(event, default=str, separators=(",", ":")) + "\n"
        fd = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        return event

    def list(self, limit: int = 200) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        result = []
        for line in lines[-max(1, limit):]:
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return result
