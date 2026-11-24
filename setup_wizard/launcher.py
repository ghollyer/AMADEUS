"""AMADEUS Launcher: the one window on the microscope PC.

    double-click AMADEUS.bat        # or: python -m setup_wizard.launcher
    python -m setup_wizard.launcher --enable-firewall-fix

* First run (no amadeus_config.json): restore from an instrument profile, or
  set up from scratch. **Export profile…** writes that profile: connection
  details, modes and the instrument calibration, minus machine-local paths. It
  is all you need to reinstall AMADEUS on this microscope.
* **Readiness** lists what is still missing after an install (static IP,
  DM script, smoke test, firewall), with the exact fix for each.
* **AMADEUS server** (relay + in-process hardware backend + live-stream proxy)
  runs as one subprocess of this interpreter, with every setting taken from
  the config.
* **DM server** must run inside Digital Micrograph. This window says what to
  open there and shows when it is up.
* Status lights come from the server's own ``ping`` reply, which reports both
  backends. **Settings…** reopens the setup. **Network doctor** checks
  listeners and Windows Firewall (read-only). The one-click firewall rule only
  appears when ``experimental.firewall_autofix`` is true or with
  ``--enable-firewall-fix``.

Stdlib + optional pyzmq (for health pings), Python 3.10-compatible.
"""

import argparse
import datetime
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

from common import config as amadeus_config
from setup_wizard import netdiag, readiness

COLORS = {"up": "#2e9d4f", "busy": "#d9a400", "down": "#c0392b", "idle": "#9a9a9a",
          "none": "#d8d8d8"}
POLL_S = 3.0
# The relay's ping probes each backend with a 2 s timeout before replying.
RELAY_PING_TIMEOUT_MS = 3500


def _zmq_request(address: str, cmd: str, timeout_ms: int = 1500):
    """One-shot REQ round trip. Returns the reply dict, or None on timeout.
    Raises ImportError if pyzmq is unavailable."""
    import zmq
    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.REQ)
    sock.setsockopt(zmq.LINGER, 0)
    sock.setsockopt(zmq.RCVTIMEO, timeout_ms)
    sock.setsockopt(zmq.SNDTIMEO, timeout_ms)
    try:
        sock.connect(address)
        sock.send_json({"cmd": cmd, "params": {}, "request_id": "launcher"})
        return sock.recv_json()
    except zmq.Again:
        return None
    finally:
        sock.close()


