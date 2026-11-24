"""AMADEUS setup: writes amadeus_config.json.

Microscope PC: the launcher (``AMADEUS.bat``) opens these pages on first run
and from its **Settings…** button.

Control laptop: nothing is copied by hand. The laptop fetches the PC's
config through the relay:

    amadeus-setup --pc 10.77.77.1        # non-interactive
    amadeus-setup                        # GUI: type the PC's IP, press Fetch

"Save a copy…" / "Import…" remain as an offline fallback.

The pure helpers at the top (``network_warnings``, ``find_vjem310_pythons``,
``apply_answers``, ``merge_fetched``) hold the logic. The Tk classes below are
just the UI.
"""

import argparse
import copy
import json
import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from common import config as amadeus_config
from common import netutil

ROLE_PC = "microscope_pc"
ROLE_LAPTOP = "control_pc"
ROLE_LABELS = {ROLE_PC: "Microscope PC (runs DM, PyJEM, relay)",
               ROLE_LAPTOP: "Control laptop (runs the MCP server / console)"}


# ---------------------------------------------------------------------------
# Pure helpers (tested in tests/test_setup_wizard.py)
# ---------------------------------------------------------------------------

def network_warnings(role: str, this_ip: str, other_ip: str) -> list:
    """Plain-language warnings about the chosen addresses."""
    warnings = []
    other = "microscope PC" if role == ROLE_LAPTOP else "laptop"
    if this_ip and other_ip and this_ip == other_ip and not this_ip.startswith("127."):
        warnings.append(f"This computer and the {other} have the same IP ({this_ip}). "
                        f"Enter the {other}'s own address, from `ipconfig` / Network "
                        "settings on that machine.")
    for label, ip in (("This computer", this_ip), (f"The {other}", other_ip)):
        if ip and netutil.is_link_local(ip):
            warnings.append(f"{label} uses {ip}, a link-local (169.254.x.x) address. "
                            "Windows/macOS pick these when DHCP fails, and they can change "
                            "after a reboot or cable replug. Set static addresses instead, "
                            "e.g. PC 10.77.77.1 and laptop 10.77.77.2, mask "
                            "255.255.255.0, no gateway (docs/ETHERNET_SETUP.md, "
                            "steps 2-3).")
    if this_ip and other_ip and this_ip != other_ip \
            and not netutil.same_subnet(this_ip, other_ip):
        warnings.append(f"{this_ip} and {other_ip} are not on the same subnet, so the "
                        "direct Ethernet link will not work. Check both adapters' settings.")
    return warnings


def find_vjem310_pythons(env=None) -> list:
    """Existing candidate vjem310 interpreters, most likely first."""
    env = os.environ if env is None else env
    roots = []
    for var in ("USERPROFILE", "HOME"):
        if env.get(var):
            home = Path(env[var])
            roots += [home / ".conda" / "envs" / "vjem310",
                      home / "anaconda3" / "envs" / "vjem310",
                      home / "miniconda3" / "envs" / "vjem310"]
    for var in ("ProgramData", "PROGRAMDATA"):
        if env.get(var):
            roots += [Path(env[var]) / "Anaconda3" / "envs" / "vjem310",
                      Path(env[var]) / "Miniconda3" / "envs" / "vjem310"]
    roots += [Path("C:/vjem310"), Path("C:/ProgramData/Anaconda3/envs/vjem310")]
    if "vjem310" in sys.prefix.lower():
        roots.insert(0, Path(sys.prefix))
    found, seen = [], set()
    for root in roots:
        for exe in (root / "python.exe", root / "bin" / "python"):
            key = str(exe).lower()
            if key not in seen and exe.is_file():
                seen.add(key)
                found.append(str(exe))
    return found


def check_interpreter(python: str) -> "tuple[bool, str]":
    """Run *python* and report version, pyzmq and PyJEM availability."""
    probe = ("import sys\n"
             "print('Python', sys.version.split()[0])\n"
             "try:\n import zmq; print('pyzmq', zmq.__version__)\n"
             "except Exception as e: print('MISSING pyzmq:', e)\n"
             "try:\n import PyJEM; print('PyJEM', getattr(PyJEM, '__version__', '?').strip())\n"
             "except Exception as e: print('PyJEM not importable:', e)\n"
             "try:\n import tkinter; print('tkinter ok')\n"
             "except Exception as e: print('MISSING tkinter:', e)\n")
    try:
        kwargs = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
        out = subprocess.run([python, "-c", probe], capture_output=True, timeout=30, **kwargs)
    except Exception as exc:
        return False, f"could not run {python}: {exc}"
    text = (out.stdout + out.stderr).decode(errors="replace").strip()
    return out.returncode == 0 and "MISSING pyzmq" not in text, text


