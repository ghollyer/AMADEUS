"""Generate the MCP host entry that launches the AMADEUS MCP server.

The entry carries only the interpreter, PYTHONPATH and the absolute path to
``amadeus_config.json``. IPs and ports stay in the config file, so changing the
network never means editing MCP JSON again.
"""

import json
import os
import sys
from pathlib import Path

from common.config import CONFIG_FILENAME, REPO_ROOT

SERVER_NAME = "amadeus"


def venv_python(repo_root: Path = REPO_ROOT) -> str:
    """The checkout's .venv interpreter if present, else the current one."""
    candidates = [repo_root / ".venv" / "Scripts" / "python.exe",
                  repo_root / ".venv" / "bin" / "python"]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def server_entry(config_path: "str | os.PathLike | None" = None,
                 python: "str | None" = None, repo_root: Path = REPO_ROOT) -> dict:
    config_path = Path(config_path) if config_path else repo_root / CONFIG_FILENAME
    return {
        "command": python or venv_python(repo_root),
        "args": ["-m", "client.server", "--amadeus-config", str(config_path.resolve())],
        "env": {"PYTHONPATH": str(repo_root)},
    }


def claude_desktop_config_path() -> "Path | None":
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "Claude" / "claude_desktop_config.json"
    return None


def write_entry(target: "str | os.PathLike", entry: dict, name: str = SERVER_NAME) -> Path:
    """Merge ``entry`` into the ``mcpServers`` of the JSON file at *target*,
    keeping every other server and setting. Writes a ``.bak`` copy first."""
    target = Path(target)
    data = {}
    if target.is_file():
        text = target.read_text(encoding="utf-8")
        data = json.loads(text) if text.strip() else {}
        target.with_suffix(target.suffix + ".bak").write_text(text, encoding="utf-8")
    data.setdefault("mcpServers", {})[name] = entry
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return target
