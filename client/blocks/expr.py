"""A small, safe expression language for block conditions and arguments.

Python expression syntax, evaluated by walking a whitelisted AST: no
``eval``, no attribute access on real objects, no calls except a fixed set of
functions. Sequences are files people share, so a condition must never be
able to run code.

* Names are sequence variables; a leading ``$`` is allowed (``$img``).
* ``a.b`` reads key ``b`` of dict ``a`` (never a Python attribute).
* ``a[0]``, ``a["k"]`` index lists/dicts.
* Literals, ``+ - * / // % **``, comparisons, ``and/or/not``, ``x if c else y``,
  list/tuple/dict displays.
* Functions: ``has(cap)``, ``mode()``, ``len abs min max round int float str
  bool sum any all``.
"""

from __future__ import annotations

import ast
import operator
import re
from typing import Any, Callable

_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UNARY = {ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Not: operator.not_}
_CMP = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
    ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b,
}
_SAFE_FUNCS: dict[str, Callable] = {
    "len": len, "abs": abs, "min": min, "max": max, "round": round,
    "int": int, "float": float, "str": str, "bool": bool, "sum": sum,
    "any": any, "all": all,
}
_MAX_LEN = 2000
_MAX_POW = 64
_DOLLAR = re.compile(r"\$(?=[A-Za-z_])")


class ExprError(ValueError):
    pass


def _parse(source: str) -> ast.Expression:
    if not isinstance(source, str):
        raise ExprError(f"expression must be a string, got {type(source).__name__}")
    if len(source) > _MAX_LEN:
        raise ExprError("expression too long")
    try:
        return ast.parse(_DOLLAR.sub("", source.strip()), mode="eval")
    except SyntaxError as exc:
        raise ExprError(f"syntax error in {source!r}: {exc.msg}") from None


def evaluate(source: str, variables: dict[str, Any] | None = None,
             functions: dict[str, Callable] | None = None) -> Any:
    """Evaluate *source*. *functions* adds callables such as ``has``/``mode``."""
    tree = _parse(source)
    funcs = {**_SAFE_FUNCS, **(functions or {})}
    return _Eval(variables or {}, funcs).visit(tree.body)


def has_calls(source: str) -> set[str]:
    """Capability names that *source* tests with ``has("...")`` in a way that
    guards the then-branch: the whole condition, or a top-level ``and`` term.
    ``not has(x)`` and ``has(x) or ...`` guarantee nothing, so they are left out."""
    try:
        body = _parse(source).body
    except ExprError:
        return set()
    terms = body.values if isinstance(body, ast.BoolOp) and isinstance(body.op, ast.And) \
        else [body]
    out = set()
    for term in terms:
        if (isinstance(term, ast.Call) and isinstance(term.func, ast.Name)
                and term.func.id == "has" and len(term.args) == 1
                and isinstance(term.args[0], ast.Constant)
                and isinstance(term.args[0].value, str)):
            out.add(term.args[0].value)
    return out


def uses_function(source: str, name: str) -> bool:
    try:
        tree = _parse(source)
    except ExprError:
        return False
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
               for n in ast.walk(tree))


def check(source: str, functions: set[str] | None = None) -> None:
    """Raise :class:`ExprError` if *source* uses anything not allowed, without
    evaluating it (for validating a sequence before it runs)."""
    tree = _parse(source)
    allowed = set(_SAFE_FUNCS) | {"has", "mode"} | (functions or set())
    for node in ast.walk(tree):
        if type(node) not in _ALLOWED_NODES:
            raise ExprError(f"{type(node).__name__} is not allowed in expressions")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in allowed:
                raise ExprError("only these functions are allowed: "
                                + ", ".join(sorted(allowed)))
            if node.keywords:
                raise ExprError("keyword arguments are not allowed in expressions")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ExprError(f"field {node.attr!r} is not allowed")


_ALLOWED_NODES = {
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.BinOp, ast.UnaryOp,
    ast.Compare, ast.IfExp, ast.Call, ast.Name, ast.Load, ast.Constant,
    ast.Attribute, ast.Subscript, ast.Slice, ast.List, ast.Tuple, ast.Dict,
    *_BINOPS, *_UNARY, *_CMP,
}


