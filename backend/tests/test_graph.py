import pytest

from excel_copilot.graph import build_graph, estimate_tokens, render_context


@pytest.mark.parametrize("budget", [150, 250, 400, 800, 3000])
def test_rendering_respects_budget(demo, budget):
    ctx = render_context(demo.profile, "revenue by customer segment", budget)
    assert ctx.tokens == estimate_tokens(ctx.text)
    # the only thing allowed to exceed the budget is the irreducible header when budget is tiny
    assert ctx.tokens <= budget or all(v <= 0 for v in ctx.levels.values())


def test_relevant_tables_keep_more_detail(demo):
    ctx = render_context(demo.profile, "total revenue by customer segment", 300)
    assert ctx.levels["orders"] >= 1 and ctx.levels["customers"] >= 1
    assert ctx.levels["targets"] <= 0 and ctx.levels["lookups"] <= 0
    assert "Segment" in ctx.text and "Revenue" in ctx.text


def test_full_context_is_much_smaller_than_profile_json(demo):
    full = estimate_tokens(demo.profile.model_dump_json())
    ctx = render_context(demo.profile, None, 100_000)
    assert ctx.tokens * 5 < full
    assert "=[Units]*[Unit Price]*(1-[Discount])" in ctx.text      # formulas carried as meaning
    assert "-> customers.Customer ID" in ctx.text and "~> products.SKU" in ctx.text
    assert "Jan..Dec (12 period cols)" in ctx.text                  # wide table collapsed


def test_pii_never_reaches_context(demo, raw_sheet):
    emails = [r["Email"] for r in raw_sheet("Customers")]
    for budget in (300, 2000, 100_000):
        text = render_context(demo.profile, "customer email", budget).text
        assert not any(e in text for e in emails)


def test_coder_view_restricts_tables_and_keeps_columns(demo):
    ctx = render_context(demo.profile, "x", 120, only_tables=["orders"], min_level=1)
    assert "TABLE orders" in ctx.text and "TABLE customers" not in ctx.text
    assert all(f"  {c.name}:" in ctx.text for c in demo.profile.table("orders").columns)


def test_graph_edges(demo):
    g = build_graph(demo.profile)
    edges = {(e.source, e.target, e.kind) for e in g.edges}
    for src in ("Units", "Unit Price", "Discount"):
        assert ("col:orders.Revenue", f"col:orders.{src}", "derived_from") in edges
    assert ("col:orders.SKU", "col:products.SKU", "formula_lookup") in edges
    assert ("name:FX_EUR_USD", "sheet:Targets", "references") in edges
    assert len({n.id for n in g.nodes}) == len(g.nodes)
