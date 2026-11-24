"""Register the AMADEUS MCP server with your MCP host (control laptop).

    python scripts/write_mcp_config.py                 # writes <repo>/.mcp.json (Claude Code)
    python scripts/write_mcp_config.py --claude-desktop
    python scripts/write_mcp_config.py --print         # just show the JSON

The entry points at amadeus_config.json, so later IP/port changes need no MCP
edits. Existing servers in the target file are kept; a .bak copy is written.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.config import REPO_ROOT  # noqa: E402
from setup_wizard import mcp_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--amadeus-config", default=None,
                        help="config file the MCP server should use "
                             "(default: <repo>/amadeus_config.json)")
    parser.add_argument("--claude-desktop", action="store_true",
                        help="write Claude Desktop's config instead of .mcp.json")
    parser.add_argument("--target", default=None, help="explicit JSON file to update")
    parser.add_argument("--print", action="store_true", help="print only, write nothing")
    args = parser.parse_args()

    entry = mcp_config.server_entry(args.amadeus_config)
    if args.print:
        print(json.dumps({"mcpServers": {mcp_config.SERVER_NAME: entry}}, indent=2))
        return 0
    if args.target:
        target = Path(args.target)
    elif args.claude_desktop:
        target = mcp_config.claude_desktop_config_path()
        if target is None:
            print("Claude Desktop config location unknown on this OS; use --target.")
            return 1
    else:
        target = REPO_ROOT / ".mcp.json"
    written = mcp_config.write_entry(target, entry)
    print(f"Wrote MCP server '{mcp_config.SERVER_NAME}' to {written}")
    print("Restart / reload your MCP host to pick it up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