def apply_answers(cfg: dict, role: str, answers: dict) -> dict:
    """Return a copy of *cfg* updated with the wizard's answers.

    ``answers`` keys: this_ip, other_ip, adapter_alias, connection_mode,
    repo_path, vjem310_python, hw_mode, hardware_backend ("hw" or "dm": the
    DM EM* fallback), hw_host (bool: own process), listen_all, data_dir,
    timeout_s, ports (dict). The hardware answers edit the ``drivers``
    section (``common.config.set_hardware``). Keys that are missing leave the
    config unchanged, so the other machine's settings survive."""
    out = copy.deepcopy({k: v for k, v in cfg.items() if not k.startswith("_")})
    other_role = ROLE_LAPTOP if role == ROLE_PC else ROLE_PC
    if answers.get("this_ip"):
        out[role]["ip"] = answers["this_ip"]
    if answers.get("other_ip"):
        out[other_role]["ip"] = answers["other_ip"]
    if "connection_mode" in answers:
        out["connection"]["mode"] = answers["connection_mode"]
    if "ports" in answers:
        out["ports"].update(answers["ports"])
    if role == ROLE_PC:
        for key in ("adapter_alias", "repo_path", "vjem310_python"):
            if key in answers:
                out["microscope_pc"][key] = answers[key]
        if "listen_all" in answers:
            out["microscope_pc"]["listen_ip"] = "*" if answers["listen_all"] \
                else out["microscope_pc"]["ip"]
        amadeus_config.set_hardware(
            out, mode=answers.get("hw_mode"),
            host=None if "hw_host" not in answers
            else ("process" if answers["hw_host"] else "in_process"),
            via_dm=None if "hardware_backend" not in answers
            else answers["hardware_backend"] == "dm")
    else:
        if answers.get("data_dir"):
            out["control_pc"]["data_dir"] = answers["data_dir"]
        if answers.get("timeout_s"):
            out["client"]["timeout_s"] = int(answers["timeout_s"])
    return out


def detect_vjem310_python() -> str:
    """This interpreter if it is vjem310 (AMADEUS.bat ran us), else the most
    likely installed one, else empty."""
    if "vjem310" in sys.prefix.lower():
        return sys.executable
    found = find_vjem310_pythons()
    return found[0] if found else ""


def prepare_pc(cfg: dict) -> list:
    """The machine-side steps after saving a PC config: persist AMADEUS_HOME
    for DM, and create the test-data folder. Returns messages."""
    messages = []
    repo = Path(cfg["microscope_pc"].get("repo_path") or amadeus_config.REPO_ROOT)
    ok, msg = set_amadeus_home(repo)
    if ok:
        messages.append(msg)
    try:
        (repo / "test-data").mkdir(exist_ok=True)
    except OSError as exc:
        messages.append(f"could not create {repo / 'test-data'}: {exc}")
    return messages


def restore_from_profile(path, keep_online: bool = False,
                         repo: "Path | None" = None) -> "tuple[dict, dict]":
    """Load an instrument profile and fill in this machine's paths.
    Returns ``(config, info)``; nothing is saved."""
    profile, info = amadeus_config.load_profile(path)
    cfg = amadeus_config.restore_profile(profile, repo or amadeus_config.REPO_ROOT,
                                         detect_vjem310_python(), keep_online=keep_online)
    return cfg, info


def set_amadeus_home(repo: Path) -> "tuple[bool, str]":
    """Persist AMADEUS_HOME for the current Windows user (read by
    scripts/start_amadeus_dm.py when DM gives scripts no __file__)."""
    if sys.platform != "win32":
        return False, "not Windows"
    try:
        subprocess.run(["setx", "AMADEUS_HOME", str(repo)], capture_output=True,
                       timeout=30, creationflags=0x08000000, check=True)
        return True, f"AMADEUS_HOME={repo} (restart DM once so it sees this)"
    except Exception as exc:
        return False, f"could not set AMADEUS_HOME: {exc}"


def fetch_config_from_pc(host: str, port: int = 9875, timeout_ms: int = 5000) -> dict:
    """Ask the running AMADEUS server on the microscope PC for its config."""
    import zmq
    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.REQ)
    sock.setsockopt(zmq.LINGER, 0)
    sock.setsockopt(zmq.RCVTIMEO, timeout_ms)
    sock.setsockopt(zmq.SNDTIMEO, timeout_ms)
    try:
        sock.connect(f"tcp://{host}:{port}")
        sock.send_json({"cmd": "get_config", "params": {}, "request_id": "setup"})
        reply = sock.recv_json()
    except zmq.Again:
        route = netutil.check_route(host)
        raise RuntimeError(
            f"no answer from {host}:{port}. "
            + (f"Wrong adapter: {route}" if route else
               "Is the AMADEUS server running on the PC (launcher shows it green)? "
               "If it is, run 'Network doctor' there."))
    finally:
        sock.close()
    if reply.get("status") != "ok":
        raise RuntimeError(reply.get("error", "unknown error"))
    return reply["data"]["config"]


