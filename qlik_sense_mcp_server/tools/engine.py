"""Tools backed by the Engine API: script, fields, sheets, hypercubes.

Every tool here runs inside the Engine client's transaction, so a call
holds the single shared WebSocket for its whole duration — see
`_engine_serialised`.
"""

import time
from typing import Any, Dict, List, Optional

from . import context
from .context import mcp
from .helpers import (
    _check,
    _engine_serialised,
    _err,
    _ok,
    _timed,
    _to_bool,
    _wildcard_to_regex,
)
logger = context.logger

from ..config import (
    DEFAULT_FIELD_LIMIT,
    MAX_FIELD_LIMIT,
    DEFAULT_HYPERCUBE_MAX_ROWS,
)


@mcp.tool()
@_timed
@_engine_serialised
def get_app_script(app_id: str) -> str:
    """
    Read an app's load script — the LOAD statements that built its data.

    WHEN TO USE
        To learn where the data came from and what was done to it: source
        systems, joins, renames, and the `$(variable)` definitions chart
        expressions rely on. Worth reading before writing anything
        elaborate, since the script explains names that look arbitrary in
        the data model.

    WHEN NOT TO USE
        To find out which fields exist or how large they are —
        `get_app_details` answers that directly and costs less to read.

    Args:
        app_id: application GUID, from `get_apps` or `get_app_details`.

    Returns:
        `qScript` (the whole script as one string), `script_length`,
        `app_id`. An empty script comes with a `note` saying whether the
        app has none or this identity may not read it — reading the script
        needs Professional access, and an Analyzer licence gets an empty
        string rather than an error.

    Does not return:
        Data, field lists, or the reload history.
    """
    e = _check()
    if e:
        return e
    try:
        # no_data=False so the cached connection is reusable for later data calls
        app_handle = context.engine_api.ensure_app(app_id, no_data=False)
        script = context.engine_api.get_script(app_handle)
        reply = {
            "qScript": script,
            "app_id": app_id,
            "script_length": len(script) if script else 0,
        }
        if not script:
            # Engine returns an empty string rather than an error when the
            # identity may not read the script — an Analyzer licence, for
            # one. Without saying so, "this app has no script" and "you are
            # not allowed to see it" look identical.
            reply["note"] = (
                "Engine returned an empty script. Either the app genuinely "
                "has none, or this identity may not read it — reading the "
                "load script needs Professional access, an Analyzer licence "
                "silently returns nothing."
            )
        return _ok(reply)
    except Exception as ex:
        return _err(str(ex), app_id=app_id)



@mcp.tool()
@_timed
@_engine_serialised
def get_app_field_statistics(
    app_id: str,
    field_name: str,
    full: bool = False,
) -> str:
    """
    Measure how complete and how varied one field is.

    WHEN TO USE
        To answer "how much of this field is filled in" before trusting an
        average or a share computed from it. `null_count` against
        `non_null_count` is what this tool is for.

    WHEN NOT TO USE
        To learn a date field's loaded period — `engine_get_field_range`
        answers that in a fraction of the time, and sum or average over a
        timestamp is a meaningless number.
        To see example values — `get_app_field` lists them.
        To learn how many different values a field holds —
        `get_app_details` already carries `distinct_values` per field.
        On a large fact table with `full=true`: median and standard
        deviation there take tens of seconds.

    Args:
        app_id: application GUID.
        field_name: exact field name, no square brackets. Names are
            case-sensitive; copy them from `get_app_details`.
        full: also compute avg, sum, median, mode and standard deviation.
            Default false. Worth it on a small numeric field, expensive on
            a large one.

    Returns:
        `unique_values`, `non_null_count` (Qlik's `Count`), `null_count`
        (`NullCount`), `total_count` (their sum — the rows the field
        appears in), `min_value`, `max_value`, `null_percentage`,
        `completeness_percentage`. Each figure is
        `{text, numeric, is_numeric}`. With `full=true`, also `avg_value`,
        `sum_value`, `median_value`, `mode_value`, `std_deviation`.

    Does not return:
        The values themselves, or anything about other fields.
    """
    e = _check()
    if e:
        return e
    try:
        app_handle = context.engine_api.ensure_app(app_id, no_data=False)
        result = context.engine_api.get_field_statistics(app_handle, field_name, light=not full)
        return _ok(result)
    except Exception as ex:
        return _err(str(ex))



@mcp.tool()
@_timed
@_engine_serialised
def engine_get_field_range(app_id: str, field_name: str) -> str:
    """
    The bounds of one field: how many different values, the smallest and
    the largest.

    WHEN TO USE
        To learn a date field's loaded period before asking about a period
        inside it. Also for the range of a numeric field, or the size of a
        key. Engine answers from its symbol table without reading rows, so
        this returns in under a second on tables of any size.

    WHEN NOT TO USE
        To list values — `get_app_field` does that.
        To measure completeness — `get_app_field_statistics` counts nulls.

    Args:
        app_id: application GUID.
        field_name: exact field name, no square brackets.

    Returns:
        `field_name`, `unique_values`, `min_value`, `max_value`, each as
        `{text, numeric, is_numeric}`. For a date, `text` is the writing
        Qlik displays and `numeric` is its serial number.

    Does not return:
        Null counts, averages, or the values between the bounds.
    """
    e = _check()
    if e:
        return e
    try:
        app_handle = context.engine_api.ensure_app(app_id, no_data=False)
        return _ok(context.engine_api.get_field_range(app_handle, field_name))
    except Exception as ex:
        return _err(str(ex), app_id=app_id, field_name=field_name)



