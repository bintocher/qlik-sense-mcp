"""The shapes a typed query is written in.

Declared as models rather than as bare dictionaries so the caller sees
them in the tool schema: which keys exist, which are required, and what
values are allowed. Written out as prose in a description, the same
information reaches the caller only if it reads to the end - and an
aggregation named `average` instead of `avg` is refused after the call
rather than before it.
"""
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field


Aggregation = Literal["sum", "count", "count_distinct", "avg", "min", "max",
                      "median", "stdev", "fractile"]
Operation = Literal["divide", "multiply", "add", "subtract"]
Combination = Literal["union", "intersect", "exclude", "symmetric_difference"]


class Scope(BaseModel):
    """What a query counts over, before any filter narrows it."""

    model_config = {"extra": "forbid"}

    ignore_selections: Optional[bool] = Field(
        None, description="Count over the whole model, whatever is selected.")
    current_selection: Optional[bool] = Field(
        None, description="Count over what is selected right now.")
    bookmark: Optional[str] = Field(
        None, description="Count over what this bookmark selects.")
    state: Optional[str] = Field(
        None, description="Count over this alternate state.")
    selection_back: Optional[int] = Field(
        None, description="The selections as they were N steps ago.")
    selection_forward: Optional[int] = Field(
        None, description="The selections N steps forward again.")
    combine: Optional[Combination] = Field(
        None, description="How the sets in `of` are joined.")
    of: Optional[List[Dict[str, Any]]] = Field(
        None, description="The sets to join; each is a scope with filters "
                          "of its own.")


class Filter(BaseModel):
    """One condition on one field. One kind of condition per filter."""

    model_config = {"extra": "forbid"}

    field: str = Field(..., description="Field name, exactly as the model "
                                        "spells it.")
    values: Optional[List[Any]] = Field(None, description="Keep these values.")
    exclude: Optional[List[Any]] = Field(None, description="Drop these.")
    add: Optional[List[Any]] = Field(
        None, description="Add these to what is selected.")
    intersect: Optional[List[Any]] = Field(
        None, description="Keep what is in both.")
    period: Optional[str] = Field(
        None, description="A year, a month or a day: 2024, 2024-03, "
                          "2024-03-05.")
    from_: Optional[Any] = Field(None, alias="from",
                                 description="Lower bound, inclusive.")
    to: Optional[Any] = Field(None, description="Upper bound, inclusive.")
    greater_than: Optional[Any] = Field(
        None, description="Lower bound, exclusive.")
    less_than: Optional[Any] = Field(
        None, description="Upper bound, exclusive.")
    contains: Optional[str] = Field(
        None, description="Text search, case-insensitive.")
    starts_with: Optional[str] = None
    ends_with: Optional[str] = None
    match_expression: Optional[str] = Field(
        None, description="Keep the values this expression holds for.")
    matching: Optional[Dict[str, Any]] = Field(
        None, description="Values of this field that satisfy a condition on "
                          "another; takes `filters` of its own.")
    not_matching: Optional[Dict[str, Any]] = Field(
        None, description="Values that do not satisfy it.")


class Metric(BaseModel):
    """One number to compute."""

    model_config = {"extra": "forbid"}

    field: Optional[str] = Field(None, description="Field to aggregate.")
    agg: Optional[Aggregation] = Field(
        None, description="How to aggregate it; `sum` when omitted.")
    label: Optional[str] = Field(None, description="Name of the column.")
    p: Optional[float] = Field(
        None, description="Fraction for `fractile`, between 0 and 1.")
    filters: Optional[List[Filter]] = Field(
        None, description="Narrows this metric alone. [] means no filter at "
                          "all; omitted means the filters of the query.")
    scope: Optional[Scope] = Field(
        None, description="What this metric counts over.")
    total: Optional[bool] = Field(
        None, description="Count across the grouping instead of within it.")
    total_except: Optional[List[str]] = Field(
        None, description="Count across all of it but these fields.")
    inner_agg: Optional[Aggregation] = Field(
        None, description="Aggregate once per group first.")
    per: Optional[Union[str, List[str]]] = Field(
        None, description="What each inner value is computed for.")
    op: Optional[Operation] = Field(
        None, description="Arithmetic over the parts in `of`.")
    of: Optional[List[Dict[str, Any]]] = Field(
        None, description="The parts; each is a metric of its own.")


class Measure(BaseModel):
    """A number written as an expression, or named from the app's library."""

    model_config = {"extra": "forbid"}

    expression: Optional[str] = Field(
        None, description="Qlik expression; mark where a filter goes with "
                          "{filter}.")
    master: Optional[str] = Field(
        None, description="Name of a measure this app defines; see `library` "
                          "in get_app_details.")
    label: Optional[str] = None
    filters: Optional[List[Filter]] = None
    scope: Optional[Scope] = None


class Query(BaseModel):
    """One question: what to group by, what to compute, what to narrow to."""

    model_config = {"extra": "forbid"}

    id: Optional[str] = Field(None, description="Name for this query in the "
                                                "reply.")
    group_by: Optional[List[Union[str, Dict[str, Any]]]] = Field(
        None, description="Fields to group by. An entry may carry a `label`; "
                          "an entry starting with = is an expression.")
    dimensions: Optional[List[Union[str, Dict[str, Any]]]] = Field(
        None, description="Same as group_by.")
    metrics: Optional[List[Metric]] = None
    measures: Optional[List[Union[str, Measure]]] = None
    filters: Optional[List[Filter]] = None
    scope: Optional[Scope] = None
    sort_by: Optional[str] = Field(
        None, description="One column name, from `columns` of the reply.")
    sort_order: Optional[Literal["desc", "asc"]] = None
    limit: Optional[int] = None
    offset: Optional[int] = None
    exclude_null_dimensions: Optional[bool] = None
    suppress_zero: Optional[bool] = None
    include_raw_layout: Optional[bool] = None
