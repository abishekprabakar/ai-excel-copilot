"""Typed profile of a workbook. This is the structured JSON the profiler emits."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ColumnType = Literal["integer", "number", "currency", "percent", "date", "datetime",
                     "boolean", "string", "empty"]
ColumnRole = Literal["identifier", "measure", "dimension", "temporal", "text", "flag", "empty"]
Severity = Literal["info", "warning", "error"]


class Issue(BaseModel):
    code: str
    severity: Severity
    message: str
    location: str | None = None       # A1-style, e.g. "Orders!L254"
    count: int = 1


class NumericStats(BaseModel):
    min: float
    max: float
    mean: float
    sum: float
    p50: float


class ColumnProfile(BaseModel):
    name: str                          # cleaned, unique header used everywhere downstream
    header: str | None                 # raw header text as found in the sheet
    letter: str                        # Excel column letter
    index: int                         # 0-based position within the table
    type: ColumnType
    type_confidence: float             # share of non-null values that conform to `type`
    role: ColumnRole
    non_null: int
    nulls: int
    distinct: int
    unique: bool
    numeric: NumericStats | None = None
    date_min: str | None = None
    date_max: str | None = None
    top_values: list[tuple[str, int]] = Field(default_factory=list)
    samples: list[str] = Field(default_factory=list)
    number_format: str | None = None
    pii: str | None = None             # "email" | "phone" | "ssn" | "card"
    formula: str | None = None         # normalised fill-down formula, e.g. "=F{r}*G{r}*(1-H{r})"
    formula_readable: str | None = None  # same with column names: "=[Units]*[Unit Price]*(1-[Discount])"
    formula_coverage: float = 0.0      # share of rows that carry that formula
    references: list[str] = Field(default_factory=list)  # "Products!A2:E26", "table.col" ...
    issues: list[Issue] = Field(default_factory=list)


class TableProfile(BaseModel):
    id: str                            # stable, identifier-safe, e.g. "orders"
    sheet: str
    name: str                          # ListObject name or derived name
    source: Literal["excel_table", "detected"]
    range: str                         # "A3:L603"
    header_row: int                    # 1-based sheet row
    n_rows: int
    columns: list[ColumnProfile]
    title: str | None = None           # text found just above the header (e.g. a merged title)
    layout: Literal["long", "wide"] = "long"   # "wide": headers are periods (Jan..Dec, 2021..)
    issues: list[Issue] = Field(default_factory=list)


class Relationship(BaseModel):
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    kind: Literal["foreign_key", "formula_lookup"]
    containment: float                 # share of distinct from-values present in to-column
    confidence: float


class NamedRange(BaseModel):
    name: str
    refers_to: str
    value: str | None = None


class SheetProfile(BaseModel):
    name: str
    state: Literal["visible", "hidden", "veryHidden"]
    dimensions: str
    max_row: int
    max_column: int
    tables: list[str]                  # table ids
    merged_ranges: list[str] = Field(default_factory=list)
    loose_cells: list[tuple[str, str]] = Field(default_factory=list)  # (A1, value) outside tables
    formula_cells: int = 0
    error_cells: int = 0


class WorkbookProfile(BaseModel):
    schema_version: Literal["1"] = "1"
    file_name: str
    sha256: str
    size_bytes: int
    sheets: list[SheetProfile]
    tables: list[TableProfile]
    relationships: list[Relationship]
    named_ranges: list[NamedRange]
    issues: list[Issue]
    has_macros: bool = False
    profiled_ms: float = 0.0

    def table(self, table_id: str) -> TableProfile:
        for t in self.tables:
            if t.id == table_id:
                return t
        raise KeyError(table_id)
