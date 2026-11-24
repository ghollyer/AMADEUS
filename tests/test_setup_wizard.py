"""Setup wizard / launcher logic, without opening any Tk window."""

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("tkinter")

from common import config as amadeus_config  # noqa: E402
from setup_wizard import mcp_config, netdiag  # noqa: E402
from setup_wizard import launcher  # noqa: E402
from setup_wizard.wizard import (ROLE_LAPTOP, ROLE_PC, apply_answers,  # noqa: E402
                                 find_vjem310_pythons, merge_fetched, network_warnings)


@pytest.fixture
def cfg():
    cfg = amadeus_config.load_defaults()
    cfg["microscope_pc"].update(ip="192.168.50.2", adapter_alias="Ethernet 2")
    cfg["control_pc"]["ip"] = "192.168.50.1"
    return cfg


# ---------------------------------------------------------------- wizard

def test_warns_when_both_ips_are_the_same():
    # 2026-10-26: the laptop probed its own address instead of the PC's.
    msgs = network_warnings(ROLE_LAPTOP, "169.254.44.193", "169.254.44.193")
    assert any("same IP" in m for m in msgs)


def test_warns_about_link_local_addresses():
    msgs = network_warnings(ROLE_LAPTOP, "169.254.44.193", "169.254.202.200")
    assert any("link-local" in m for m in msgs)
    assert not any("same subnet" in m for m in msgs)


def test_warns_about_different_subnets():
    msgs = network_warnings(ROLE_PC, "192.168.50.2", "10.0.0.1")
    assert any("same subnet" in m for m in msgs)


def test_clean_static_setup_has_no_warnings():
    assert network_warnings(ROLE_PC, "192.168.50.2", "192.168.50.1") == []


def test_apply_answers_pc_keeps_laptop_settings(cfg):
    cfg["control_pc"]["data_dir"] = "/data/session"
    out = apply_answers(cfg, ROLE_PC, {
        "this_ip": "192.168.50.20", "other_ip": "192.168.50.10",
        "repo_path": r"C:\AMADEUS", "vjem310_python": r"C:\vjem310\python.exe",
        "hw_mode": "online", "hardware_backend": "dm", "listen_all": False,
        "ports": {"relay": 9975},
    })
    assert out["microscope_pc"]["ip"] == "192.168.50.20"
    assert out["microscope_pc"]["listen_ip"] == "192.168.50.20"
    assert out["control_pc"] == {"ip": "192.168.50.10", "data_dir": "/data/session"}
    assert out["drivers"]["hw"]["offline"] is False                   # online
    assert amadeus_config.hardware_driver(out)[0] == "dm"              # EM* fallback
    assert out["ports"]["relay"] == 9975 and out["ports"]["dm"] == 9876
    assert amadeus_config.validate(out) == []
    assert cfg["microscope_pc"]["ip"] == "192.168.50.2"   # input not mutated


def test_apply_answers_laptop_keeps_pc_settings(cfg):
    cfg["microscope_pc"]["vjem310_python"] = r"C:\vjem310\python.exe"
    out = apply_answers(cfg, ROLE_LAPTOP, {"this_ip": "192.168.50.1",
                                           "data_dir": "~/amadeus", "timeout_s": "900",
                                           "hw_mode": "online"})
    assert out["microscope_pc"]["vjem310_python"] == r"C:\vjem310\python.exe"
    assert amadeus_config.hardware_mode(out) == "offline"  # PC-only answer ignored
    assert out["control_pc"]["data_dir"] == "~/amadeus"
    assert out["client"]["timeout_s"] == 900


def test_find_vjem310_pythons(tmp_path):
    exe = tmp_path / ".conda" / "envs" / "vjem310" / "python.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    assert str(exe) in find_vjem310_pythons({"USERPROFILE": str(tmp_path)})


# ------------------------------------------------------------ MCP config

def test_mcp_entry_has_no_ips(tmp_path):
    entry = mcp_config.server_entry(tmp_path / "amadeus_config.json", python="/py")
    assert entry["command"] == "/py"
    assert entry["args"][:2] == ["-m", "client.server"]
    assert "--amadeus-config" in entry["args"]
    assert not any("tcp://" in a for a in entry["args"])


def test_write_entry_merges_and_backs_up(tmp_path):
    target = tmp_path / "claude_desktop_config.json"
    target.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}))
    mcp_config.write_entry(target, {"command": "py"})
    data = json.loads(target.read_text())
    assert data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["amadeus"] == {"command": "py"}
    assert data["theme"] == "dark"
    assert (tmp_path / "claude_desktop_config.json.bak").exists()


