"""A tiny, typed expression language for IR conditions and derived columns.

Syntax is a strict subset of Python expressions, plus backtick-quoted column
names for headers with spaces:

    `Unit Price` * Units * (1 - Discount)
    year(`Order Date`) == 2024 and Region in ['West', 'East']
    if_else(Revenue > 0, (Revenue - Cost) / Revenue, 0)

Parsing goes through Python's `ast` with an allowlist of node types; anything
else (attributes, subscripts, lambdas, comprehensions, unknown functions) is a
validation error, not a runtime surprise. Each node is type-checked against the
input frame's schema and compiled to vectorised pandas.
"""
from __future__ import annotations

import ast
import datetime as dt
import re
from dataclasses import dataclass
from typing import Literal

EType = Literal["num", "str", "date", "bool", "null"]

COLUMN_TYPE_MAP: dict[str, EType] = {
    "integer": "num", "number": "num", "currency": "num", "percent": "num",
    "string": "str", "date": "date", "datetime": "date", "boolean": "bool", "empty": "null",
}


class ExprError(ValueError):
    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


@dataclass
class Typed:
    code: str                       # compiled pandas source
    type: EType
    is_series: bool                 # False for scalar literals


# name -> (arg kinds, return type). "num|date" etc. are alternatives.
FUNCTIONS: dict[str, tuple[list[str], EType | str]] = {
    "year": (["date"], "num"), "month": (["date"], "num"), "quarter": (["date"], "num"),
    "day": (["date"], "num"), "weekday": (["date"], "num"),
    "abs": (["num"], "num"), "round": (["num", "intlit"], "num"),
    "lower": (["str"], "str"), "upper": (["str"], "str"), "strip": (["str"], "str"),
    "contains": (["str", "strlit"], "bool"), "startswith": (["str", "strlit"], "bool"),
    "isnull": (["any"], "bool"), "notnull": (["any"], "bool"),
    "coalesce": (["any", "any"], "same"), "if_else": (["bool", "any", "any"], "same2"),
    "between": (["num|date", "any", "any"], "bool"),
    "date": (["strlit"], "date"),
}
_ALLOWED_NODES = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.Call, ast.Name,
                  ast.Constant, ast.List, ast.Tuple, ast.Load, ast.Add, ast.Sub, ast.Mult, ast.Div,
                  ast.FloorDiv, ast.Mod, ast.Pow, ast.USub, ast.UAdd, ast.Not, ast.And, ast.Or, ast.Eq,
                  ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn)
_BACKTICK = re.compile(r"`([^`]*)`")
MAX_LEN = 500


def _preprocess(src: str) -> str:
    if len(src) > MAX_LEN:
        raise ExprError(f"expression longer than {MAX_LEN} characters")
    return _BACKTICK.sub(lambda m: f"col({m.group(1)!r})", src)


def parse(src: str) -> ast.Expression:
    try:
        tree = ast.parse(_preprocess(src.strip()), mode="eval")
    except SyntaxError as e:
        hint = "use == for equality" if re.search(r"[^=!<>]=[^=]", src) else None
        raise ExprError(f"syntax error in expression {src!r}: {e.msg}", hint) from None
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ExprError(f"{type(node).__name__} is not allowed in expressions",
                            "only columns, literals, arithmetic, comparisons, and/or/not, "
                            f"and functions {', '.join(sorted(FUNCTIONS))} are allowed")
    return tree


def _parse_date(s: str) -> str:
    try:
        return dt.date.fromisoformat(s[:10]).isoformat()
    except ValueError:
        raise ExprError(f"{s!r} is not an ISO date (YYYY-MM-DD)") from None


