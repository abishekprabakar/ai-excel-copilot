"""The intermediate representation (IR) the coder LLM emits.

A program is a flat list of dataframe steps, each naming its output frame.
Design constraints, all deliberate:

* Flat and non-recursive, so it fits JSON-schema constrained decoding (no
  recursive schemas, no `discriminator`, every field required, < 16 unions).
* Expressions are short strings in a tiny formula language (see expr.py), parsed
  and type-checked by us. The LLM never writes Python.
* Every column reference can be checked against the workbook profile before
  anything runs.
"""
from __future__ import annotations

from typing import Literal, Union

from pydantic import BaseModel, Field

def _op(name: str):
    # Pydantic renders single-value Literals as `const`, which constrained decoding
    # does not enforce (the SDK moves it into the description). A one-item enum is enforced.
    return Field(json_schema_extra={"enum": [name]})


AggFunc = Literal["sum", "mean", "median", "min", "max", "count", "nunique", "std"]


class Load(BaseModel):
    op: Literal["load"] = _op("load")
    out: str = Field(description="name of the new frame, snake_case")
    table: str = Field(description="table id from the workbook context, e.g. 'orders'")


class Filter(BaseModel):
    op: Literal["filter"] = _op("filter")
    out: str
    input: str
    condition: str = Field(description="boolean expression, e.g. year(`Order Date`) == 2024 and Region != 'West'")


class Derive(BaseModel):
    op: Literal["derive"] = _op("derive")
    out: str
    input: str
    column: str = Field(description="name of the new or replaced column")
    expression: str = Field(description="expression over columns of `input`")


class Select(BaseModel):
    op: Literal["select"] = _op("select")
    out: str
    input: str
    columns: list[str]


class Aggregation(BaseModel):
    column: str = Field(description="column to aggregate; '*' with func 'count' counts rows")
    func: AggFunc
    as_name: str


class Aggregate(BaseModel):
    op: Literal["aggregate"] = _op("aggregate")
    out: str
    input: str
    group_by: list[str] = Field(description="grouping columns; empty for a grand total")
    aggregations: list[Aggregation]


class Join(BaseModel):
    op: Literal["join"] = _op("join")
    out: str
    left: str
    right: str
    left_on: list[str]
    right_on: list[str]
    how: Literal["inner", "left"]


class SortKey(BaseModel):
    column: str
    descending: bool


class Sort(BaseModel):
    op: Literal["sort"] = _op("sort")
    out: str
    input: str
    by: list[SortKey]


class Limit(BaseModel):
    op: Literal["limit"] = _op("limit")
    out: str
    input: str
    n: int


class Unpivot(BaseModel):
    op: Literal["unpivot"] = _op("unpivot")
    out: str
    input: str
    id_columns: list[str]
    value_columns: list[str]
    var_name: str
    value_name: str


class Pivot(BaseModel):
    op: Literal["pivot"] = _op("pivot")
    out: str
    input: str
    index: list[str]
    columns: str
    values: str
    func: AggFunc


Step = Union[Load, Filter, Derive, Select, Aggregate, Join, Sort, Limit, Unpivot, Pivot]


class Chart(BaseModel):
    kind: Literal["none", "bar", "line"]
    x: str = Field(description="column for the x axis, or '' when kind is none")
    y: str = Field(description="numeric column for the y axis, or '' when kind is none")


class Program(BaseModel):
    steps: list[Step]
    output: str = Field(description="name of the frame that is the answer")
    title: str = Field(description="short title for the result table")
    chart: Chart

    def step_outputs(self) -> list[str]:
        return [s.out for s in self.steps]


class Plan(BaseModel):
    """What the planner LLM emits: intent and approach, not code."""
    answerable: bool = Field(description="false if the workbook cannot answer the question")
    reason: str = Field(description="if not answerable, why; otherwise ''")
    tables: list[str] = Field(description="table ids needed")
    columns: list[str] = Field(description="'table.column' references needed")
    steps: list[str] = Field(description="ordered natural-language steps")
    assumptions: list[str] = Field(description="interpretations made, e.g. 'revenue excludes Returned orders'")
