"""Expression language and IR validator/compiler."""
import pytest

from excel_copilot.ir.compiler import compile_program
from excel_copilot.ir.expr import Checker, ExprError
from excel_copilot.sandbox.verify import verify

from .conftest import program

SCHEMA = {"Revenue": "num", "Units": "num", "Region": "str", "Order Date": "date", "Flag": "bool"}


def chk(src):
    return Checker(dict(SCHEMA), "df").check(src)


@pytest.mark.parametrize("src,code,typ", [
    ("Revenue / Units", "xl.div(df['Revenue'], df['Units'])", "num"),
    ("`Order Date` >= '2024-07-01'", "(df['Order Date'] >= pd.Timestamp('2024-07-01'))", "bool"),
    ("year(`Order Date`) == 2024 and Region != 'West'",
     "((df['Order Date'].dt.year == 2024) & (df['Region'] != 'West'))", "bool"),
    ("Region in ['West', 'East']", "df['Region'].isin(['West', 'East'])", "bool"),
    ("not Flag", "(~df['Flag'])", "bool"),
    ("0 < Units <= 10", "((0 < df['Units']) & (df['Units'] <= 10))", "bool"),
    ("if_else(Units > 0, Revenue, 0)", "xl.if_else(df, (df['Units'] > 0), df['Revenue'], 0)", "num"),
    ("round(Revenue * 1.09, 2)", "xl.round_((df['Revenue'] * 1.09), 2)", "num"),
])
def test_compiles(src, code, typ):
    t = chk(src)
    assert (t.code, t.type) == (code, typ)


@pytest.mark.parametrize("src,msg", [
    ("Revnue > 0", "did you mean 'Revenue'"),
    ("Region * 2", "arithmetic needs num"),
    ("Region == 3", "cannot compare str with num"),
    ("Region = 'West'", "use == for equality"),
    ("df.columns", "Attribute is not allowed"),
    ("__import__('os')", "unknown function __import__"),
    ("(lambda: 1)()", "Lambda is not allowed"),
    ("Revenue[0]", "Subscript is not allowed"),
    ("`Order Date` > '07/01/2024'", "not an ISO date"),
    ("sum(Revenue)", "unknown function sum"),
    ("year(Region)", "year() expects date"),
    ("Revenue == None", "use isnull"),
])
def test_rejects(src, msg):
    with pytest.raises(ExprError) as e:
        chk(src)
    assert msg in f"{e.value} {e.value.hint}"


def errors(prog, demo):
    c = compile_program(prog, demo.profile)
    return c, [d.render() for d in c.errors]


def test_valid_program_compiles_and_verifies(demo):
    p = program([
        {"op": "load", "out": "o", "table": "orders"},
        {"op": "load", "out": "c", "table": "customers"},
        {"op": "join", "out": "oc", "left": "o", "right": "c", "left_on": ["Customer ID"],
         "right_on": ["Customer ID"], "how": "left"},
        {"op": "aggregate", "out": "g", "input": "oc", "group_by": ["Segment"],
         "aggregations": [{"column": "Revenue", "func": "sum", "as_name": "Revenue"}]},
    ], "g", chart={"kind": "bar", "x": "Segment", "y": "Revenue"})
    c, errs = errors(p, demo)
    assert c.ok and not errs
    assert c.tables_used == ["customers", "orders"]
    assert c.schemas["g"] == {"Segment": "str", "Revenue": "num"}
    verify(c.source)


@pytest.mark.parametrize("steps,output,needle", [
    ([{"op": "load", "out": "o", "table": "order"}], "o", "unknown table 'order'"),
    ([{"op": "filter", "out": "f", "input": "o", "condition": "Units > 0"}], "f", "frame 'o' is not defined"),
    ([{"op": "load", "out": "o", "table": "orders"},
      {"op": "aggregate", "out": "g", "input": "o", "group_by": [],
       "aggregations": [{"column": "Customer ID", "func": "sum", "as_name": "x"}]}], "g", "is an identifier"),
    ([{"op": "load", "out": "o", "table": "orders"},
      {"op": "aggregate", "out": "g", "input": "o", "group_by": ["Region"],
       "aggregations": [{"column": "Region", "func": "mean", "as_name": "x"}]}], "g", "needs a numeric column"),
    ([{"op": "load", "out": "o", "table": "orders"}, {"op": "load", "out": "p", "table": "products"},
      {"op": "join", "out": "j", "left": "o", "right": "p", "left_on": ["Customer ID"], "right_on": ["SKU"],
       "how": "inner"}], "j", "join key types differ"),
    ([{"op": "load", "out": "o", "table": "orders"},
      {"op": "filter", "out": "f", "input": "o", "condition": "Units + 1"}], "f", "must be boolean"),
    ([{"op": "load", "out": "tables", "table": "orders"}], "tables", "invalid frame name"),
    ([{"op": "load", "out": "o", "table": "orders"}, {"op": "load", "out": "o", "table": "products"}], "o",
     "already defined"),
    ([{"op": "load", "out": "o", "table": "orders"}], "missing", "output frame 'missing'"),
])
def test_validation_errors(demo, steps, output, needle):
    c, errs = errors(program(steps, output), demo)
    assert not c.ok and c.source == ""
    assert any(needle in e for e in errs), errs


def test_errors_carry_hints_for_repair(demo):
    c, errs = errors(program([{"op": "load", "out": "o", "table": "orders"},
                              {"op": "select", "out": "s", "input": "o", "columns": ["Revnue"]}], "s"), demo)
    assert "did you mean 'Revenue'" in errs[0] and errs[0].startswith("[error] step 1 (columns)")


def test_chart_validation(demo):
    c, errs = errors(program([{"op": "load", "out": "o", "table": "orders"}], "o",
                             chart={"kind": "bar", "x": "Region", "y": "Status"}), demo)
    assert any("chart y must be numeric" in e for e in errs)


def test_join_schema_tracks_suffixes(demo):
    c, _ = errors(program([
        {"op": "load", "out": "o", "table": "orders"}, {"op": "load", "out": "p", "table": "products"},
        {"op": "join", "out": "j", "left": "o", "right": "p", "left_on": ["SKU"], "right_on": ["SKU"], "how": "left"},
        {"op": "select", "out": "s", "input": "j", "columns": ["SKU", "Unit Cost", "Unit Cost_right", "Category"]}],
        "s"), demo)
    assert c.ok, [d.render() for d in c.diagnostics]
