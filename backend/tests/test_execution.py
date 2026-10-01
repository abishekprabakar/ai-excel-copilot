"""Compiled programs executed in the sandbox, checked against values computed
independently from raw openpyxl cell reads (not from the profiler's frames)."""
import math
from collections import defaultdict

import pandas as pd
import pytest

from excel_copilot.ir.compiler import compile_program
from excel_copilot.sandbox.executor import execute

from .conftest import program


def run(demo, steps, output):
    c = compile_program(program(steps, output), demo.profile)
    assert c.ok, [d.render() for d in c.diagnostics]
    res = execute(c.source, {t: demo.frames[t] for t in c.tables_used})
    assert res.ok, res.error
    return res.frame


def test_revenue_by_region_matches_raw_cells(demo, raw_orders):
    df = run(demo, [{"op": "load", "out": "o", "table": "orders"},
                    {"op": "aggregate", "out": "g", "input": "o", "group_by": ["Region"],
                     "aggregations": [{"column": "Revenue", "func": "sum", "as_name": "Revenue"},
                                      {"column": "*", "func": "count", "as_name": "Orders"}]}], "g")
    exp = defaultdict(float)
    cnt = defaultdict(int)
    for r in raw_orders:
        exp[r["Region"]] += r["Revenue"]
        cnt[r["Region"]] += 1
    got = {row.Region: (row.Revenue, row.Orders) for row in df.itertuples()}
    assert set(got) == set(exp)
    for k in exp:
        assert got[k][0] == pytest.approx(exp[k], rel=1e-12)
        assert got[k][1] == cnt[k]


def test_filter_dates_and_division_semantics(demo, raw_orders):
    df = run(demo, [{"op": "load", "out": "o", "table": "orders"},
                    {"op": "filter", "out": "f", "input": "o",
                     "condition": "`Order Date` >= '2024-07-01' and `Order Date` < '2024-10-01' and Status == 'Shipped'"},
                    {"op": "derive", "out": "d", "input": "f", "column": "Rev per unit", "expression": "Revenue / Units"},
                    {"op": "aggregate", "out": "g", "input": "d", "group_by": [],
                     "aggregations": [{"column": "Revenue", "func": "sum", "as_name": "rev"},
                                      {"column": "Rev per unit", "func": "max", "as_name": "mx"}]}], "g")
    rows = [r for r in raw_orders if r["Status"] == "Shipped" and 7 <= r["Order Date"].month <= 9]
    assert df["rev"][0] == pytest.approx(sum(r["Revenue"] for r in rows), rel=1e-12)
    per = [r["Revenue"] / int(r["Units"]) for r in rows if int(r["Units"]) != 0]
    assert df["mx"][0] == pytest.approx(max(per), rel=1e-12)


def test_zero_division_is_missing_not_infinite(demo):
    df = run(demo, [{"op": "load", "out": "o", "table": "orders"},
                    {"op": "filter", "out": "z", "input": "o", "condition": "Units == 0"},
                    {"op": "derive", "out": "d", "input": "z", "column": "r", "expression": "Revenue / Units"}], "d")
    assert len(df) == 1 and pd.isna(df["r"][0])


def test_join_and_left_join_keeps_orphans(demo, raw_orders, raw_sheet):
    df = run(demo, [{"op": "load", "out": "o", "table": "orders"}, {"op": "load", "out": "c", "table": "customers"},
                    {"op": "join", "out": "j", "left": "o", "right": "c", "left_on": ["Customer ID"],
                     "right_on": ["Customer ID"], "how": "left"},
                    {"op": "aggregate", "out": "g", "input": "j", "group_by": ["Segment"],
                     "aggregations": [{"column": "Revenue", "func": "sum", "as_name": "Revenue"}]}], "g")
    seg = {r["Customer ID"]: r["Segment"] for r in raw_sheet("Customers")}
    exp = defaultdict(float)
    for r in raw_orders:
        exp[seg.get(r["Customer ID"])] += r["Revenue"]
    got = {(None if pd.isna(k) else k): v
           for k, v in zip(df["Segment"], df["Revenue"])}
    assert got.keys() == exp.keys() and None in got              # orphan orders kept, segment missing
    for k in exp:
        assert got[k] == pytest.approx(exp[k], rel=1e-12)


def test_unpivot_wide_targets(demo, raw_sheet):
    df = run(demo, [{"op": "load", "out": "t", "table": "targets"},
                    {"op": "unpivot", "out": "u", "input": "t", "id_columns": ["Region"],
                     "value_columns": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
                     "var_name": "Month", "value_name": "Target"},
                    {"op": "aggregate", "out": "g", "input": "u", "group_by": ["Region"],
                     "aggregations": [{"column": "Target", "func": "sum", "as_name": "Annual"}]}], "g")
    rows = [r for r in raw_sheet("Targets") if r["Region"] in ("West", "East", "North", "South")]
    exp = {r["Region"]: sum(v for k, v in r.items() if k != "Region") for r in rows}
    assert dict(zip(df["Region"], df["Annual"])) == exp


def test_top_n_and_pivot(demo, raw_orders):
    df = run(demo, [{"op": "load", "out": "o", "table": "orders"},
                    {"op": "aggregate", "out": "g", "input": "o", "group_by": ["SKU"],
                     "aggregations": [{"column": "Units", "func": "sum", "as_name": "Units"}]},
                    {"op": "sort", "out": "s", "input": "g", "by": [{"column": "Units", "descending": True},
                                                                     {"column": "SKU", "descending": False}]},
                    {"op": "limit", "out": "top", "input": "s", "n": 3}], "top")
    tot = defaultdict(int)
    for r in raw_orders:
        tot[r["SKU"]] += int(r["Units"])
    exp = sorted(tot.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    assert list(zip(df["SKU"], df["Units"])) == exp
    pv = run(demo, [{"op": "load", "out": "o", "table": "orders"},
                    {"op": "pivot", "out": "p", "input": "o", "index": ["Region"], "columns": "Status",
                     "values": "Revenue", "func": "sum"}], "p")
    assert set(pv.columns) == {"Region", "Pending", "Returned", "Shipped"}
    west = sum(r["Revenue"] for r in raw_orders if r["Region"] == "West" and r["Status"] == "Returned")
    assert pv.set_index("Region").loc["West", "Returned"] == pytest.approx(west, rel=1e-12)


def test_join_explosion_is_refused(demo):
    c = compile_program(program([
        {"op": "load", "out": "a", "table": "orders"}, {"op": "load", "out": "b", "table": "orders"},
        {"op": "join", "out": "j", "left": "a", "right": "b", "left_on": ["Region"], "right_on": ["Region"],
         "how": "inner"}], "j"), demo.profile)
    assert c.ok
    big = {"orders": demo.frames["orders"].sample(n=6000, replace=True, random_state=1)}
    res = execute(c.source, big)
    assert not res.ok and "join would produce" in res.error
