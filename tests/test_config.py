"""Unified deployment config (common/config.py) and how entry points use it."""

import ast
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from common import config as amadeus_config
from common import netutil

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch, tmp_path):
    """Isolate from a real amadeus_config.json / AMADEUS_CONFIG on the dev box."""
    monkeypatch.delenv(amadeus_config.ENV_VAR, raising=False)
    monkeypatch.setattr(amadeus_config, "REPO_ROOT", tmp_path)


def write(tmp_path, data, name="amadeus_config.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return path


def test_defaults_without_file():
    assert amadeus_config.find_config() is None
    assert amadeus_config.load_or_none() is None
    cfg = amadeus_config.load()
    assert cfg["_path"] is None
    assert cfg["ports"] == {"relay": 9875, "dm": 9876, "stream": 9877, "dm_stream": 9879}


def test_example_config_is_valid():
    cfg = amadeus_config.load(REPO / "amadeus_config.example.json")
    assert amadeus_config.relay_address(cfg) == "tcp://192.168.50.2:9875"
    assert cfg["experimental"]["firewall_autofix"] is False


def test_partial_file_merges_over_defaults(tmp_path):
    write(tmp_path, {"microscope_pc": {"ip": "169.254.202.200"}, "ports": {"relay": 9999}})
    cfg = amadeus_config.load()
    assert cfg["_path"].endswith("amadeus_config.json")
    assert cfg["ports"]["relay"] == 9999 and cfg["ports"]["dm"] == 9876
    assert cfg["microscope_pc"]["listen_ip"] == "*"
    assert amadeus_config.relay_address(cfg) == "tcp://169.254.202.200:9999"
    assert amadeus_config.stream_address(cfg) == "tcp://169.254.202.200:9877"


def test_env_var_and_explicit_path(tmp_path, monkeypatch):
    other = write(tmp_path, {"microscope_pc": {"ip": "10.0.0.5"}}, "other.json")
    monkeypatch.setenv(amadeus_config.ENV_VAR, str(other))
    assert amadeus_config.load()["microscope_pc"]["ip"] == "10.0.0.5"
    explicit = write(tmp_path, {"microscope_pc": {"ip": "10.0.0.6"}}, "explicit.json")
    assert amadeus_config.load(explicit)["microscope_pc"]["ip"] == "10.0.0.6"


def test_missing_explicit_path_is_an_error(tmp_path):
    with pytest.raises(amadeus_config.ConfigError, match="not found"):
        amadeus_config.load(tmp_path / "nope.json")


@pytest.mark.parametrize("bad, match", [
    ({"microscope_pc": {"ip": "not-an-ip"}}, "microscope_pc.ip"),
    ({"ports": {"relay": 70000}}, "port relay"),
    ({"ports": {"relay": 9876}}, "distinct"),
    ({"drivers": {"x": {"kind": "jeol", "slots": ["warp"]}}}, "drivers.x.slots"),
    ({"connection": {"mode": "carrier-pigeon"}}, "connection.mode"),
])
def test_validation(tmp_path, bad, match):
    write(tmp_path, bad)
    with pytest.raises(amadeus_config.ConfigError, match=match):
        amadeus_config.load()


def test_invalid_json_is_reported(tmp_path):
    (tmp_path / "amadeus_config.json").write_text("{ not json")
    with pytest.raises(amadeus_config.ConfigError, match="not valid JSON"):
        amadeus_config.load()


def test_save_round_trip_drops_private_keys(tmp_path):
    cfg = amadeus_config.load()
    cfg["microscope_pc"]["ip"] = "192.168.50.2"
    path = amadeus_config.save(cfg)
    on_disk = json.loads(path.read_text())
    assert "_path" not in on_disk
    assert amadeus_config.load()["microscope_pc"]["ip"] == "192.168.50.2"


def test_save_refuses_invalid(tmp_path):
    cfg = amadeus_config.load()
    cfg["ports"]["hw"] = cfg["ports"]["dm"]
    with pytest.raises(amadeus_config.ConfigError):
        amadeus_config.save(cfg)
    assert not (tmp_path / "amadeus_config.json").exists()


def test_ssh_mode_addresses(tmp_path):
    write(tmp_path, {"microscope_pc": {"ip": "192.168.50.2"}, "connection": {"mode": "ssh"}})
    cfg = amadeus_config.load()
    assert amadeus_config.relay_address(cfg) == "tcp://127.0.0.1:29875"
    assert amadeus_config.stream_address(cfg) == "tcp://127.0.0.1:29877"
    assert amadeus_config.relay_listen(cfg) == "tcp://127.0.0.1:9875"
    assert "-L 127.0.0.1:29875:127.0.0.1:9875" in amadeus_config.ssh_command(cfg, "me")


def test_relay_listen_specific_ip(tmp_path):
    write(tmp_path, {"microscope_pc": {"ip": "192.168.50.2", "listen_ip": "192.168.50.2"}})
    assert amadeus_config.relay_listen(amadeus_config.load()) == "tcp://192.168.50.2:9875"


def test_relative_data_dir_resolves_inside_repo(tmp_path):
    cfg = amadeus_config.load()
    assert amadeus_config.data_dir(cfg) == tmp_path / "amadeus_data" / "session"


# ---------------------------------------------------------------------------
# Entry-point precedence: CLI > config > default
# ---------------------------------------------------------------------------

def test_v1_config_is_migrated(tmp_path):
    # Written by the first wizard: separate hw port, laptop used DM's stream port.
    write(tmp_path, {"version": 1,
                     "ports": {"relay": 9875, "dm": 9876, "dm_stream": 9977, "hw": 9878},
                     "connection": {"mode": "ssh",
                                    "ssh_local_ports": {"relay": 29875, "dm_stream": 29877}}})
    cfg = amadeus_config.load()
    assert cfg["version"] == 4
    assert cfg["drivers"]["hw"]["kind"] == "jeol" and "hw_server" not in cfg
    assert cfg["ports"] == {"relay": 9875, "dm": 9876, "stream": 9977, "dm_stream": 9879}
    assert cfg["connection"]["ssh_local_ports"] == {"relay": 29875, "stream": 29877}


def test_unknown_port_key_is_rejected(tmp_path):
    write(tmp_path, {"version": 2, "ports": {"hw": 9878}})
    with pytest.raises(amadeus_config.ConfigError, match="unknown ports"):
        amadeus_config.load()


def test_relay_side_addresses():
    cfg = amadeus_config.load_defaults()
    dm = amadeus_config.effective_drivers(cfg)["dm"]
    assert dm["host"] == "tcp://127.0.0.1:9876" and dm["stream"] == "tcp://127.0.0.1:9879"
    assert amadeus_config.relay_stream_listen(cfg) == "tcp://*:9877"
    cfg["connection"]["mode"] = "ssh"
    assert amadeus_config.relay_stream_listen(cfg) == "tcp://127.0.0.1:9877"


def _client_args(**kw):
    base = dict(relay_address=None, stream_address=None,
                data_dir=None, timeout_s=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_mcp_server_settings(tmp_path):
    from client.server import resolve_settings
    legacy = resolve_settings(_client_args(), None)
    assert legacy["address"] == "tcp://localhost:9875"
    assert legacy["stream_address"] is None and legacy["timeout_ms"] == 30_000

    cfg = amadeus_config.load_defaults()
    cfg["microscope_pc"]["ip"] = "192.168.50.2"
    s = resolve_settings(_client_args(timeout_s=120), cfg)
    assert s["address"] == "tcp://192.168.50.2:9875"
    assert s["stream_address"] == "tcp://192.168.50.2:9877"
    assert s["data_dir"] == tmp_path / "amadeus_data" / "session"
    assert s["timeout_ms"] == 120_000

    # An explicit relay address keeps the old stream derivation (None = derive).
    s = resolve_settings(_client_args(relay_address="tcp://h:1"), cfg)
    assert s["address"] == "tcp://h:1" and s["stream_address"] is None


# ---------------------------------------------------------------------------
# Python 3.10 compatibility of everything that runs in vjem310
# ---------------------------------------------------------------------------

PY310_PATHS = ["common", "relay", "drivers", "setup_wizard",
               "scripts/start_amadeus_dm.py", "scripts/pyjem_stage_probe.py"]


def _py_files():
    for rel in PY310_PATHS:
        path = REPO / rel
        yield from ([path] if path.is_file() else sorted(path.rglob("*.py")))


@pytest.mark.parametrize("path", list(_py_files()), ids=lambda p: str(p.relative_to(REPO)))
def test_parses_as_python_310(path):
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 10))


