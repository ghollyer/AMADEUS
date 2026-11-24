"""Windows network doctor for the microscope PC.

``collect()`` runs read-only PowerShell queries (listeners, network profiles,
firewall rules). ``analyze()`` is a pure function that turns those results into
plain-language findings, each with an optional copyable fix command.

The one-click firewall fix (``apply_firewall_rule``) is opt-in. It only shows
when ``experimental.firewall_autofix`` is true or the launcher gets
``--enable-firewall-fix``. It *adds* one narrow allow rule via an elevated
PowerShell (UAC prompt) and never deletes or disables existing rules.
"""

import base64
import json
import subprocess
import sys

RULE_NAME = "AMADEUS laptop Ethernet"
# Only the relay (vjem310 python.exe) accepts network connections; DM binds
# 127.0.0.1, which Windows Firewall does not filter.
PROGRAM_PATTERN = "python"

OK, WARN, FAIL, INFO = "OK", "WARN", "FAIL", "INFO"

_PS_LISTENERS = r"""
$ports = @({ports})
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
  Where-Object {{ $ports -contains $_.LocalPort }} |
  ForEach-Object {{
    $p = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue
    [pscustomobject]@{{ LocalAddress = $_.LocalAddress; LocalPort = $_.LocalPort;
                        Process = $(if ($p) {{ $p.ProcessName }} else {{ '' }});
                        Path = $(if ($p) {{ $p.Path }} else {{ '' }}) }}
  }} | ConvertTo-Json -Compress
"""

_PS_PROFILES = r"""
Get-NetConnectionProfile -ErrorAction SilentlyContinue |
  Select-Object InterfaceAlias, Name, @{n='NetworkCategory';e={$_.NetworkCategory.ToString()}} |
  ConvertTo-Json -Compress
"""

# Filters are fetched once in bulk and joined on InstanceID == rule Name; piping
# each rule through Get-NetFirewall*Filter takes minutes on a busy PC.
_PS_RULES = r"""
$app = @{}; Get-NetFirewallApplicationFilter -All -ErrorAction SilentlyContinue |
  ForEach-Object { $app[$_.InstanceID] = $_.Program }
$port = @{}; Get-NetFirewallPortFilter -All -ErrorAction SilentlyContinue |
  ForEach-Object { $port[$_.InstanceID] = ($_.LocalPort -join ',') }
$wanted = @(%(ports)s)
Get-NetFirewallRule -Direction Inbound -Enabled True -ErrorAction SilentlyContinue |
  ForEach-Object {
    $prog = $app[$_.Name]; $lp = $port[$_.Name]
    $hitPort = $false
    if ($lp) { foreach ($p in $lp.Split(',')) { if ($wanted -contains $p.Trim()) { $hitPort = $true } } }
    if (($prog -match '%(pattern)s') -or $hitPort -or ($_.DisplayName -eq '%(rule)s')) {
      [pscustomobject]@{ Name = $_.DisplayName; Action = $_.Action.ToString();
                         Profile = $_.Profile.ToString(); Program = $prog; LocalPort = $lp }
    }
  } | ConvertTo-Json -Compress
"""


def _powershell(script: str, timeout: int = 120) -> str:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    out = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True, timeout=timeout, creationflags=0x08000000)
    return out.stdout.decode(errors="replace").strip()


def _as_list(text: str) -> list:
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    return data if isinstance(data, list) else [data]


def collect(cfg: dict) -> dict:
    """Run the read-only queries. Windows only; can take tens of seconds on
    machines with many firewall rules."""
    if sys.platform != "win32":
        raise RuntimeError("the network doctor queries Windows Firewall; run it on the microscope PC")
    ports = ",".join(str(p) for p in cfg["ports"].values())
    return {
        "listeners": _as_list(_powershell(_PS_LISTENERS.format(ports=ports))),
        "profiles": _as_list(_powershell(_PS_PROFILES)),
        "rules": _as_list(_powershell(_PS_RULES % {
            "ports": ",".join("'%d'" % p for p in cfg["ports"].values()),
            "pattern": PROGRAM_PATTERN, "rule": RULE_NAME})),
    }


def _ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def firewall_rule_command(cfg: dict) -> str:
    """The narrow allow rule (ETHERNET_SETUP.md step 7) filled from the config."""
    pc = cfg["microscope_pc"]
    ports = cfg["ports"]
    parts = [
        "New-NetFirewallRule", "-DisplayName", _ps_quote(RULE_NAME),
        "-Direction", "Inbound", "-Action", "Allow", "-Protocol", "TCP",
        "-LocalPort", f"{ports['relay']},{ports['stream']}",
        "-LocalAddress", pc["ip"],
        "-RemoteAddress", cfg["control_pc"]["ip"],
        "-Profile", "Any",
    ]
    if pc.get("adapter_alias"):
        parts += ["-InterfaceAlias", _ps_quote(pc["adapter_alias"])]
    return " ".join(parts)


