"""Profiler tests. Expected values come from how examples/make_demo_workbook.py builds
the file (row positions, injected defects) or from reading cells directly with openpyxl."""
import io
import zipfile

import pytest

from excel_copilot.profiler import UnsafeWorkbook, profile_workbook

from .conftest import make_xlsx


def test_tables_found_with_correct_ranges(demo):
    p = demo.profile
    got = {t.id: (t.sheet, t.range, t.source, t.n_rows) for t in p.tables}
    assert got == {
        "orders": ("Orders", "A3:L603", "detected", 600),          # title row + blank row skipped
        "products": ("Products", "A1:E26", "detected", 25),
        "customers": ("Customers", "A1:F81", "excel_table", 80),   # ListObject tblCustomers -> "customers"
        "targets": ("Targets", "A1:M5", "detected", 4),
        "lookups": ("Lookups", "A1:B4", "detected", 3),
    }
    assert p.table("orders").title == "Sales Orders FY2024"


def test_column_types_and_roles(demo):
    cols = {c.name: c for c in demo.profile.table("orders").columns}
    expect = {"Order ID": ("integer", "identifier"), "Order Date": ("date", "temporal"),
              "Customer ID": ("integer", "identifier"), "SKU": ("string", "identifier"),
              "Region": ("string", "dimension"), "Units": ("integer", "measure"),
              "Unit Price": ("currency", "measure"), "Discount": ("percent", "measure"),
              "Revenue": ("currency", "measure"), "Status": ("string", "dimension")}
    for name, (typ, role) in expect.items():
        assert (cols[name].type, cols[name].role) == (typ, role), name
    assert cols["Order ID"].unique and cols["Order ID"].distinct == 600


def test_injected_defects_are_reported(demo):
    cols = {c.name: c for c in demo.profile.table("orders").columns}
    text = [i for i in cols["Units"].issues if i.code == "number_stored_as_text"]
    assert text and text[0].count == 3                       # rows n=31,32,333 in the generator
    err = [i for i in cols["Margin %"].issues if i.code == "formula_error"]
    assert err and err[0].count == 1 and err[0].location == "Orders!L254"   # n=250 -> sheet row 254
    assert cols["Margin %"].nulls == 1
    codes = {i.code for i in demo.profile.issues}
    assert "hidden_sheet" in codes


def test_numbers_stored_as_text_are_coerced(demo, raw_orders):
    units = demo.frames["orders"]["Units"]
    assert str(units.dtype) == "Int64"
    expected = sum(int(r["Units"]) for r in raw_orders)       # int("12") for the text cells
    assert int(units.sum()) == expected


def test_formulas_normalised_and_readable(demo):
    cols = {c.name: c for c in demo.profile.table("orders").columns}
    assert cols["Revenue"].formula == "=F{r}*G{r}*(1-H{r})"
    assert cols["Revenue"].formula_readable == "=[Units]*[Unit Price]*(1-[Discount])"
    assert cols["Revenue"].formula_coverage == 1.0
    assert cols["Unit Cost"].references == ["Products!$A$2:$E$26"]


def test_relationships(demo, raw_orders, raw_sheet):
    rels = {(r.from_table, r.from_column, r.to_table, r.to_column): r for r in demo.profile.relationships}
    fk = rels[("orders", "Customer ID", "customers", "Customer ID")]
    ids = {r["Customer ID"] for r in raw_orders}
    known = {r["Customer ID"] for r in raw_sheet("Customers")}
    assert fk.kind == "foreign_key"
    assert fk.containment == pytest.approx(len(ids & known) / len(ids), abs=1e-4)
    assert fk.containment < 1                                  # two orphan IDs were injected
    lk = rels[("orders", "SKU", "products", "SKU")]
    assert lk.kind == "formula_lookup" and lk.containment == 1.0


def test_pii_wide_and_named_ranges(demo):
    email = next(c for c in demo.profile.table("customers").columns if c.name == "Email")
    assert email.pii == "email"
    assert all("***@" in s for s in email.samples)
    assert demo.profile.table("targets").layout == "wide"
    nr = {n.name: n for n in demo.profile.named_ranges}
    assert nr["FX_EUR_USD"].value == "1.09"


def test_two_tables_on_one_sheet_and_headerless_block():
    def build(wb):
        ws = wb.active
        ws.title = "Mixed"
        ws.append(["Name", "Score", None, "City", "Pop"])
        for i in range(5):
            ws.append([f"n{i}", i * 10, None, f"c{i}", 1000 + i])
        ws2 = wb.create_sheet("Raw")
        for i in range(4):
            ws2.append([i, i * 2.5, i * 3])
    res = profile_workbook(make_xlsx(build), "m.xlsx")
    ranges = sorted((t.sheet, t.range) for t in res.profile.tables)
    assert ranges == [("Mixed", "A1:B6"), ("Mixed", "D1:E6"), ("Raw", "A1:C4")]
    raw = next(t for t in res.profile.tables if t.sheet == "Raw")
    assert [c.name for c in raw.columns] == ["column_A", "column_B", "column_C"]
    assert raw.n_rows == 4


def test_duplicate_and_blank_headers():
    def build(wb):
        ws = wb.active
        ws.append(["Amount", "Amount", None, "Note"])
        for i in range(3):
            ws.append([i, i + 1, i + 2, "x"])
    t = profile_workbook(make_xlsx(build)).profile.tables[0]
    assert [c.name for c in t.columns] == ["Amount", "Amount_2", "column_C", "Note"]
    assert {i.code for i in t.issues} >= {"blank_header", "duplicate_header"}


def test_formulas_without_cached_values_are_flagged():
    def build(wb):
        ws = wb.active
        ws.append(["a", "b", "c"])
        for r in range(2, 6):
            ws.append([r, r * 2, f"=A{r}+B{r}"])
    p = profile_workbook(make_xlsx(build)).profile
    assert any(i.code == "no_cached_values" and i.count == 4 for i in p.issues)
    c = p.tables[0].columns[2]
    assert c.formula_readable == "=[a]+[b]"


def test_rejects_non_xlsx_and_zip_bombs():
    with pytest.raises(UnsafeWorkbook, match="zip signature"):
        profile_workbook(b"\xd0\xcf\x11\xe0 legacy xls", "old.xls")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/workbook.xml", "<x/>")
        z.writestr("xl/bomb.xml", b"\0" * (400 * 1024 * 1024))   # 400 MB of zeros compresses to ~400 KB
    with pytest.raises(UnsafeWorkbook, match="zip bomb|expands"):
        profile_workbook(buf.getvalue(), "bomb.xlsx")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hello.txt", "not a spreadsheet")
    with pytest.raises(UnsafeWorkbook, match="not a spreadsheet"):
        profile_workbook(buf.getvalue(), "x.xlsx")