class Checker:
    """Type-checks an expression against `schema` and compiles it for frame `df`."""

    def __init__(self, schema: dict[str, EType], df: str):
        self.schema = schema
        self.df = df
        self.columns_used: list[str] = []

    def column(self, name: str) -> Typed:
        if name not in self.schema:
            import difflib
            close = difflib.get_close_matches(name, list(self.schema), n=3, cutoff=0.5)
            raise ExprError(f"unknown column {name!r}",
                            f"did you mean {', '.join(map(repr, close))}?" if close
                            else f"available: {', '.join(list(self.schema)[:20])}")
        self.columns_used.append(name)
        return Typed(f"{self.df}[{name!r}]", self.schema[name], True)

    def check(self, src: str) -> Typed:
        return self.visit(parse(src).body)

    # ------------------------------------------------------------------ visitors
    def visit(self, n: ast.AST) -> Typed:
        if isinstance(n, ast.Constant):
            v = n.value
            if isinstance(v, bool):
                return Typed(repr(v), "bool", False)
            if isinstance(v, (int, float)):
                return Typed(repr(v), "num", False)
            if isinstance(v, str):
                return Typed(repr(v), "str", False)
            if v is None:
                return Typed("None", "null", False)
            raise ExprError(f"unsupported literal {v!r}")
        if isinstance(n, ast.Name):
            if n.id in ("true", "false"):
                return Typed(repr(n.id == "true"), "bool", False)
            return self.column(n.id)
        if isinstance(n, ast.UnaryOp):
            x = self.visit(n.operand)
            if isinstance(n.op, ast.Not):
                self._want(x, "bool", "not")
                return Typed(f"(~{x.code})" if x.is_series else f"(not {x.code})", "bool", x.is_series)
            self._want(x, "num", "unary minus")
            return Typed(f"(-{x.code})", "num", x.is_series)
        if isinstance(n, ast.BinOp):
            a, b = self.visit(n.left), self.visit(n.right)
            if isinstance(n.op, ast.Add) and a.type == b.type == "str":
                return Typed(f"({a.code} + {b.code})", "str", a.is_series or b.is_series)
            if isinstance(n.op, ast.Sub) and a.type == b.type == "date":
                return Typed(f"xl.days_between({b.code}, {a.code})", "num", True)
            self._want(a, "num", "arithmetic")
            self._want(b, "num", "arithmetic")
            ser = a.is_series or b.is_series
            if isinstance(n.op, ast.Div):
                return Typed(f"xl.div({a.code}, {b.code})", "num", ser)
            op = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.FloorDiv: "//", ast.Mod: "%", ast.Pow: "**"}[type(n.op)]
            return Typed(f"({a.code} {op} {b.code})", "num", ser)
        if isinstance(n, ast.BoolOp):
            parts = [self.visit(v) for v in n.values]
            for p in parts:
                self._want(p, "bool", "and/or")
            op = " & " if isinstance(n.op, ast.And) else " | "
            return Typed("(" + op.join(p.code for p in parts) + ")", "bool", any(p.is_series for p in parts))
        if isinstance(n, ast.Compare):
            left = self.visit(n.left)
            pieces = []
            for op, right_node in zip(n.ops, n.comparators):
                if isinstance(op, (ast.In, ast.NotIn)):
                    pieces.append(self._isin(left, right_node, isinstance(op, ast.NotIn)))
                    continue
                right = self.visit(right_node)
                left_c, right_c = self._coerce_pair(left, right)
                sym = {ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">="}[type(op)]
                if sym not in ("==", "!=") and left_c.type in ("bool", "null"):
                    raise ExprError(f"cannot order-compare {left_c.type} values")
                pieces.append(f"({left_c.code} {sym} {right_c.code})")
                left = right
            return Typed("(" + " & ".join(pieces) + ")" if len(pieces) > 1 else pieces[0], "bool", True)
        if isinstance(n, ast.Call):
            return self._call(n)
        if isinstance(n, (ast.List, ast.Tuple)):
            raise ExprError("lists are only allowed on the right of 'in'")
        raise ExprError(f"unsupported syntax: {type(n).__name__}")

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _want(x: Typed, t: str, what: str) -> None:
        if x.type != t and x.type != "null":
            raise ExprError(f"{what} needs {t} values but got {x.type} ({x.code})",
                            "dates support year()/month()/quarter() and date('YYYY-MM-DD') comparisons"
                            if x.type == "date" else None)

    def _coerce_pair(self, a: Typed, b: Typed) -> tuple[Typed, Typed]:
        """Allow date columns to compare with ISO date strings; otherwise types must match."""
        if a.type == "date" and b.type == "str" and not b.is_series:
            return a, Typed(f"pd.Timestamp({_parse_date(ast.literal_eval(b.code))!r})", "date", False)  # literal only
        if b.type == "date" and a.type == "str" and not a.is_series:
            return Typed(f"pd.Timestamp({_parse_date(ast.literal_eval(a.code))!r})", "date", False), b
        if "null" in (a.type, b.type):
            raise ExprError("compare with None is not supported", "use isnull(x) / notnull(x)")
        if a.type != b.type:
            raise ExprError(f"cannot compare {a.type} with {b.type} ({a.code} vs {b.code})",
                            "numbers stored as text are converted during loading; compare like with like")
        return a, b

    def _isin(self, left: Typed, node: ast.AST, negate: bool) -> str:
        if not isinstance(node, (ast.List, ast.Tuple)) or not all(isinstance(e, ast.Constant) for e in node.elts):
            raise ExprError("'in' needs a literal list, e.g. Region in ['West', 'East']")
        vals = [e.value for e in node.elts]  # type: ignore[attr-defined]
        if not vals or len(vals) > 200:
            raise ExprError("'in' list must have 1-200 items")
        kinds = {"str" if isinstance(v, str) else "bool" if isinstance(v, bool) else "num" for v in vals}
        if left.type == "date" and kinds == {"str"}:
            vals = [_parse_date(v) for v in vals]
            code = f"{left.code}.isin([pd.Timestamp(v) for v in {vals!r}])"
        else:
            if kinds != {left.type}:
                raise ExprError(f"'in' list items must be {left.type}")
            code = f"{left.code}.isin({vals!r})"
        if not left.is_series:
            raise ExprError("left side of 'in' must be a column expression")
        return f"(~{code})" if negate else code

    def _call(self, n: ast.Call) -> Typed:
        if not isinstance(n.func, ast.Name):
            raise ExprError("only plain function calls are allowed")
        name = n.func.id
        if n.keywords:
            raise ExprError(f"{name}() takes positional arguments only")
        if name == "col":
            if len(n.args) != 1 or not isinstance(n.args[0], ast.Constant) or not isinstance(n.args[0].value, str):
                raise ExprError("col() takes one column name string")
            return self.column(n.args[0].value)
        if name not in FUNCTIONS:
            import difflib
            close = difflib.get_close_matches(name, list(FUNCTIONS), n=2)
            raise ExprError(f"unknown function {name}()",
                            f"did you mean {', '.join(close)}?" if close else f"available: {', '.join(sorted(FUNCTIONS))}")
        kinds, ret = FUNCTIONS[name]
        if len(n.args) != len(kinds):
            raise ExprError(f"{name}() takes {len(kinds)} argument(s), got {len(n.args)}")
        if name == "date":
            a = n.args[0]
            if not (isinstance(a, ast.Constant) and isinstance(a.value, str)):
                raise ExprError("date() takes a literal 'YYYY-MM-DD' string")
            return Typed(f"pd.Timestamp({_parse_date(a.value)!r})", "date", False)
        args: list[Typed] = []
        for a, kind in zip(n.args, kinds):
            if kind == "intlit":
                if not (isinstance(a, ast.Constant) and isinstance(a.value, int) and 0 <= a.value <= 10):
                    raise ExprError(f"{name}() digits must be an integer literal 0-10")
                args.append(Typed(repr(a.value), "num", False))
                continue
            if kind == "strlit":
                if not (isinstance(a, ast.Constant) and isinstance(a.value, str)):
                    raise ExprError(f"{name}() needs a literal string")
                args.append(Typed(repr(a.value), "str", False))
                continue
            t = self.visit(a)
            if kind != "any" and t.type not in kind.split("|") and t.type != "null":
                raise ExprError(f"{name}() expects {kind} but got {t.type}")
            args.append(t)
        x = args[0]
        if name in ("year", "month", "quarter", "day", "weekday") and not x.is_series:
            raise ExprError(f"{name}() needs a date column")
        templates = {
            "year": lambda: f"{x.code}.dt.year", "month": lambda: f"{x.code}.dt.month", "quarter": lambda: f"{x.code}.dt.quarter",
            "day": lambda: f"{x.code}.dt.day", "weekday": lambda: f"{x.code}.dt.dayofweek",
            "abs": lambda: f"xl.abs_({x.code})", "round": lambda: f"xl.round_({x.code}, {args[-1].code})",
            "lower": lambda: f"{x.code}.str.lower()", "upper": lambda: f"{x.code}.str.upper()", "strip": lambda: f"{x.code}.str.strip()",
            "contains": lambda: f"{x.code}.str.contains({args[-1].code}, case=False, regex=False)",
            "startswith": lambda: f"{x.code}.str.startswith({args[-1].code})",
            "isnull": lambda: f"xl.isnull({x.code})", "notnull": lambda: f"(~xl.isnull({x.code}))",
            "coalesce": lambda: f"xl.coalesce({x.code}, {args[-1].code})",
            "if_else": lambda: f"xl.if_else({self.df}, {args[0].code}, {args[1].code}, {args[2].code})",
            "between": lambda: "",
        }
        code = templates[name]()
        if name == "between":
            lo, hi = (self._coerce_pair(x, a)[1] for a in args[1:])
            return Typed(f"(({x.code} >= {lo.code}) & ({x.code} <= {hi.code}))", "bool", True)
        if name in ("contains", "startswith", "lower", "upper", "strip") and not x.is_series:
            raise ExprError(f"{name}() needs a text column")
        if ret == "same":
            if args[0].type != args[1].type and "null" not in (args[0].type, args[1].type):
                raise ExprError(f"coalesce() arguments must share a type ({args[0].type} vs {args[1].type})")
            return Typed(code, args[0].type if args[0].type != "null" else args[1].type, True)
        if ret == "same2":
            a, b = args[1], args[2]
            if a.type != b.type and "null" not in (a.type, b.type):
                raise ExprError(f"if_else() branches must share a type ({a.type} vs {b.type})")
            return Typed(code, a.type if a.type != "null" else b.type, True)
        return Typed(code, ret, True)  # type: ignore[arg-type]