# ---------------------------------------------------------------------------
# netutil parsers
# ---------------------------------------------------------------------------

IPCONFIG = """
Windows IP Configuration

Ethernet adapter Ethernet 2:

   Connection-specific DNS Suffix  . :
   Autoconfiguration IPv4 Address. . : 169.254.202.200
   Subnet Mask . . . . . . . . . . . : 255.255.0.0

Wireless LAN adapter Wi-Fi:

   IPv4 Address. . . . . . . . . . . : 10.1.2.3(Preferred)
"""

IFCONFIG = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en5: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet6 fe80::1%en5 prefixlen 64
\tinet 169.254.44.193 netmask 0xffff0000 broadcast 169.254.255.255
"""


def test_parse_ipconfig():
    assert netutil.parse_ipconfig(IPCONFIG) == [
        ("Ethernet 2", "169.254.202.200"), ("Wi-Fi", "10.1.2.3")]


def test_parse_ifconfig():
    assert netutil.parse_ifconfig(IFCONFIG) == [
        ("lo0", "127.0.0.1"), ("en5", "169.254.44.193")]


def test_link_local_and_subnet():
    assert netutil.is_link_local("169.254.44.193")
    assert not netutil.is_link_local("192.168.50.2")
    assert netutil.same_subnet("169.254.44.193", "169.254.202.200")   # /16 for link-local
    assert netutil.same_subnet("192.168.50.1", "192.168.50.2")
    assert not netutil.same_subnet("192.168.50.1", "192.168.51.2")


ROUTE_GET = """   route to: 169.254.202.200
destination: 169.254.202.200
  interface: en0
      flags: <UP,HOST,DONE,LLINFO,STATIC,b016,WASCLONED>
