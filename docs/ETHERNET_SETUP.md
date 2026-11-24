# AMADEUS: connecting the laptop to the microscope PC over Ethernet

A step-by-step procedure for linking the control laptop to the Windows microscope PC (JEOL PyJEM + Gatan Digital Micrograph) with a direct Ethernet cable. Do the steps in order. Each one ends with a **Check** — don't move on until it passes.

**Default addresses** (used throughout; change them only if step 2.1 shows the subnet is taken):

| | Microscope PC | Laptop |
|---|---|---|
| IPv4 address | **`10.77.77.1`** | **`10.77.77.2`** |
| Subnet mask | `255.255.255.0` (prefix `24`) | `255.255.255.0` (prefix `24`) |
| Gateway / router | **blank** | **blank** |
| DNS | **blank** | **blank** |

Paths assume the checkout is at `C:\AMADEUS` on the PC; substitute yours.

---

## Part A — One-time setup

### Step 1. Connect the cable

1. Plug a **USB-Ethernet adapter** into the microscope PC. It must be a *new, separate* adapter: **never reconfigure an adapter the PC already uses for the microscope, cameras or detectors.**
2. Plug the laptop's Ethernet port (or its USB-Ethernet adapter) into it with a standard Ethernet cable. A small switch in between is also fine.
3. Leave Wi-Fi on the laptop alone; it can keep providing internet.

You do **not** need Windows Network Bridge or Internet Connection Sharing.

**Check:** the link lights on both adapters are on.

> Without further setup both ends will give themselves a `169.254.x.x` address. Don't use those: they can change on every replug, and on macOS Wi-Fi also claims the 169.254 range, so traffic to the PC silently goes out over Wi-Fi instead of the cable. Steps 2 and 3 replace them with fixed addresses.

### Step 2. Set the microscope PC's IP address (`10.77.77.1`)

This changes **one adapter only** — the USB adapter from step 1 — and adds no gateway or DNS, so it cannot reroute any other traffic on the PC. Do it while the microscope is idle. It needs an **Administrator** prompt (you may need facility IT).

**2.1 Survey (read-only).** Open *Command Prompt* or *PowerShell* **as Administrator** and run:

```bat
ipconfig /all
route print -4
```

- Find the USB adapter: its **Description** names a USB device (e.g. *Realtek USB GbE Family Controller*), and its address is currently `169.254.x.x` ("Autoconfiguration IPv4 Address"). Note its **name** from the heading, e.g. `Ethernet adapter Ethernet 4:` → the name is `Ethernet 4`.
- Confirm nothing in either output already uses `10.77.77.` If something does, pick another unused private subnet (e.g. `10.88.88.x`) and use it consistently everywhere below.
- Save both outputs (e.g. paste into Notepad) so you can compare afterwards.

**2.2 Set the address.** Replace `Ethernet 4` with the name from 2.1:

```bat
netsh interface ipv4 set address name="Ethernet 4" source=static address=10.77.77.1 mask=255.255.255.0
```

Do **not** add a gateway. (`netsh` works on every Windows version; the PowerShell `New-NetIPAddress` cmdlets are missing on some older PCs.)

<details><summary>GUI alternative</summary>

`Win+R` → `ncpa.cpl` → right-click the USB adapter → *Properties* → *Internet Protocol Version 4 (TCP/IPv4)* → *Properties* → *Use the following IP address*: `10.77.77.1`, subnet mask `255.255.255.0`. Leave **Default gateway** and **DNS** empty → OK.
</details>

**2.3 Verify nothing else changed.** Run `ipconfig /all` and `route print -4` again and compare with 2.1:
- Only the USB adapter's row differs; it now shows `10.77.77.1`, mask `255.255.255.0`, and **no** default gateway.
- `route print` gained only `10.77.77.0/255.255.255.0` routes on that adapter. The `0.0.0.0` (default) routes are unchanged.
- The microscope software and cameras still connect.

**Rollback** (if ever needed — returns the adapter to automatic addressing):

```bat
netsh interface ipv4 set address name="Ethernet 4" source=dhcp
```

> Keep the adapter in the **same USB port**. Windows ties the setting to that adapter instance; another port can appear as a new "Ethernet N" with no address, and you'd repeat this step.

**Check:** `ipconfig` shows `10.77.77.1` on the USB adapter.

### Step 3. Set the laptop's IP address (`10.77.77.2`)

