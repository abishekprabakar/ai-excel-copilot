from excel_copilot.ir.models import Program
from excel_copilot.ir.wire import WireError, from_program, to_program

import pytest

ALL_OPS = Program.model_validate({"steps": [
    {"op": "load", "out": "o", "table": "orders"},
    {"op": "filter", "out": "f", "input": "o", "condition": "Units > 0"},
    {"op": "derive", "out": "d", "input": "f", "column": "x", "expression": "Units * 2"},
    {"op": "select", "out": "s", "input": "d", "columns": ["Region", "x"]},
    {"op": "aggregate", "out": "a", "input": "s", "group_by": ["Region"],
     "aggregations": [{"column": "x", "func": "sum", "as_name": "X"}]},
    {"op": "load", "out": "t", "table": "targets"},
    {"op": "join", "out": "j", "left": "a", "right": "t", "left_on": ["Region"], "right_on": ["Region"], "how": "left"},
    {"op": "sort", "out": "so", "input": "j", "by": [{"column": "X", "descending": True}]},
    {"op": "limit", "out": "l", "input": "so", "n": 3},
    {"op": "unpivot", "out": "u", "input": "l", "id_columns": ["Region"], "value_columns": ["Jan", "Feb"],
     "var_name": "Month", "value_name": "T"},
    {"op": "pivot", "out": "p", "input": "u", "index": ["Region"], "columns": "Month", "values": "T", "func": "sum"},
], "output": "p", "title": "t", "chart": {"kind": "none", "x": "", "y": ""}})


def test_round_trip_every_op():
    assert to_program(from_program(ALL_OPS)) == ALL_OPS


def test_bad_values_reported_per_step():
    w = from_program(ALL_OPS)
    w.steps[6].how = "outer"
    w.steps[10].func = "avg"
    w.chart_kind = "pie"
    with pytest.raises(WireError) as e:
        to_program(w)
    text = " | ".join(e.value.problems)
    assert "step 6" in text and "'outer'" in text and "step 10" in text and "'avg'" in text and "pie" in text