def merge_fetched(remote: dict, local: dict, laptop_ip: "str | None" = None,
                  reached_ip: "str | None" = None) -> dict:
    """The PC's config, with this laptop's own settings kept.

    *reached_ip* is the address we actually reached the PC on. It replaces
    ``microscope_pc.ip`` in direct mode, since that is provably the right one
    from here."""
    out = amadeus_config._merge(amadeus_config.DEFAULTS,
                                amadeus_config.migrate(copy.deepcopy(remote)))
    out["control_pc"]["data_dir"] = local["control_pc"]["data_dir"]
    out["client"] = copy.deepcopy(local["client"])
    if laptop_ip:
        out["control_pc"]["ip"] = laptop_ip
    if reached_ip and out["connection"]["mode"] == "direct":
        out["microscope_pc"]["ip"] = reached_ip
    return out


def pick_local_ip(pc_ip: str) -> "str | None":
    """This machine's address on the same subnet as the PC, if any."""
    for _, ip in netutil.local_ipv4_addresses():
        if netutil.same_subnet(ip, pc_ip):
            return ip
    return None


# ---------------------------------------------------------------------------
# Tk UI
# ---------------------------------------------------------------------------

class Wizard:
    """Setup pages. Pass *role* to skip the "which computer?" page (the
    launcher does this for the microscope PC). *root* may be a Tk or a
    Toplevel."""

    def __init__(self, root, cfg: dict, config_path: Path, role: "str | None" = None):
        self.root = root
        self.config_path = config_path
        self.embedded = role is not None
        root.title("AMADEUS Setup")
        root.geometry("760x560")

        on_windows = sys.platform == "win32"
        self.role = tk.StringVar(value=role or (ROLE_PC if on_windows else ROLE_LAPTOP))
        self._load_vars(cfg)

        self.body = ttk.Frame(root, padding=14)
        self.body.pack(fill="both", expand=True)
        nav = ttk.Frame(root, padding=(14, 0, 14, 12))
        nav.pack(fill="x")
        self.back_btn = ttk.Button(nav, text="< Back", command=self.back)
        self.back_btn.pack(side="left")
        self.next_btn = ttk.Button(nav, text="Next >", command=self.next)
        self.next_btn.pack(side="right")
        self.step_label = ttk.Label(nav, foreground="#666")
        self.step_label.pack(side="right", padx=12)

        self.pages = [self.page_network, self.page_machine, self.page_ports,
                      self.page_finish]
        if not self.embedded:
            self.pages.insert(0, self.page_welcome)
        self.index = 0
        self.show()

    def _load_vars(self, cfg: dict):
        """(Re)initialise every field from *cfg*."""
        on_windows = sys.platform == "win32"
        self.cfg = cfg
        self.this_ip = tk.StringVar()
        self.other_ip = tk.StringVar()
        self.adapter_alias = tk.StringVar(value=cfg["microscope_pc"].get("adapter_alias", ""))
        self.connection_mode = tk.StringVar(value=cfg["connection"]["mode"])
        self.repo_path = tk.StringVar(value=cfg["microscope_pc"].get("repo_path")
                                      or (str(amadeus_config.REPO_ROOT) if on_windows else ""))
        pythons = find_vjem310_pythons()
        self.vjem310_python = tk.StringVar(value=cfg["microscope_pc"].get("vjem310_python")
                                           or (pythons[0] if pythons else ""))
        _, hw_spec = amadeus_config.hardware_driver(cfg)
        hw_mode = amadeus_config.hardware_mode(cfg)
        self.hw_mode = tk.StringVar(value=hw_mode if hw_mode in ("online", "offline", "sim")
                                    else "offline")
        self.hardware_backend = tk.StringVar(
            value="dm" if hw_spec.get("kind") == "dm" else "hw")
        self.hw_host = tk.BooleanVar(value=any(
            s.get("kind") in ("jeol", "sim") and s.get("host") == "process"
            for s in (cfg.get("drivers") or {}).values()))
        self.listen_all = tk.BooleanVar(value=cfg["microscope_pc"].get("listen_ip", "*") == "*")
        self.data_dir = tk.StringVar(value=cfg["control_pc"]["data_dir"])
        self.timeout_s = tk.StringVar(value=str(cfg["client"]["timeout_s"]))
        self.port_vars = {k: tk.StringVar(value=str(v)) for k, v in cfg["ports"].items()}
        self.mcp_code = tk.BooleanVar(value=not on_windows)
        self.mcp_desktop = tk.BooleanVar(value=False)

    # ------------------------------------------------------------ helpers
    def _clear(self):
        for child in self.body.winfo_children():
            child.destroy()

    def _title(self, text, sub=None):
        ttk.Label(self.body, text=text, font=("TkDefaultFont", 14, "bold")).pack(anchor="w")
        if sub:
            ttk.Label(self.body, text=sub, wraplength=700, foreground="#444").pack(
                anchor="w", pady=(2, 10))

    def _row(self, parent, label, var, browse=None, width=48):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=3)
        ttk.Label(row, text=label, width=24).pack(side="left")
        ttk.Entry(row, textvariable=var, width=width).pack(side="left", fill="x", expand=True)
        if browse:
            ttk.Button(row, text="Browse…", command=browse).pack(side="left", padx=4)
        return row

    @property
    def is_pc(self):
        return self.role.get() == ROLE_PC

    def show(self):
        self._clear()
        self.pages[self.index]()
        self.back_btn.state(["disabled"] if self.index == 0 else ["!disabled"])
        last = self.index == len(self.pages) - 1
        self.next_btn.config(text="Close" if last else "Next >")
        self.step_label.config(text=f"Step {self.index + 1} of {len(self.pages)}")

    def back(self):
        if self.index > 0:
            self.index -= 1
            self.show()

    def next(self):
        if self.index == len(self.pages) - 1:
            self.root.destroy()
            return
        page = self.pages[self.index]
        if page == self.page_network and not self._validate_network():
            return
        if page == self.page_ports and not self._validate_ports():
            return
        self.index += 1
        self.show()

    # -------------------------------------------------------------- pages
    def page_welcome(self):
        self._title("Welcome to AMADEUS setup",
                    "This writes one file, amadeus_config.json, with every IP address, port "
                    "and path AMADEUS needs. Run the wizard on both computers and use the "
                    "same file on each.")
        box = ttk.LabelFrame(self.body, text="Which computer is this?", padding=10)
        box.pack(fill="x", pady=8)
        for role, label in ROLE_LABELS.items():
            ttk.Radiobutton(box, text=label, value=role, variable=self.role).pack(anchor="w")
        src = self.cfg.get("_path")
        info = f"Editing existing config: {src}" if src else "No config yet. Starting from defaults."
        ttk.Label(self.body, text=info, foreground="#444").pack(anchor="w", pady=(10, 2))
        ttk.Button(self.body, text="Import a config from the other computer…",
                   command=self.import_config).pack(anchor="w")

    def import_config(self):
        path = filedialog.askopenfilename(filetypes=[("AMADEUS config", "*.json")])
        if not path:
            return
        try:
            cfg = amadeus_config.load(path)
        except amadeus_config.ConfigError as exc:
            messagebox.showerror("AMADEUS", str(exc))
            return
        # Saving still goes to this machine's checkout, not the import location.
        cfg["_path"] = f"imported from {path}"
        self._load_vars(cfg)
        self.show()

    def page_network(self):
        other = "Laptop" if self.is_pc else "Microscope PC"
        self._title("Network",
                    "Pick this computer's address on the Ethernet link to the other "
                    f"computer, then enter the {other.lower()}'s address.")
        adapters = netutil.local_ipv4_addresses()
        own_key = self.role.get()
        other_key = ROLE_LAPTOP if self.is_pc else ROLE_PC
        ips = [ip for _, ip in adapters]
        saved = self.cfg[own_key]["ip"]
        # Only a never-configured setup (defaults, not restored, no file yet)
        # gets an adapter picked for it.
        fresh = saved.startswith("127.") and not self.cfg.get("_path") \
            and not self.cfg.get("_restored")
        if not self.this_ip.get():
            # Keep a configured address even if it's missing here (a restored
            # profile): the laptop's config points at it.
            self.this_ip.set(saved if (saved in ips or not fresh) else (ips[0] if ips else saved))
        if not self.other_ip.get():
            self.other_ip.set(self.cfg[other_key]["ip"])

        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=3)
        ttk.Label(row, text="This computer's IP", width=24).pack(side="left")
        choices = [f"{ip}   ({name})" for name, ip in adapters]
        combo = ttk.Combobox(row, values=choices, width=46)
        combo.set(self.this_ip.get())
        combo.pack(side="left")

        def on_pick(_=None):
            text = combo.get().split()[0] if combo.get().strip() else ""
            self.this_ip.set(text)
            for name, ip in adapters:
                if ip == text and self.is_pc:
                    self.adapter_alias.set(name)
            refresh()
        combo.bind("<<ComboboxSelected>>", on_pick)
        combo.bind("<FocusOut>", on_pick)

        self._row(self.body, f"{other} IP" + (" (optional)" if self.is_pc else ""),
                  self.other_ip)
        if not self.is_pc:
            fetch = ttk.Frame(self.body)
            fetch.pack(fill="x", pady=(2, 4))
            ttk.Button(fetch, text="Fetch settings from the microscope PC",
                       command=self.fetch_from_pc).pack(side="left")
            self.fetch_msg = ttk.Label(fetch, foreground="#444", wraplength=480)
            self.fetch_msg.pack(side="left", padx=8)
        mode = ttk.LabelFrame(self.body, text="Connection", padding=8)
        mode.pack(fill="x", pady=8)
        ttk.Radiobutton(mode, text="Direct Ethernet (laptop connects straight to the PC)",
                        value="direct", variable=self.connection_mode).pack(anchor="w")
        ttk.Radiobutton(mode, text="SSH tunnel (only if SSH to the PC already works)",
                        value="ssh", variable=self.connection_mode).pack(anchor="w")

        self.net_msg = ttk.Label(self.body, wraplength=700, foreground="#b7791f",
                                 justify="left")
        self.net_msg.pack(anchor="w", pady=6)
        self._local_ips = ips
        tests = ttk.Frame(self.body)
        tests.pack(anchor="w")
        ttk.Button(tests, text=f"Test reaching the {other.lower()}",
                   command=self.test_reach).pack(side="left")
        self.reach_msg = ttk.Label(tests, foreground="#444", wraplength=520, justify="left")
        self.reach_msg.pack(side="left", padx=8)

        def refresh(*_):
            msgs = network_warnings(self.role.get(), self.this_ip.get().strip(),
                                    self.other_ip.get().strip())
            mine = self.this_ip.get().strip()
            if mine and not mine.startswith("127.") and mine not in self._local_ips:
                msgs.insert(0, f"No adapter on this computer has {mine} yet. Set it as a "
                               "static IPv4 address on the Ethernet adapter (Settings → "
                               "Network & internet → Ethernet → IP assignment → Manual), or "
                               "pick one of this computer's addresses above.")
            self.net_msg.config(text="\n\n".join("⚠ " + m for m in msgs))
        self.other_ip.trace_add("write", refresh)
        refresh()

    def fetch_from_pc(self):
        host = self.other_ip.get().strip()
        port = int(self.port_vars["relay"].get() or 9875)
        if self.connection_mode.get() == "ssh":
            host, port = "127.0.0.1", self.cfg["connection"]["ssh_local_ports"]["relay"]
        self.fetch_msg.config(text=f"asking {host}:{port}…", foreground="#444")

        def run():
            try:
                remote = fetch_config_from_pc(host, port)
            except Exception as exc:
                msg, ok = str(exc), False
            else:
                local = apply_answers(self.cfg, ROLE_LAPTOP, self.answers())
                merged = merge_fetched(remote, local, self.this_ip.get().strip() or None,
                                       reached_ip=None if host == "127.0.0.1" else host)
                merged["_path"] = self.cfg.get("_path")
                msg, ok = "Got the PC's settings. Check them on the next pages.", True

            def done():
                if ok:
                    this_ip = self.this_ip.get()
                    self._load_vars(merged)
                    self.this_ip.set(this_ip)
                    self.show()
                    self.fetch_msg.config(text=msg, foreground="#2e9d4f")
                else:
                    self.fetch_msg.config(text=msg, foreground="#c0392b")
            self.root.after(0, done)
        threading.Thread(target=run, daemon=True).start()

    def test_reach(self):
        host = self.other_ip.get().strip()
        self.reach_msg.config(text="testing…")

        def run():
            parts = ["ping ok" if netutil.ping(host) else
                     "no ping reply (Windows often blocks ping; not conclusive)"]
            if not self.is_pc:
                ok, msg = netutil.tcp_probe(host, int(self.port_vars["relay"].get() or 9875))
                parts.append(f"relay port: {msg}")
                route = netutil.check_route(host, self.this_ip.get().strip() or None)
                if route:
                    parts.append("wrong adapter: " + route)
            self.root.after(0, lambda: self.reach_msg.config(text="; ".join(parts)))
        threading.Thread(target=run, daemon=True).start()

    def _validate_network(self):
        import ipaddress
        other = self.other_ip.get().strip()
        try:
            ipaddress.ip_address(self.this_ip.get().strip())
            if other or not self.is_pc:          # laptop IP is optional on the PC
                ipaddress.ip_address(other)
        except ValueError:
            messagebox.showerror("AMADEUS", "IP fields must be IPv4 addresses, "
                                            "e.g. 10.77.77.1.")
            return False
        if self.this_ip.get().strip() == self.other_ip.get().strip() and \
                not self.this_ip.get().startswith("127."):
            return messagebox.askyesno("AMADEUS", "Both computers have the same IP. "
                                                  "Continue anyway?")
        return True

    def page_machine(self):
        if self.is_pc:
            self._page_pc()
        else:
            self._page_laptop()

    def _page_pc(self):
        self._title("Microscope PC", "Where things live on this PC, and how AMADEUS "
                                     "should drive the hardware. AMADEUS.bat starts "
                                     "with the vjem310 python.exe below.")
        self._row(self.body, "AMADEUS folder", self.repo_path,
                  browse=lambda: self._pick_dir(self.repo_path))
        self._row(self.body, "vjem310 python.exe", self.vjem310_python,
                  browse=lambda: self._pick_file(self.vjem310_python))
        test = ttk.Frame(self.body)
        test.pack(fill="x")
        ttk.Button(test, text="Test interpreter", command=self.test_python).pack(side="left")
        self.py_msg = ttk.Label(test, foreground="#444", justify="left")
        self.py_msg.pack(side="left", padx=8)

        inst = ttk.LabelFrame(self.body, text="Instrument parameters (saved in the config "
                                              "and the instrument profile)", padding=8)
        inst.pack(fill="x", pady=(8, 0))
        self.inst_msg = ttk.Label(inst, text=amadeus_config.instrument_summary(self.cfg),
                                  wraplength=520)
        self.inst_msg.pack(side="left")
        ttk.Button(inst, text="Load calibration file…",
                   command=self.load_calibration).pack(side="right")

        hw = ttk.LabelFrame(self.body, text="Hardware mode", padding=8)
        hw.pack(fill="x", pady=8)
        for value, label in (("offline", "Offline: PyJEM simulator (safe; start here)"),
                             ("online", "Online: controls the real microscope"),
                             ("sim", "Sim: AMADEUS's own simulator, no PyJEM needed")):
            ttk.Radiobutton(hw, text=label, value=value, variable=self.hw_mode).pack(anchor="w")
        ttk.Checkbutton(hw, text="Run the hardware driver in its own process (a crash or "
                                 "hang in PyJEM can't stop the server; it is restarted)",
                        variable=self.hw_host).pack(anchor="w", pady=(4, 0))
        if self.cfg.get("drivers"):
            ttk.Label(hw, foreground="#a05000", wraplength=640,
                      text="This config has a 'drivers' section, which sets the drivers "
                           "explicitly; the choices above don't change it:\n"
                           + "\n".join(amadeus_config.describe_drivers(self.cfg))
                      ).pack(anchor="w", pady=(4, 0))

        route = ttk.LabelFrame(self.body, text="Hardware command routing (operator setting)",
                               padding=8)
        route.pack(fill="x")
        ttk.Radiobutton(route, text="PyJEM (normal)", value="hw",
                        variable=self.hardware_backend).pack(anchor="w")
        ttk.Radiobutton(route, text="DM EM* scripting (fallback)", value="dm",
                        variable=self.hardware_backend).pack(anchor="w")
        ttk.Checkbutton(self.body, text="AMADEUS server listens on all network adapters "
                                        "(untick to accept only the laptop-facing adapter)",
                        variable=self.listen_all).pack(anchor="w", pady=6)

    def _page_laptop(self):
        self._title("Control laptop", "Where session data is stored, and which apps "
                                      "should launch the AMADEUS MCP server.")
        self._row(self.body, "Data folder", self.data_dir,
                  browse=lambda: self._pick_dir(self.data_dir))
        ttk.Label(self.body, text="The MCP server, console and monitor all share this folder. "
                                  "Relative paths are inside the AMADEUS checkout.",
                  foreground="#666", wraplength=700).pack(anchor="w")
        self._row(self.body, "Command timeout (s)", self.timeout_s, width=10)
        mcp = ttk.LabelFrame(self.body, text="Register the MCP server on Finish", padding=8)
        mcp.pack(fill="x", pady=10)
        ttk.Checkbutton(mcp, text="Claude Code (.mcp.json in this checkout)",
                        variable=self.mcp_code).pack(anchor="w")
        ttk.Checkbutton(mcp, text="Claude Desktop", variable=self.mcp_desktop).pack(anchor="w")

    def load_calibration(self):
        """Fold a calibration JSON (drivers/calibration.py format) into the
        config's instrument section, so the profile carries it."""
        path = filedialog.askopenfilename(filetypes=[("Calibration JSON", "*.json")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError) as exc:
            messagebox.showerror("AMADEUS", f"Can't read {path}: {exc}")
            return
        self.cfg["instrument"] = amadeus_config._merge(self.cfg.get("instrument") or {}, data)
        for spec in (self.cfg.get("drivers") or {}).values():
            spec.pop("calibration", None)              # now inline; no separate file
        self.inst_msg.config(text=amadeus_config.instrument_summary(self.cfg))

    def test_python(self):
        path = self.vjem310_python.get().strip()
        self.py_msg.config(text="testing…")

        def run():
            ok, text = check_interpreter(path)
            self.root.after(0, lambda: self.py_msg.config(
                text=text, foreground="#2e9d4f" if ok else "#c0392b"))
        threading.Thread(target=run, daemon=True).start()

    def _pick_dir(self, var):
        path = filedialog.askdirectory(initialdir=var.get() or None)
        if path:
            var.set(path)

    def _pick_file(self, var):
        path = filedialog.askopenfilename(initialdir=str(Path(var.get()).parent)
                                          if var.get() else None)
        if path:
            var.set(path)

    def page_ports(self):
        self._title("Ports", "The defaults are fine unless another program already uses "
                             "one of these ports.")
        labels = {"relay": "AMADEUS server (laptop connects)",
                  "stream": "Live stream (laptop subscribes)",
                  "dm": "DM server (internal)",
                  "dm_stream": "DM live stream (internal)"}
        for key, label in labels.items():
            self._row(self.body, label, self.port_vars[key], width=10)

    def _validate_ports(self):
        try:
            ports = {k: int(v.get()) for k, v in self.port_vars.items()}
        except ValueError:
            messagebox.showerror("AMADEUS", "Ports must be whole numbers.")
            return False
        if len(set(ports.values())) != len(ports) or \
                not all(1 <= p <= 65535 for p in ports.values()):
            messagebox.showerror("AMADEUS", "Ports must be distinct and between 1 and 65535.")
            return False
        return True

    def answers(self) -> dict:
        ans = {
            "this_ip": self.this_ip.get().strip(),
            "other_ip": self.other_ip.get().strip(),
            "connection_mode": self.connection_mode.get(),
            "ports": {k: int(v.get()) for k, v in self.port_vars.items()},
        }
        if self.is_pc:
            ans.update(adapter_alias=self.adapter_alias.get(),
                       repo_path=self.repo_path.get().strip(),
                       vjem310_python=self.vjem310_python.get().strip(),
                       hw_mode=self.hw_mode.get(),
                       hardware_backend=self.hardware_backend.get(),
                       hw_host=self.hw_host.get(),
                       listen_all=self.listen_all.get())
        else:
            ans.update(data_dir=self.data_dir.get().strip(), timeout_s=self.timeout_s.get())
        return ans

    def page_finish(self):
        new_cfg = apply_answers(self.cfg, self.role.get(), self.answers())
        self.new_cfg = new_cfg
        self._title("Review and save", f"Saves to {self.config_path}")
        preview = scrolledtext.ScrolledText(self.body, height=12, font=("Courier", 9))
        preview.insert("1.0", json.dumps(new_cfg, indent=2))
        preview.config(state="disabled")
        preview.pack(fill="both", expand=True)
        buttons = ttk.Frame(self.body)
        buttons.pack(fill="x", pady=6)
        ttk.Button(buttons, text="Save", command=self.save).pack(side="left")
        ttk.Button(buttons, text="Save a copy for the other computer…",
                   command=self.save_copy).pack(side="left", padx=6)
        if not self.is_pc:
            ttk.Button(buttons, text="Test connection", command=self.test_connection
                       ).pack(side="left", padx=6)
        self.result = scrolledtext.ScrolledText(self.body, height=9, font=("Courier", 9))
        self.result.pack(fill="both", expand=True)

    def _log(self, text):
        self.result.insert("end", text + "\n")
        self.result.see("end")

    def save(self):
        try:
            path = amadeus_config.save(self.new_cfg, self.config_path)
        except amadeus_config.ConfigError as exc:
            messagebox.showerror("AMADEUS", str(exc))
            return
        self._log(f"Saved {path}")
        if self.is_pc:
            for msg in prepare_pc(self.new_cfg):
                self._log(msg)
            self._log("Next: " + ("close this window to return to the launcher"
                                  if self.embedded else "double-click AMADEUS.bat")
                      + ". In DM, run scripts\\start_amadeus_dm.py.")
            if amadeus_config.is_online(self.new_cfg):
                self._log("ONLINE mode: run python -m drivers.smoke --driver <name> first "
                          "and compare the stage readout with the TEM Center console.")
        else:
            self._register_mcp(path)
            self._log("Next: press 'Test connection'. The AMADEUS server must be running "
                      "on the microscope PC.")

    def _register_mcp(self, config_path: Path):
        from setup_wizard import mcp_config
        entry = mcp_config.server_entry(config_path)
        targets = []
        if self.mcp_code.get():
            targets.append(amadeus_config.REPO_ROOT / ".mcp.json")
        if self.mcp_desktop.get():
            desktop = mcp_config.claude_desktop_config_path()
            if desktop:
                targets.append(desktop)
        for target in targets:
            try:
                mcp_config.write_entry(target, entry)
                self._log(f"Registered MCP server in {target} (restart that app)")
            except Exception as exc:
                self._log(f"Could not update {target}: {exc}")

    def save_copy(self):
        path = filedialog.asksaveasfilename(initialfile=amadeus_config.CONFIG_FILENAME,
                                            defaultextension=".json")
        if path:
            try:
                amadeus_config.save(self.new_cfg, path)
                self._log(f"Copy saved to {path}. On the other computer, put it in the "
                          "AMADEUS folder (or use Import… in its wizard).")
            except amadeus_config.ConfigError as exc:
                messagebox.showerror("AMADEUS", str(exc))

    def test_connection(self):
        try:
            cfg = amadeus_config.load(self.config_path)
        except amadeus_config.ConfigError as exc:
            self._log(f"Save first: {exc}")
            return
        self._log("Running connection checks…")

        def run():
            try:
                from client.diagnostics import run_checks
            except ImportError as exc:
                self.root.after(0, lambda e=exc: self._log(
                    f"Install the client first (uv pip install -e .): {e}"))
                return
            lines = []
            run_checks(cfg, out=lines.append)
            self.root.after(0, lambda: self._log("\n".join(lines)))
        threading.Thread(target=run, daemon=True).start()


def setup_laptop_from_pc(pc: str, port: int = 9875, data_dir: "str | None" = None,
                         register_mcp: bool = True, claude_desktop: bool = False,
                         config_path: "Path | None" = None, out=print) -> Path:
    """Non-interactive laptop setup: fetch the PC's config, keep this laptop's
    settings, save, and register the MCP server."""
    from setup_wizard import mcp_config
    target = config_path or amadeus_config.REPO_ROOT / amadeus_config.CONFIG_FILENAME
    local = amadeus_config.load_or_none(target if Path(target).is_file() else None) \
        or amadeus_config.load_defaults()
    if data_dir:
        local["control_pc"]["data_dir"] = data_dir
    remote = fetch_config_from_pc(pc, port)
    laptop_ip = pick_local_ip(pc)
    cfg = merge_fetched(remote, local, laptop_ip,
                        reached_ip=None if pc.startswith("127.") else pc)
    for warning in network_warnings(ROLE_LAPTOP, cfg["control_pc"]["ip"],
                                    cfg["microscope_pc"]["ip"]):
        out("warning: " + warning)
    path = amadeus_config.save(cfg, target)
    out(f"Saved {path}")
    out(f"  microscope PC {cfg['microscope_pc']['ip']} ({cfg['connection']['mode']}), "
        f"this laptop {cfg['control_pc']['ip']}, data dir {cfg['control_pc']['data_dir']}")
    if register_mcp:
        entry = mcp_config.server_entry(path)
        targets = [amadeus_config.REPO_ROOT / ".mcp.json"]
        if claude_desktop and mcp_config.claude_desktop_config_path():
            targets.append(mcp_config.claude_desktop_config_path())
        for t in targets:
            mcp_config.write_entry(t, entry)
            out(f"Registered MCP server in {t}")
    out("Next: amadeus-check")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="AMADEUS setup")
    amadeus_config.add_config_argument(parser)
    parser.add_argument("--pc", default=None,
                        help="laptop, non-interactive: fetch the config from the AMADEUS "
                             "server at this IP (127.0.0.1 with an SSH tunnel)")
    parser.add_argument("--port", type=int, default=None,
                        help="relay port for --pc (default 9875; 29875 with SSH)")
    parser.add_argument("--data-dir", default=None, help="with --pc: session data folder")
    parser.add_argument("--no-mcp", action="store_true",
                        help="with --pc: don't register the MCP server")
    parser.add_argument("--claude-desktop", action="store_true",
                        help="with --pc: also register with Claude Desktop")
    parser.add_argument("--export", metavar="PROFILE", default=None,
                        help="microscope PC: write an instrument profile (connection "
                             "details + calibration, no machine-local paths)")
    parser.add_argument("--restore", metavar="PROFILE", default=None,
                        help="microscope PC: rebuild amadeus_config.json from an "
                             "instrument profile")
    parser.add_argument("--keep-online", action="store_true",
                        help="with --restore: keep Online hardware mode (default: "
                             "restore Online profiles as Offline until the smoke test passes)")
    args = parser.parse_args()

    if args.export:
        try:
            cfg = amadeus_config.load(args.amadeus_config)
            path = amadeus_config.export_profile(cfg, args.export)
        except amadeus_config.ConfigError as exc:
            sys.exit(f"export failed: {exc}")
        print(f"Wrote instrument profile {path}")
        print(f"  {amadeus_config.instrument_summary(cfg)}")
        return
    if args.restore:
        try:
            cfg, info = restore_from_profile(args.restore, keep_online=args.keep_online)
            target = Path(args.amadeus_config) if args.amadeus_config else \
                amadeus_config.REPO_ROOT / amadeus_config.CONFIG_FILENAME
            path = amadeus_config.save(cfg, target)
        except amadeus_config.ConfigError as exc:
            sys.exit(f"restore failed: {exc}")
        print(f"Restored {path} from {args.restore}"
              + (f" (exported {info['exported']} on {info['exported_from']})"
                 if info.get("exported") else ""))
        for msg in prepare_pc(cfg):
            print("  " + msg)
        from setup_wizard import readiness
        print("Readiness:")
        for level, title, detail in readiness.readiness(cfg, readiness.gather(cfg)):
            print(f"  [{level}] {title}" + (f": {detail}" if detail else ""))
        return

    if args.pc:
        try:
            setup_laptop_from_pc(args.pc, args.port or 9875, args.data_dir,
                                 register_mcp=not args.no_mcp,
                                 claude_desktop=args.claude_desktop,
                                 config_path=Path(args.amadeus_config)
                                 if args.amadeus_config else None)
        except Exception as exc:
            sys.exit(f"setup failed: {exc}")
        return

    try:
        cfg = amadeus_config.load_or_none(args.amadeus_config) \
            or amadeus_config.load_defaults()
    except amadeus_config.ConfigError as exc:
        print(f"Existing config is invalid. Starting from defaults.\n{exc}")
        cfg = amadeus_config.load_defaults()
    target = Path(cfg["_path"]) if cfg.get("_path") else \
        amadeus_config.REPO_ROOT / amadeus_config.CONFIG_FILENAME
    root = tk.Tk()
    Wizard(root, cfg, target)
    root.mainloop()


if __name__ == "__main__":
    main()
