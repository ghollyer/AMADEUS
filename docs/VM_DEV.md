# Developing against DM in a Windows VM

Before going to the microscope, try DM-side changes (and the full AMADEUS stack) in a
Windows VM with no hardware. The reference setup is a UTM VM on a Mac, with:

- a free/offline GMS 3.6x install (DigitalMicrograph, Python 3.10 with numpy and pyzmq);
- the JEOL vjem310 conda env (Python 3.10, pyzmq, PyJEM offline), e.g. `C:\vjem310\python.exe`.

There is no camera, DigiScan or EM* hardware in the VM, so those calls raise
`NotImplementedError` there (expected). Everything else works: the relay, the JEOL driver
in offline mode, DM scripting, the DM window tools, file loading and the
`amadeus-check --image` round trip.

Placeholders used below:

| | |
|---|---|
| `<guest>` | the VM's IP address, with an SSH server (OpenSSH for Windows) and a Windows user `<user>` |
| `$KEY` | your SSH key for the guest (pass `-o IdentitiesOnly=yes` if you have several) |

## Getting at the guest

- **Shell:** `ssh -i $KEY <user>@<guest> powershell -Command "<command>"`.
  Programs started this way are invisible on the VM's desktop. Start DM itself from
  the VM's desktop, or through an interactive scheduled task.
- **Files:** `scp -i $KEY ... <user>@<guest>:C:/...`. Shared folders (SMB, UTM's WebDAV)
  can be unreliable for newly created files, so prefer `scp`.
- **Typing into the VM** with an automation tool can garble keystrokes. Pasting through
  the clipboard is more reliable.

## Running the full stack in the VM

1. **Code → `C:\AMADEUS`** (the first place `scripts/start_amadeus_dm.py` looks). Copy
   only the code (`git ls-files` minus `docs/`, plus any new untracked code files), not
   `.git` or your instrument's `amadeus_config*.json`. The VM runs with no config file.
   ```bash
   { git ls-files; git ls-files --others --exclude-standard -- '*.py'; } \
     | grep -v -E '^docs/' > /tmp/files.txt
   COPYFILE_DISABLE=1 tar -czf /tmp/amadeus.tgz -T /tmp/files.txt
   scp -i $KEY /tmp/amadeus.tgz <user>@<guest>:C:/Users/<user>/
   ssh -i $KEY <user>@<guest> "tar -xzf C:\Users\<user>\amadeus.tgz -C C:\AMADEUS"
   ```
   Afterwards, compare `Get-FileHash` in the guest with `shasum -a 256` on the host
   to confirm the copy is current.

2. **Relay** (PyJEM offline, loopback only). It must outlive the SSH session, so start
   it through WMI rather than directly:
   ```powershell
   $cmd = 'cmd.exe /c cd /d C:\AMADEUS && C:\vjem310\python.exe -u -m relay --offline --listen tcp://127.0.0.1:9875 > C:\AMADEUS\logs\relay_vm.log 2>&1'
   Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=$cmd}
   ```
   The relay keeps running when DM is closed.

3. **DM server.** Start DM (click OK on any "elevated privileges" warning, then wait for
   it to load). File > Open `C:\AMADEUS\scripts\start_amadeus_dm.py` (Language: Python),
   tick **Execute on Background Thread**, then Execute. On a background thread DM keeps
   repainting and stays usable; on the main thread the server's poll loop freezes DM's UI.
   Check with `netstat -ano | findstr LISTENING | findstr 987`: you should see 9875
   (relay), 9876 and 9879 (DM).

4. **Tunnel from the host.** No Windows firewall rule is needed:
   ```bash
   ssh -N -i $KEY -o ExitOnForwardFailure=yes \
       -L 39875:127.0.0.1:9875 -L 39877:127.0.0.1:9877 <user>@<guest>
   ```

5. **Laptop config.** Copy `amadeus_config.example.json` and set `microscope_pc.ip` and
   `control_pc.ip` to `127.0.0.1`, `ports.relay` to `39875`, `ports.stream` to `39877`, and
   `control_pc.data_dir` to a scratch directory. Then
   `uv run amadeus-check --amadeus-config <that file>` should pass every check.

6. **Calling MCP tools from a script** (the way an agent would):
   ```python
   mcp, zmq, *_ = build_server("tcp://127.0.0.1:39875", data_dir, enable_monitor=False)
   await zmq.connect()          # without it: "'NoneType' object has no attribute 'setsockopt'"
   result = await mcp.call_tool("list_dm_windows", {})
   ```
   Make test images with the `execute_dm_script` tool, name them `mcp_test…`, and close
   them afterwards with `close_dm_window(doc_id=…, discard_unsaved=True)`.

## Gotchas

- **Reloading `dm_server/` code needs no DM restart in the VM** (on the F200 it did).
  Copy the code, call `shutdown_dm_server` (the DM ports close), then press Execute on
  the `start_amadeus_dm.py` window again. The relay can stay up.
- **DM's UI doesn't repaint** while a script holds DM's main thread, so screenshots show
  stale windows. Run the server on a background thread (step 3).
- **A fresh DM numbers image document IDs from 0.**
- If DM disappears without a crash event in the Windows Application log, someone most
  likely closed it; restart it as in step 3.
- To test a helper module without the server, copy `dm_server/` into the guest,
  `sys.path.insert` that folder in a DM Python script, and import the helper.
  `dm_server.server._dispatch(fake_socket, request)` exercises the real dispatcher.