def analyze(cfg: dict, data: dict) -> list:
    """Return ``[(level, title, detail, fix_command_or_None), ...]``."""
    findings = []
    ports = cfg["ports"]
    direct = cfg["connection"]["mode"] == "direct"
    listeners = data.get("listeners", [])
    by_port = {}
    for entry in listeners:
        by_port.setdefault(int(entry.get("LocalPort", 0)), []).append(entry)

    # (port key, label, must be reachable from the laptop)
    roles = [("relay", "AMADEUS server (relay)", True),
             ("stream", "AMADEUS live stream", True),
             ("dm", "DM server", False),
             ("dm_stream", "DM live-frame publisher", False)]
    hints = {"relay": "Start the AMADEUS server from the launcher.",
             "stream": "Opened by the AMADEUS server. Start it from the launcher.",
             "dm": "Run scripts\\start_amadeus_dm.py inside DM.",
             "dm_stream": "Opened by the DM server. Run scripts\\start_amadeus_dm.py inside DM."}
    for key, label, remote in roles:
        port = ports[key]
        entries = by_port.get(port, [])
        if not entries:
            findings.append((FAIL, f"{label} not listening on port {port}", hints[key], None))
            continue
        addrs = sorted({e.get("LocalAddress", "") for e in entries})
        procs = sorted({e.get("Process", "") for e in entries if e.get("Process")})
        detail = f"bound to {', '.join(addrs)}" + (f" by {', '.join(procs)}" if procs else "")
        loopback_only = all(a in ("127.0.0.1", "::1") for a in addrs)
        if remote and direct and loopback_only:
            findings.append((FAIL, f"{label} (port {port}) only accepts local connections",
                             detail + ". The laptop can't reach it. Set microscope_pc.listen_ip "
                             "to '*' (all interfaces) in the setup.", None))
        elif not remote and not loopback_only:
            findings.append((INFO, f"{label} (port {port}) is exposed to the network",
                             detail + ". Only the relay needs it. Restart DM's server with "
                             "scripts\\start_amadeus_dm.py, which binds 127.0.0.1.", None))
        else:
            findings.append((OK, f"{label} listening on port {port}", detail, None))

    alias = cfg["microscope_pc"].get("adapter_alias", "")
    for profile in data.get("profiles", []):
        if alias and profile.get("InterfaceAlias") != alias:
            continue
        category = profile.get("NetworkCategory", "")
        if category == "Public":
            findings.append((INFO, f"adapter '{profile.get('InterfaceAlias')}' is on a Public network",
                             "Windows treats unidentified links (169.254.x.x, no gateway) as "
                             "Public. Allow rules scoped to Private/Domain only do not apply here.",
                             None))

    rules = data.get("rules", [])
    # Every rule collect() returns matches the AMADEUS ports or programs.
    blocks = [r for r in rules if r.get("Action") == "Block"]
    for rule in blocks:
        target = rule.get("Program") if rule.get("Program") not in (None, "", "Any") \
            else f"TCP port(s) {rule.get('LocalPort')}"
        findings.append((
            FAIL, f"firewall BLOCK rule for {target}",
            f"Rule '{rule.get('Name')}' (profile {rule.get('Profile')}) blocks inbound traffic "
            "to this program, and block rules override every allow rule. Windows creates "
            "these when its first-run 'allow access?' prompt is dismissed. Disable it in "
            "'Windows Defender Firewall with Advanced Security' (wf.msc) if it covers the "
            "vjem310 python.exe that runs the AMADEUS server.",
            f"Disable-NetFirewallRule -DisplayName {_ps_quote(rule.get('Name', ''))}"))

    needed = {str(ports["relay"]), str(ports["stream"])}
    allow_ports = set()
    program_allowed = False
    for rule in rules:
        if rule.get("Action") != "Allow":
            continue
        for p in str(rule.get("LocalPort", "")).split(","):
            allow_ports.add(p.strip())
        if rule.get("Program") and rule.get("Program") != "Any":
            program_allowed = True
    if direct and not needed <= allow_ports and not program_allowed:
        findings.append((WARN, "no inbound allow rule for the AMADEUS ports",
                         f"Nothing explicitly allows TCP {', '.join(sorted(needed))} from the "
                         "laptop. Windows Firewall may drop the connections. Run this in an "
                         "Administrator PowerShell:",
                         firewall_rule_command(cfg)))
    elif direct:
        findings.append((OK, "inbound allow rule present",
                         "found a rule covering the AMADEUS ports or the Python program", None))
    return findings


def rule_exists(data: dict) -> bool:
    return any(r.get("Name") == RULE_NAME for r in data.get("rules", []))


def apply_firewall_rule(cfg: dict) -> None:
    """Opt-in one-click fix: add the narrow allow rule through an elevated
    PowerShell (Windows shows a UAC prompt). Adds only. Never removes rules."""
    if sys.platform != "win32":
        raise RuntimeError("Windows only")
    inner = firewall_rule_command(cfg) + "; Write-Host 'AMADEUS rule added.'; Start-Sleep 3"
    encoded = base64.b64encode(inner.encode("utf-16-le")).decode("ascii")
    launcher = ("Start-Process powershell -Verb RunAs -ArgumentList "
                f"'-NoProfile','-EncodedCommand','{encoded}'")
    subprocess.run(["powershell", "-NoProfile", "-Command", launcher],
                   timeout=60, creationflags=0x08000000)
