"""Static verification of compiled programs (defence in depth).

The compiler is deterministic and quotes every user/LLM string with repr(), so
its output should always pass. This check exists so that a compiler bug, or a
future code path that emits Python some other way, cannot smuggle in imports,
dunder access, I/O or arbitrary calls. It runs on the exact source that is
executed.
"""
from __future__ import annotations

import ast

ALLOWED_NODES = (
    ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Assign, ast.Return, ast.Expr,
    ast.Name, ast.Load, ast.Store, ast.Constant, ast.List, ast.Tuple, ast.Dict,
    ast.Attribute, ast.Subscript, ast.Call, ast.keyword, ast.Slice,
    ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.ListComp, ast.comprehension,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.USub, ast.UAdd,
    ast.Not, ast.Invert, ast.And, ast.Or, ast.BitAnd, ast.BitOr,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
)
ALLOWED_GLOBALS = {"tables", "xl", "pd"}
XL_FUNCS = {"mask", "div", "days_between", "abs_", "round_", "isnull", "coalesce", "if_else",
            "with_column", "aggregate", "join", "pivot"}
PD_FUNCS = {"Timestamp"}
METHODS = {"copy", "loc", "sort_values", "head", "melt", "isin", "dt", "str", "year", "month",
           "quarter", "day", "dayofweek", "lower", "upper", "strip", "contains", "startswith"}


class VerificationError(ValueError):
    pass


def verify(source: str) -> None:
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as e:
        raise VerificationError(f"compiled code does not parse: {e}") from None
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef) or tree.body[0].name != "run":
        raise VerificationError("program must be exactly one function `run`")
    fn = tree.body[0]
    if fn.decorator_list or fn.returns or [a.arg for a in fn.args.args] != ["tables"] \
            or fn.args.vararg or fn.args.kwarg or fn.args.kwonlyargs or fn.args.defaults:
        raise VerificationError("run() must take exactly one argument `tables`")
    if not fn.body or not isinstance(fn.body[-1], ast.Return):
        raise VerificationError("run() must end with return")

    local: set[str] = set()
    for stmt in fn.body[:-1]:
        if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)):
            raise VerificationError(f"line {stmt.lineno}: only `name = expression` statements are allowed")
    comp_vars: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.comprehension):
            if not isinstance(node.target, ast.Name):
                raise VerificationError("comprehension target must be a name")
            comp_vars.add(node.target.id)

    for stmt in fn.body:
        for node in ast.walk(stmt):
            if not isinstance(node, ALLOWED_NODES):
                raise VerificationError(f"line {getattr(node, 'lineno', '?')}: {type(node).__name__} not allowed")
            if isinstance(node, ast.Name):
                if node.id.startswith("_"):
                    raise VerificationError(f"name {node.id!r} not allowed")
                if isinstance(node.ctx, ast.Load) and node.id not in ALLOWED_GLOBALS | local | comp_vars:
                    raise VerificationError(f"line {node.lineno}: unknown name {node.id!r}")
            if isinstance(node, ast.Attribute):
                if node.attr.startswith("_"):
                    raise VerificationError(f"line {node.lineno}: private attribute {node.attr!r}")
                base = node.value
                if isinstance(base, ast.Name) and base.id == "xl":
                    if node.attr not in XL_FUNCS:
                        raise VerificationError(f"xl.{node.attr} is not an allowed helper")
                elif isinstance(base, ast.Name) and base.id == "pd":
                    if node.attr not in PD_FUNCS:
                        raise VerificationError(f"pd.{node.attr} is not allowed")
                elif node.attr not in METHODS:
                    raise VerificationError(f"line {node.lineno}: method/attribute {node.attr!r} not allowed")
            if isinstance(node, ast.Call):
                f = node.func
                if not (isinstance(f, ast.Attribute)):
                    raise VerificationError(f"line {node.lineno}: only method and helper calls are allowed")
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) > 2000:
                raise VerificationError("string literal too long")
        if isinstance(stmt, ast.Assign):
            name = stmt.targets[0].id  # type: ignore[union-attr]
            if name in ALLOWED_GLOBALS or name.startswith("_"):
                raise VerificationError(f"cannot assign to {name!r}")
            local.add(name)
