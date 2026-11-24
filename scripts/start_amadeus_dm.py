"""Start the AMADEUS DM server — run this INSIDE Digital Micrograph.

In DM: File > Open... this file, make sure the script window language is
Python, then press Execute. Do this once per DM session; the script keeps
running (that is expected — it is the server).

Nothing here needs editing. Ports come from ``amadeus_config.json`` (written by
the setup in AMADEUS.bat). The AMADEUS checkout is found automatically from:
    1. this file's location (when DM provides ``__file__``)
    2. the AMADEUS_HOME environment variable (AMADEUS.bat's setup sets it;
       restart DM once after running the wizard)
    3. common locations: C:\\AMADEUS, %USERPROFILE%\\AMADEUS,
       %USERPROFILE%\\Documents\\AMADEUS

Deliberately self-contained (stdlib + the dm_server package only): the DM
server does not import ``common/``.
"""

import json
import os
import sys

CONFIG_FILENAME = "amadeus_config.json"
DEFAULT_PORTS = {"dm": 9876, "dm_stream": 9879}


def _looks_like_repo(path):
    return bool(path) and os.path.isfile(os.path.join(path, "dm_server", "server.py"))


def find_repo_root():
    candidates = []
    try:
        candidates.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    except NameError:
        pass  # DM's script window may not define __file__
    if os.environ.get("AMADEUS_HOME"):
        candidates.append(os.environ["AMADEUS_HOME"])
    home = os.path.expanduser("~")
    candidates += [r"C:\AMADEUS", os.path.join(home, "AMADEUS"),
                   os.path.join(home, "Documents", "AMADEUS"), os.getcwd()]
    for candidate in candidates:
        if _looks_like_repo(candidate):
            return os.path.abspath(candidate)
    return None


def read_settings(repo_root):
    """Return (port, stream_port, source) from the unified config.

    DM always listens on 127.0.0.1: only the relay on this PC connects to it,
    and the relay republishes the live stream for the laptop."""
    path = os.environ.get("AMADEUS_CONFIG") or os.path.join(repo_root, CONFIG_FILENAME)
    if not os.path.isfile(path):
        return DEFAULT_PORTS["dm"], DEFAULT_PORTS["dm_stream"], "defaults (no config file)"
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    ports = cfg.get("ports", {})
    if cfg.get("version", 1) < 2:
        # v1 used ports.dm_stream for the stream the laptop saw. That port now
        # belongs to the relay, so DM keeps its internal default.
        ports = {"dm": ports.get("dm", DEFAULT_PORTS["dm"])}
    port = int(ports.get("dm", DEFAULT_PORTS["dm"]))
    stream_port = int(ports.get("dm_stream", DEFAULT_PORTS["dm_stream"]))
    return port, stream_port, path


def preflight():
    ok = True
    print("[amadeus] Python %s" % sys.version.split()[0])
    for module in ("zmq", "numpy", "DigitalMicrograph"):
        try:
            __import__(module)
        except Exception as exc:
            ok = False
            print("[amadeus] MISSING: cannot import %s (%s)" % (module, exc))
    if not ok:
        print("[amadeus] Install the missing package(s) into the Python environment "
              "DM uses (sys.prefix = %s), then restart DM." % sys.prefix)
    return ok


def main():
    repo_root = find_repo_root()
    if repo_root is None:
        print("[amadeus] ERROR: cannot find the AMADEUS checkout. Run AMADEUS.bat "
              "(its setup) on this PC, then restart DM, or set AMADEUS_HOME to the "
              "checkout folder.")
        return
    print("[amadeus] checkout: %s" % repo_root)
    if not preflight():
        return
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    port, stream_port, source = read_settings(repo_root)
    print("[amadeus] settings from: %s" % source)

    import zmq
    from dm_server.server import run_server
    try:
        run_server(port=port, stream_port=stream_port, bind_ip="127.0.0.1")
    except zmq.ZMQError as exc:
        if exc.errno == zmq.EADDRINUSE:
            print("[amadeus] ERROR: port %d or %d is already in use. The DM server "
                  "is probably already running in this DM session; do not start a "
                  "second copy." % (port, stream_port))
        else:
            raise


main()
