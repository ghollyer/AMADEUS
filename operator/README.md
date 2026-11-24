# Locked-down operator workspace

Run `claude` from this folder to operate the instrument with an agent that can
only use the AMADEUS MCP tools:

- `.mcp.json` starts `amadeus-server --no-scripting` from the repository root
  (`uv run --directory ..`), so `execute_dm_script`, `execute_tem3_call` and
  `execute_vendor_call` are not registered at all. It reads
  `<repo>/amadeus_config.json` like every other entry point.
- `.claude/settings.json` also denies those tools, the shell, file editing and
  the web, and lets the agent read only `../amadeus_data/` (exported PNGs).
  Add deny rules for any other MCP servers your Claude Code has configured.
- To drive the browser console instead, start `amadeus-web --no-scripting` and
  switch `enabledMcpjsonServers` from `amadeus` to `amadeus-web`.

`CLAUDE.md` holds the instrument rules the agent follows. The ones there are
for the JEOL F200 this was developed on (retractable cameras, stage backlash,
the F200 focus lens); adapt them to your microscope.
