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
    Get the full load script (LOAD ... FROM ... statements) of a Qlik Sense app.

    Use this to understand how data is ingested into the app — source systems,
    transformations, joins, variables, and section access. Read this BEFORE
    writing non-trivial set analysis: the script reveals field renames, data
    model shape, and any `$(variable)` definitions used in expressions.

    Args:
        app_id: Application GUID. Required. Get it from `get_apps` or
            `get_app_details`.

    Returns:
        JSON with `qScript` (the full script as a single string),
        `script_length` (character count), and `app_id`.
    
    Example:
        Call: {"app_id": "a1b2c3d4-1111-2222-3333-444455556666"}
        Returns: {"tool_call_seconds": 1.12,
                  "qScript": "SET ThousandSep=' ';\nOrders:\nLOAD ... FROM ...",
                  "app_id": "a1b2c3d4-1111-2222-3333-444455556666",
                  "script_length": 14872}

    Qlik session limit: this server keeps ONE Engine session for all
    calls. Qlik allows max 5 concurrent sessions per user and may LOCK
    the account beyond that — never run these calls in parallel or
    start a second MCP process with the same credentials.
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
    Compute statistics for a single field via a measures-only hypercube.

    DEFAULT (LIGHT) MODE — fast on any table size, returns:
        unique_values, non_null_count, null_count, total_count, min_value,
        max_value, null_percentage, completeness_percentage.
        `non_null_count` is Qlik's `Count()`, `null_count` is `NullCount()`,
        and `total_count` is their sum — the rows the field appears in.

    FULL MODE (`full=True`) — adds avg, sum, median, mode, std_deviation.
    These extra measures are EXTREMELY SLOW on large fact tables (>100M
    rows) and meaningless for date/text fields. Use only when you actually
    need them on a small dimension table.

    DO NOT CALL THIS ON DATE FIELDS to learn the loaded period — sum/avg
    of timestamps is nonsense and slow. Instead use `engine_create_hypercube`
    with measures `Min([YourDateField])` and `Max([YourDateField])`,
    no dimensions, `max_rows=1`. Same for "give me a couple sample values"
    — use `get_app_field` (which itself falls back to a hypercube on
    high-cardinality fields).

    PERFORMANCE: even in light mode, calling this on a 500M-row fact table
    can still take tens of seconds — Engine has to count nulls. If you
    already have `distinct_values` and row count from `get_app_details`,
    you usually don't need this tool at all.

    Args:
        app_id: Application GUID. Required.
        field_name: Exact field name as it appears in the data model. No square
            brackets — pass `"<FieldName>"`, NOT `"[<FieldName>]"`. Get valid field
            names from `get_app_details` (`fields[*].name`).
        full: If True, also compute avg/sum/median/mode/stdev. Default False.
            Only enable for small (<10M rows) numeric fields.

    Returns:
        JSON with unique_values, total_count, non_null_count, min_value,
        max_value, null_percentage, completeness_percentage. If `full=True`,
        also avg_value/sum_value/median_value/mode_value/std_deviation. Each
        stat is `{text, numeric, is_numeric}`.
    
    Example (default light mode):
        Call: {"app_id": "a1b2...", "field_name": "Amount"}
        Returns: {"tool_call_seconds": 3.4, "field_name": "Amount",
                  "unique_values": {"text": "9421", "numeric": 9421,
                                    "is_numeric": true},
                  "non_null_count": {"...": "..."},
                  "null_count": {"...": "..."},
                  "total_count": {"...": "..."},
                  "min_value": {"...": "..."}, "max_value": {"...": "..."},
                  "null_percentage": 0.67, "completeness_percentage": 99.33,
                  "debug_log": ["..."]}

    Example (full mode — adds avg/sum/median/mode/stdev, slow):
        Call: {"app_id": "a1b2...", "field_name": "Amount", "full": true}
        Returns: {"tool_call_seconds": 41.7, "avg_value": {"...": "..."},
                  "sum_value": {"...": "..."}, "median_value": {"...": "..."},
                  "mode_value": {"...": "..."}, "std_deviation": {"...": "..."}}

    Qlik session limit: this server keeps ONE Engine session for all
    calls. Qlik allows max 5 concurrent sessions per user and may LOCK
    the account beyond that — never run these calls in parallel or
    start a second MCP process with the same credentials.
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
    Lightning-fast bounds query for a single field: distinct count + min + max.

    Use this BEFORE building any heavy hypercube to learn:
      - the loaded period of a date field (`Min`/`Max`)
      - the cardinality of a key column
      - the range of a numeric measure

    Internally builds a measures-only hypercube with 3 expressions
    (`Count(DISTINCT)`, `Min`, `Max`) and no dimensions. Engine resolves
    these from the symbol table without scanning rows, so the call returns
    in seconds even on multi-billion-row tables — orders of magnitude
    faster than `get_app_field_statistics`.

    PREFER THIS OVER:
      - `get_app_field_statistics` (which adds slow Sum/Avg/Median/Mode/Stdev)
      - `get_app_field` (which materializes individual values — heavy on
        high-cardinality keys)

    Args:
        app_id: Application GUID. Required.
        field_name: Exact field name, no square brackets. Required.

    Returns:
        JSON `{ "field_name": ..., "unique_values": {text,numeric},
        "min_value": {text,numeric}, "max_value": {text,numeric} }`.
    
    Example:
        Call: {"app_id": "a1b2...", "field_name": "OrderDate"}
        Returns: {"tool_call_seconds": 0.9, "field_name": "OrderDate",
                  "unique_values": {"text": "939", "numeric": 939,
                                    "is_numeric": true},
                  "min_value": {"text": "2024-01-01", "numeric": 45292,
                                "is_numeric": true},
                  "max_value": {"text": "2026-07-27", "numeric": 46230,
                                "is_numeric": true}}

    Qlik session limit: this server keeps ONE Engine session for all
    calls. Qlik allows max 5 concurrent sessions per user and may LOCK
    the account beyond that — never run these calls in parallel or
    start a second MCP process with the same credentials.
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
    filters: Optional[List[Dict[str, Any]]] = None,
    sort_by: Optional[str] = None,
    sort_order: str = "desc",
    limit: int = 100,
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

        There is no cap on how many. Plan the whole question in one call
        rather than discovering it one round-trip at a time.

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
            median, stdev. `label` is optional and defaults to
            `<agg>_<field>`; it names the column and is what `sort_by`
            takes.
        filters: what to narrow to. Two shapes:
            {"field": "OrderDate", "from": "2024-01-01", "to": "2024-12-31"}
            {"field": "Region", "values": ["North", "South"]}
            A period bound may be a day (2024-01-31 or 31.01.2024), a month
            (2024-01), a year (2024) or a Qlik serial number. `from` and
            `to` include both ends. `{"field": "OrderDate", "period":
            "2024"}` is the whole year in one key. Filters combine with AND.
        sort_by: a metric label or a grouping field. Set it whenever
            `limit` might cut the result, otherwise the rows that come back
            are arbitrary rather than the largest.
        sort_order: "desc" (default) or "asc".
        limit: rows per query, default 100, cap 5000.
        queries: a list of whole queries, each with the keys above plus an
            optional "id". Use it for several independent questions at
            once. When present, the single-query arguments are ignored.

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

    DOES NOT RETURN
        Raw Qlik layout, per-cell state, or the expressions it wrote.
        Row-level records — every reply is aggregated.

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
            "filters": filters or [],
            "sort_by": sort_by,
            "sort_order": sort_order,
            "limit": limit,
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
    exclude_null_dimensions: bool = True,
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
            NULL — the one Qlik displays as `"-"`. Default True.
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
            "to": "2024-12-31"}` or `{"field": "Region", "values":
            ["North"]}`.

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
    List distinct values of a single field (like `SELECT DISTINCT field FROM ... LIMIT N`),
    with optional wildcard filtering and pagination.

    Use this to see what values a field actually contains before writing set
    analysis (`{<Field={'value1','value2'}>}`). Particularly useful for
    dimension fields like status codes, categories, region names.

    For min/max/cardinality of a single field prefer the much faster
    `engine_get_field_range`. For grouped aggregations use
    `engine_create_hypercube`.

    IMPLEMENTATION NOTE: this tool first tries the lightweight ListObject
    API. If ListObject returns an empty result (which can happen for fields
    in fact tables that have no current "state"), it transparently falls
    back to a one-dimension hypercube. The response then includes
    `fallback_used: "hypercube"`. If both methods return nothing, the
    response includes a `warning` explaining why and suggesting next steps.

    Args:
        app_id: Application GUID. Required.
        field_name: Exact field name, no square brackets. Case-sensitive.
        limit: Max values to return per page. Default 10, cap 100.
        offset: Number of values to skip for pagination. Default 0.
        search_string: Optional wildcard filter applied to the text form of
            the value. Supports `*` and `%` as multi-character wildcards.
            Example: `"<prefix>*"` matches any value starting with `<prefix>`.
            Leave `None` to return all values.
        search_number: Optional wildcard filter on the numeric/text form.
            Matches values whose number OR text representation matches the
            pattern. Useful for filtering IDs by prefix.
        case_sensitive: If `False` (default) the wildcard match is
            case-insensitive; set to `True` for exact case matching.

    Returns:
        JSON `{ "field_values": ["val1", "val2", ...] }` — plain list after
        filtering and pagination. Order is by frequency descending on the
        Qlik side. When the load script attached a `COMMENT FIELD` text to
        this field, the response also carries `field_comment` with that
        business description.

    Example (see what values a dimension holds):
        Call: {"app_id": "a1b2...", "field_name": "Region", "limit": 5}
        Returns: {"tool_call_seconds": 0.7,
                  "field_values": ["North", "South", "West"],
                  "field_comment": "Sales region of the client"}

    Example (wildcard search; `fallback_used` appears when the fast
    ListObject path returned nothing and a hypercube was used instead):
        Call: {"app_id": "a1b2...", "field_name": "OrderID",
               "search_string": "ORD-2026*", "limit": 10}
        Returns: {"tool_call_seconds": 2.1,
                  "field_values": ["ORD-2026-000001"],
                  "fallback_used": "hypercube"}

    Qlik session limit: this server keeps ONE Engine session for all
    calls. Qlik allows max 5 concurrent sessions per user and may LOCK
    the account beyond that — never run these calls in parallel or
    start a second MCP process with the same credentials.
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
            field_data = context.engine_api.get_field_values(
                app_handle, field_name, lim, include_frequency=False, offset=off)
            values = [v.get("value", "") for v in field_data.get("values", [])]
            out = {"field_values": values}
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
    List user-defined Qlik variables (`SET`/`LET` from script, or UI-created),
    split by source. System/reserved variables are always excluded.

    Use this to discover `$(vCurrentYear)`-style shortcuts used in chart
    expressions — expanding them manually gives you the real set analysis.

    Args:
        app_id: Application GUID. Required.
        limit: Max variables per page. Default 10, cap 100.
        offset: Number of variables to skip for pagination. Default 0.
        created_in_script: Filter by source. Accepts `"true"` / `"false"`
            (case-insensitive). `"true"` — only script-created (`SET`/`LET`);
            `"false"` — only UI-created; `None` (default) — both.
        search_string: Optional wildcard filter on variable name OR its
            text value. Supports `*` and `%`. Leave `None` for no filter.
        case_sensitive: Toggle case-sensitive wildcard match. Default `False`.

    Returns:
        JSON `{ "variables_from_script": {name: value, ...},
        "variables_from_ui": {name: value, ...}, "count": N,
        "total_found": M }`. Both groups are always objects — an empty one
        is `{}`, never `""`. `count` is what this page holds, `total_found`
        how many matched before paging.
    
    Example (default — both sources):
        Call: {"app_id": "a1b2..."}
        Returns: {"tool_call_seconds": 0.8,
                  "variables_from_script": {"vCurrentYear": "2026"},
                  "variables_from_ui": {"vSelectedRegion": "North"},
                  "count": 2, "total_found": 2}

    Example (script SET/LET variables only):
        Call: {"app_id": "a1b2...", "created_in_script": "true", "limit": 50}
        Returns: {"tool_call_seconds": 0.85,
                  "variables_from_script": {"vCurrentYear": "2026",
                      "vSetPeriod": "{<[Year]={2026}>}"},
                  "variables_from_ui": {}, "count": 2, "total_found": 2}

    Qlik session limit: this server keeps ONE Engine session for all
    calls. Qlik allows max 5 concurrent sessions per user and may LOCK
    the account beyond that — never run these calls in parallel or
    start a second MCP process with the same credentials.
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
    List all sheets (tabs) in a Qlik Sense application.

    Use this to discover which sheets exist before drilling into their objects
    with `get_app_sheet_objects`. Sheets are the top-level pages users see in
    the Qlik dashboard UI.

    Args:
        app_id: Application GUID. Required.

    Returns:
        JSON `{ "app_id": ..., "total_sheets": N, "sheets": [{sheet_id, title,
        description}, ...] }`. Pass `sheet_id` into `get_app_sheet_objects` to
        list the charts/tables on that sheet.
    
    Example:
        Call: {"app_id": "a1b2..."}
        Returns: {"tool_call_seconds": 1.3, "app_id": "a1b2...",
                  "total_sheets": 2,
                  "sheets": [{"sheet_id": "b2c3d4e5-1111-...",
                              "title": "Overview", "description": "..."}]}

    Qlik session limit: this server keeps ONE Engine session for all
    calls. Qlik allows max 5 concurrent sessions per user and may LOCK
    the account beyond that — never run these calls in parallel or
    start a second MCP process with the same credentials.
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
    List all visualization objects (charts, tables, KPIs, filters, etc) placed
    on a specific sheet, with their IDs and types.

    Use this to discover the `object_id` of a specific chart the user mentions
    by title, then pass it to `get_app_object` to inspect the chart's full
    layout (dimensions, measures, expressions, current selections).

    Args:
        app_id: Application GUID. Required.
        sheet_id: Sheet ID from `get_app_sheets`. Required.

    Returns:
        JSON with `objects` array where each element has `object_id`,
        `object_type` (e.g. `"barchart"`, `"table"`, `"kpi"`, `"listbox"`),
        `object_description` (title) and `fields_used` — the fields the
        object's dimensions and measures refer to, which answers "what does
        this sheet work with" without opening every object. Use `object_id`
        in `get_app_object` for the full layout.

    Example:
        Call: {"app_id": "a1b2...", "sheet_id": "b2c3d4e5-1111-..."}
        Returns: {"tool_call_seconds": 1.1, "total_objects": 2,
                  "objects": [{"object_id": "AbCdEf",
                               "object_type": "barchart",
                               "object_description": "Sales by Region",
                               "fields_used": ["Region", "Sales"]}]}

    Qlik session limit: this server keeps ONE Engine session for all
    calls. Qlik allows max 5 concurrent sessions per user and may LOCK
    the account beyond that — never run these calls in parallel or
    start a second MCP process with the same credentials.
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
    Fetch the full layout of a specific visualization object (chart, table, KPI,
    pivot table, etc.) by its object ID — equivalent to Engine API
    `GetObject` + `GetLayout`.

    This returns everything the Qlik client renders: the hypercube with
    current data, dimension/measure definitions and expressions, title,
    subtitle, colors, sort order, current selections applied to the chart.
    Use it to reverse-engineer how a dashboard chart is computed before
    rebuilding the same logic in `engine_create_hypercube`.

    Args:
        app_id: Application GUID. Required.
        object_id: Object ID from `get_app_sheet_objects`. Required.

    Returns:
        JSON with the full `qLayout` of the object, plus `measures`,
        `dimensions` and `fields_used`. Read the expressions from
        `measures` — Engine does NOT put them in the layout, where
        `qMeasureInfo` carries only the fallback title, formatting and
        statistics. Master measures and dimensions are resolved to their
        library definitions. In the layout itself, look for
        `qHyperCube.qDimensionInfo` and `qDataPages[0].qMatrix` for the
        computed data.

    Example:
        Call: {"app_id": "a1b2...", "object_id": "AbCdEf"}
        Returns: {"tool_call_seconds": 1.4,
                  "qLayout": {"qInfo": {"qId": "AbCdEf", "qType": "barchart"},
                              "qMeta": {"title": "Sales by Region"},
                              "qHyperCube": {"qDimensionInfo": ["..."],
                                             "qMeasureInfo": ["..."],
                                             "qDataPages": ["..."]}}}

    Use this to copy an existing chart's expressions into
    `engine_create_hypercube`.

    Qlik session limit: this server keeps ONE Engine session for all
    calls. Qlik allows max 5 concurrent sessions per user and may LOCK
    the account beyond that — never run these calls in parallel or
    start a second MCP process with the same credentials.
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
    Find where a value lives in an app: which field contains it, and how it
    is actually spelled there.

    Use this BEFORE writing a set-analysis filter on a value you have not
    seen in the data. Qlik does not refuse a filter on a value that does not
    exist — it returns zeros — so "Moscow" against a field holding "Moskva"
    produces a clean, wrong answer. One call here settles it.

    Also use it when the user names something ("the Northwest district",
    "customer C0001") and you do not know which field holds it.

    Args:
        app_id: Application GUID. Required.
        term: What to look for. Matched as a prefix by Qlik's own search, so
            "Mos" finds "Moskva". Case-insensitive.
        fields: Optional list of field names to search. Omit to search the
            whole app. On a 10M-row app the whole-app search takes ~30s
            against about a second for a named field, so pass the field names
            when you have a good guess.
        max_fields: How many matching fields to report. Default 8.
        max_values: How many matching values per field. Default 5.

    Returns:
        JSON with `matches`: for each field that contains the term, the
        field name and the matching values exactly as Qlik stores them.
        An empty `matches` means the value is not in the app — which is an
        answer, not a failure.

    Example:
        Call: {"app_id": "a1b2...", "term": "Mos"}
        Returns: {"tool_call_seconds": 1.4, "term": "Mos", "fields_matched": 1,
                  "matches": [{"field": "region_name", "values": ["Moskva"]}]}
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