class _Eval:
    def __init__(self, variables: dict, functions: dict):
        self.vars = variables
        self.funcs = functions

    def visit(self, node: ast.AST) -> Any:
        method = getattr(self, "v_" + type(node).__name__, None)
        if method is None:
            raise ExprError(f"{type(node).__name__} is not allowed in expressions")
        return method(node)

    def v_Constant(self, node):
        return node.value

    def v_Name(self, node):
        if node.id in ("True", "False", "None"):
            return {"True": True, "False": False, "None": None}[node.id]
        if node.id in self.vars:
            return self.vars[node.id]
        raise ExprError(f"unknown variable {node.id!r}")

    def v_Attribute(self, node):
        if node.attr.startswith("_"):
            raise ExprError(f"field {node.attr!r} is not allowed")
        base = self.visit(node.value)
        if isinstance(base, dict):
            if node.attr in base:
                return base[node.attr]
            raise ExprError(f"no field {node.attr!r} (have: {', '.join(map(str, base))})")
        raise ExprError(f"cannot read .{node.attr} of a {type(base).__name__}")

    def v_Subscript(self, node):
        base = self.visit(node.value)
        if isinstance(node.slice, ast.Slice):
            parts = [None if p is None else self.visit(p)
                     for p in (node.slice.lower, node.slice.upper, node.slice.step)]
            if not isinstance(base, (list, tuple, str)):
                raise ExprError("only lists and strings can be sliced")
            return base[slice(*parts)]
        key = self.visit(node.slice)
        if not isinstance(base, (dict, list, tuple, str)):
            raise ExprError(f"cannot index a {type(base).__name__}")
        try:
            return base[key]
        except (KeyError, IndexError, TypeError) as exc:
            raise ExprError(f"index {key!r} not found: {exc}") from None

    def v_List(self, node):
        return [self.visit(e) for e in node.elts]

    def v_Tuple(self, node):
        return [self.visit(e) for e in node.elts]

    def v_Dict(self, node):
        if any(k is None for k in node.keys):
            raise ExprError("dict unpacking is not allowed")
        return {self.visit(k): self.visit(v) for k, v in zip(node.keys, node.values)}

    def v_BoolOp(self, node):
        if isinstance(node.op, ast.And):
            value = True
            for v in node.values:
                value = self.visit(v)
                if not value:
                    return value
            return value
        value = False
        for v in node.values:
            value = self.visit(v)
            if value:
                return value
        return value

    def v_BinOp(self, node):
        op = _BINOPS.get(type(node.op))
        if op is None:
            raise ExprError(f"operator {type(node.op).__name__} is not allowed")
        left, right = self.visit(node.left), self.visit(node.right)
        if isinstance(node.op, ast.Pow) and isinstance(right, (int, float)) \
                and abs(right) > _MAX_POW:
            raise ExprError("exponent too large")
        if isinstance(node.op, ast.Mult) and (
                isinstance(left, (str, list)) or isinstance(right, (str, list))):
            raise ExprError("repeating strings/lists is not allowed")
        try:
            return op(left, right)
        except Exception as exc:
            raise ExprError(f"{type(exc).__name__}: {exc}") from None

    def v_UnaryOp(self, node):
        op = _UNARY.get(type(node.op))
        if op is None:
            raise ExprError(f"operator {type(node.op).__name__} is not allowed")
        return op(self.visit(node.operand))

    def v_Compare(self, node):
        left = self.visit(node.left)
        for op_node, comp in zip(node.ops, node.comparators):
            right = self.visit(comp)
            try:
                ok = _CMP[type(op_node)](left, right)
            except TypeError as exc:
                raise ExprError(f"cannot compare: {exc}") from None
            if not ok:
                return False
            left = right
        return True

    def v_IfExp(self, node):
        return self.visit(node.body) if self.visit(node.test) else self.visit(node.orelse)

    def v_Call(self, node):
        if not isinstance(node.func, ast.Name):
            raise ExprError("only plain function calls are allowed")
        func = self.funcs.get(node.func.id)
        if func is None:
            raise ExprError(f"unknown function {node.func.id!r}")
        if node.keywords:
            raise ExprError("keyword arguments are not allowed in expressions")
        args = [self.visit(a) for a in node.args]
        try:
            return func(*args)
        except ExprError:
            raise
        except Exception as exc:
            raise ExprError(f"{node.func.id}(): {type(exc).__name__}: {exc}") from None


def resolve_args(args: Any, variables: dict, functions: dict | None = None,
                 roles: Callable[[str], Any] | None = None) -> Any:
    """Resolve ``{"$expr": ...}`` and ``{"$role": ...}`` values in *args*."""
    if isinstance(args, dict):
        if set(args) == {"$expr"}:
            return evaluate(args["$expr"], variables, functions)
        if set(args) == {"$role"}:
            if roles is None:
                raise ExprError(f"no role table to resolve {args['$role']!r}")
            return roles(args["$role"])
        return {k: resolve_args(v, variables, functions, roles) for k, v in args.items()}
    if isinstance(args, list):
        return [resolve_args(v, variables, functions, roles) for v in args]
    return args