# --------------------------------------------------------------- launcher

def test_server_command_runs_the_relay_with_this_interpreter():
    cmd = launcher.server_command("C:/AMADEUS/amadeus_config.json")
    assert cmd == [sys.executable, "-u", "-m", "relay", "--amadeus-config",
                   "C:/AMADEUS/amadeus_config.json"]


def test_health_uses_the_relays_backend_report(cfg, monkeypatch):
    reply = {"status": "ok", "data": {
        "backends": {"dm": {"ok": False, "kind": "dm"}, "hw": {"ok": True, "kind": "jeol"}},
        "routing": {"column": ["hw"], "stage": ["hw"], "camera": ["dm"]}}}
    monkeypatch.setattr(launcher, "service_state",
                        lambda port, **kw: ("up", reply) if port == 9875 else ("down", None))
    assert launcher.health(cfg) == {"server": "up", "dm": "down", "hw": "up",
                                    "drivers": {"dm": "down", "hw": "up"}}
    monkeypatch.setattr(launcher, "service_state",
                        lambda port, **kw: ("up", None) if port == 9876 else ("down", None))
    assert launcher.health(cfg) == {"server": "down", "dm": "up", "hw": "idle"}


# ------------------------------------------------------- laptop fetch

def test_merge_fetched_keeps_laptop_settings(cfg):
    remote = amadeus_config.public(cfg)
    remote["microscope_pc"]["vjem310_python"] = r"C:\vjem310\python.exe"
    remote["control_pc"]["data_dir"] = r"C:\ignored"
    local = amadeus_config.load_defaults()
    local["control_pc"]["data_dir"] = "~/amadeus"
    local["client"]["timeout_s"] = 900
    out = merge_fetched(remote, local, laptop_ip="192.168.50.7", reached_ip="192.168.50.9")
    assert out["microscope_pc"]["vjem310_python"] == r"C:\vjem310\python.exe"
    assert out["microscope_pc"]["ip"] == "192.168.50.9"      # the address that worked
    assert out["control_pc"] == {"ip": "192.168.50.7", "data_dir": "~/amadeus"}
    assert out["client"]["timeout_s"] == 900
    assert amadeus_config.validate(out) == []


def test_merge_fetched_ssh_keeps_pc_ip(cfg):
    remote = amadeus_config.public(cfg)
    remote["connection"]["mode"] = "ssh"
    out = merge_fetched(remote, amadeus_config.load_defaults(), reached_ip="127.0.0.1")
    assert out["microscope_pc"]["ip"] == "192.168.50.2"


# ---------------------------------------------------------- network doctor

def _listener(port, addr="0.0.0.0", proc="python"):
    return {"LocalAddress": addr, "LocalPort": port, "Process": proc}


def test_doctor_flags_block_rule_and_missing_services(cfg):
    data = {
        "listeners": [_listener(9877), _listener(9875)],
        "profiles": [{"InterfaceAlias": "Ethernet 2", "NetworkCategory": "Public"}],
        "rules": [{"Name": "python.exe", "Action": "Block", "Profile": "Public",
                   "Program": r"C:\vjem310\python.exe", "LocalPort": "Any"}],
    }
    findings = netdiag.analyze(cfg, data)
    titles = {(lvl, t) for lvl, t, _, _ in findings}
    assert (netdiag.FAIL, "DM server not listening on port 9876") in titles
    assert (netdiag.OK, "AMADEUS server (relay) listening on port 9875") in titles
    block = [f for f in findings if "BLOCK" in f[1]]
    assert block and block[0][3].startswith("Disable-NetFirewallRule")
    assert any("Public" in t for _, t, _, _ in findings)
    missing = [f for f in findings if f[1] == "no inbound allow rule for the AMADEUS ports"]
    assert missing and missing[0][3] == netdiag.firewall_rule_command(cfg)


def test_doctor_flags_dm_exposed_to_network(cfg):
    data = {"listeners": [_listener(9876, "0.0.0.0", "DigitalMicrograph")],
            "profiles": [], "rules": []}
    findings = netdiag.analyze(cfg, data)
    assert any(lvl == netdiag.INFO and "exposed to the network" in t
               for lvl, t, _, _ in findings)


def test_doctor_flags_localhost_only_relay_in_direct_mode(cfg):
    data = {"listeners": [_listener(9875, "127.0.0.1")], "profiles": [], "rules": []}
    findings = netdiag.analyze(cfg, data)
    assert any(lvl == netdiag.FAIL and "only accepts local" in t for lvl, t, _, _ in findings)


