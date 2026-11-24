"""Small network helpers shared by the setup wizard, launcher and checks.

Stdlib only, Python 3.10-compatible.
"""

import ipaddress
import re
import socket
import subprocess
import sys

_WIN_ADAPTER = re.compile(r"^(\S.*?adapter\s+(.+?)):\s*$", re.IGNORECASE)
_WIN_IPV4 = re.compile(r"IPv4[^:]*:\s*([0-9.]+)", re.IGNORECASE)
_NIX_IFACE = re.compile(r"^([A-Za-z0-9_.\-]+):\s")
_NIX_INET = re.compile(r"^\s+inet\s+([0-9.]+)")
_LINUX_IP = re.compile(r"^\d+:\s+(\S+)\s+inet\s+([0-9.]+)")


def _run(cmd) -> str:
    try:
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        out = subprocess.run(cmd, capture_output=True, timeout=10, **kwargs)
        # ipconfig writes in the console code page; be forgiving.
        return out.stdout.decode(errors="replace")
    except Exception:
        return ""


def parse_ipconfig(text: str) -> list:
    """Parse Windows ``ipconfig`` output into ``[(adapter, ip), ...]``."""
    result, adapter = [], None
    for line in text.splitlines():
        m = _WIN_ADAPTER.match(line)
        if m:
            adapter = m.group(2).strip()
            continue
        m = _WIN_IPV4.search(line)
        if m and adapter is not None:
            result.append((adapter, m.group(1)))
    return result


def parse_ifconfig(text: str) -> list:
    """Parse macOS/BSD ``ifconfig`` output into ``[(iface, ip), ...]``."""
    result, iface = [], None
    for line in text.splitlines():
        m = _NIX_IFACE.match(line)
        if m:
            iface = m.group(1)
            continue
        m = _NIX_INET.match(line)
        if m and iface is not None:
            result.append((iface, m.group(1)))
    return result


def parse_ip_addr(text: str) -> list:
    """Parse Linux ``ip -o -4 addr`` output."""
    result = []
    for line in text.splitlines():
        m = _LINUX_IP.match(line)
        if m:
            result.append((m.group(1), m.group(2).split("/")[0]))
    return result


def local_ipv4_addresses(include_loopback: bool = False) -> list:
    """``[(adapter_name, ip), ...]`` for this machine's IPv4 addresses."""
    if sys.platform == "win32":
        pairs = parse_ipconfig(_run(["ipconfig"]))
    elif sys.platform == "darwin":
        pairs = parse_ifconfig(_run(["ifconfig"]))
    else:
        pairs = parse_ip_addr(_run(["ip", "-o", "-4", "addr"])) \
            or parse_ifconfig(_run(["ifconfig"]))
    if not pairs:
        try:
            infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
            pairs = [("?", info[4][0]) for info in infos]
        except OSError:
            pairs = []
    seen, out = set(), []
    for adapter, ip in pairs:
        if ip in seen:
            continue
        seen.add(ip)
        if not include_loopback and ip.startswith("127."):
            continue
        out.append((adapter, ip))
    return out


def is_link_local(ip: str) -> bool:
    """True for 169.254.x.x. Windows/macOS assign these when DHCP fails, and
    they can change after a reboot or cable replug."""
    try:
        return ipaddress.ip_address(ip).is_link_local
    except ValueError:
        return False


def is_local_address(ip: str) -> bool:
    """True if *ip* belongs to this machine (including loopback)."""
    if ip in ("localhost",) or ip.startswith("127."):
        return True
    return ip in {addr for _, addr in local_ipv4_addresses(include_loopback=True)}


def same_subnet(ip_a: str, ip_b: str, prefix: int = 24) -> bool:
    try:
        if is_link_local(ip_a) and is_link_local(ip_b):
            prefix = 16
        net = ipaddress.ip_network(f"{ip_a}/{prefix}", strict=False)
        return ipaddress.ip_address(ip_b) in net
    except ValueError:
        return False


