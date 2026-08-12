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
import difflib
import logging
import time
import uuid

logger = logging.getLogger(__name__)

# The opening of a set modifier. Its presence is the one thing this module
# reads out of an expression itself; everything about the expression —
# syntax, which names exist, which fields a modifier filters on — is
# answered by Engine.
_SET_MODIFIER_OPENER = "{<"

# Where a described filter is written into a hand-written expression.
_FILTER_MARKER = "{filter}"


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

    def _known_field_names(self, app_handle: int) -> List[str]:
        """Every field in the data model, for suggesting a near miss.

        Only called on the error path — it walks the model, which the
        happy path has no reason to pay for.
        """
        try:
            # Served from the per-app cache when it is warm: this runs on
            # the error path, where a second model read would add latency
            # to a reply the caller is already unhappy about.
            read_model = getattr(self, "cached_fields", None)
            # getattr: instances that skip __init__ have no cached app id,
            # and an AttributeError here would silently cost the caller its
            # "did you mean" suggestions.
            app_id = getattr(self, "_cached_app_id", "") or ""
            model = (read_model(app_handle, app_id, None)
                     if read_model else self.get_fields(app_handle))
        except Exception as exc:
            logger.debug("Could not list fields for a suggestion: %s", exc)
            return []
        # get_fields names the key `field_name`; the tool layer renames it to
        # `name` on the way out. Accept both so this keeps working whichever
        # one it is handed. Deduplicated: a join key appears once per table it
        # belongs to, and suggesting it three times helps nobody.
        names = [
            f.get("field_name") or f.get("name") or ""
            for f in (model.get("fields") or [])
            if f.get("field_name") or f.get("name")
        ]
        return list(dict.fromkeys(names))

    def _validate_cube_inputs(
        self, app_handle: int,
        dimensions: List[Dict[str, Any]],
        measures: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Catch the mistakes Qlik answers with a number instead of an error.

        Every judgement here comes from Engine, in one pipelined batch that
        costs about 4ms against 75ms for the smallest hypercube:

        `ExpandExpression` resolves `$(...)`, so the checks see the text
        that will actually run rather than the variable reference.

        `CheckExpression` reports syntax and, in `qBadFieldNames`, names the
        data model does not have. Measured: it covers a bare
        dimension field, a calculated dimension and an aggregation, and it
        stops at the set modifier.

        `GetFieldsFromExpression` covers what `CheckExpression` does not: the
        fields a set modifier filters on. A modifier whose field Engine does
        not recognise is dropped by Qlik, and the measure then returns the
        unfiltered total — larger than the truth, with nothing to mark it as
        wrong.

        A dimension on a field that does not exist is the same class of
        failure: Qlik evaluates the name as an expression and the cube
        collapses to one row holding the grand total. Measured: a
        cube on `no_such_field` came back as a single row worth
        49,989,556,885.52 — the total over all ten regions.
        """
        warnings: List[str] = []

        # Every expression that goes to Qlik, in one list: bare dimension
        # fields, calculated dimensions, measure expressions. Engine reads
        # all three the same way, so they are checked the same way.
        dimension_texts = [
            str(dim.get("field") or "").strip()
            for dim in dimensions if str(dim.get("field") or "").strip()
        ]
        measure_texts = [
            str(measure.get("expression") or "").strip()
            for measure in measures if str(measure.get("expression") or "").strip()
        ]
        every_text = dimension_texts + measure_texts
        if not every_text:
            return {"warnings": warnings}

        expanded = self.expand_expressions(app_handle, every_text)
        faults = self.check_expressions(
            app_handle, [expanded.get(text, text) for text in every_text])
        # Faults come back keyed by the expanded text; map them back to what
        # the caller wrote, which is what it has to fix.
        by_original = {
            text: faults[expanded.get(text, text)]
            for text in every_text if expanded.get(text, text) in faults
        }

        broken = {
            text: fault["error"] for text, fault in by_original.items()
            if fault.get("error")
        }
        if broken:
            first_expression, first_error = next(iter(broken.items()))
            return {
                "error": f"Qlik cannot parse {first_expression!r}: {first_error}",
                "error_category": "invalid_expression",
                "failed_step": "validate",
                "invalid_expressions": broken,
                "hint": (
                    "The message comes from Qlik's own parser. Qlik is not "
                    "SQL: name a measure with the `label` argument rather "
                    "than `AS`, filter with set analysis "
                    "(Sum({<Year={2026}>} Amount)) rather than WHERE, and "
                    "wrap an inner aggregation in Aggr() or TOTAL."
                ),
            }

        # A set modifier Engine did not read is a filter that will not
        # apply. Engine lists the modifier fields it recognised; an
        # expression that carries a modifier and yields none of them is
        # filtering on something this app does not have.
        with_modifier = [
            text for text in measure_texts
            if _SET_MODIFIER_OPENER in expanded.get(text, text)
        ]
        if with_modifier:
            recognised = self.fields_in_expressions(
                app_handle, [expanded.get(text, text) for text in with_modifier])
            unread = [
                text for text in with_modifier
                if expanded.get(text, text) in recognised
                and not recognised[expanded.get(text, text)]
            ]
            if unread:
                return {
                    "error": (
                        "Set analysis in " + ", ".join(repr(t) for t in unread)
                        + " filters on a field this app does not have."
                    ),
                    "error_category": "field_not_found",
                    "failed_step": "validate",
                    "invalid_expressions": {t: "no filter field recognised"
                                            for t in unread},
                    "next_actions": [
                        "call get_app_details(app_id) and copy the field name "
                        "exactly — field names are case-sensitive",
                        "or state the filter as `filters` and let the server "
                        "write the set analysis",
                    ],
                    "hint": (
                        "Qlik does not reject a set modifier on an unknown "
                        "field, it drops the condition. The measure then "
                        "returns the unfiltered total — a number larger than "
                        "the truth, with nothing to mark it as wrong."
                    ),
                }

        unknown_dimension_fields = list(dict.fromkeys(
            name for text in dimension_texts
            for name in by_original.get(text, {}).get("bad_fields", [])
        ))
        unknown_measure_fields = [
            name for name in dict.fromkeys(
                name for text in measure_texts
                for name in by_original.get(text, {}).get("bad_fields", [])
            ) if name not in unknown_dimension_fields
        ]

        if unknown_dimension_fields:
            known = self._known_field_names(app_handle)
            # Matching case-insensitively and answering with the real name:
            # field names are case-sensitive in Qlik, so the wrong case is
            # the most common way to miss — and `REGION_NAME` is exactly the
            # miss a case-sensitive comparison fails to explain.
            folded = {name.casefold(): name for name in known}
            suggestions = {}
            for name in unknown_dimension_fields:
                matches = difflib.get_close_matches(
                    name.casefold(), list(folded), n=3, cutoff=0.6)
                if matches:
                    suggestions[name] = [folded[m] for m in matches]
            # A list of candidates still leaves the caller to rebuild the
            # whole call. Hand back the corrected one instead.
            corrected = None
            if len(unknown_dimension_fields) == 1:
                only = unknown_dimension_fields[0]
                best = suggestions.get(only)
                if best:
                    corrected = [
                        (best[0] if d.get("field", "").strip("[]") == only
                         else d.get("field"))
                        for d in dimensions
                    ]
            return {
                "error": (
                    "Unknown field(s) in dimensions: "
                    + ", ".join(repr(f) for f in unknown_dimension_fields)
                ),
                "error_category": "field_not_found",
                "failed_step": "validate",
                "unknown_fields": unknown_dimension_fields,
                "did_you_mean": {k: v for k, v in suggestions.items() if v},
                "next_actions": ([
                    f"retry with dimensions={corrected!r}",
                ] if corrected else [
                    "call get_app_details(app_id) and read `fields[].name`",
                    "field names are case-sensitive; copy them exactly",
                ]) + ["do not guess another spelling without checking"],
                "hint": (
                    "Qlik does not refuse an unknown dimension — it evaluates "
                    "the name as an expression, and the cube collapses to one "
                    "row holding the grand total, which reads as a real "
                    "answer. Check the name with get_app_details (field names "
                    "are case-sensitive)."
                ),
            }

        if unknown_measure_fields:
            # Engine's verdict, not a guess at one: `qBadFieldNames` marks a
            # name the data model does not have, and Qlik scores such a name
            # as 0. The measure would come back as a column of zeros that
            # reads as a real answer, so the query stops here.
            known = self._known_field_names(app_handle)
            folded = {name.casefold(): name for name in known}
            suggestions = {}
            for name in unknown_measure_fields:
                matches = difflib.get_close_matches(
                    name.casefold(), list(folded), n=3, cutoff=0.6)
                if matches:
                    suggestions[name] = [folded[m] for m in matches]
            return {
                "error": (
                    "Unknown field(s) in measures: "
                    + ", ".join(repr(f) for f in unknown_measure_fields)
                ),
                "error_category": "field_not_found",
                "failed_step": "validate",
                "unknown_fields": unknown_measure_fields,
                "did_you_mean": {k: v for k, v in suggestions.items() if v},
                "next_actions": [
                    "call get_app_details(app_id) and read `fields[].name`",
                    "field names are case-sensitive; copy them exactly",
                ],
                "hint": (
                    "Qlik scores a name it does not have as 0, so this "
                    "measure would return a column of zeros rather than fail."
                ),
            }

        return {"warnings": warnings}

    @staticmethod
    def _measure_columns_are_empty(rows: List[List[Any]], n_dims: int,
                                   column_names: List[str]) -> List[str]:
        """Name the measures that came back entirely 0 or '-'.

        The signature of a mistake Qlik will not report: a set-analysis
        filter on a value that does not exist, a misspelled field inside an
        aggregation, SQL syntax. All of them return a full, well-formed
        result whose numbers are meaningless.
        """
        if not rows:
            return []
        empty = []
        for col in range(n_dims, len(column_names)):
            values = [row[col] for row in rows if col < len(row)]
            if not values:
                continue
            if all(v in (0, 0.0, "-", "", None) for v in values):
                empty.append(column_names[col])
        return empty

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

    # Qlik's number-format types whose value is a point in time. A cell of
    # one of these carries both a serial number and the text Qlik displays
    # for it, and the two are different writings of the same thing.
    _TEMPORAL_FORMATS = ("D", "T", "TS", "IV")

    @classmethod
    def _temporal_columns(cls, hypercube: Dict[str, Any]) -> set:
        """Indexes of the columns holding a date, time or timestamp.

        Engine says so itself, in each column's `qNumFormat.qType`. Column
        order is fixed by the API: dimensions first, then measures.
        """
        temporal = set()
        columns = ((hypercube.get("qDimensionInfo") or [])
                   + (hypercube.get("qMeasureInfo") or []))
        for index, info in enumerate(columns):
            fmt = (info or {}).get("qNumFormat") or {}
            if fmt.get("qType") in cls._TEMPORAL_FORMATS:
                temporal.add(index)
        return temporal

    @staticmethod
    def _matrix_to_rows(
        data_pages: List[Dict[str, Any]],
        column_names: List[str],
        temporal_columns: set = None,
    ) -> List[List[Any]]:
        """
        Flatten Engine's qMatrix into plain rows of values.

        Each Engine cell is `{qText, qNum, qElemNumber, qState}`; consumers
        of this tool only ever need the value, and `qNum == "NaN"` is
        Engine's way of saying "this cell is text or empty". Returning
        numbers as numbers means an LLM can compare and sum them without
        first parsing locale-formatted strings like "95 552 568 044,926".

        A date is the exception. Its number is a serial day count — `45292`
        for the first of January 2024 — while every other reply about the
        same field says `01.01.2024`: the sample values in
        `get_app_details`, the values from `get_app_field`, the bounds from
        `engine_get_field_range`. One value gets one writing everywhere, so
        a temporal column returns the text Qlik displays.
        """
        temporal = temporal_columns or set()
        rows: List[List[Any]] = []
        for page in data_pages or []:
            for matrix_row in page.get("qMatrix", []) or []:
                row: List[Any] = []
                for index, cell in enumerate(matrix_row):
                    num = cell.get("qNum")
                    text = cell.get("qText")
                    if index in temporal and text:
                        row.append(text)
                    else:
                        row.append(text if num == "NaN" or num is None else num)
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

    @staticmethod
    def _hypercube_def(
        converted_dimensions: List[Dict[str, Any]],
        converted_measures: List[Dict[str, Any]],
        page_offset: int,
        page_height: int,
        inter_column_sort_order: List[int],
        suppress_zero: bool,
        exclude_null_dimensions: bool,
    ) -> Dict[str, Any]:
        """The `qHyperCubeDef` Engine is asked to create.

        One place builds it, so a single query and a batch of them cannot
        drift apart in how they sort, page or suppress rows.
        """
        n_cols = len(converted_dimensions) + len(converted_measures)
        return {
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
                                "qExpression": EngineHypercubeMixin._as_value_expr(
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
                    "qDef": {"qDef": measure["expression"],
                             "qLabel": measure.get("label", f"Measure_{i}")},
                    "qSortBy": measure["sort_by"],
                }
                for i, measure in enumerate(converted_measures)
            ],
            "qInitialDataFetch": [
                {
                    # Paging starts where the caller asked. Without this
                    # every page was the first page, so a result wider
                    # than the row cap had no second page at all.
                    "qTop": page_offset,
                    "qLeft": 0,
                    "qHeight": page_height,
                    "qWidth": n_cols,
                }
            ],
            "qSuppressZero": bool(suppress_zero),
            # Always off. Measured: qSuppressMissing drops
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

    def _read_remaining_pages(self, cube_handle: int, n_cols: int,
                              rows_so_far: int, wanted_rows: int,
                              timings: Dict[str, Any],
                              start_at: int = 0) -> "tuple[List[Dict[str, Any]], int]":
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
                    ["/qHyperCubeDef", [{"qTop": start_at + rows_so_far, "qLeft": 0,
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
        offset: int = 0,
        filters: Optional[List[Dict[str, Any]]] = None,
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
            for position, dim in enumerate(dimensions):
                if isinstance(dim, str):
                    dim = {"field": dim}
                else:
                    dim = dict(dim)
                    # A measure is `{"expression": ...}`, so a model writing a
                    # calculated dimension reaches for the same key — and used
                    # to get `KeyError: 'field'`, an opaque crash instead of an
                    # answer. Accept the spelling; Qlik takes both a field name
                    # and an `=expression` in the same slot anyway.
                    if "field" not in dim:
                        for alias in ("expression", "name", "definition", "qDef"):
                            if dim.get(alias):
                                dim["field"] = dim.pop(alias)
                                break
                if not str(dim.get("field") or "").strip():
                    return {
                        "error": (
                            f"dimensions[{position}] has no field name: "
                            f"{dim!r}"
                        ),
                        "error_category": "invalid_argument",
                        "failed_step": "plan",
                        "hint": (
                            "A dimension is {\"field\": \"<FieldName>\"} — a "
                            "real field, or an expression starting with '=' "
                            "such as {\"field\": \"=Year(OrderDate)\"}. A bare "
                            "string is accepted too. Aggregations belong in "
                            "`measures`, not here."
                        ),
                    }
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

            # A described filter is written as set analysis by the server
            # and placed where the expression marks it. A set modifier
            # belongs inside the aggregation it narrows, and only the
            # author of the expression knows which aggregation that is —
            # hence the marker rather than a guess.
            filters_applied: List[Dict[str, Any]] = []
            if filters:
                step = "filters"
                built = self.build_filters(
                    app_handle, getattr(self, "_cached_app_id", "") or "",
                    filters)
                if built.get("error"):
                    built.setdefault("failed_step", "filters")
                    return built
                marker_used = False
                for measure in converted_measures:
                    expression = measure.get("expression") or ""
                    if _FILTER_MARKER in expression:
                        measure["expression"] = expression.replace(
                            _FILTER_MARKER, built["modifier"])
                        marker_used = True
                if not marker_used:
                    return {
                        "error": (
                            "`filters` was given but no measure marks where "
                            "the filter goes."
                        ),
                        "error_category": "invalid_argument",
                        "failed_step": "filters",
                        "next_actions": [
                            "write the marker inside the aggregation: "
                            "\"Sum({filter} Amount)\"",
                            "or call engine_query, which writes the whole "
                            "expression for you",
                        ],
                        "hint": (
                            "A set modifier narrows the aggregation it sits "
                            "in. Marking the place keeps that choice with "
                            "whoever wrote the expression."
                        ),
                    }
                filters_applied = built.get("applied", [])

            # Hard limits enforced in our layer — NOT in Qlik Engine.
            # The intent is to force the LLM to design narrow, focused
            # hypercubes (with set analysis, smart dimensions, and top-N
            # patterns) rather than bulk-dump huge tables and post-process
            # client-side. If the LLM needs more data, it should issue
            # multiple well-scoped queries, not one giant one.
            HARD_MAX_ROWS = self.HARD_MAX_ROWS
            HARD_MAX_CELLS = self.HARD_MAX_CELLS
            page_offset = max(0, int(offset or 0))
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
                    "next_actions": [
                        f"retry with limit={min(HARD_MAX_ROWS, 50)} and "
                        f"sort_by set to the measure you care about",
                        "or narrow every measure with set analysis, "
                        "e.g. Sum({<Year={2026}>} Amount)",
                        "do not retry the same limit — it is refused before "
                        "Qlik is contacted",
                    ],
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
                    "next_actions": [
                        f"retry with limit={suggested_rows} at the current "
                        f"{n_cols} columns",
                        "or drop a dimension/measure and keep the limit",
                        "do not retry the same combination",
                    ],
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

            hypercube_def = self._hypercube_def(
                converted_dimensions, converted_measures, page_offset,
                first_page_height, inter_column_sort_order, suppress_zero,
                exclude_null_dimensions,
            )

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

            # Before spending a session object on it: does the query name
            # things that exist? Qlik answers a typo with a number, so this
            # is the difference between an error and a wrong answer.
            step = "validate"
            t_step = time.monotonic()
            validation = self._validate_cube_inputs(
                app_handle, converted_dimensions, converted_measures)
            timings["validate_seconds"] = round(time.monotonic() - t_step, 3)
            if validation.get("error"):
                return validation
            input_warnings: List[str] = validation.get("warnings", [])

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
            wanted_rows = min(max_rows, max(0, total_rows_on_server - page_offset))
            if rows_fetched < wanted_rows:
                extra_pages, rows_fetched = self._read_remaining_pages(
                    cube_handle, n_cols, rows_fetched, wanted_rows, timings,
                    start_at=page_offset)
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

            temporal_columns = self._temporal_columns(hypercube)
            rows = self._matrix_to_rows(initial_pages, column_names,
                                        temporal_columns)
            warnings = list(input_warnings)
            if not rows and converted_measures:
                # No rows at all is the *strongest* version of the same
                # signal, and `suppress_zero` turns a table of zeros into
                # exactly this — so the check below, which needs rows to
                # look at, would go quiet precisely when the result is
                # emptiest.
                warnings.append(
                    "The query returned no rows. With suppress_zero=True that "
                    "is what a measure evaluating to 0 everywhere looks like; "
                    "otherwise the dimensions have no values under the current "
                    "filters. Re-run without suppress_zero to tell the two "
                    "apart."
                    if suppress_zero else
                    "The query returned no rows: no dimension value satisfied "
                    "the query. Check the field names and any set-analysis "
                    "filter values with get_app_field."
                )
            for empty_column in self._measure_columns_are_empty(rows, n_dims, column_names):
                warnings.append(
                    f"Every value of {empty_column!r} came back 0 or '-'. That "
                    f"is what Qlik returns for a set-analysis filter matching "
                    f"no value, a misspelled field inside the aggregation, or "
                    f"an expression it could not evaluate — it does not report "
                    f"any of them as an error. Check the filter values with "
                    f"get_app_field before trusting this as a real zero."
                )

            response: Dict[str, Any] = {
                "columns": column_names,
                "rows": rows,
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
                # Same rules as the rows: a cell with no qNum at all is text
                # rather than a null, and a date reads as the text Qlik
                # displays. The grand-total row holds measures only, so its
                # column indexes start after the dimensions.
                "grand_total": [
                    cell.get("qText")
                    if (cell.get("qNum") in (None, "NaN")
                        or (n_dims + index) in temporal_columns)
                    else cell.get("qNum")
                    for index, cell in enumerate(
                        hypercube.get("qGrandTotalRow", []) or [])
                ],
                "hard_max_rows": HARD_MAX_ROWS,
                "truncation_warning": truncation_warning,
                "warnings": warnings,
                "offset": page_offset,
                # There IS a next page and here is how to ask for it. A
                # refusal used to be the only answer to "more than the cap",
                # which left the caller reformulating a query that was fine.
                "has_more": (page_offset + rows_fetched) < total_rows_on_server,
                "next_offset": (page_offset + rows_fetched
                                if (page_offset + rows_fetched) < total_rows_on_server
                                else None),
                "timings": timings,
                "dimensions": converted_dimensions,
                "measures": converted_measures,
            }
            if filters_applied:
                # What each described filter resolved to, including the
                # period actually selected and how many values of the field
                # fall inside it.
                response["filters_applied"] = filters_applied
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

