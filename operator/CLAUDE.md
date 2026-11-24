# AMADEUS operator session

You are operating a JEOL F200 TEM/STEM (Gatan DigitalMicrograph) through the
`amadeus` MCP tools. Those tools are the only way you can act on the instrument.

- There is no scripting in this session. `execute_vendor_call`, `execute_dm_script`,
  `execute_tem3_call`, the shell and file editing are turned off on purpose. If a task needs something the tools
  can't do, stop and tell the operator what's missing. Don't look for a workaround.
- You can Read files under `../amadeus_data/` (the AMADEUS data directory). Exported PNGs are in
  `amadeus_data/session/exports/`: Read them to see images and maps.

## Instrument rules

- Some commands need the operator's go-ahead (a `policy.confirm` command, switching
  between cameras in an exclusive group, a raw vendor write, a beam shift beyond
  `max_beam_shift`). AMADEUS asks the operator itself, in their console or as a
  prompt here; you can't confirm on their behalf. If the result says the operator
  declined, don't retry: ask them what they want instead.
- Values are device units: lens/deflector/stigmator values are raw DACs
  (0-65535, 32768 = deflector neutral). Never invent physical units.
- Both cameras are retractable. Switching cameras physically moves them (about
  30 s), so ask the operator first and always pass `camera` (a role or id from
  `list_cameras`). Metro acquisition
  doesn't work yet, so don't use it.
- Stage: the F200 loses 0.5-0.7 um on direction reversals. Use `approach=True`
  for precise moves, and `convert_pixel_to_stage` for pixel -> stage
  targets. Never add a pixel offset to the stage position yourself. `stop_stage` always works,
  even mid-move.
- Image calibration is in DM's units (often um). The tools convert it, and they refuse
  uncalibrated images rather than assume nm/px.
- Don't discard unsaved DM images (`close_dm_window(discard_unsaved=True)`)
  without asking the operator.
- Prefix any test entries you create (saved locations, map objects) with
  `mcp_test`. They can't be deleted afterwards.

## Common workflows

- **STEM focus**: `stem_autofocus` (STEM mode only). It sweeps the `focus.fine`
  lens (OLf on the F200), defaulting to +/-200 DAC around the current value, and narrows in on the sharpest frame. If it
  warns that the best frame is at the edge of the range, widen the range and rerun.
- **TEM defocus/astigmatism**: take an image of thin amorphous material, then
  call `compute_fft(fit_ctf=True)`. Only trust the result if it says `reliable: true`. If
  `sign_ambiguous`, |C1| is right but check the sign by stepping focus.
- **EDS SI to maps**: `configure_si`, then `start_si`, then poll `get_si_status`
  until it is done. Then call `list_dm_windows` to find the SI (and its DF image) and
  `save_dm_window(path="C:\\AMADEUS_data\\<date>\\<name>.dm4", doc_id=..., fetch=True)`
  for each. Then call `eds_map_elements(si_image_id=..., df_image_id=...)`. Read
  its PNGs, and use `eds_edge_profile` to check for core/shell structure.
- **Particles**: `measure_nanorods` (isolated rods only) and
  `find_particle_clusters` (gives stage targets).

## Web console mode (amadeus-web)

When the operator runs `amadeus-web --no-scripting` on the laptop and switches
`enabledMcpjsonServers` in `.claude/settings.json` from `amadeus` to `amadeus-web`,
your tools come from the web console instead. Everything above still applies, plus:

- Every tool call you make shows up as a block in the operator's action queue.
  It waits its turn behind the operator's own blocks, and the operator can pause
  the queue or remove your call before it runs. A call that comes back with
  "removed from the queue" was refused by the operator: don't retry it, ask why.
- When you have finished a task and have nothing to do, call `wait_for_operator`
  (and call it again when it times out). That is how the operator gives you work.
- A tool result may end with `operator_message: ...`. That is the operator typing
  to you mid-task: act on it now.
- Use `ask_operator` for decisions that are the operator's (camera switches,
  valves, which particle to study). Use `tell_operator` for short status lines.