@mcp.tool()
@_timed
@_engine_serialised
def engine_query(
    app_id: str,
    queries: Optional[List[Dict[str, Any]]] = None,
    group_by: Optional[List[str]] = None,
    metrics: Optional[List[Dict[str, Any]]] = None,
    measures: Optional[List[Dict[str, Any]]] = None,
    filters: Optional[List[Dict[str, Any]]] = None,
    scope: Optional[Dict[str, Any]] = None,
    sort_by: Optional[str] = None,
    sort_order: str = "desc",
    limit: int = 100,
    offset: int = 0,
    exclude_null_dimensions: bool = False,
    suppress_zero: bool = False,
    include_raw_layout: bool = False,
) -> str:
    """
    Answer a question about an app's data: group by fields, aggregate
    fields, filter by period or by value. The server writes the Qlik
    expressions.

    WHEN TO USE
        Any question of the form "how much / how many, by what, for when".
        This is the first tool to reach for once `get_app_details` has
        shown which fields exist.

        Ask several unrelated things at once. Every query in `queries` is
        independent — its own grouping, its own measures, its own filters —
        and the whole list shares three round-trips instead of three per
        query:

            {"app_id": "...",
             "queries": [
               {"id": "by_region",
                "group_by": ["Region"],
                "metrics": [{"field": "Amount", "agg": "sum"}],
                "sort_by": "sum_Amount", "limit": 20},
               {"id": "by_category",
                "group_by": ["Category"],
                "metrics": [{"field": "Amount", "agg": "sum"},
                            {"field": "OrderId", "agg": "count_distinct"}]},
               {"id": "yearly",
                "group_by": ["Year"],
                "metrics": [{"field": "Amount", "agg": "sum"}],
                "filters": [{"field": "OrderDate", "from": "2023", "to": "2025"}]}
             ]}

        Up to 25 queries in one call, and up to 200 grouping fields,
        measures and filter values between them — every one of those is
        checked by Qlik before the batch runs. Plan the whole question in
        one call rather than discovering it one round-trip at a time.

    WHEN NOT TO USE
        A calculation this vocabulary cannot state — a nested aggregation,
        `Aggr()`, `FirstSortedValue`, set analysis with P()/E(), a
        comparison of two periods inside one column. Write the expression
        yourself with `engine_create_hypercube`; `filters` works there too.

    ARGUMENTS
        app_id: application GUID.
        group_by: field names to group by. Omit for a single total row.
        metrics: what to aggregate, as
            {"field": "Amount", "agg": "sum", "label": "Revenue"}.
            `agg` is one of sum, count, count_distinct, avg, min, max,
            median, stdev, fractile. `fractile` also takes `p` — the
            fraction, so 0.85 is the 85th percentile. `label` is optional
            and defaults to `<agg>_<field>`; it names the column and is
            what `sort_by` takes.

            A metric may aggregate over groups rather than over rows. Add
            `per` — the field each inner value is computed for — and
            `inner_agg` — how it is computed:

                {"field": "tis_days", "inner_agg": "sum",
                 "per": "IssueId", "agg": "fractile", "p": 0.85}

            becomes `Fractile(Aggr(Sum([tis_days]), [IssueId]), 0.85)`:
            days summed per issue first, then the 85th percentile across
            issues. This is a different question from `median` over rows,
            and the answers differ — measured on one small set, 10 against
            6. `per` takes one field or a list of them.

            A metric may also narrow itself, overriding the query's
            `filters`, so a KPI holds its numerator and denominator at
            once:

                "metrics": [
                  {"field": "IssueId", "agg": "count_distinct"},
                  {"field": "IssueId", "agg": "count_distinct",
                   "label": "all", "filters": []}]

            No `filters` key means the query's filters; `[]` means none at
            all. The reply says which measure used which slice in
            `measure_filters`.
            A metric may ignore the grouping: `"total": true` aggregates
            across every group, which is the denominator of a share, and
            `"total_except": ["Region"]` ignores all of the grouping but
            those fields, which is a share within a group.

            Arithmetic between aggregations is stated as an operation over
            parts rather than written as an expression:

                {"label": "share", "op": "divide", "of": [
                   {"field": "Amount", "agg": "sum"},
                   {"field": "Amount", "agg": "sum", "total": true}]}

            `op` is divide, multiply, add or subtract. Each part is a
            metric in its own right, with its own filters, grouping and
            nesting. Division guards itself: a zero denominator answers
            with no value rather than with Qlik's dash.

        scope: which set the filters narrow, when it is not the data as
            loaded. `{"ignore_selections": true}` for everything,
            `{"bookmark": "BM01"}`, `{"state": "Compare"}`, or both
            together for a bookmark belonging to a state.

        filters: what to narrow to. Several shapes:
            {"field": "OrderDate", "from": "2024-01-01", "to": "2024-12-31"}
            {"field": "Region", "values": ["North", "South"]}
            `from` and `to` include both ends, and either may be left out
            for an open end. What they mean follows the field: on a date
            field they are days — written as 2024-01-31, 31.01.2024,
            2024-01 for a whole month, 2024 for a whole year, or a Qlik
            serial number — and on any other field they are the values
            themselves, so {"field": "Discount", "from": 400} is a
            discount of 400 or more. For a bound that excludes itself use
            `greater_than` / `less_than`: "more than 400" is
            {"field": "Discount", "greater_than": 400}, and the difference
            between the two is every row sitting exactly on 400.
            `{"field": "OrderDate", "period": "2024"}` is the whole year in
            one key. Filters combine with AND.
            `{"field": "Region", "exclude": ["North"]}` keeps everything
            else; `add` and `intersect` combine with what is selected.

            `{"field": "Name", "contains": "smith"}`, `starts_with`,
            `ends_with` match by text, case insensitively. The value is
            compared as text, so `*` and `?` inside it are ordinary
            characters rather than wildcards.

            `{"field": "Client", "matching": {"filters": [...]}}` keeps the
            values of a field that satisfy a condition on another field —
            the customers who bought a product, whatever else they bought.
            `not_matching` keeps those that do not. Both together read as
            "these and not those": bought in 2023 and not in 2024. Inside,
            `of_field` carries the answer from one field to another, and
            `base` is `"all"` (the default) or `"current"`.

            `{"field": "Year", "match_expression": "[Year]>2023"}` is the
            way out for a condition none of the above states. The server
            wraps it and lets Qlik judge it; it does not read it.

            A filter that selects nothing is refused with what the field
            does hold, rather than answered with a zero.
        sort_by: a metric label or a grouping field. Set it whenever
            `limit` might cut the result, otherwise the rows that come back
            are arbitrary rather than the largest.
        sort_order: "desc" (default) or "asc".
        limit: rows per query, default 100, cap 5000.
        queries: a list of whole queries, each with the keys above plus an
            optional "id". Use it for several independent questions at
            once. When present, the single-query arguments are ignored.
        measures: an escape hatch inside a query, for one expression this
            vocabulary cannot state — `[{"expression": "Sum({filter} A) / "
            "Count({filter} B)", "label": "AOV"}]`. With `filters`, the
            expression must mark where the filter goes: a set modifier
            narrows the aggregation it sits in, and only the author knows
            which one that is. Without the marker the query is refused
            rather than answered with an unfiltered number.

    RETURNS
        `results`, one entry per query, each with `id`, `columns`, `rows`
        (numbers as numbers, dates as the text Qlik displays), `total_rows`,
        `grand_total` across all groups, and `sorted_by`.
        `filters_applied` states what each filter resolved to.
        `period_check` gives the earliest and latest value of each filtered
        date field inside the result, so the period is verifiable from the
        answer itself.
        A query that fails carries `error` and `error_category`; the others
        in the batch still answer.

    SETS
        `scope` says what a query counts over before any filter narrows
        it: `{"ignore_selections": true}` for the whole model,
        `{"bookmark": "BM01"}`, `{"state": "Compare"}`,
        `{"selection_back": 1}` for the selections a step ago. Two sets
        join with one operation between them:

            {"combine": "union",
             "of": [{"ignore_selections": true,
                     "filters": [{"field": "Year", "values": ["2023"]}]},
                    {"ignore_selections": true,
                     "filters": [{"field": "Region",
                                  "values": ["South"]}]}]}

        `union` is everything in either, `intersect` only what is in both,
        `exclude` the first without the second, `symmetric_difference`
        what belongs to exactly one of two. Each set carries filters of its
        own; a filter written outside the combination is refused, because
        Qlik reads no modifier around one. A scope stated on the query
        reaches every measure; stated on a metric or on one part of an
        arithmetic metric, only that one.

    PAGE AND SHAPE
        `limit` and `offset` walk the result; `has_more` and `next_offset`
        say whether there is another page. `exclude_null_dimensions` drops
        the group with no value for the grouping field — off by default,
        because such a fact is still a fact. `suppress_zero` drops groups
        whose measures are all zero. `include_raw_layout` adds
        `hypercube_data`, the untouched Qlik answer, beside the shaped
        one - the same key `engine_create_hypercube` uses.
        Every one of them can also be stated per query inside `queries`.

    DOES NOT RETURN
        Per-cell state or the expressions it wrote. Row-level records —
        every reply is aggregated. The untouched Qlik layout only on
        request, through `include_raw_layout`.

    A filter that selects nothing is refused before the query runs, and a
    value the field does not hold is answered with what the field holds
    instead. Qlik itself reports neither: it returns 0 for the first and
    the unfiltered total for the second.
    """
    e = _check()
    if e:
        return e
    if queries is None:
        queries = [{
            "group_by": group_by or [],
            "metrics": metrics or [],
            "measures": measures or [],
            "filters": filters or [],
            "scope": scope,
            "sort_by": sort_by,
            "sort_order": sort_order,
            "limit": limit,
            "offset": offset,
            "exclude_null_dimensions": exclude_null_dimensions,
            "suppress_zero": suppress_zero,
            "include_raw_layout": include_raw_layout,
        }]
    if not isinstance(queries, list) or not queries:
        return _err(
            "queries must be a non-empty list of query objects",
            error_category="invalid_argument",
            hint='One query: {"group_by": ["Region"], "metrics": '
                 '[{"field": "Amount", "agg": "sum"}]}.',
        )
    try:
        app_handle = context.engine_api.ensure_app(app_id, no_data=False)
        return _ok(context.engine_api.run_queries(app_handle, app_id, queries))
    except Exception as ex:
        logger.exception("engine_query failed")
        return _err(str(ex) or repr(ex), error_type=type(ex).__name__,
                    app_id=app_id)