def _listening(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def service_state(port: int, cmd: str = "ping", host: str = "127.0.0.1",
                  timeout_ms: int = 1500):
    """``(state, reply)``. *state* is 'up' (answers), 'busy' (listening but
    no reply in time, e.g. a long acquisition) or 'down' (nothing listening)."""
    if not _listening(port, host):
        return "down", None
    try:
        reply = _zmq_request(f"tcp://{host}:{port}", cmd, timeout_ms)
    except ImportError:
        return "up", None   # without pyzmq, "listening" is the best signal we have
    if reply and reply.get("status") == "ok":
        return "up", reply
    return "busy", reply


def health(cfg: dict) -> dict:
    """States for the three lights: dm, server, hw. ``hw`` covers the
    drivers holding the column and stage; ``dm`` the Digital Micrograph
    driver(s). ``none`` = this microscope has no such driver. With a relay
    that reports drivers, ``drivers`` has one state per driver."""
    ports = cfg["ports"]
    listen = cfg["microscope_pc"].get("listen_ip", "*")
    host = "127.0.0.1" if listen == "*" else listen
    server, reply = service_state(ports["relay"], host=host,
                                  timeout_ms=RELAY_PING_TIMEOUT_MS)
    if server == "up" and reply:
        data = reply.get("data", {})
        backends = data.get("backends", {})
        routing = data.get("routing")

        def light(names):
            if not names:
                return "none"
            return "up" if all(backends.get(n, {}).get("ok") for n in names) else "down"
        routing = routing or {}
        dm_names = [n for n, i in backends.items() if i.get("kind") == "dm"]
        hw_names = sorted(set(routing.get("column", []) + routing.get("stage", [])))
        return {"server": "up", "dm": light(dm_names), "hw": light(hw_names),
                "drivers": {n: light([n]) for n in backends}}
    dm, _ = service_state(ports["dm"])
    return {"server": server, "dm": dm, "hw": "idle"}


def server_command(config_path: str) -> list:
    # The .bat already picked the vjem310 interpreter, so use this one.
    return [sys.executable, "-u", "-m", "relay", "--amadeus-config", config_path]


class ManagedProcess:
    """A child service whose output goes to a queue and a log file."""

    def __init__(self, name: str, argv: list, cwd: Path, log_dir: Path, sink: queue.Queue):
        self.name, self.argv, self.cwd = name, argv, cwd
        self.log_dir, self.sink = log_dir, sink
        self.proc = None

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        if self.running():
            return
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = self.log_dir / f"{self.name}_{stamp}.log"
        env = dict(os.environ, PYTHONUNBUFFERED="1",
                   PYTHONPATH=str(self.cwd) + os.pathsep + os.environ.get("PYTHONPATH", ""))
        kwargs = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
        self.proc = subprocess.Popen(
            self.argv, cwd=str(self.cwd), env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kwargs)
        self.sink.put((self.name, f"$ {' '.join(self.argv)}\n(log: {log_path})\n"))
        threading.Thread(target=self._pump, args=(self.proc, log_path), daemon=True).start()

    def _pump(self, proc, log_path: Path) -> None:
        with open(log_path, "w", encoding="utf-8") as log:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode(errors="replace")
                log.write(line)
                log.flush()
                self.sink.put((self.name, line))
        code = proc.wait()
        self.sink.put((self.name, f"[exited with code {code}]\n"))

    def stop(self, graceful_address: "str | None" = None) -> None:
        """Ask nicely (``shutdown_relay``), then terminate."""
        if not self.running():
            return
        if graceful_address:
            try:
                _zmq_request(graceful_address, "shutdown_relay", timeout_ms=2000)
                self.proc.wait(timeout=5)
                return
            except Exception:
                pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class Launcher:
    def __init__(self, root: tk.Tk, cfg: dict, enable_firewall_fix: bool):
        self.root = root
        self.repo = amadeus_config.REPO_ROOT
        self.out = queue.Queue()
        self.enable_firewall_fix = enable_firewall_fix
        self.server = None
        self._closing = False
        self._last_doctor = None
        self._doctor_findings = None
        self._states = {}
        root.title("AMADEUS Launcher")
        root.geometry("900x620")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.apply_config(cfg)
        threading.Thread(target=self._health_loop, daemon=True).start()
        root.after(150, self._drain)
        root.after(5000, self.show_readiness)   # after the first health poll

    def apply_config(self, cfg: dict) -> None:
        self.cfg = cfg
        self.config_path = cfg["_path"] or str(self.repo / amadeus_config.CONFIG_FILENAME)
        argv = server_command(self.config_path)
        if self.server is None:
            self.server = ManagedProcess("server", argv, self.repo, self.repo / "logs", self.out)
        else:
            self.server.argv = argv
        repo = Path(cfg["microscope_pc"].get("repo_path") or self.repo)
        try:
            (repo / "test-data").mkdir(exist_ok=True)   # used by amadeus-check --image
        except OSError:
            pass
        for child in self.root.winfo_children():
            if not isinstance(child, tk.Toplevel):
                child.destroy()
        self._build()

    # ---------------------------------------------------------------- UI
    def _build(self) -> None:
        pc, ports = self.cfg["microscope_pc"], self.cfg["ports"]
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")
        mode = amadeus_config.hardware_mode(self.cfg)
        ttk.Label(top, text=f"This PC {pc['ip']}  ·  laptop connects to :{ports['relay']} "
                            f"and :{ports['stream']}  ·  hardware: {mode.upper()}",
                  font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Button(top, text="Settings…", command=self.open_settings).pack(side="right")
        ttk.Button(top, text="Export profile…", command=self.export_profile
                   ).pack(side="right", padx=4)
        if amadeus_config.is_online(self.cfg):
            ttk.Label(self.root, padding=(8, 0), foreground="#c0392b",
                      text="ONLINE mode: AMADEUS controls the real instrument. On a new "
                           "install, run python -m drivers.smoke --driver <name> first "
                           "(see Readiness).").pack(fill="x")

        rows = ttk.Frame(self.root, padding=8)
        rows.pack(fill="x")
        self.lights = {}

        def light(row, key):
            canvas = tk.Canvas(rows, width=18, height=18, highlightthickness=0)
            canvas.grid(row=row, column=0, padx=4, pady=3)
            self.lights[key] = (canvas, canvas.create_oval(2, 2, 16, 16, fill=COLORS["idle"]))

        light(0, "dm")
        ttk.Label(rows, text="1. DM server", width=24).grid(row=0, column=1, sticky="w")
        ttk.Label(rows, text="In DM: File > Open scripts\\start_amadeus_dm.py, then Execute"
                  ).grid(row=0, column=2, sticky="w")
        dm_script = Path(pc.get("repo_path") or self.repo) / "scripts" / "start_amadeus_dm.py"
        ttk.Button(rows, text="Copy path", command=lambda: self._copy(str(dm_script))
                   ).grid(row=0, column=3, padx=2)

        light(1, "server")
        ttk.Label(rows, text="2. AMADEUS server", width=24).grid(row=1, column=1, sticky="w")
        buttons = ttk.Frame(rows)
        buttons.grid(row=1, column=2, sticky="w")
        ttk.Button(buttons, text="Start", command=self.start).pack(side="left")
        ttk.Button(buttons, text="Stop", command=self.stop).pack(side="left", padx=4)

        light(2, "hw")
        ttk.Label(rows, text="    hardware", width=24).grid(row=2, column=1, sticky="w")
        ttk.Label(rows, text="; ".join(amadeus_config.describe_drivers(self.cfg)),
                  foreground="#666", wraplength=560, justify="left"
                  ).grid(row=2, column=2, sticky="w")

        bar = ttk.Frame(self.root, padding=(8, 0))
        bar.pack(fill="x")
        ttk.Button(bar, text="Readiness", command=self.show_readiness).pack(side="left")
        ttk.Button(bar, text="Network doctor", command=self.run_doctor).pack(side="left", padx=4)
        ttk.Label(bar, text=amadeus_config.instrument_summary(self.cfg),
                  foreground="#666").pack(side="right")
        if self.enable_firewall_fix or self.cfg["experimental"]["firewall_autofix"]:
            ttk.Button(bar, text="Add firewall rule (admin)…",
                       command=self.fix_firewall).pack(side="left", padx=6)

        self.log = scrolledtext.ScrolledText(self.root, height=24, font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, padx=8, pady=8)
        self.log.tag_config("err", foreground="#c0392b")
        self.log.tag_config("ok", foreground="#2e9d4f")
        self.log.tag_config("warn", foreground="#b7791f")
        self._write("launcher", f"config: {self.config_path}\n"
                                "Start the DM server inside DM, then press Start.\n")

    def _copy(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._write("launcher", f"Copied to clipboard: {text}\n")

    def _write(self, source: str, line: str, tag=None) -> None:
        if tag is None:
            low = line.lower()
            tag = "err" if ("error" in low or "traceback" in low or "fail" in low) else None
        self.log.insert("end", f"[{source}] {line}", tag)
        self.log.see("end")

    def _drain(self) -> None:
        try:
            while True:
                source, payload = self.out.get_nowait()
                if source == "_state":
                    for key, state in payload.items():
                        if key not in self.lights or state not in COLORS:
                            continue           # e.g. the per-driver states
                        canvas, oval = self.lights[key]
                        canvas.itemconfig(oval, fill=COLORS[state])
                else:
                    self._write(source, payload)
        except queue.Empty:
            pass
        except tk.TclError:
            pass   # widgets being rebuilt by apply_config
        if not self._closing:
            self.root.after(150, self._drain)

    # ----------------------------------------------------------- services
    def _server_address(self) -> str:
        listen = self.cfg["microscope_pc"].get("listen_ip", "*")
        host = "127.0.0.1" if listen == "*" else listen
        return f"tcp://{host}:{self.cfg['ports']['relay']}"

    def start(self) -> None:
        try:
            self.server.start()
        except OSError as exc:
            self._write("launcher", f"could not start the AMADEUS server: {exc}\n", "err")

    def stop(self) -> None:
        threading.Thread(target=self.server.stop, args=(self._server_address(),),
                         daemon=True).start()

    def _health_loop(self) -> None:
        last = None
        while not self._closing:
            states = health(self.cfg)
            if states["server"] == "down" and not self.server.running():
                states["server"] = "idle"
            self._states = states
            if states != last:
                self.out.put(("_state", dict(states)))
                last = states
            time.sleep(POLL_S)

    # ----------------------------------------------------------- settings
    def open_settings(self) -> None:
        from setup_wizard.wizard import ROLE_PC, Wizard
        window = tk.Toplevel(self.root)
        window.transient(self.root)
        Wizard(window, self.cfg, Path(self.config_path), role=ROLE_PC)

        def reload(event):
            if event.widget is not window:
                return
            try:
                cfg = amadeus_config.load(self.config_path)
            except amadeus_config.ConfigError as exc:
                self._write("launcher", f"config not reloaded: {exc}\n", "err")
                return
            was_running = self.server.running()
            self.apply_config(cfg)
            if was_running and messagebox.askyesno(
                    "AMADEUS", "Restart the AMADEUS server to apply the new settings?"):
                def restart():
                    self.server.stop(self._server_address())
                    self.server.start()
                threading.Thread(target=restart, daemon=True).start()
        window.bind("<Destroy>", reload)

    # ------------------------------------------------- readiness / profile
    def show_readiness(self) -> None:
        def run():
            facts = readiness.gather(self.cfg, dict(self._states))
            items = readiness.readiness(self.cfg, facts, self._doctor_findings)
            todo = sum(1 for lvl, _, _ in items if lvl == readiness.TODO)
            self.out.put(("ready", "Readiness: " + (f"{todo} step(s) to do\n" if todo
                                                     else "nothing to do\n")))
            for level, title, detail in items:
                self.out.put(("ready", f"{level:4s} {title}\n"))
                if detail and level != readiness.OK:
                    self.out.put(("ready", f"     {detail}\n"))
        threading.Thread(target=run, daemon=True).start()

    def export_profile(self) -> None:
        from tkinter import filedialog
        label = (self.cfg.get("instrument") or {}).get("label") or "instrument"
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in label).strip("-")
        path = filedialog.asksaveasfilename(
            title="Export instrument profile", initialfile=safe + amadeus_config.PROFILE_SUFFIX,
            defaultextension=".json", filetypes=[("AMADEUS profile", "*.json")])
        if not path:
            return
        try:
            written = amadeus_config.export_profile(self.cfg, path)
        except amadeus_config.ConfigError as exc:
            messagebox.showerror("AMADEUS", str(exc))
            return
        self._write("launcher", f"Exported instrument profile to {written}. Keep a copy off "
                                "this PC (USB / lab share): restoring it reinstalls AMADEUS "
                                "here.\n", "ok")

    # ------------------------------------------------------------ doctor
    def run_doctor(self) -> None:
        self._write("doctor", "Querying listeners and Windows Firewall (read-only)…\n")

        def run():
            try:
                data = netdiag.collect(self.cfg)
                findings = netdiag.analyze(self.cfg, data)
                self._last_doctor = data
                self._doctor_findings = findings
            except Exception as exc:
                self.out.put(("doctor", f"failed: {exc}\n"))
                return
            for level, title, detail, fix in findings:
                self.out.put(("doctor", f"{level:4s} {title}\n"))
                if detail:
                    self.out.put(("doctor", f"     {detail}\n"))
                if fix:
                    self.out.put(("doctor", f"     > {fix}\n"))
            self.out.put(("doctor", "Done. Commands after '>' can be copied into an "
                                    "Administrator PowerShell.\n"))
        threading.Thread(target=run, daemon=True).start()

    def fix_firewall(self) -> None:
        if self._last_doctor and netdiag.rule_exists(self._last_doctor):
            messagebox.showinfo("AMADEUS", f"A rule named '{netdiag.RULE_NAME}' already exists. "
                                           "Edit it in wf.msc if it needs changing.")
            return
        cmd = netdiag.firewall_rule_command(self.cfg)
        if not messagebox.askyesno(
                "Add firewall rule",
                "This adds ONE inbound allow rule. Windows will ask for administrator "
                "permission. No existing rule is changed or removed.\n\n" + cmd):
            return
        try:
            netdiag.apply_firewall_rule(self.cfg)
            self._write("doctor", "Requested elevated rule creation. Run 'Network doctor' "
                                  "again to confirm.\n", "ok")
        except Exception as exc:
            self._write("doctor", f"firewall fix failed: {exc}\n", "err")

    def close(self) -> None:
        if self.server.running() and not messagebox.askyesno(
                "AMADEUS", "Stop the AMADEUS server and quit?"):
            return
        self._closing = True
        self.server.stop(self._server_address())
        self.root.destroy()


def first_run_setup() -> "dict | None":
    """No config yet: restore from a profile or set up from scratch. Either
    way the setup pages open (pre-filled for a restore) so the operator
    reviews and saves. Returns the saved config, or None if cancelled."""
    from tkinter import filedialog
    from setup_wizard.wizard import ROLE_PC, Wizard, restore_from_profile
    root = tk.Tk()
    root.title("AMADEUS: first run")
    choice = {"value": None}
    frame = ttk.Frame(root, padding=20)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="AMADEUS isn't set up on this PC yet.",
              font=("TkDefaultFont", 13, "bold")).pack(anchor="w")
    ttk.Label(frame, wraplength=460, foreground="#444",
              text="Reinstalling on a microscope that already ran AMADEUS? Restore its "
                   "instrument profile (exported from the launcher). It brings back the "
                   "addresses, ports, modes and calibration.").pack(anchor="w", pady=(6, 14))

    def pick(value):
        choice["value"] = value
        for child in root.winfo_children():
            child.destroy()
        root.quit()
    ttk.Button(frame, text="Restore from a profile…",
               command=lambda: pick("restore")).pack(fill="x")
    ttk.Button(frame, text="Set up from scratch",
               command=lambda: pick("scratch")).pack(fill="x", pady=6)
    root.mainloop()

    target = amadeus_config.REPO_ROOT / amadeus_config.CONFIG_FILENAME
    cfg = amadeus_config.load_defaults()
    if choice["value"] is None:
        root.destroy()
        return None
    if choice["value"] == "restore":
        path = filedialog.askopenfilename(
            parent=root, title="Instrument profile",
            filetypes=[("AMADEUS profile", "*.json"), ("All files", "*.*")])
        if path:
            try:
                profile, _ = amadeus_config.load_profile(path)
                keep_online = True
                if amadeus_config.is_online(profile):
                    keep_online = not messagebox.askyesno(
                        "AMADEUS", "This profile drives the real column (Online mode).\n\n"
                        "Start in Offline mode until the driver smoke test has passed on "
                        "this install? (recommended)", parent=root)
                cfg, info = restore_from_profile(path, keep_online=keep_online)
                cfg["_path"] = None
                cfg["_restored"] = True
            except amadeus_config.ConfigError as exc:
                messagebox.showerror("AMADEUS", f"{exc}\n\nContinuing with a fresh setup.",
                                     parent=root)
    Wizard(root, cfg, target, role=ROLE_PC)
    root.mainloop()
    try:
        root.destroy()
    except tk.TclError:
        pass
    return amadeus_config.load_or_none()


def main() -> None:
    parser = argparse.ArgumentParser(description="AMADEUS microscope-PC launcher")
    amadeus_config.add_config_argument(parser)
    parser.add_argument("--enable-firewall-fix", action="store_true",
                        help="show the one-click firewall rule button "
                             "(experimental; off by default)")
    args = parser.parse_args()
    try:
        cfg = amadeus_config.load_or_none(args.amadeus_config)
    except amadeus_config.ConfigError as exc:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("AMADEUS", f"{exc}\n\nFix the file or delete it to run setup again.")
        return
    if cfg is None:
        cfg = first_run_setup()
        if cfg is None:
            return
    root = tk.Tk()
    Launcher(root, cfg, args.enable_firewall_fix)
    root.mainloop()


if __name__ == "__main__":
    main()
