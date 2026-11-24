"""Static syntax checker for DM-Script (Gatan Digital Micrograph scripting).

There is no DM-Script parser outside DM, so a malformed script in a helper is
only discovered on the instrument as an opaque ``Error in line N / Syntax
error``.  This is a lint pass over a real tokenizer that catches the error
classes that have actually bitten AMADEUS, offline:

  E001  unbalanced / mismatched ``()`` ``[]`` ``{}``
  E002  unterminated string or block comment
  E003  statement ends right before ``}`` on the same line with no ``;``
        (``if (x) { a = 1 }`` — DM rejects this; a newline or ``;`` is needed)
  E004  two statements on one line with no ``;`` between them
        (``{ Sleep(0.1) n = n + 1 }``)
  E005  character that is not DM-Script (``#``, ``@``, ``$``, backtick) —
        usually a Python comment or format leftover
  E006  Python literal ``True`` / ``False`` / ``None`` (DM uses ``true`` /
        ``false``) — usually an f-string interpolating a Python value

It is deliberately conservative: it only reasons within a single line (DM
treats a newline as a statement terminator), so multi-line constructs are
never flagged.  A clean result is not proof the script compiles — DM still
checks names, types and argument counts.

Usage::

    from common.dmscript_lint import check, assert_valid
    issues = check(script)              # [] when clean
    assert_valid(script)                # raises DMScriptSyntaxError

    python -m common.dmscript_lint file.s [more.s ...]   # exit 1 on issues

Stdlib only, Python 3.10-compatible.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

# Tokens after which a statement can be complete.
_ENDS = {"ident", "number", "string", ")", "]"}
# Tokens that can start a new statement / expression operand.
_STARTS = {"ident", "number", "string"}
# Keywords whose parenthesised condition is followed by a statement on the
# same line (``if (x) y = 1``) — legal, unlike ``Foo(x) y = 1``.
_CONTROL = {"if", "while", "for", "foreach", "switch"}
# Keywords that may be followed directly by an operand.
_OPERAND_KEYWORDS = {"return", "else", "do", "case", "throw", "new"}
_PY_LITERALS = {"True", "False", "None"}
_BAD_CHARS = set("#@$`")
_PAIRS = {")": "(", "]": "[", "}": "{"}


@dataclass(frozen=True)
class Issue:
    line: int
    col: int
    code: str
    message: str

    def __str__(self) -> str:
        return f"line {self.line}, col {self.col}: {self.code} {self.message}"


class DMScriptSyntaxError(ValueError):
    def __init__(self, issues: list[Issue], script: str):
        self.issues = issues
        lines = script.splitlines()
        parts = []
        for i in issues:
            src = lines[i.line - 1].strip() if 0 < i.line <= len(lines) else ""
            parts.append(f"  {i}\n      {src}")
        super().__init__("DM-Script syntax check failed:\n" + "\n".join(parts))


@dataclass
class _Tok:
    kind: str      # ident | number | string | op | one of ()[]{};,
    text: str
    line: int
    col: int


def _tokenize(src: str, issues: list[Issue]) -> list[_Tok]:
    toks: list[_Tok] = []
    i, n, line, line_start = 0, len(src), 1, 0

    def col(pos: int) -> int:
        return pos - line_start + 1

    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            line_start = i + 1
            i += 1
        elif c in " \t\r\f\v":
            i += 1
        elif src.startswith("//", i):
            while i < n and src[i] != "\n":
                i += 1
        elif src.startswith("/*", i):
            start_line, start_col = line, col(i)
            end = src.find("*/", i + 2)
            if end < 0:
                issues.append(Issue(start_line, start_col, "E002",
                                    "unterminated /* comment"))
                break
            for j in range(i, end):
                if src[j] == "\n":
                    line += 1
                    line_start = j + 1
            i = end + 2
        elif c in "\"'":
            start, start_col = i, col(i)
            i += 1
            while i < n and src[i] != c and src[i] != "\n":
                i += 2 if src[i] == "\\" else 1
            if i >= n or src[i] != c:
                issues.append(Issue(line, start_col, "E002",
                                    f"unterminated string literal ({c})"))
                continue
            i += 1
            toks.append(_Tok("string", src[start:i], line, start_col))
        elif c.isalpha() or c == "_":
            start = i
            while i < n and (src[i].isalnum() or src[i] == "_"):
                i += 1
            toks.append(_Tok("ident", src[start:i], line, col(start)))
        elif c.isdigit() or (c == "." and i + 1 < n and src[i + 1].isdigit()):
            start = i
            if src.startswith(("0x", "0X"), i):
                i += 2
                while i < n and src[i] in "0123456789abcdefABCDEF":
                    i += 1
            else:
                while i < n and (src[i].isdigit() or src[i] == "."):
                    i += 1
                if i < n and src[i] in "eE":
                    j = i + 1
                    if j < n and src[j] in "+-":
                        j += 1
                    if j < n and src[j].isdigit():
                        i = j
                        while i < n and src[i].isdigit():
                            i += 1
            toks.append(_Tok("number", src[start:i], line, col(start)))
        elif c in "()[]{};,":
            toks.append(_Tok(c, c, line, col(i)))
            i += 1
        elif c in _BAD_CHARS:
            issues.append(Issue(line, col(i), "E005",
                                f"{c!r} is not DM-Script (Python leftover?)"))
            i += 1
        else:
            toks.append(_Tok("op", c, line, col(i)))
            i += 1
    return toks


def check(script: str) -> list[Issue]:
    """Return the syntax issues found in *script* (empty list when clean)."""
    issues: list[Issue] = []
    toks = _tokenize(script, issues)

    # (opener token, opener-is-a-control-condition) — for E001 and E004.
    stack: list[tuple[_Tok, bool]] = []
    # Index of each ')' that closes an if/while/for condition.
    control_close: set[int] = set()

    for k, t in enumerate(toks):
        prev = toks[k - 1] if k else None

        if t.kind == "ident" and t.text in _PY_LITERALS:
            issues.append(Issue(t.line, t.col, "E006",
                                f"Python literal {t.text!r}; DM-Script uses "
                                f"true/false/0"))

        if t.kind in "([{":
            is_ctrl = (t.kind == "(" and prev is not None
                       and prev.kind == "ident" and prev.text in _CONTROL)
            stack.append((t, is_ctrl))
        elif t.kind in ")]}":
            if not stack or stack[-1][0].kind != _PAIRS[t.kind]:
                issues.append(Issue(t.line, t.col, "E001",
                                    f"unmatched {t.kind!r}"))
            else:
                _, was_ctrl = stack.pop()
                if was_ctrl:
                    control_close.add(k)

        if prev is None or prev.line != t.line:
            continue  # a newline terminates a statement in DM-Script

        if t.kind == "}" and prev.kind in _ENDS:
            issues.append(Issue(t.line, t.col, "E003",
                                "missing ';' before '}' (statement and closing "
                                "brace on the same line)"))
        elif t.kind in _STARTS and prev.kind in _ENDS:
            if prev.kind == "ident" and t.kind == "ident":
                continue  # declaration: `number x`, `TagGroup tg`
            if prev.kind == "ident" and prev.text in _OPERAND_KEYWORDS:
                continue  # `return x`, `else y = 1`
            if prev.kind == ")" and (k - 1) in control_close:
                continue  # `if (x) y = 1`
            issues.append(Issue(t.line, t.col, "E004",
                                f"missing ';' between statements "
                                f"(after {prev.text!r}, before {t.text!r})"))

    for opener, _ in stack:
        issues.append(Issue(opener.line, opener.col, "E001",
                            f"unclosed {opener.kind!r}"))
    issues.sort(key=lambda i: (i.line, i.col))
    return issues


def assert_valid(script: str) -> None:
    """Raise :class:`DMScriptSyntaxError` if *script* has syntax issues."""
    issues = check(script)
    if issues:
        raise DMScriptSyntaxError(issues, script)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("usage: python -m common.dmscript_lint FILE.s [FILE.s ...]  "
              "('-' reads stdin)", file=sys.stderr)
        return 2
    bad = 0
    for path in argv:
        if path == "-":
            src = sys.stdin.read()
        else:
            with open(path, encoding="utf-8", errors="replace") as fh:
                src = fh.read()
        for issue in check(src):
            print(f"{path}:{issue.line}:{issue.col}: {issue.code} {issue.message}")
            bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
