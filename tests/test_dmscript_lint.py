"""Tests for the offline DM-Script syntax checker (common/dmscript_lint.py)."""

from pathlib import Path

import pytest

from common.dmscript_lint import DMScriptSyntaxError, assert_valid, check, main

REPO = Path(__file__).resolve().parents[1]


def codes(script):
    return [i.code for i in check(script)]


# ── Regressions: the two bugs found on the F200 on 2026-11-07 ──────────────

def test_get_stem_settings_regression():
    # dm_server/helpers/digiscan.py — DM: "Error in line 5 / Syntax error"
    assert codes("number active = 0\nif (DSIsAcquisitionActive()) { active = 1 }\n") == ["E003"]


def test_live_teardown_regression():
    # dm_server/helpers/live.py — made stop_live_scan leave DigiScan rastering
    s = "while ( DSIsAcquisitionActive() && _n < 50 ) { Sleep(0.1) _n = _n + 1 }"
    assert sorted(codes(s)) == ["E003", "E004"]


# ── Valid DM-Script must stay clean ──────────────────────────────────────────

@pytest.mark.parametrize("script", [
    "if (DSIsAcquisitionActive()) { active = 1; }",
    "if (DSIsAcquisitionActive()) DSStopAcquisition(-1)",
    "number x = 1\nnumber y = 2\n",                      # newline terminates
    "number origin_x, scale_x\nstring units_x\n",
    "TagGroup tg = GetPersistentTagGroup()",
    "image img := DSGetLastAcquiredImage(0)\nimg.ShowImage()",
    "for (i = 0; i < n; i++) {\n  Sleep(0.1)\n}",
    "if (a) { b = 1; } else { b = 2; }",
    "number f(number a) { return a * 2; }",
    'Result("µm \\"quoted\\" // not a comment\\n")',
    "x = 1 // trailing comment }\n/* block { comment\n spans */ y = 2",
    "number t = 1.5e-3\nnumber h = 0xFF",
    "{\n}\n{ }",
])
def test_valid_scripts(script):
    assert check(script) == []


# ── Each error class ────────────────────────────────────────────────────────

@pytest.mark.parametrize("script, code", [
    ("if (x) { y = 1;", "E001"),                # unclosed {
    ("Foo(1))", "E001"),                        # unmatched )
    ("Foo(1]", "E001"),                         # mismatched
    ('Result("abc)', "E002"),
    ("x = 1 /* never closed", "E002"),
    ("{ return x }", "E003"),
    ("x = 1 y = 2", "E004"),
    ('Result("a") Result("b")', "E004"),
    ("x = 1  # python comment", "E005"),
    ("SetFlag(True)", "E006"),
    ("Foo({name})", "E003"),                    # unfilled f-string placeholder
])
def test_error_classes(script, code):
    assert code in codes(script)


def test_line_and_column_reported():
    (issue,) = check("number a = 1\nif (b) { a = 2 }")
    assert (issue.line, issue.col, issue.code) == (2, 16, "E003")


def test_assert_valid_message_shows_source_line():
    with pytest.raises(DMScriptSyntaxError) as exc:
        assert_valid("ok = 1\nif (b) { a = 2 }")
    assert "line 2" in str(exc.value)
    assert "if (b) { a = 2 }" in str(exc.value)
    assert exc.value.issues[0].code == "E003"


# ── Real scripts in the repo ────────────────────────────────────────────────

@pytest.mark.parametrize("path", sorted(REPO.glob("Reference/**/*.s")),
                         ids=lambda p: p.name)
def test_reference_scripts_clean(path):
    assert check(path.read_text(encoding="utf-8", errors="replace")) == []


def test_cli(tmp_path, capsys):
    good = tmp_path / "good.s"
    good.write_text("number x = 1;\n")
    bad = tmp_path / "bad.s"
    bad.write_text("if (x) { y = 1 }\n")
    assert main([str(good)]) == 0
    assert main([str(good), str(bad)]) == 1
    assert "bad.s:1:16: E003" in capsys.readouterr().out