**macOS**
1. System Settings → **Network** → select the Ethernet / USB LAN adapter (not Wi-Fi) → **Details…** → **TCP/IP**.
2. Configure IPv4: **Manually**. IP address `10.77.77.2`, subnet mask `255.255.255.0`, **Router: leave blank**. → OK.
3. Verify in Terminal that the PC is reached over the cable, **with Wi-Fi still on**:
   ```bash
   route -n get 10.77.77.1      # "interface:" must be the Ethernet adapter (e.g. en10), not en0/Wi-Fi
   ```
   (`amadeus-check` and the setup run this check too, and warn if the wrong adapter is used.)

**Windows laptop**
Same as step 2.2 with address `10.77.77.2`, on the laptop's Ethernet adapter, no gateway.

**Check:** from the laptop, `ping 10.77.77.1` answers. If it doesn't, carry on anyway — Windows often blocks ping while allowing AMADEUS's ports; step 8 tests the real ports.

### Step 4. Put AMADEUS on both computers

Use the **same revision** of the repository on both machines. Do this while the laptop still has internet.

1. **Laptop:** clone the repo, then in the checkout:
   ```bash
   uv pip install -e ".[dev]"
   ```
   (Without `uv`: `python3 -m venv .venv && .venv/bin/python -m pip install -e .`)
2. **PC:** copy the checkout to `C:\AMADEUS` (a local folder, not a mapped drive). **Install nothing** — AMADEUS runs from source under JEOL's **vjem310** Python, which only needs `pyzmq`. If the setup in step 6 reports pyzmq missing:
   ```bat
   <path to vjem310 python.exe> -m pip install pyzmq
   ```
   Don't copy the laptop's `.venv` to the PC, and don't `pip install` the AMADEUS package into vjem310.

**Check:** `amadeus-check --help` runs on the laptop.

### Step 5. Check Digital Micrograph's Python

In DM, open a new script window, set its language to **Python**, paste, and Execute:

```python
import sys, numpy, zmq
import DigitalMicrograph as DM
print(sys.version, sys.prefix, zmq.__version__)
```

If an import fails, install the missing package into **the Python environment DM uses** (shown by `sys.prefix` — it is not necessarily vjem310), then restart DM.

**Check:** the script prints a Python version and a pyzmq version.

### Step 6. Run the first-time setup on the PC

1. Double-click **`C:\AMADEUS\AMADEUS.bat`**. The first time, it opens the setup.
2. **This computer's IP:** `10.77.77.1`. **Laptop IP:** `10.77.77.2`.
3. Confirm the detected vjem310 `python.exe` (*Test interpreter* should report Python, pyzmq, PyJEM, tkinter) and the checkout folder.
4. **Hardware mode: Offline** (PyJEM simulator) for the first run. *Online* drives the real column — that comes in Part D.
5. **Hardware routing:** PyJEM (leave as is).
6. Save. The launcher opens.

**Check:** the launcher window is open and its Readiness list has no address warnings.

### Step 7. Allow the laptop through Windows Firewall

The laptop needs inbound **TCP 9875** (commands) and **9877** (live frames), both to the vjem310 `python.exe`. DM listens on `127.0.0.1` only and never needs a rule.