def tcp_probe(host: str, port: int, timeout: float = 3.0):
    """Try a TCP connect. Returns ``(ok, message)``. "Refused" means the host
    answered but nothing is listening there, or a firewall rejected it.
    "Timed out" usually means the wrong IP, a broken link, or a firewall that
    drops packets."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "open"
    except ConnectionRefusedError:
        return False, "refused (nothing listening, or the firewall rejected it)"
    except socket.timeout:
        return False, "timed out (wrong IP, link down, or firewall dropping packets)"
    except OSError as exc:
        return False, f"error: {exc}"


def ping(host: str, timeout_s: int = 2) -> bool:
    """One ICMP echo via the OS ``ping`` command. Windows often blocks ICMP,
    so a failure here does not prove the link is down."""
    if sys.platform == "win32":
        cmd = ["ping", "-n", "1", "-w", str(timeout_s * 1000), host]
    elif sys.platform == "darwin":
        cmd = ["ping", "-c", "1", "-t", str(timeout_s), host]
    else:
        cmd = ["ping", "-c", "1", "-W", str(timeout_s), host]
    try:
        kwargs = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
        return subprocess.run(cmd, capture_output=True, timeout=timeout_s + 3,
                              **kwargs).returncode == 0
    except Exception:
        return False


_ROUTE_IFACE = re.compile(r"^\s*interface:\s*(\S+)", re.MULTILINE)


def parse_route_get(text: str) -> "str | None":
    """Interface name from macOS ``route -n get <ip>`` output."""
    m = _ROUTE_IFACE.search(text)
    return m.group(1) if m else None


def route_interface(ip: str) -> "str | None":
    """The adapter the OS sends traffic for *ip* out of. macOS only (``None``
    elsewhere or when the lookup fails)."""
    if sys.platform != "darwin":
        return None
    return parse_route_get(_run(["route", "-n", "get", ip]))


def route_mismatch(pc_ip: str, iface: "str | None", adapters: list,
                   expected_ip: "str | None" = None) -> "str | None":
    """Warning text if traffic for *pc_ip* leaves via the wrong adapter.

    The expected adapter is the one holding *expected_ip* (this laptop's
    configured address) or else the one on the PC's subnet. Classic cause:
    on macOS every adapter gets a 169.254/16 route and Wi-Fi's wins, so a
    link-local Ethernet link silently goes out over Wi-Fi."""
    if not iface:
        return None
    by_iface = {}
    for name, ip in adapters:
        by_iface.setdefault(name, []).append(ip)
    expected = None
    if expected_ip:
        expected = next((n for n, ip in adapters if ip == expected_ip), None)
    if expected is None:
        expected = next((n for n, ip in adapters if same_subnet(ip, pc_ip)), None)
    if expected is None or expected == iface:
        return None
    own = ", ".join(by_iface.get(iface, [])) or "no IPv4 address"
    exp_ip = expected_ip if expected_ip in by_iface.get(expected, []) \
        else ", ".join(by_iface[expected])
    hint = ("Every adapter gets a 169.254/16 route and the higher-priority one "
            "(usually Wi-Fi) wins: turn Wi-Fi off, or give the cable a static "
            "private subnet on both ends (e.g. 10.77.77.x/24, no router)."
            if is_link_local(pc_ip) else
            "Another adapter has a route for this address: turn Wi-Fi/VPN off "
            "or move the Ethernet link to a subnet nothing else uses.")
    return (f"traffic to {pc_ip} goes out via {iface} ({own}), not {expected} "
            f"({exp_ip}). {hint}")


def check_route(pc_ip: str, expected_ip: "str | None" = None) -> "str | None":
    """:func:`route_mismatch` for this machine (macOS only; ``None`` otherwise)."""
    if pc_ip in ("localhost",) or pc_ip.startswith("127."):
        return None
    iface = route_interface(pc_ip)
    if iface is None:
        return None
    return route_mismatch(pc_ip, iface, local_ipv4_addresses(), expected_ip)
