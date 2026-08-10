"""Hypercubes: the GROUP BY of this server.

Sorting, NULL handling, the row and cell ceilings, page completion and
the compact response shape. The subtleties are documented at their point
of use — they are the difference between a ranking and a plausible-looking
list of arbitrary rows.
"""

from ..config import (
    DEFAULT_HYPERCUBE_MAX_ROWS,
)
from typing import Dict, List, Any, Optional
import logging
import time
import uuid

logger = logging.getLogger(__name__)


class EngineHypercubeMixin:
    # How long a socket is trusted without probing after its last answered
    # frame. Anything shorter buys nothing: a socket that answered a moment
    # ago is alive, and the probe itself costs a round-trip.
    # Row and cell ceilings for one hypercube request. The cell cap sits
    # just under Qlik's own 10000-per-NxPage limit (error 7009
    # calc-pages-too-large), so a page this server plans always fits.
    HARD_MAX_ROWS = 5000
    HARD_MAX_CELLS = 9900

    # Sort-direction aliases accepted from LLM callers. Qlik encodes the
    # direction as -1 (descending) / 1 (ascending) / 0 (criterion disabled).
    _SORT_ORDER_ALIASES = {
        "desc": -1, "descending": -1, "down": -1, "high": -1,
        "highest": -1, "top": -1, "-1": -1,
        "asc": 1, "ascending": 1, "up": 1, "low": 1,
        "lowest": 1, "bottom": 1, "1": 1,
    }

    @staticmethod
    def _normalize_sort_order(sort_order: Any) -> Optional[int]:
        """
        Map a human/LLM-supplied sort direction onto Qlik's -1 / 1.

        Returns None when the value is not recognised, so the caller can
        answer with an explicit error instead of silently sorting the
        wrong way round.
        """
        if isinstance(sort_order, bool):
            return None
        if isinstance(sort_order, int):
            return sort_order if sort_order in (-1, 1) else None
        if isinstance(sort_order, str):
            return EngineHypercubeMixin._SORT_ORDER_ALIASES.get(sort_order.strip().lower())
        return None

    @staticmethod
    def _column_names(
        converted_dimensions: List[Dict[str, Any]],
        converted_measures: List[Dict[str, Any]],
    ) -> List[str]:
        """
        Human-readable column names in hypercube column order.

        Column order is fixed by the Engine API: every dimension first
        (0..D-1, in declaration order), then every measure (D..D+M-1).
        """
        names = [str(d.get("field", "")) for d in converted_dimensions]
        for i, m in enumerate(converted_measures):
            names.append(str(m.get("label") or m.get("expression") or f"Measure_{i}"))
        return names

    @staticmethod
    def _as_value_expr(expression: Any) -> Dict[str, Any]:
        """
        Wrap a sort expression into Qlik's ValueExpr shape `{"qv": "..."}`.

        Accepts the bare string this tool documents AND the native Qlik
        form, which is what a caller reading Qlik's own API docs will
        write. Without this, `{"qv": "Sum(x)"}` was wrapped a second time
        into `{"qv": {"qv": "Sum(x)"}}` and the Engine ignored the sort.
        """
        if isinstance(expression, dict):
            return {"qv": expression.get("qv", "")}
        return {"qv": expression or ""}

    @staticmethod
    def _matrix_to_rows(
        data_pages: List[Dict[str, Any]],
        column_names: List[str],
    ) -> List[List[Any]]:
        """
        Flatten Engine's qMatrix into plain rows of values.

        Each Engine cell is `{qText, qNum, qElemNumber, qState}`; consumers
        of this tool only ever need the value, and `qNum == "NaN"` is
        Engine's way of saying "this cell is text or empty". Returning
        numbers as numbers means an LLM can compare and sum them without
        first parsing locale-formatted strings like "95 552 568 044,926".
        """
        rows: List[List[Any]] = []
        for page in data_pages or []:
            for matrix_row in page.get("qMatrix", []) or []:
                row: List[Any] = []
                for cell in matrix_row:
                    num = cell.get("qNum")
                    row.append(cell.get("qText") if num == "NaN" or num is None else num)
                rows.append(row)
        return rows

    @staticmethod
    def _resolve_sort_column(
        sort_by: Any,
        converted_dimensions: List[Dict[str, Any]],
        converted_measures: List[Dict[str, Any]],
    ) -> Optional[int]:
        """
        Resolve `sort_by` to a hypercube column index, or None if unknown.

        Accepts, in this order of preference:
          * an int — used directly as a column index;
          * a measure label, a measure expression, or the auto-generated
            `Measure_<i>` name;
          * a dimension field name.
        Matching is case-insensitive and tolerates surrounding square
        brackets, because LLMs routinely write `[Sales]` for a field.
        """
        n_dims = len(converted_dimensions)
        n_cols = n_dims + len(converted_measures)

        if isinstance(sort_by, bool):
            return None
        if isinstance(sort_by, int):
            return sort_by if 0 <= sort_by < n_cols else None
        if not isinstance(sort_by, str):
            return None

        def _key(value: Any) -> str:
            return str(value or "").strip().strip("[]").casefold()

        target = _key(sort_by)
        if not target:
            return None

        # Measures win over dimensions: "sort by Sales" almost always means
        # the aggregate, and a measure label may legitimately repeat the
        # name of the field it aggregates.
        for i, measure in enumerate(converted_measures):
            candidates = {
                _key(measure.get("label")),
                _key(measure.get("expression")),
                _key(f"Measure_{i}"),
            }
            if target in candidates - {""}:
                return n_dims + i

        for i, dim in enumerate(converted_dimensions):
            if target == _key(dim.get("field")):
                return i

        return None

    def _read_remaining_pages(self, cube_handle: int, n_cols: int,
                              rows_so_far: int, wanted_rows: int,
                              timings: Dict[str, Any]) -> "tuple[List[Dict[str, Any]], int]":
        """Fetch the rows GetLayout did not include, page by page.

        Stops on an empty page: an Engine that keeps answering with nothing
        would otherwise spin forever. A page never exceeds the same cell
        budget the initial fetch is planned against, because Engine rejects
        anything over 10000 cells with error 7009.
        """
        pages: List[Dict[str, Any]] = []
        rows_per_page = max(1, self.HARD_MAX_CELLS // max(1, n_cols))
        t_pages = time.monotonic()
        fetches = 0

        while rows_so_far < wanted_rows:
            height = min(rows_per_page, wanted_rows - rows_so_far)
            try:
                reply = self.send_request(
                    "GetHyperCubeData",
                    ["/qHyperCubeDef", [{"qTop": rows_so_far, "qLeft": 0,
                                         "qHeight": height, "qWidth": n_cols}]],
                    handle=cube_handle,
                    timeout=self.ws_operation_timeout,
                )
            except Exception as exc:
                # Partial data beats losing the rows already in hand, but
                # the caller has to be told the difference between "this is
                # the top N you asked for" and "the rest could not be
                # read" — the two look identical in the payload otherwise.
                logger.warning("create_hypercube: GetHyperCubeData(top=%d) failed: %s",
                               rows_so_far, exc)
                timings["page_fetch_error"] = f"{type(exc).__name__}: {exc}"
                break

            page_rows = 0
            for page in reply.get("qDataPages", []) or []:
                matrix = page.get("qMatrix", [])
                if matrix:
                    pages.append(page)
                    page_rows += len(matrix)
            fetches += 1
            if page_rows == 0:
                # Engine had more rows a moment ago and now returns none.
                # Whatever the reason, the result is short of what was
                # asked for, and that has to reach the caller.
                logger.warning(
                    "create_hypercube: GetHyperCubeData(top=%d) returned no rows, "
                    "stopping with %d of %d rows", rows_so_far, rows_so_far, wanted_rows)
                timings["page_fetch_error"] = (
                    f"Engine returned an empty page at row {rows_so_far}")
                break
            rows_so_far += page_rows

        if fetches:
            timings["extra_pages_seconds"] = round(time.monotonic() - t_pages, 3)
            timings["extra_page_fetches"] = fetches
        return pages, rows_so_far

    def create_hypercube(
        self,
        app_handle: int,
        dimensions: List[Dict[str, Any]] = None,
        measures: List[Dict[str, Any]] = None,
        max_rows: int = DEFAULT_HYPERCUBE_MAX_ROWS,
        sort_by: Optional[Any] = None,
        sort_order: str = "desc",
        suppress_zero: bool = False,
        include_raw_layout: bool = False,
        exclude_null_dimensions: bool = True,
    ) -> Dict[str, Any]:
        """
        Create a hypercube (grouped aggregation) and return its first page.

        `sort_by` names the column that drives the row order — a measure
        label/expression or a dimension field name. It is translated into
        `qInterColumnSortOrder` with that column first, which is the only
        way the Engine sorts rows by a measure. Sorting by an already
        computed measure column costs nothing extra, unlike
        `qSortByExpression`, which makes the Engine evaluate the aggregate
        a second time purely for ordering.

        `exclude_null_dimensions` (default True) sets `qNullSuppression`
        on every dimension, dropping the row whose dimension value is
        NULL (Qlik renders it as "-"). Unattributed facts often
        accumulate into that single row, which then wins the ranking and
        pushes out the real values — a top-10 that starts with "unknown"
        is rarely what the caller wanted.
        """
        import time
        import traceback as _tb
        step = "init"
        t0 = time.monotonic()
        timings: Dict[str, float] = {}
        created_object_id: Optional[str] = None
        try:
            # Handle empty dimensions/measures
            if dimensions is None:
                dimensions = []
            if measures is None:
                measures = []

            # Normalise both the legacy string form and the dict form into
            # dicts. Every branch COPIES the caller's dict — mutating the
            # argument in place would leak our defaults back into the
            # caller's data and make `dimensions` in the response something
            # other than the echoed input it claims to be.
            converted_dimensions = []
            for dim in dimensions:
                if isinstance(dim, str):
                    dim = {"field": dim}
                else:
                    dim = dict(dim)
                dim.setdefault("sort_by", {
                    "qSortByNumeric": 0,
                    "qSortByAscii": 1,  # Default: ASCII ascending
                    "qSortByExpression": 0,
                    "qExpression": "",
                })
                converted_dimensions.append(dim)

            converted_measures = []
            for measure in measures:
                if isinstance(measure, str):
                    measure = {"expression": measure}
                else:
                    measure = dict(measure)
                # Default: numeric descending. Only takes effect once this
                # measure is the leading column of qInterColumnSortOrder.
                measure.setdefault("sort_by", {"qSortByNumeric": -1})
                converted_measures.append(measure)

            # Hard limits enforced in our layer — NOT in Qlik Engine.
            # The intent is to force the LLM to design narrow, focused
            # hypercubes (with set analysis, smart dimensions, and top-N
            # patterns) rather than bulk-dump huge tables and post-process
            # client-side. If the LLM needs more data, it should issue
            # multiple well-scoped queries, not one giant one.
            HARD_MAX_ROWS = self.HARD_MAX_ROWS
            HARD_MAX_CELLS = self.HARD_MAX_CELLS
            n_cols = len(converted_dimensions) + len(converted_measures)
            n_dims = len(converted_dimensions)
            column_names = self._column_names(converted_dimensions, converted_measures)

            # ── Resolve the requested sort ────────────────────────────────
            # Both failures below are caller mistakes, so they are reported
            # as structured `plan` errors listing the valid options rather
            # than being silently ignored — a silently ignored sort returns
            # plausible-looking rows in the wrong order, which is worse
            # than an error.
            step = "plan"
            sort_column_index: Optional[int] = None
            sort_direction: Optional[int] = None
            if sort_by is not None and n_cols > 0:
                sort_direction = self._normalize_sort_order(sort_order)
                if sort_direction is None:
                    return {
                        "error": (
                            f"sort_order={sort_order!r} is not a valid sort "
                            f"direction."
                        ),
                        "error_category": "invalid_sort",
                        "failed_step": "plan",
                        "hint": (
                            "Use sort_order=\"desc\" for largest-first "
                            "(top-N) or sort_order=\"asc\" for "
                            "smallest-first (bottom-N)."
                        ),
                    }
                sort_column_index = self._resolve_sort_column(
                    sort_by, converted_dimensions, converted_measures
                )
                if sort_column_index is None:
                    return {
                        "error": (
                            f"sort_by={sort_by!r} does not match any column "
                            f"of this hypercube."
                        ),
                        "error_category": "invalid_sort",
                        "failed_step": "plan",
                        "available_columns": column_names,
                        "hint": (
                            "sort_by must name one of the columns listed in "
                            "`available_columns` — a measure label, a "
                            "measure expression, or a dimension field name "
                            "(matching ignores case and square brackets). "
                            "To rank by an aggregate, give the measure a "
                            "`label` and pass that same label as sort_by."
                        ),
                    }

            # Reject a limit that asks for no rows at all. Without this the
            # qHeight clamp below turns limit=0 or limit=-7 into a single
            # row, quietly returning data the caller did not ask for.
            if not isinstance(max_rows, int) or isinstance(max_rows, bool) or max_rows < 1:
                return {
                    "error": (
                        f"limit={max_rows!r} is not a positive row count."
                    ),
                    "error_category": "invalid_limit",
                    "failed_step": "plan",
                    "hard_max_rows": HARD_MAX_ROWS,
                    "hint": (
                        f"Pass an integer between 1 and {HARD_MAX_ROWS}. For a "
                        f"single grand-total row use limit=1 with no "
                        f"dimensions; for a ranking use a small limit (10-50) "
                        f"together with sort_by."
                    ),
                }

            # Reject max_rows over the hard cap.
            if max_rows > HARD_MAX_ROWS:
                return {
                    "error": (
                        f"max_rows={max_rows} exceeds the hard limit of "
                        f"{HARD_MAX_ROWS}. This limit is enforced by the MCP "
                        f"server to force narrow, focused queries."
                    ),
                    "error_category": "limit_exceeded",
                    "failed_step": "plan",
                    "hard_max_rows": HARD_MAX_ROWS,
                    "hint": (
                        "Design a smaller query instead of bulk-dumping:\n"
                        "  1. Add set analysis to narrow the period/scope, "
                        "e.g. {<[<DimPeriod>]={<val>}>} inside each measure "
                        "(substitute real field and value).\n"
                        "  2. For top-N ranking: pass max_rows=N (N<=50) "
                        "with qSortByExpression on the ranking measure.\n"
                        "  3. Split the problem: 100 small focused queries "
                        "beat one giant scan — iterate over values of one "
                        "categorical dimension, one hypercube per value.\n"
                        "  4. Use get_app_details to see distinct_values "
                        "for each dimension BEFORE building the cube."
                    ),
                }

            # Reject cubes whose theoretical width*height exceeds Qlik's
            # 10k-cell page cap — we refuse instead of auto-paginating.
            # This teaches the LLM to either reduce dimensions/measures
            # or reduce max_rows.
            if n_cols > 0 and n_cols * max_rows > HARD_MAX_CELLS:
                suggested_rows = max(1, HARD_MAX_CELLS // n_cols)
                return {
                    "error": (
                        f"Hypercube too wide: columns={n_cols} * "
                        f"max_rows={max_rows} = {n_cols * max_rows} cells, "
                        f"exceeds Qlik's {HARD_MAX_CELLS}-cell limit per "
                        f"fetch page."
                    ),
                    "error_category": "cell_cap_exceeded",
                    "failed_step": "plan",
                    "columns": n_cols,
                    "max_rows_requested": max_rows,
                    "cells_requested": n_cols * max_rows,
                    "cell_cap": HARD_MAX_CELLS,
                    "hint": (
                        f"Either drop to max_rows={suggested_rows} with the "
                        f"current {n_cols} columns, OR reduce the number "
                        f"of dimensions/measures. The MCP server refuses "
                        f"to auto-paginate — design the query you actually "
                        f"need instead. Unique-row estimation: multiply "
                        f"distinct_values of all your dimensions "
                        f"(from get_app_details) — if the product is over "
                        f"{HARD_MAX_ROWS}, the query is too broad."
                    ),
                }

            first_page_height = max(1, min(max_rows, HARD_MAX_ROWS))
            logger.info(
                "create_hypercube: planning qInitialDataFetch height=%d "
                "(max_rows=%d, columns=%d, cells=%d/%d)",
                first_page_height, max_rows, n_cols,
                n_cols * first_page_height, HARD_MAX_CELLS,
            )

            # ── Apply the resolved sort ───────────────────────────────────
            # qInterColumnSortOrder decides WHICH column drives the row
            # order; the column's own criteria decide the DIRECTION. Both
            # halves are required: a measure whose qSortBy is left at the
            # Engine default ("ascending alphabetic") produces a nonsense
            # order even when it leads qInterColumnSortOrder.
            inter_column_sort_order = list(range(n_cols))
            if sort_column_index is not None and sort_direction is not None:
                inter_column_sort_order = (
                    [sort_column_index]
                    + [i for i in range(n_cols) if i != sort_column_index]
                )
                if sort_column_index >= n_dims:
                    # Sorting by a measure: order by its computed numeric value.
                    converted_measures[sort_column_index - n_dims]["sort_by"] = {
                        "qSortByNumeric": sort_direction,
                    }
                else:
                    # Sorting by a dimension: set both numeric and ASCII in
                    # the same direction. Qlik applies numeric ordering to
                    # numeric fields and alphabetical ordering to text
                    # fields, so this single pair covers both without the
                    # caller having to know the field's type.
                    converted_dimensions[sort_column_index]["sort_by"] = {
                        "qSortByNumeric": sort_direction,
                        "qSortByAscii": sort_direction,
                        "qSortByExpression": 0,
                        "qExpression": "",
                    }

            # Create correct hypercube structure
            hypercube_def = {
                "qDimensions": [
                    {
                        "qDef": {
                            "qFieldDefs": [dim["field"]],
                            "qSortCriterias": [
                                {
                                    "qSortByState": 0,
                                    "qSortByFrequency": 0,
                                    "qSortByNumeric": dim["sort_by"].get("qSortByNumeric", 0),
                                    "qSortByAscii": dim["sort_by"].get("qSortByAscii", 1),
                                    "qSortByLoadOrder": 0,
                                    "qSortByExpression": dim["sort_by"].get("qSortByExpression", 0),
                                    "qExpression": self._as_value_expr(
                                        dim["sort_by"].get("qExpression", "")
                                    ),
                                }
                            ],
                        },
                        "qNullSuppression": bool(exclude_null_dimensions),
                        "qIncludeElemValue": True,
                    }
                    for dim in converted_dimensions
                ],
                "qMeasures": [
                    {
                        "qDef": {"qDef": measure["expression"], "qLabel": measure.get("label", f"Measure_{i}")},
                        "qSortBy": measure["sort_by"],
                    }
                    for i, measure in enumerate(converted_measures)
                ],
                "qInitialDataFetch": [
                    {
                        "qTop": 0,
                        "qLeft": 0,
                        "qHeight": first_page_height,
                        "qWidth": n_cols,
                    }
                ],
                "qSuppressZero": bool(suppress_zero),
                # Always off. Measured on Qlik 31.62: qSuppressMissing drops
                # exactly one row — the NULL-dimension group (50002 rows to
                # 50001 on a field with 50k values plus NULLs) — and leaves
                # rows with a NULL *measure* alone. That is precisely what
                # qNullSuppression on each dimension already does, under the
                # caller's control via exclude_null_dimensions. Setting both
                # only meant the cube-wide flag could override an explicit
                # request to keep the NULL group.
                "qSuppressMissing": False,
                "qMode": "S",
                "qInterColumnSortOrder": inter_column_sort_order,
            }

            # The qId must be unique per call, not per request shape. Reusing
            # an id that was destroyed moments ago in the same Engine session
            # can hand back a stale cached calculation instead of evaluating
            # this call's own qHyperCubeDef.
            obj_def = {
                "qInfo": {
                    "qId": (f"hypercube-{len(converted_dimensions)}d"
                            f"-{len(converted_measures)}m-{uuid.uuid4().hex[:12]}"),
                    "qType": "HyperCube",
                },
                "qHyperCubeDef": hypercube_def,
            }

            step = "CreateSessionObject"
            logger.info(
                "create_hypercube: %s (dims=%d, measures=%d, max_rows=%d, op_timeout=%.1fs)",
                step, len(converted_dimensions), len(converted_measures),
                max_rows, self.ws_operation_timeout,
            )
            t_step = time.monotonic()
            result = self.send_request(
                "CreateSessionObject", [obj_def], handle=app_handle,
                timeout=self.ws_operation_timeout,
            )
            timings["create_session_object_seconds"] = round(time.monotonic() - t_step, 3)
            logger.info("create_hypercube: %s done in %.2fs",
                        step, timings["create_session_object_seconds"])

            if "qReturn" not in result or "qHandle" not in result["qReturn"]:
                return {
                    "error": "Failed to create hypercube session object",
                    "step": step,
                    "response": result,
                }

            cube_handle = result["qReturn"]["qHandle"]
            # From here on the session object exists on the server and MUST be
            # released on every path. This connection is deliberately
            # long-lived, so a leak on an error path pins that result set in
            # Engine memory for the rest of the session.
            created_object_id = obj_def["qInfo"]["qId"]

            # Get layout with data
            step = "GetLayout"
            logger.info("create_hypercube: %s (cube_handle=%d)", step, cube_handle)
            t_step = time.monotonic()
            layout = self.send_request("GetLayout", [], handle=cube_handle,
                                       timeout=self.ws_operation_timeout)
            timings["get_layout_seconds"] = round(time.monotonic() - t_step, 3)
            logger.info("create_hypercube: %s done in %.2fs",
                        step, timings["get_layout_seconds"])

            if "qLayout" not in layout or "qHyperCube" not in layout["qLayout"]:
                return {
                    "error": "No hypercube in layout",
                    "step": step,
                    "layout": layout,
                }

            hypercube = layout["qLayout"]["qHyperCube"]
            total_rows_on_server = hypercube.get("qSize", {}).get("qcy", 0)
            total_cols = hypercube.get("qSize", {}).get("qcx", n_cols)

            # Count how many rows we actually got back.
            initial_pages = hypercube.get("qDataPages", []) or []
            rows_fetched = sum(
                len(p.get("qMatrix", [])) for p in initial_pages
            )

            # Engine is not obliged to hand back the whole qInitialDataFetch
            # in one go — it trims a page at its own cell budget. Anything
            # missing is fetched with GetHyperCubeData before the reply is
            # assembled, otherwise a request for 4000 rows quietly returns
            # fewer and the caller has no way to tell a short page from a
            # short result.
            wanted_rows = min(max_rows, total_rows_on_server)
            if rows_fetched < wanted_rows:
                extra_pages, rows_fetched = self._read_remaining_pages(
                    cube_handle, n_cols, rows_fetched, wanted_rows, timings)
                initial_pages = initial_pages + extra_pages

            # Warn the caller if the server has MORE data than we returned.
            truncation_warning: Optional[str] = None
            page_error = timings.get("page_fetch_error")
            if page_error and rows_fetched < wanted_rows:
                # An incomplete read is not the same thing as a deliberate
                # top-N, and must not be dressed up as one: the rows here
                # are a prefix of the answer, not the answer.
                truncation_warning = (
                    f"INCOMPLETE: {rows_fetched} of the {wanted_rows} requested "
                    f"rows were read before the Engine refused to return more "
                    f"({page_error}). These rows are correct but partial — "
                    f"re-run with a smaller limit, or narrow the query with "
                    f"set analysis."
                )
            elif total_rows_on_server > rows_fetched:
                if sort_column_index is not None:
                    # Already a ranked query: the truncation is intended,
                    # the caller asked for the top/bottom N of a bigger set.
                    truncation_warning = (
                        f"Showing the {rows_fetched} "
                        f"{'highest' if sort_direction == -1 else 'lowest'} "
                        f"rows by '{column_names[sort_column_index]}' out of "
                        f"{total_rows_on_server} total rows on the server. "
                        f"This is expected for a ranked query — raise `limit` "
                        f"only if you genuinely need more of the ranking."
                    )
                else:
                    truncation_warning = (
                        f"TRUNCATED: server has {total_rows_on_server} rows, "
                        f"returned only {rows_fetched} (limit={max_rows}, "
                        f"HARD_LIMIT={HARD_MAX_ROWS}), and NO sort was "
                        f"requested — so these are arbitrary rows, not the "
                        f"most important ones.\n"
                        f"  - to rank: pass sort_by=\"<measure label>\" with "
                        f"sort_order=\"desc\" (top-N) or \"asc\" (bottom-N)\n"
                        f"  - to narrow: add set-analysis filters inside the "
                        f"measures, e.g. {{<[<DimPeriod>]={{<val>}}>}}\n"
                        f"  - to split: run one focused query per category "
                        f"instead of one giant dump"
                    )

            timings["total_seconds"] = round(time.monotonic() - t0, 3)

            response: Dict[str, Any] = {
                "columns": column_names,
                "rows": self._matrix_to_rows(initial_pages, column_names),
                "total_rows": total_rows_on_server,
                "returned_rows": rows_fetched,
                "total_columns": total_cols,
                "sorted_by": (
                    column_names[sort_column_index]
                    if sort_column_index is not None else None
                ),
                "sort_order": (
                    ("desc" if sort_direction == -1 else "asc")
                    if sort_column_index is not None else None
                ),
                # Same rule as the rows: a cell with no qNum at all is text,
                # not a null. Checking only against the "NaN" sentinel put a
                # JSON null in the totals wherever Engine omitted the key.
                "grand_total": [
                    cell.get("qText")
                    if cell.get("qNum") in (None, "NaN") else cell.get("qNum")
                    for cell in hypercube.get("qGrandTotalRow", []) or []
                ],
                "hard_max_rows": HARD_MAX_ROWS,
                "truncation_warning": truncation_warning,
                "timings": timings,
                "dimensions": converted_dimensions,
                "measures": converted_measures,
            }
            if include_raw_layout:
                # Opt-in: the full qHyperCube (qDimensionInfo, qMeasureInfo,
                # qDataPages with qElemNumber/qState per cell). Costs several
                # times more tokens than `rows`, so it is off by default.
                response["hypercube_handle"] = cube_handle
                response["hypercube_data"] = hypercube
            return response

        except Exception as e:
            elapsed = time.monotonic() - t0
            err_type = type(e).__name__
            err_msg = str(e) or repr(e)
            # Classify the error so the caller knows WHAT actually failed
            import socket as _socket
            if isinstance(e, (_socket.timeout, TimeoutError)):
                category = "socket_timeout"
                hint = (
                    f"Qlik Engine did not answer within ~{self.ws_operation_timeout:.0f}s "
                    f"on step '{step}'. Make the query cheaper before making the "
                    f"timeout longer:\n"
                    f"  1. Narrow every measure with set analysis, e.g. "
                    f"Sum({{<[Year]={{2026}}>}}Amount) — this is the single "
                    f"biggest win on large fact tables.\n"
                    f"  2. Drop high-cardinality dimensions; rank with "
                    f"sort_by + a small limit instead of returning every group.\n"
                    f"  3. Never put an expression in a dimension `field` — it "
                    f"is evaluated per row of the fact table.\n"
                    f"  4. Only then raise QLIK_WS_TIMEOUT (currently "
                    f"{self.ws_operation_timeout:.0f}s)."
                )
                # Timeout on recv leaves the socket in an inconsistent state —
                # invalidate cache so the next call opens a fresh connection.
                self._invalidate_cache()
                try:
                    if self.ws:
                        self.ws.close()
                except Exception:
                    pass
                self.ws = None
            elif err_msg.startswith("Engine API error:"):
                category = "engine_api_error"
                hint = "Qlik Engine rejected the request (bad expression, missing field, etc)."
            elif isinstance(e, ConnectionError):
                category = "connection_error"
                hint = "WebSocket connection problem (server unreachable, SSL, auth)."
            else:
                category = "unexpected"
                hint = "Unexpected error — see traceback."

            tb = _tb.format_exc()
            logger.error(
                "create_hypercube FAILED on step '%s' after %.2fs: %s: %s\n%s",
                step, elapsed, err_type, err_msg, tb,
            )
            return {
                "error": err_msg,
                "error_type": err_type,
                "error_category": category,
                "failed_step": step,
                "elapsed_seconds": round(elapsed, 2),
                "ws_operation_timeout": self.ws_operation_timeout,
                "timings": timings,
                "hint": hint,
                "traceback": tb,
                "details": "Error in create_hypercube method",
            }

        finally:
            # Runs on every path once the object exists: success, early
            # return for a malformed layout, and any exception in between.
            # Skipped when the socket was already killed (timeout handling
            # above), since there is nothing left to talk to.
            if created_object_id and self.ws is not None:
                try:
                    self.send_request(
                        "DestroySessionObject", [created_object_id],
                        handle=app_handle, timeout=self.ws_operation_timeout,
                    )
                except Exception as cleanup_exc:
                    # Never turn a completed query into a failure because
                    # cleanup did not go through.
                    logger.warning(
                        "create_hypercube: DestroySessionObject(%s) failed: %s",
                        created_object_id, cleanup_exc,
                    )

