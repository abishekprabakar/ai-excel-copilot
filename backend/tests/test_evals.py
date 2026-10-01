from pathlib import Path

import pandas as pd

from excel_copilot.evals import matches, run_eval
from excel_copilot.llm.heuristic import HeuristicBackend

SPEC = Path(__file__).resolve().parents[1] / "evals" / "sales_ops.json"


def test_matcher_ignores_names_and_order_but_not_values():
    exp = pd.DataFrame({"Region": ["West", "East"], "Revenue": [10.0, 20.0]})
    assert matches(pd.DataFrame({"r": ["east", "west"], "total": [20.0, 10.0], "extra": [1, 2]}), exp)[0]
    assert not matches(pd.DataFrame({"r": ["east", "west"], "total": [10.0, 20.0]}), exp)[0]   # rows misaligned
    assert not matches(pd.DataFrame({"r": ["West", "East"], "total": [10.0, 20.1]}), exp)[0]
    assert not matches(pd.DataFrame({"r": ["West"], "total": [10.0]}), exp)[0]
    assert not matches(pd.DataFrame({"r": ["East", "West"], "t": [20.0, 10.0]}), exp, ordered=True)[0]


def test_offline_backend_on_eval_set():
    results = run_eval(SPEC, HeuristicBackend())      # also validates every reference program
    assert len(results) == 12
    passed = {r.id for r in results if r.passed}
    # the rule-based planner handles single-table/single-join aggregations; the
    # compositional cases (derived profit, actual-vs-target, shares) need Claude
    assert {"rev_by_region", "top5_sku_q3", "avg_discount_segment", "monthly_shipped", "top_category"} <= passed
    assert "actual_vs_target_q1" not in passed