def test_doctor_accepts_allow_rule(cfg):
    data = {"listeners": [], "profiles": [],
            "rules": [{"Name": netdiag.RULE_NAME, "Action": "Allow", "Profile": "Any",
                       "Program": "Any", "LocalPort": "9875,9877"}]}
    findings = netdiag.analyze(cfg, data)
    assert any(t == "inbound allow rule present" for _, t, _, _ in findings)
    assert netdiag.rule_exists(data)


def test_firewall_rule_is_narrow(cfg):
    cmd = netdiag.firewall_rule_command(cfg)
    assert "-LocalPort 9875,9877" in cmd
    assert "-LocalAddress 192.168.50.2" in cmd
    assert "-RemoteAddress 192.168.50.1" in cmd
    assert "-InterfaceAlias 'Ethernet 2'" in cmd
    assert "Remove" not in cmd and "Disable" not in cmd


def test_firewall_autofix_off_by_default():
    assert amadeus_config.load_defaults()["experimental"]["firewall_autofix"] is False
    example = json.loads((Path(__file__).resolve().parent.parent
                          / "amadeus_config.example.json").read_text())
    assert example["experimental"]["firewall_autofix"] is False


# --------------------------------------------------------------- readiness

from setup_wizard import readiness  # noqa: E402


def _facts(**kw):
    base = {"local_ips": ["192.168.50.2"], "zmq": True, "amadeus_home": None,
            "driver_smokes": {}, "python": "/vjem310/python", "windows": False,
            "health": {"server": "up", "dm": "up", "hw": "up"}}
    base.update(kw)
    return base


def _levels(items):
    return {title: level for level, title, _ in items}


def test_readiness_all_good(cfg):
    items = readiness.readiness(cfg, _facts())     # DM answering proves it found the checkout
    assert not [t for lvl, t, _ in items if lvl in (readiness.TODO, readiness.WARN)]


def test_readiness_checks_amadeus_home_before_dm_runs(cfg):
    cfg["microscope_pc"]["repo_path"] = "/repo"
    down = {"server": "up", "dm": "down", "hw": "up"}
    assert "DM may not find the AMADEUS checkout" in _levels(
        readiness.readiness(cfg, _facts(health=down)))
    assert _levels(readiness.readiness(cfg, _facts(health=down, amadeus_home="/repo")))[
        "DM can find the AMADEUS checkout"] == readiness.OK


def test_readiness_flags_missing_static_ip_first(cfg):
    items = readiness.readiness(cfg, _facts(local_ips=["169.254.1.9"]))
    level, title, detail = items[0]
    assert level == readiness.TODO and "192.168.50.2" in title
    assert "Ethernet 2" in detail


def test_readiness_flags_services_and_dm(cfg):
    levels = _levels(readiness.readiness(cfg, _facts(health={"server": "idle"}, zmq=False)))
    assert levels["AMADEUS server not running"] == readiness.TODO
    assert levels["DM server not answering"] == readiness.TODO
    assert levels["pyzmq missing in this Python"] == readiness.TODO


def test_readiness_requires_smoke_test_for_online_on_this_install(cfg):
    amadeus_config.set_hardware(cfg, mode="online")
    title = "driver hw (jeol) not yet checked on this install"
    assert _levels(readiness.readiness(cfg, _facts()))[title] == readiness.WARN
    other_python = {"hw": {"passed": True, "python": "/old/python"}}
    assert title in _levels(readiness.readiness(cfg, _facts(driver_smokes=other_python)))
    ok = {"hw": {"passed": True, "python": "/vjem310/python", "time": "t"}}
    assert _levels(readiness.readiness(cfg, _facts(driver_smokes=ok)))[
        "driver hw (jeol) check passed on this install"] == readiness.OK
    amadeus_config.set_hardware(cfg, mode="offline")          # offline needs none
    assert not [t for t in _levels(readiness.readiness(cfg, _facts())) if "checked" in t]


def test_readiness_reports_doctor_findings_on_windows(cfg):
    findings = [("FAIL", "firewall BLOCK rule for python.exe", "", None)]
    items = readiness.readiness(cfg, _facts(windows=True), findings)
    assert _levels(items)["network doctor found problems"] == readiness.TODO


# ------------------------------------------------- profile restore (PC CLI)