1. In the launcher press **Network doctor** (read-only). It shows what is listening, the adapter's network profile, any firewall **block** rules for `python.exe`, and prints the exact allow rule with your addresses.
2. If it lists a **block** rule for `python.exe` (created when Windows' first "Allow access?" prompt was dismissed), disable it in `wf.msc` — block rules win over allow rules.
3. In an **Administrator PowerShell**, add the allow rule (use the adapter name from step 2.1):
   ```powershell
   New-NetFirewallRule -DisplayName "AMADEUS laptop Ethernet" `
     -Direction Inbound -Action Allow -Protocol TCP `
     -LocalPort 9875,9877 -LocalAddress 10.77.77.1 `
     -RemoteAddress 10.77.77.2 -InterfaceAlias "Ethernet 4" -Profile Any
   ```
   `-Profile Any` is needed because Windows classifies a gateway-less link as *Public*; the rule is still limited to this adapter and the laptop's address.

AMADEUS's ports have **no authentication or encryption** and expose scripting and hardware control. Keep them on this dedicated cable; never expose them to another network.

**Check:** Network doctor shows no block rules and the allow rule present.

### Step 8. Start the servers

1. **AMADEUS server:** in the launcher press **Start**. The server light turns green.
2. **DM server:** in DM, *File › Open* `C:\AMADEUS\scripts\start_amadeus_dm.py` (language **Python**) → **Execute**. It keeps running (an active script is expected). Expected output:
   ```text
   [dm_server] Listening on tcp://127.0.0.1:9876  (live stream tcp://127.0.0.1:9879)
   ```

**Check:** the launcher shows the server, DM and hardware lights green.

### Step 9. Set up and verify the laptop

```bash
amadeus-setup --pc 10.77.77.1    # fetches the PC's config, registers the MCP server
amadeus-check                    # config, route, ports, both backends, read-only reads
amadeus-check --image            # + a synthetic DM image round trip (moves no hardware)
```

`amadeus-setup --pc` saves the laptop's `amadeus_config.json` and registers the MCP server in `<checkout>/.mcp.json` (add `--claude-desktop` for Claude Desktop). Without `--pc` it opens a window with **Fetch settings from the microscope PC**.

`amadeus-check` checks, in order: the config (fails if the "PC" address is actually the laptop's own; warns on link-local or mismatched subnets, and on macOS if the PC is routed over the wrong adapter), TCP 9875/9877, both backends, then read-only stage / focus / magnification / live status. In Offline mode, zero stage or HT readings and `calibrated: false` focus are normal.

**Check:** `amadeus-check` passes.

### Step 10. Connect the AI app and save a profile

1. Start (or reload) your MCP host — in Claude Code, `/mcp`. Call `backend_status`: both `dm` and `hw` must report `ok: true`.
2. On the PC, press **Export profile…** in the launcher and keep the file **off the PC**. It turns a reinstall into four steps (Part F).

**Check:** `backend_status` reports both backends ok.

---

## Part B — Daily startup

1. Connect the cable (same USB port on the PC).
2. PC: `AMADEUS.bat` → **Start**.
3. DM: run `scripts\start_amadeus_dm.py`.
4. Laptop: `amadeus-check`.
5. Start the AI app (`/mcp`) and/or `amadeus-console` to supervise (`--read-only` for a passive monitor).

The launcher must stay open while AMADEUS is in use; closing it stops the server.

## Part C — Shutdown

1. Stop any acquisition or live scan (`stop_live_scan`). Check in DM that DigiScan is no longer scanning.
2. Close the launcher (stops the AMADEUS server).
3. Close the laptop client.
4. **The DM server cannot be stopped once it has been started.** There is no working stop command; the only way to stop it is to **close and restart Digital Micrograph**. Leaving it running between sessions is harmless — it listens on `127.0.0.1` only, so nothing outside the PC can reach it without the AMADEUS server. Restart DM when you need it gone (e.g. to load new AMADEUS code, Part E), and don't run `start_amadeus_dm.py` a second time in the same DM session.

## Part D — Switching to the real microscope (Online)

Offline success proves the software link, not the instrument.

1. In the launcher, **Stop** the server.
2. Run the read-only PyJEM check in vjem310:
   ```bat
   cd /d C:\AMADEUS
   <vjem310 python.exe> -m drivers.smoke --driver hw
   ```
   It must pass. (`Apt3.GetKind` is known to fail on the F200 and is reported as `WARN`.)
3. **Settings…** → Hardware mode **Online** → save → **Start**.
4. Compare AMADEUS's readouts (stage, mag, focus) with the JEOL console **before issuing any writes**.

Units: stage is metres/degrees on the AMADEUS wire (nm/degrees in PyJEM); focus is a raw DAC value unless a measured calibration is loaded. Never treat an uncalibrated value as nanometres.

## Part E — Updating AMADEUS on the PC

1. Stop any live scan.
2. In the launcher, **Stop** the server.
3. **Close Digital Micrograph** — this is the only way to stop the running DM server (Part C).
4. Copy the new files into `C:\AMADEUS` (same revision as the laptop).
5. Start DM, then run `scripts\start_amadeus_dm.py` so it loads the new code. Launcher **Start**.
6. Laptop: `/mcp` to reload the MCP server, then `amadeus-check`.

## Part F — Reinstalling the PC

1. Copy the checkout to the PC.
2. `AMADEUS.bat` → **Restore from a profile…** → pick the file from step 10.
3. Review and save.
4. **Start**, then run the DM script.

The folder and vjem310 path are re-detected. A profile saved Online comes back **Offline** until the driver smoke test (`python -m drivers.smoke --driver hw`) passes on the new install. **Readiness** lists anything left — typically the adapter's static IP (redo step 2) if Windows reset it.

## Part G — Alternative: SSH tunnel instead of a firewall rule

If the PC already runs an SSH server with your laptop authorised, choose *SSH tunnel* in the setup (step 6). The server then listens on `127.0.0.1` only and step 7 is unnecessary. Keep this running on the laptop:

```bash
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -L 127.0.0.1:29875:127.0.0.1:9875 -L 127.0.0.1:29877:127.0.0.1:9877 WINDOWS_USER@10.77.77.1
```

and use `amadeus-setup --pc 127.0.0.1 --port 29875` in step 9.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Laptop can't reach the PC | Run `amadeus-check` first (catches using the laptop's own IP, subnet mismatches, wrong adapter). Then **Network doctor** on the PC: server not started, bound to `127.0.0.1`, or a firewall *block* rule |
| Works with Wi-Fi off, fails with Wi-Fi on (macOS) | Traffic is routed over Wi-Fi: `route -n get 10.77.77.1` shows `en0`. Almost always because one end is still on `169.254.x.x` — redo steps 2–3 |
| Address shows `169.254.x.x` | The static address isn't set (or the adapter moved USB port). Redo step 2 / 3 |
| PC address set to `10.77.77.0` | `.0` is the network address of a /24 — use `10.77.77.1` |
| `New-NetIPAddress` / `New-NetIPInterface` "not recognized" | Older Windows; use the `netsh` command in step 2.2 |
| `amadeus-setup --pc` gets no answer | The AMADEUS server must be running (launcher green) and reachable; same causes as the first row |
| Ping fails but `amadeus-check` passes | ICMP is blocked; harmless |
| Server up, DM down | Run `scripts\start_amadeus_dm.py` inside DM; it prints missing imports and port conflicts |
| Server up, hardware down | Launcher log: wrong mode for this machine (Online needs real PyJEM), or a PyJEM import error. Run `python -m drivers.smoke --driver hw` |
| `No module named dm_server` in DM | Re-run the setup (it sets `AMADEUS_HOME`) and restart DM, or keep the checkout at `C:\AMADEUS` |
| `No module named DigitalMicrograph` | The script is running outside DM's Python |
| `Address already in use` | An old server or tunnel holds the port; stop it, or change the port in Settings |
| Commands work, live frames don't | TCP 9877 (29877 over SSH) must be reachable, a live scan must be active, and the DM server running |
| Long acquisition times out | Raise `relay.timeout_ms` and `client.timeout_s` in the config (both must cover it) |
| Readiness: "no adapter on this PC has 10.77.77.1" | Windows reset the adapter (or it's in a different USB port). Redo step 2 |
| Pre-2026-11 config file | Upgraded automatically on load |

## Reference

### What runs where

```text
Laptop                                          Windows PC (10.77.77.1)
MCP client / AI application
    │ starts AMADEUS over stdio
    ▼
AMADEUS MCP server ───── TCP 9875 ─────────────► AMADEUS server (vjem310 python.exe)
                                                   ├─ hardware backend, in-process → PyJEM
                                                   ├─ 127.0.0.1:9876 → dm_server inside DM
Live-frame subscriber ◄─── TCP 9877 ──────────────┴─ republishes 127.0.0.1:9879 (DM live PUB)
```

| Component | Runs in | Does |
|---|---|---|
| AMADEUS MCP server, console | Laptop, Python 3.11+ | MCP tools, local images, state, analysis |
| AMADEUS server (`python -m relay`) | PC, vjem310 (Python 3.10 + pyzmq) | The only network listener. PyJEM in-process; forwards camera/scan/file requests to DM; proxies live frames |
| DM server | Python **inside** Digital Micrograph | Cameras, DigiScan, spectroscopy, files. `127.0.0.1` only |

Every setting lives in one `amadeus_config.json`, written by the setup on the PC; the laptop fetches its copy with `amadeus-setup --pc`. Nothing is hand-edited.

### MCP notes

- The MCP entry `amadeus-setup` writes contains only the interpreter, `PYTHONPATH` and `--amadeus-config <path>`; addresses come from the config, so network changes never need MCP edits. Rewrite it with `python scripts/write_mcp_config.py [--claude-desktop | --print]`.
- `fetch_remote_file` pulls a DM file's array + calibration into the image store for analysis. `download_remote_file` copies the original file byte-for-byte (full DM tag tree) into `<data_dir>/files/`.

### What has been verified

- **F200, physical cable, 2026-11-07** (PyJEM Offline): static `10.77.77.x` link with Wi-Fi on; `amadeus-check`; MCP `backend_status` (both backends); TEM camera and STEM acquisition; STEM and camera live streaming over the relay; listing and fetching a `.dm4`.
- **Not yet verified:** Online PyJEM on the F200 (physical motion, calibration accuracy), spectroscopy / EDS, and the raw file transfer (`download_remote_file`) on the instrument.