@mcp.tool()
@_timed
@_engine_serialised
def engine_create_hypercube(
    app_id: str,
    dimensions: Optional[List[Dict[str, Any]]] = None,
    measures: Optional[List[Dict[str, Any]]] = None,
    limit: int = DEFAULT_HYPERCUBE_MAX_ROWS,
    sort_by: Optional[str] = None,
    sort_order: str = "desc",
    suppress_zero: bool = False,
    exclude_null_dimensions: bool = False,
    include_raw_layout: bool = False,
    max_rows: Optional[int] = None,
    offset: int = 0,
    filters: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    Run a grouped aggregation written as Qlik expressions.

    WHEN TO USE
        A calculation `engine_query` cannot state: a nested aggregation,
        `Aggr()`, `FirstSortedValue`, `P()`/`E()` set analysis, two periods
        compared inside one column, a calculated dimension.

    WHEN NOT TO USE
        A plain "how much by what, for when". `engine_query` states that
        without any expression, writes the set analysis itself and returns
        the control values that show the filter applied. Reach for it
        first; come here when it cannot say what you need.

    THE SHAPE
        `dimensions` group, `measures` aggregate, `sort_by` and
        `sort_order` order, `limit` cuts:

            {"app_id": "...",
             "dimensions": [{"field": "clientid"}],
             "measures": [{"expression": "Sum(ggr)", "label": "GGR"}],
             "sort_by": "GGR", "sort_order": "desc", "limit": 10}

        Omit `dimensions` for a single total row.

    FILTERING
        Two ways, and the first is safer.

        Describe the filter and mark where it belongs. The server writes
        the set analysis, checks that it selects something, and reports
        what it resolved to:

            {"measures": [{"expression": "Sum({filter} Amount)",
                           "label": "Revenue"}],
             "filters": [{"field": "OrderDate", "period": "2024"}]}

        The marker `{filter}` is required with `filters`, because a set
        modifier narrows the aggregation it sits in and only the author of
        the expression knows which one that is.

        Or write the set analysis yourself:

            Sum({<[Year]={2024}>} Amount)

        Quoting decides the meaning and a wrong choice returns 0 rather
        than an error: 'single' is an exact value, "double" is a search
        where comparisons and wildcards work. A range is one string with
        no spaces: {">=100<200"}.

        Comparison inside a modifier runs against the text Qlik displays
        for a value. On a date field displayed as `01.01.2024`, a serial
        number range returns 0; on one displayed as `45292`, it works.
        Stating the period as `filters` avoids the question — the server
        measures which form this field answers to.

    PERFORMANCE
        Filter inside the aggregation rather than with `If()`: set
        analysis is an index lookup before aggregation, `If()` scans every
        row of the fact table.
        A dimension `field` should be a plain field name. An expression
        there is evaluated per row of the fact table.
        Rank with `sort_by`, not with `qSortByExpression` in the
        dimension: measured on a 91M-row table, the latter took 286s
        against 66s for the same ranking.
        Read `timings` when a call is slow. `open_app_seconds` is loading
        the app into memory, paid once per app; `get_layout_seconds` is
        the computation itself.

    Args:
        app_id: Application GUID. Required. From `get_apps`.
        dimensions: GROUP BY columns. Each item is
            `{"field": "<FieldName>"}` — a real field name, no brackets,
            no expression. Omit or pass `[]` for a grand-total row.
            Advanced: an optional `"sort_by"` object per dimension maps
            straight onto Qlik `qSortCriterias`
            (`qSortByNumeric` / `qSortByAscii` / `qSortByExpression`, each
            -1 desc / 0 off / 1 asc, plus `qExpression`, accepted either
            as a plain string or in Qlik's native `{"qv": "..."}` form).
            The top-level `sort_by` argument overrides it and is what you
            normally want.
        measures: Aggregate expressions. Each item is
            `{"expression": "Sum(Amount)", "label": "Revenue"}`. Always
            give a `label` — it is the column name AND the value you pass
            to `sort_by`. Any Qlik aggregation works: Sum, Count,
            Count(DISTINCT ...), Avg, Min, Max, Only, FirstSortedValue,
            RangeSum.
        limit: Max rows to return (the SQL LIMIT). Default 1000, must be
            at least 1, hard cap 5000. Also capped by
            `columns * limit <= 9900` (Qlik itself refuses pages over
            10000 cells). For ranked queries a small limit (10-50) is
            both faster and easier to read.
        sort_by: Name of the column to order by — a measure `label`, a
            measure expression, or a dimension field name. Case- and
            bracket-insensitive; a measure wins over a dimension of the
            same name. Omit to keep Qlik's default order (ascending by
            the first dimension) — but then a truncated result contains
            ARBITRARY rows, not the most important ones.
        sort_order: `"desc"` (default, largest first — top-N) or `"asc"`
            (smallest first — bottom-N).
        offset: Row to start from, for reading a result in pages. The reply
            carries `has_more` and `next_offset`; pass that value back to
            get the following page. Keep `sort_by` identical between pages,
            otherwise the order — and therefore the paging — changes under
            you.
        suppress_zero: Drop rows whose measure is 0. Default False.
            Useful for `sort_order="asc"`, where zero-valued groups
            would otherwise fill the entire result.
        exclude_null_dimensions: Drop the row whose dimension value is
            NULL — the one Qlik displays as `"-"`. Default False: such a
            fact is still a fact, so leaving it out is yours to say.
            Facts that carry no value for the grouping field all pile
            into that single row, so it frequently holds a large total
            and wins the ranking, pushing the real values out of a
            top-N. Pass False when you specifically want to see how much
            data is unattributed — a large `"-"` row means the grouping
            field is not linked to those facts in the data model.
        include_raw_layout: Also return the untouched Qlik `qHyperCube`
            (per-cell `qElemNumber`/`qState`, `qDimensionInfo`,
            `qMeasureInfo`). Default False, because it costs several
            times more tokens than `rows` and is rarely needed.
        max_rows: Alias for `limit`. If both are given, `max_rows` wins.
        filters: Filters described rather than written, applied wherever a
            measure carries the `{filter}` marker. Same shapes as
            `engine_query`: `{"field": "OrderDate", "from": "2024-01-01",
            "to": "2024-12-31"}` for a period, `{"field": "Discount",
            "from": 400}` for a range of any other field, or
            `{"field": "Region", "values": ["North"]}` for named values.

    Returns:
          - `columns`: column names, in row order.
          - `rows`: values as data — numbers as numbers, dates as the text
            Qlik displays for them, which is the same writing every other
            reply about that field uses.
          - `total_rows`: how many groups exist on the server.
          - `returned_rows`: how many rows are in `rows`.
          - `sorted_by` / `sort_order`: the sort actually applied
            (`null` when unsorted).
          - `grand_total`: totals across all groups, per measure — correct
            even when the rows are truncated.
          - `truncation_warning`: set when `total_rows > returned_rows`.
            For an unsorted query the rows are arbitrary; add `sort_by`.
          - `filters_applied`: what each described filter resolved to.
          - `timings`: seconds per step.

    Does not return:
        Row-level records, or the Qlik layout unless
        `include_raw_layout=true`.

    Errors carry `error`, `error_category` and the fix:
        `invalid_expression` — Qlik's own parser rejected an expression;
            its message is quoted.
        `field_not_found` — a name the data model does not have, in a
            dimension, a measure or a set modifier; `did_you_mean` lists
            near matches.
        `invalid_sort` — `sort_by` matched no column; `available_columns`
            lists the valid ones.
        `invalid_limit` / `limit_exceeded` / `cell_cap_exceeded` — the
            reply names a limit that fits.
        `empty_period` / `value_not_found` — a described filter selects
            nothing; the reply says what the field holds.
        `socket_timeout` — the query is too heavy; the hint lists what to
            cut, in order of impact.
    """
    import traceback as _tb
    e = _check()
    if e:
        return e
    # `max_rows` is the pre-1.6 name for `limit`. Honour it when supplied
    # so existing callers and saved prompts keep working unchanged.
    effective_limit = max_rows if max_rows is not None else limit
    stage = "ensure_app"
    t_open = time.monotonic()
    try:
        app_handle = context.engine_api.ensure_app(app_id, no_data=False)
        open_app_seconds = round(time.monotonic() - t_open, 3)
        stage = "create_hypercube"
        result = context.engine_api.create_hypercube(
            app_handle,
            dimensions or [],
            measures or [],
            effective_limit,
            sort_by=sort_by,
            sort_order=sort_order,
            suppress_zero=suppress_zero,
            include_raw_layout=include_raw_layout,
            exclude_null_dimensions=exclude_null_dimensions,
            offset=offset,
            filters=filters,
        )
        # Opening the app dominates the first call against a cold app, so
        # report it next to the query time instead of hiding it in the
        # total — otherwise a fast query looks slow.
        if isinstance(result, dict) and isinstance(result.get("timings"), dict):
            result["timings"]["open_app_seconds"] = open_app_seconds
        return _ok(result)
    except Exception as ex:
        logger.exception("engine_create_hypercube failed at stage=%s", stage)
        return _err(
            str(ex) or repr(ex),
            error_type=type(ex).__name__,
            failed_stage=stage,
            app_id=app_id,
            ws_operation_timeout=context.engine_api.ws_operation_timeout,
            traceback=_tb.format_exc(),
        )



@mcp.tool()
@_timed
@_engine_serialised
def get_app_field(
    app_id: str,
    field_name: str,
    limit: int = DEFAULT_FIELD_LIMIT,
    offset: int = 0,
    search_string: Optional[str] = None,
    search_number: Optional[str] = None,
    case_sensitive: bool = False,
) -> str:
    """
    List the values a field holds, most frequent first.

    WHEN TO USE
        To see how a value is really spelled before filtering on it. A
        filter on a value the field does not hold returns zeros rather
        than an error, so `Moscow` against data saying `Moskva` produces a
        clean, wrong table. One look here settles it.
        Also to find a value by pattern, with `search_string`.

    WHEN NOT TO USE
        When `engine_query` will do the filtering: it checks the values
        itself and says what the field holds instead.
        For bounds or a count of different values — `engine_get_field_range`.
        For values of a field you have not identified yet — `search_app`
        finds which field holds a term.

    Args:
        app_id: application GUID.
        field_name: exact field name, no square brackets, case-sensitive.
        limit: values per page. Default 10, cap 100.
        offset: values to skip, for reading further pages.
        search_string: wildcard filter on the text form of the value.
            `*` and `%` both stand for any run of characters. The search
            runs in Engine, over the whole field.
        search_number: wildcard filter matching either the numeric or the
            text form. Pass one of the two searches, not both.
        case_sensitive: default false.

    Returns:
        `field_values`, a plain list. With a search, also `total_matches`
        and `search_truncated` when the scan was capped. `field_comment`
        carries the load script's `COMMENT FIELD` text when there is one.
        `fallback_used` appears when the light path returned nothing and a
        hypercube answered instead.

    Does not return:
        Counts per value, or anything about other fields.
    """
    e = _check()
    if e:
        return e
    lim = min(max(limit or DEFAULT_FIELD_LIMIT, 1), MAX_FIELD_LIMIT)
    off = max(offset or 0, 0)
    try:
        app_handle = context.engine_api.ensure_app(app_id, no_data=False)
        # Verify the field exists before reading values. The hypercube
        # fallback treats an unknown field name as an expression, and Qlik
        # evaluates an unknown symbol to 0 — so asking for a misspelled
        # field used to return `{"field_values": ["0"]}`, which reads as
        # data rather than as a mistake. GetFieldDescription is one cheap
        # call and returns {} for a field the model does not know.
        description = context.engine_api.get_field_description(app_handle, field_name)
        if not description:
            return _err(
                f"Field '{field_name}' does not exist in this app's data model",
                error_category="field_not_found",
                hint="Call get_app_details for the exact field names — Qlik "
                     "field names are case-sensitive and often differ from "
                     "the labels shown in charts.",
            )
        # A search runs in Engine, over the whole field. Matching locally
        # against a prefetched prefix — what this did before — can only
        # find values inside that prefix: on a 200k-value field a match at
        # position 150k simply did not exist, and the reply looked like a
        # clean "no matches".
        pattern = search_string or search_number
        if search_string and search_number:
            return _err(
                "Pass either search_string or search_number, not both",
                error_category="invalid_argument",
                hint="They filter the same values; combining them silently "
                     "used to drop the first filter.",
            )
        if pattern:
            matched = context.engine_api.search_field_values(
                app_handle, field_name, pattern, limit=lim, offset=off,
                case_sensitive=case_sensitive)
            values = [v.get("value", "") for v in matched.get("values", [])]
            field_data = matched
            out: Dict[str, Any] = {"field_values": values}
            # Everything the search says about its own completeness has to
            # reach the caller. Dropping `search_truncated` turned a capped
            # scan into what looks like the whole answer.
            for key in ("total_matches", "total_matches_at_least",
                        "search_truncated", "candidates_scanned"):
                if matched.get(key) is not None:
                    out[key] = matched[key]
        else:
            # One more than asked for, to tell "this is the whole field"
            # from "this is where the page ended". Without it a caller that
            # asked for 100 and got 100 could not tell the two apart.
            field_data = context.engine_api.get_field_values(
                app_handle, field_name, lim + 1, include_frequency=False,
                offset=off)
            values = [v.get("value", "") for v in field_data.get("values", [])]
            out: Dict[str, Any] = {"field_values": values[:lim]}
            if len(values) > lim:
                out["has_more"] = True
                out["next_offset"] = off + lim
        # COMMENT FIELD text of this very field, when the script sets one —
        # already fetched above by the existence check, no second call.
        comment = description.get("comment")
        if comment:
            out["field_comment"] = comment
        # Surface internal hints from get_field_values so the LLM knows
        # whether the result came from the fast ListObject path or from the
        # heavier hypercube fallback, and whether anything looked off.
        if isinstance(field_data, dict):
            if field_data.get("fallback_used"):
                out["fallback_used"] = field_data["fallback_used"]
            if field_data.get("warning"):
                out["warning"] = field_data["warning"]
        return _ok(out)
    except Exception as ex:
        return _err(str(ex))



@mcp.tool()
@_timed
@_engine_serialised
def get_app_variables(
    app_id: str,
    limit: int = DEFAULT_FIELD_LIMIT,
    offset: int = 0,
    created_in_script: Optional[str] = None,
    search_string: Optional[str] = None,
    case_sensitive: bool = False,
) -> str:
    """
    List the app's own variables and what they hold.

    WHEN TO USE
        When a chart expression refers to `$(vSomething)` and you need to
        know what it stands for. A variable can hold a whole set modifier,
        so reading it is often the difference between copying a chart's
        logic and guessing at it.

    WHEN NOT TO USE
        For fields — those are in `get_app_details`. Qlik's own reserved
        variables are never listed here.

    Args:
        app_id: application GUID.
        limit: variables per page. Default 10, cap 100.
        offset: variables to skip.
        created_in_script: `"true"` for script `SET`/`LET` only, `"false"`
            for ones made in the interface, omitted for both.
        search_string: wildcard filter on the name or the value.
        case_sensitive: default false.

    Returns:
        `variables_from_script` and `variables_from_ui`, each an object of
        name to value and always present even when empty; `count` for this
        page and `total_found` before paging.

    Does not return:
        Where a variable is used, or its value after expansion in a
        particular expression.
    """
    e = _check()
    if e:
        return e
    lim = min(max(limit or DEFAULT_FIELD_LIMIT, 1), MAX_FIELD_LIMIT)
    off = max(offset or 0, 0)
    script_flag = None
    if created_in_script is not None:
        script_flag = _to_bool(created_in_script, None)
    try:
        app_handle = context.engine_api.ensure_app(app_id, no_data=False)
        var_list = context.engine_api._get_user_variables(app_handle) or []
        prepared = [{"name": v.get("name", ""), "text_value": v.get("text_value", "") or "", "is_script": v.get("is_script_created", False)} for v in var_list]
        if script_flag is True:
            prepared = [x for x in prepared if x["is_script"]]
        elif script_flag is False:
            prepared = [x for x in prepared if not x["is_script"]]
        # created_in_script=None means "no filter": both groups are
        # returned. It used to drop script variables here, which made
        # `variables_from_script` permanently empty on the default call —
        # half the reply was structurally dead.
        if search_string:
            rx = _wildcard_to_regex(search_string, case_sensitive)
            prepared = [x for x in prepared if rx.match(x["name"]) or rx.match(x["text_value"])]
        page = prepared[off:off + lim]
        # Always objects, never "" for an empty one: a field whose type
        # depends on whether it has data forces every caller to type-check
        # before indexing it.
        return _ok({
            "variables_from_script": {x["name"]: x["text_value"]
                                      for x in page if x["is_script"]},
            "variables_from_ui": {x["name"]: x["text_value"]
                                  for x in page if not x["is_script"]},
            "count": len(page),
            "total_found": len(prepared),
        })
    except Exception as ex:
        return _err(str(ex))



@mcp.tool()
@_timed
@_engine_serialised
def get_app_sheets(app_id: str) -> str:
    """
    List the app's sheets — the pages its users see.

    WHEN TO USE
        As the way in to what the app is actually about. Sheet titles say
        what its authors considered worth showing, which is a better guide
        to the data than field names.

    WHEN NOT TO USE
        For the data model — `get_app_details`.

    Args:
        app_id: application GUID.

    Returns:
        `sheets`, each with `sheet_id`, `title` and `description`, plus
        `total_sheets`. Pass a `sheet_id` to `get_app_sheet_objects`.

    Does not return:
        The charts on a sheet, or any data.
    """
    e = _check()
    if e:
        return e
    try:
        # no_data=False to keep the cached connection data-ready for later calls
        app_handle = context.engine_api.ensure_app(app_id, no_data=False)
        sheets = context.engine_api.get_sheets(app_handle)
        sheets_list = [
            {"sheet_id": s.get("qInfo", {}).get("qId", ""), "title": s.get("qMeta", {}).get("title", ""), "description": s.get("qMeta", {}).get("description", "")}
            for s in sheets
        ]
        return _ok({"app_id": app_id, "total_sheets": len(sheets_list), "sheets": sheets_list})
    except Exception as ex:
        return _err(str(ex))



@mcp.tool()
@_timed
@_engine_serialised
def get_app_sheet_objects(app_id: str, sheet_id: str) -> str:
    """
    List the charts on one sheet, with the fields and expressions behind
    them.

    WHEN TO USE
        To see what a sheet works with, in one call: `fields_used` covers
        the fields reached through master items and through a filter
        pane's listboxes as well as the obvious ones. Also to find the
        `object_id` of a chart mentioned by title.
        The `measures` here are the authors' own expressions — the closest
        thing to a definition of what a figure means in this app.

    WHEN NOT TO USE
        For a chart's current data — `get_app_object` returns that.

    Args:
        app_id: application GUID.
        sheet_id: from `get_app_sheets`.

    Returns:
        `objects`, each with `object_id`, `object_type` (`barchart`,
        `table`, `kpi`, `listbox`), `object_description` (its title),
        `fields_used`, `measures` and `dimensions`; plus `total_objects`.

    Does not return:
        Computed values, formatting, or layout position.
    """
    e = _check()
    if e:
        return e
    try:
        # no_data=False to keep the cached connection data-ready
        app_handle = context.engine_api.ensure_app(app_id, no_data=False)
        objects = context.engine_api._get_sheet_objects_detailed(app_handle, sheet_id) or []
        formatted = [
            {"object_id": o.get("object_id", ""),
             "object_type": o.get("object_type", ""),
             "object_description": o.get("object_title", ""),
             # Already computed while reading each object's layout and
             # properties. Dropping any of it meant answering "which fields
             # does this sheet work with" took one get_app_object call per
             # object — and the expressions were not reachable at all, since
             # the layout get_app_object returns does not contain them.
             "fields_used": o.get("fields_used", []),
             "measures": o.get("measures", []),
             "dimensions": o.get("dimensions", [])}
            for o in objects if isinstance(o, dict)
        ]
        return _ok({"app_id": app_id, "sheet_id": sheet_id, "total_objects": len(formatted), "objects": formatted})
    except Exception as ex:
        return _err(str(ex), app_id=app_id, sheet_id=sheet_id)



@mcp.tool()
@_timed
@_engine_serialised
def get_app_object(app_id: str, object_id: str) -> str:
    """
    Read one chart in full: its definition and the data it currently
    shows.

    WHEN TO USE
        When a figure on a dashboard has to be explained or reproduced.
        Compare the chart's own expressions with yours rather than
        comparing the two numbers — where they differ it is almost always
        the filters, not the arithmetic.

    WHEN NOT TO USE
        To survey a sheet: `get_app_sheet_objects` already carries the
        expressions and fields of every object on it, for a fraction of
        the reply size. Come here for one object, not for all of them.

    Args:
        app_id: application GUID.
        object_id: from `get_app_sheet_objects`.

    Returns:
        The object's whole `qLayout`, plus `measures`, `dimensions` and
        `fields_used`. Read the expressions from `measures`: Engine does
        not put them in the layout, where `qMeasureInfo` carries only the
        fallback title, formatting and statistics. Master items are
        resolved to their library definitions. Computed data sits in
        `qLayout.qHyperCube.qDataPages`.
        An object Engine will not open is reported as
        `object_not_available`; a failure to read its properties leaves
        the layout intact and adds `properties_error`.

    Does not return:
        Data beyond what the chart itself displays.
    """
    e = _check()
    if e:
        return e
    try:
        app_handle = context.engine_api.ensure_app(app_id, no_data=False)
        obj_result = context.engine_api.send_request("GetObject", {"qId": object_id}, handle=app_handle)
        # An object Engine knows about but hands back without a handle
        # (a type it will not open, or one the user may not read) used to
        # raise KeyError here and surface as an opaque failure.
        obj_handle = (obj_result.get("qReturn") or {}).get("qHandle")
        if obj_handle is None:
            return _err(
                f"Object {object_id} cannot be opened",
                error_category="object_not_available",
                hint="Check the id against get_app_sheet_objects — Engine "
                     "returns no handle for an unknown object, and for some "
                     "object types it refuses to open one.",
                response=obj_result,
            )
        layout_result = context.engine_api.send_request("GetLayout", [], handle=obj_handle)
        if "qLayout" not in layout_result:
            return _err("Failed to get object layout",
                        error_category="engine_api_error")
        # The layout does not contain the measure expressions — Engine puts
        # qFallbackTitle, formatting and statistics in qMeasureInfo and
        # nothing else. Since reverse-engineering a chart is what this tool
        # is for, fetch the properties too and resolve any master items.
        try:
            properties = context.engine_api.send_request(
                "GetProperties", [], handle=obj_handle)
            expressions = context.engine_api._object_expressions(properties)
            by_index = {0: expressions}
            context.engine_api._resolve_library_items(app_handle, by_index)
            layout_result["measures"] = expressions["measures"]
            layout_result["dimensions"] = expressions["dimensions"]
            layout_result["fields_used"] = sorted(
                set(context.engine_api._extract_fields_from_object(layout_result["qLayout"]))
                | {f for m in expressions["measures"]
                   for f in context.engine_api._extract_fields_from_expression(
                       m.get("expression", ""))}
                | {f for d in expressions["dimensions"] for f in d.get("fields", [])}
            )
        except Exception as prop_error:
            # The layout is still worth returning; say what is missing
            # rather than pretending the object has no measures.
            logger.warning("GetProperties failed for %s: %s", object_id, prop_error)
            layout_result["properties_error"] = str(prop_error)
        return _ok(layout_result)
    except Exception as ex:
        return _err(str(ex), app_id=app_id, object_id=object_id)




@mcp.tool()
@_timed
@_engine_serialised
def search_app(app_id: str, term: str, fields: Optional[List[str]] = None,
               max_fields: int = 8, max_values: int = 5) -> str:
    """
    Find which field holds a value, and how that value is spelled there.

    WHEN TO USE
        When someone names something — a city, a customer, a product —
        and you do not know which field it lives in. One call finds both
        the field and the exact spelling.

    WHEN NOT TO USE
        When the field is already known: `get_app_field` with
        `search_string` looks in that field alone and is far cheaper.
        Searching every field of a large app takes about thirty seconds
        against roughly a second for a named one, so pass `fields`
        whenever there is a reasonable guess.

    Args:
        app_id: application GUID.
        term: what to look for. Qlik matches it as a prefix, case
            insensitively, so "Mos" finds "Moskva".
        fields: field names to search. Omit to search the whole app.
        max_fields: matching fields to report. Default 8.
        max_values: matching values per field. Default 5.

    Returns:
        `matches`, each naming a field and the values it holds that match,
        spelled as Qlik stores them; plus `fields_matched`. An empty
        `matches` means the app does not hold this value — an answer, not
        a failure, and it comes with a hint on what to try next.

    Does not return:
        How often a value occurs, or which rows contain it.
    """
    e = _check()
    if e:
        return e
    try:
        app_handle = context.engine_api.ensure_app(app_id, no_data=False)
        result = context.engine_api.search_app(
            app_handle, term, fields=fields,
            max_fields=max_fields, max_values=max_values)
        if not result.get("matches"):
            result["hint"] = (
                f"Nothing in this app matches {term!r}. Check the spelling "
                f"against `values` / `lowest_values` in get_app_details, or "
                f"search for a shorter prefix."
            )
        return _ok(result)
    except Exception as ex:
        return _err(str(ex), app_id=app_id, term=term,
                    error_category="engine_api_error")