def test_restore_and_export_cli(cfg, tmp_path, monkeypatch, capsys):
    from setup_wizard import wizard
    amadeus_config.set_hardware(cfg, mode="online")
    cfg["instrument"] = {"label": "F200"}
    profile = amadeus_config.export_profile(cfg, tmp_path / "f200.amadeus.json")
    target = tmp_path / "restored.json"
    monkeypatch.setattr(wizard, "prepare_pc", lambda cfg: ["prepared"])   # no setx / mkdir

    monkeypatch.setattr(sys, "argv", ["amadeus-setup", "--restore", str(profile),
                                      "--amadeus-config", str(target)])
    wizard.main()
    restored = json.loads(target.read_text())
    assert restored["microscope_pc"]["ip"] == "192.168.50.2"
    assert restored["instrument"] == {"label": "F200"}
    assert amadeus_config.hardware_mode(restored) == "offline"  # safe default
    assert restored["microscope_pc"]["repo_path"] == str(amadeus_config.REPO_ROOT)
    out = capsys.readouterr().out
    assert "Readiness:" in out and "[TODO]" in out

    again = tmp_path / "again.amadeus.json"
    monkeypatch.setattr(sys, "argv", ["amadeus-setup", "--export", str(again),
                                      "--amadeus-config", str(target)])
    wizard.main()
    assert json.loads(again.read_text())["config"]["instrument"] == {"label": "F200"}


# ------------------------------------------------- multi-vendor drivers

def test_health_with_named_drivers(cfg, monkeypatch):
    reply = {"status": "ok", "data": {
        "backends": {"col": {"ok": True, "kind": "jeol"}, "gatan": {"ok": False, "kind": "dm"},
                     "eds": {"ok": True, "kind": "remote"}},
        "routing": {"column": ["col"], "stage": ["col"], "camera": ["gatan"]}}}
    monkeypatch.setattr(launcher, "service_state",
                        lambda port, **kw: ("up", reply) if port == 9875 else ("down", None))
    assert launcher.health(cfg) == {"server": "up", "dm": "down", "hw": "up",
                                    "drivers": {"col": "up", "gatan": "down", "eds": "up"}}
    reply["data"]["routing"] = {"camera": ["gatan"]}           # no column driver
    reply["data"]["backends"].pop("gatan")
    assert launcher.health(cfg)["hw"] == "none" and launcher.health(cfg)["dm"] == "none"


def test_wizard_sets_the_hardware_driver_host(cfg):
    out = apply_answers(cfg, ROLE_PC, {"hw_host": True})
    assert out["drivers"]["hw"]["host"] == "process"
    assert amadeus_config.effective_drivers(out)["hw"]["host"] == "process"
    out = apply_answers(out, ROLE_PC, {"hw_host": False})
    assert out["drivers"]["hw"]["host"] == "in_process"


def test_readiness_requires_a_smoke_test_for_every_online_driver(cfg):
    cfg["drivers"] = {
        "col": {"kind": "acme", "slots": ["column", "stage"]},
        "cam": {"kind": "sim", "slots": ["camera"]},
        "old": {"kind": "jeol", "offline": True, "slots": ["scan"]},
    }
    assert list(readiness.drivers_needing_smoke(cfg)) == ["col"]
    items = readiness.readiness(cfg, _facts())
    assert _levels(items)["driver col (acme) not yet checked on this install"] == \
        readiness.WARN
    passed = {"col": {"passed": True, "python": "/vjem310/python", "time": "t"}}
    items = readiness.readiness(cfg, _facts(driver_smokes=passed))
    assert _levels(items)["driver col (acme) check passed on this install"] == readiness.OK
    other_python = {"col": {"passed": True, "python": "/elsewhere/python"}}
    assert "driver col (acme) not yet checked on this install" in _levels(
        readiness.readiness(cfg, _facts(driver_smokes=other_python)))


def test_readiness_names_drivers_that_are_down(cfg):
    health = {"server": "up", "dm": "up", "hw": "up", "drivers": {"eds": "down"}}
    assert _levels(readiness.readiness(cfg, _facts(health=health)))[
        "driver eds not answering"] == readiness.TODO


def test_driver_smoke_records_a_result(tmp_path, monkeypatch, capsys):
    from drivers import smoke
    path = tmp_path / "amadeus_config.json"
    cfg = amadeus_config.load_defaults()
    cfg["drivers"] = {"s": {"kind": "sim", "slots": ["column", "stage", "camera"]}}
    amadeus_config.save(cfg, path)
    monkeypatch.setattr(smoke, "LOG_DIR", str(tmp_path / "logs"))
    assert smoke.main(["--amadeus-config", str(path), "--driver", "s"]) == 0
    out = capsys.readouterr().out
    assert "PASSED" in out and "get_stage_position" in out
    data = json.loads((tmp_path / "logs" / "driver_smoke_s.json").read_text())
    assert data["passed"] and data["kind"] == "sim" and data["python"] == sys.executable