"""

MAC_ADAPTERS = [("en0", "10.103.24.211"), ("en10", "169.254.196.48")]


def test_parse_route_get():
    assert netutil.parse_route_get(ROUTE_GET) == "en0"
    assert netutil.parse_route_get("route: writing to routing socket: not in table") is None


def test_route_mismatch_wifi_wins_link_local():
    msg = netutil.route_mismatch("169.254.202.200", "en0", MAC_ADAPTERS, "169.254.196.48")
    assert msg and "via en0" in msg and "not en10" in msg and "Wi-Fi" in msg
    # No configured laptop IP: fall back to the adapter on the PC's subnet.
    assert netutil.route_mismatch("169.254.202.200", "en0", MAC_ADAPTERS)


def test_route_mismatch_ok_or_unknown():
    assert netutil.route_mismatch("169.254.202.200", "en10", MAC_ADAPTERS,
                                  "169.254.196.48") is None
    assert netutil.route_mismatch("169.254.202.200", None, MAC_ADAPTERS) is None
    # Nothing on this machine is on the PC's subnet: the subnet check covers it.
    assert netutil.route_mismatch("10.77.77.1", "en0", MAC_ADAPTERS) is None
    adapters = MAC_ADAPTERS + [("en11", "10.77.77.2")]
    assert netutil.route_mismatch("10.77.77.1", "en11", adapters, "10.77.77.2") is None
    assert "subnet nothing else uses" in netutil.route_mismatch(
        "10.77.77.1", "utun13", adapters, "10.77.77.2")


def test_check_route_skips_loopback():
    assert netutil.check_route("127.0.0.1") is None


# ---------------------------------------------------------------------------
# Instrument profile: export / load / restore
# ---------------------------------------------------------------------------

INSTRUMENT = {"label": "JEOL F200",
              "focus": {"by_mode": {"TEM": {"lens": "focus.fine", "dac_per_nm": 0.75,
                                            "dac_at_zero": 32768}}}}


def _deployed():
    cfg = amadeus_config.load_defaults()
    cfg["microscope_pc"].update(ip="192.168.50.2", adapter_alias="Ethernet 2",
                                repo_path=r"C:\AMADEUS", vjem310_python=r"C:\vjem310\python.exe")
    cfg["control_pc"].update(ip="192.168.50.1", data_dir="/Users/x/session")
    amadeus_config.set_hardware(cfg, mode="online")
    cfg["instrument"] = INSTRUMENT
    return cfg


def test_profile_round_trip_drops_machine_local_paths(tmp_path):
    path = amadeus_config.export_profile(_deployed(), tmp_path / "f200.amadeus.json",
                                         note="before reinstall")
    doc = json.loads(path.read_text())
    assert doc[amadeus_config.PROFILE_KEY] == 1 and doc["note"] == "before reinstall"
    saved = doc["config"]
    assert "repo_path" not in saved["microscope_pc"]
    assert "vjem310_python" not in saved["microscope_pc"]
    assert "data_dir" not in saved["control_pc"]
    assert saved["instrument"] == INSTRUMENT
    assert saved["microscope_pc"]["adapter_alias"] == "Ethernet 2"

    profile, info = amadeus_config.load_profile(path)
    assert info["note"] == "before reinstall" and info["exported_from"]
    restored = amadeus_config.restore_profile(profile, r"D:\new\AMADEUS", r"D:\py\python.exe")
    assert restored["microscope_pc"]["repo_path"] == r"D:\new\AMADEUS"
    assert restored["microscope_pc"]["vjem310_python"] == r"D:\py\python.exe"
    assert restored["microscope_pc"]["ip"] == "192.168.50.2"
    assert restored["instrument"] == INSTRUMENT
    assert amadeus_config.hardware_mode(restored) == "online"
    assert amadeus_config.validate(restored) == []


def test_restore_can_downgrade_online_to_offline(tmp_path):
    path = amadeus_config.export_profile(_deployed(), tmp_path / "p.json")
    profile, _ = amadeus_config.load_profile(path)
    restored = amadeus_config.restore_profile(profile, "/repo", keep_online=False)
    assert amadeus_config.hardware_mode(restored) == "offline"


def test_export_folds_calibration_file_into_profile(tmp_path):
    cal = tmp_path / "cal.json"
    cal.write_text(json.dumps({"label": "from file", "stage_scale_to_m": 1e-6}))
    cfg = _deployed()
    cfg["drivers"]["hw"]["calibration"] = str(cal)
    doc = json.loads(amadeus_config.export_profile(cfg, tmp_path / "p.json").read_text())
    assert "calibration" not in doc["config"]["drivers"]["hw"]
    assert doc["config"]["instrument"]["label"] == "from file"          # file wins
    assert doc["config"]["instrument"]["focus"] == INSTRUMENT["focus"]   # inline kept


def test_export_fails_loudly_on_missing_calibration_file(tmp_path):
    cfg = _deployed()
    cfg["drivers"]["hw"]["calibration"] = str(tmp_path / "gone.json")
    with pytest.raises(amadeus_config.ConfigError, match="calibration file"):
        amadeus_config.export_profile(cfg, tmp_path / "p.json")


def test_plain_config_file_is_accepted_as_a_profile(tmp_path):
    path = write(tmp_path, {"microscope_pc": {"ip": "10.0.0.2"}}, "plain.json")
    cfg, info = amadeus_config.load_profile(path)
    assert cfg["microscope_pc"]["ip"] == "10.0.0.2" and info == {"warnings": []}


def test_instrument_summary():
    assert amadeus_config.instrument_summary(_deployed()) == \
        "JEOL F200: focus calibrated for TEM"
    assert "uncalibrated" in amadeus_config.instrument_summary(amadeus_config.load_defaults())


def test_relay_passes_instrument_section_to_backend():
    from relay.config import load_config
    cfg = load_config(_deployed())
    assert cfg.instrument == INSTRUMENT
    assert cfg.drivers["hw"]["kind"] == "jeol" and cfg.drivers["hw"]["offline"] is False


def test_allow_scripting_flag_and_config():
    """--no-scripting > client.allow_scripting > default (on). The flag can
    only turn scripting off."""
    from client.server import resolve_settings
    assert resolve_settings(_client_args(), None)["allow_scripting"] is True
    assert resolve_settings(_client_args(no_scripting=True), None)["allow_scripting"] is False
    cfg = amadeus_config.load_defaults()
    assert cfg["client"]["allow_scripting"] is True
    cfg["client"]["allow_scripting"] = False
    assert resolve_settings(_client_args(), cfg)["allow_scripting"] is False
    assert resolve_settings(_client_args(no_scripting=False), cfg)["allow_scripting"] is False
    cfg["client"]["allow_scripting"] = "no"
    assert any("allow_scripting" in p for p in amadeus_config.validate(cfg))


# ---------------------------------------------------------------------------
# Schema v3: drivers, exclusive groups, policy, provenance
# ---------------------------------------------------------------------------

def test_v2_config_migrates_to_v4_drivers(tmp_path):
    write(tmp_path, {"version": 2, "hw_server": {"mode": "online", "calibration": "c.json"},
                     "relay": {"hardware_backend": "hw", "timeout_ms": 30000},
                     "instrument": {"focus": {"by_mode": {"TEM": {"lens": "olc"}}}}})
    cfg = amadeus_config.load()
    assert cfg["version"] == 4 and "hw_server" not in cfg
    assert "hardware_backend" not in cfg["relay"]
    hw = cfg["drivers"]["hw"]
    assert hw == {"kind": "jeol", "host": "in_process", "offline": False,
                  "slots": ["column", "stage"], "calibration": "c.json"}
    assert cfg["instrument"]["focus"]["by_mode"]["TEM"]["lens"] == "focus.coarse"
    drivers = amadeus_config.effective_drivers(cfg)
    assert drivers["dm"]["kind"] == "dm" and "scan" in drivers["dm"]["slots"]
    assert drivers["dm"]["host"] == "tcp://127.0.0.1:9876"


def test_v3_dm_fallback_and_explicit_drivers_migrate(tmp_path):
    write(tmp_path, {"version": 3, "hw_server": {"mode": "sim", "host": "process"},
                     "relay": {"hardware_backend": "dm", "timeout_ms": 1}})
    cfg = amadeus_config.load()
    assert cfg["drivers"]["hw"]["kind"] == "sim" and cfg["drivers"]["hw"]["slots"] == []
    assert cfg["drivers"]["hw"]["host"] == "process"
    assert {"column", "stage", "camera"} <= set(cfg["drivers"]["dm"]["slots"])
    explicit = {"s": {"kind": "sim", "slots": ["column"]}}
    write(tmp_path, {"version": 3, "hw_server": {"mode": "online"}, "drivers": explicit})
    assert amadeus_config.load()["drivers"] == explicit      # kept, and not merged


def test_drivers_section_replaces_the_default_instead_of_merging(tmp_path):
    write(tmp_path, {"drivers": {"tvips": {"kind": "remote", "host": "tcp://x:1",
                                           "slots": ["camera"]}}})
    assert list(amadeus_config.load()["drivers"]) == ["tvips"]


def test_set_hardware_edits_the_drivers_section():
    cfg = amadeus_config.load_defaults()
    assert amadeus_config.hardware_mode(cfg) == "offline" and not amadeus_config.is_online(cfg)
    amadeus_config.set_hardware(cfg, mode="online", host="process")
    assert cfg["drivers"]["hw"]["offline"] is False and cfg["drivers"]["hw"]["host"] == "process"
    assert amadeus_config.is_online(cfg)
    amadeus_config.set_hardware(cfg, via_dm=True)
    assert amadeus_config.hardware_driver(cfg)[0] == "dm"
    assert cfg["drivers"]["hw"]["slots"] == []
    amadeus_config.set_hardware(cfg, via_dm=False)
    assert amadeus_config.hardware_driver(cfg)[0] == "hw"
    assert "column" not in cfg["drivers"]["dm"]["slots"]
    amadeus_config.set_hardware(cfg, mode="sim")
    assert amadeus_config.hardware_mode(cfg) == "sim"


def test_cli_overrides_reach_the_derived_and_explicit_drivers():
    cfg = amadeus_config.load_defaults()
    assert amadeus_config.effective_drivers(cfg, hw_mode="sim")["hw"]["kind"] == "sim"
    cfg["drivers"] = {"col": {"kind": "jeol", "slots": ["column", "stage"]},
                      "gatan": {"kind": "dm", "host": "tcp://127.0.0.1:9876",
                                "slots": ["camera"]}}
    out = amadeus_config.effective_drivers(cfg, hw_mode="offline", dm_address="tcp://x:1")
    assert out["col"]["kind"] == "jeol" and out["col"]["offline"] is True
    assert out["gatan"]["host"] == "tcp://x:1"


def test_drivers_section_is_validated():
    cfg = amadeus_config.load_defaults()
    cfg["drivers"] = {
        "a": {"kind": "jeol", "slots": ["column"]},
        "b": {"kind": "sim", "slots": ["column", "warp_drive"]},
        "c": {"kind": "dm", "host": "in_process", "slots": ["camera"]},
        "d/e": {"kind": "sim", "slots": [], "colour": "red"},
    }
    problems = "\n".join(amadeus_config.validate(cfg))
    assert "drivers.b.slots" in problems
    assert "drivers.c.host must be the dm process address" in problems
    assert "can't contain" in problems and "unknown fields ['colour']" in problems
    cfg["drivers"] = {"a": {"kind": "jeol", "slots": ["column"]},
                      "b": {"kind": "sim", "slots": ["column"]}}
    assert any("one driver only" in p for p in amadeus_config.validate(cfg))


def test_exclusive_and_policy_are_validated():
    cfg = amadeus_config.load_defaults()
    cfg["exclusive"] = [["dm/camera/0"]]
    cfg["policy"] = {"stage_limits": {"x_m": [1, -1], "w_m": [0, 1]}, "confirm": "all"}
    problems = "\n".join(amadeus_config.validate(cfg))
    assert "exclusive must be" in problems
    assert "stage_limits.x_m" in problems and "stage_limits.w_m" in problems
    assert "policy.confirm" in problems


def test_safety_policy_defaults_and_an_f200_shaped_policy():
    cfg = amadeus_config.load_defaults()
    assert cfg["policy"]["confirm_vendor_writes"] is True       # raw vendor writes gated
    cfg["exclusive"] = [["dm/camera/0", "dm/camera/1"]]
    cfg["policy"] = {"stage_limits": {"x_m": [-0.0009, 0.0009], "alpha_deg": [-25, 25]},
                     "confirm": ["set_column_valves", "set_aperture"],
                     "confirm_vendor_writes": True}
    assert amadeus_config.validate(cfg) == []
    cfg["policy"]["confirm_vendor_writes"] = "yes"
    assert any("confirm_vendor_writes" in p for p in amadeus_config.validate(cfg))


def test_export_strips_machine_local_driver_paths_and_records_provenance(tmp_path):
    cfg = _deployed()
    cfg["drivers"] = {"hw": {"kind": "jeol", "host": "process", "slots": ["column", "stage"],
                             "python": "C:/envs/vjem310/python.exe"}}
    path = amadeus_config.export_profile(cfg, tmp_path / "p.amadeus.json")
    doc = json.loads(path.read_text())
    assert "python" not in doc["config"]["drivers"]["hw"]
    assert doc["provenance"]["drivers"] == {"hw": "jeol"}
    assert doc["provenance"]["amadeus_version"]


def test_profile_warnings_flag_unrecorded_and_stale_calibrations(tmp_path):
    cfg = amadeus_config.load_defaults()
    cfg["instrument"] = {"focus": {"by_mode": {
        "TEM": {"lens": "olf", "dac_per_nm": 0.5, "dac_at_zero": 32000},
        "STEM": {"lens": "olf", "dac_per_nm": 0.5, "dac_at_zero": 32000,
                 "measured": {"date": "2020-01-01"}}}},
        "stage": {"image_mapping": {"STEM": {"stage_to_image": [[1, 0], [0, 1]],
                                             "measured": {"date": "2999-01-01"}}}}}
    warnings = amadeus_config.profile_warnings(
        cfg, {"provenance": {"amadeus_version": "0.0.1"}})
    text = "\n".join(warnings)
    assert "focus calibration (TEM): no record" in text
    assert "focus calibration (STEM) was measured" in text
    assert "image_mapping" not in text and "stage-to-image" not in text
    assert "exported with AMADEUS 0.0.1" in text


def test_restore_offline_also_covers_explicit_jeol_drivers():
    cfg = amadeus_config.load_defaults()
    cfg["drivers"] = {"hw": {"kind": "jeol", "offline": False, "slots": ["column"]}}
    out = amadeus_config.restore_profile(cfg, "C:/AMADEUS", keep_online=False)
    assert out["drivers"]["hw"]["offline"] is True
