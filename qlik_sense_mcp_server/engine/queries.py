"""Typed queries: say what to group and what to measure, not how.

A caller states fields, aggregations and filters. This module writes the
Qlik expressions, proves the filters select something, runs every query in
the batch over the same three round-trips, and returns the control values
that show whether the filter did what was asked.

Set analysis written by hand stays available through
`engine_create_hypercube`. It is the harder path: measured across ten
models, every wrong answer with a known-correct value came from a
hand-written filter on a date, and Qlik answers such a filter with a
plausible number rather than an error.
"""

import logging
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Aggregations a metric may ask for, and the Qlik function behind each.
# `count` counts values of the field, `count_distinct` counts different
# ones — the pair a caller reaches for as "how many orders" against "how
# many customers".
AGGREGATIONS = {
    "sum": "Sum({modifier} {field})",
    "count": "Count({modifier} {field})",
    "count_distinct": "Count({modifier} DISTINCT {field})",
    "avg": "Avg({modifier} {field})",
    "min": "Min({modifier} {field})",
    "max": "Max({modifier} {field})",
    "median": "Median({modifier} {field})",
    "stdev": "Stdev({modifier} {field})",
}

# Rows returned per query unless the caller says otherwise. Smaller than
# the hypercube default: a typed query is normally a ranking or a total,
# and a long tail of rows costs the reader more than it tells them.
DEFAULT_QUERY_LIMIT = 100


class EngineQueriesMixin:
    """Run typed queries, several at a time, over one socket."""

    def _plan_query(self, app_handle: int, app_id: str,
                    query: Dict[str, Any], position: int) -> Dict[str, Any]:
        """Turn one typed query into dimensions, measures and a modifier."""
        query_id = str(query.get("id") or f"q{position + 1}")

        group_by = query.get("group_by")
        if group_by is None:
            group_by = query.get("dimensions") or []
        if isinstance(group_by, str):
            group_by = [group_by]
        dimensions = []
        for item in group_by:
            field = item.get("field") if isinstance(item, dict) else item
            field = str(field or "").strip()
            if not field:
                return {"id": query_id, "error": (
                    f"Query {query_id!r} lists a grouping field with no name."),
                    "error_category": "invalid_argument"}
            dimensions.append({"field": field})

        filters = query.get("filters") or []
        built = self.build_filters(app_handle, app_id, filters)
        if built.get("error"):
            reply = dict(built)
            reply["id"] = query_id
            return reply
        modifier = built.get("modifier", "")

        measures, error = self._build_measures(query, modifier, query_id)
        if error:
            return error
        if not measures:
            return {"id": query_id, "error": (
                f"Query {query_id!r} asks for no measure."),
                "error_category": "invalid_argument",
                "hint": ('Add `metrics`, e.g. '
                         '[{"field": "Amount", "agg": "sum"}]. Aggregations: '
                         + ", ".join(sorted(AGGREGATIONS)) + ".")}

        return {
            "id": query_id,
            "dimensions": dimensions,
            "measures": measures,
            "modifier": modifier,
            "filters_applied": built.get("applied", []),
            "limit": query.get("limit", DEFAULT_QUERY_LIMIT),
            "offset": query.get("offset", 0),
            "sort_by": query.get("sort_by"),
            "sort_order": query.get("sort_order", "desc"),
        }

    @staticmethod
    def _build_measures(query: Dict[str, Any], modifier: str,
                        query_id: str) -> "tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]":
        """Write one Qlik expression per metric.

        A metric names a field and an aggregation; the filter is folded
        into every one of them, because a set modifier applies to the
        aggregation it sits in and to nothing else.
        """
        measures: List[Dict[str, Any]] = []
        prefix = (modifier + " ") if modifier else ""

        for metric in query.get("metrics") or []:
            if isinstance(metric, str):
                metric = {"field": metric, "agg": "sum"}
            if not isinstance(metric, dict):
                return [], {"id": query_id, "error": (
                    f"A metric must be an object, got {metric!r}."),
                    "error_category": "invalid_argument"}
            aggregation = str(metric.get("agg") or "sum").strip().lower()
            if aggregation not in AGGREGATIONS:
                return [], {"id": query_id, "error": (
                    f"Aggregation {aggregation!r} is not one this server "
                    f"writes."),
                    "error_category": "invalid_argument",
                    "allowed_values": sorted(AGGREGATIONS)}
            field = str(metric.get("field") or "").strip().strip("[]")
            if not field:
                return [], {"id": query_id, "error": (
                    f"Metric {metric!r} names no field."),
                    "error_category": "invalid_argument"}
            expression = AGGREGATIONS[aggregation].format(
                modifier=prefix.rstrip() if prefix else "",
                field=f"[{field}]").replace("(  ", "(").replace("( ", "(")
            measures.append({
                "expression": expression,
                "label": str(metric.get("label") or f"{aggregation}_{field}"),
            })

        # A caller that needs something this vocabulary cannot say writes
        # the expression itself; the filter is still applied for it.
        for measure in query.get("measures") or []:
            if isinstance(measure, str):
                measure = {"expression": measure}
            expression = str(measure.get("expression") or "").strip()
            if not expression:
                return [], {"id": query_id, "error": (
                    f"Measure {measure!r} carries no expression."),
                    "error_category": "invalid_argument"}
            measures.append({
                "expression": expression,
                "label": str(measure.get("label") or expression),
            })
        return measures, None

    def _control_probes(self, plan: Dict[str, Any]) -> List[Dict[str, str]]:
        """Expressions that show whether a period filter really applied.

        For each period filter: the earliest and latest value of that field
        inside the filtered set. A value outside the requested period means
        Qlik ignored the condition — the one failure that otherwise returns
        a plausible number and no sign of trouble.
        """
        probes = []
        for applied in plan.get("filters_applied", []):
            if "serial_from" not in applied:
                continue
            field = f"[{applied['field']}]"
            modifier = plan.get("modifier", "")
            inner = f"{modifier} {field}" if modifier else field
            probes.append({
                "field": applied["field"],
                "expected_from": applied["from"],
                "expected_to": applied["to"],
                "serial_from": applied["serial_from"],
                "serial_to_exclusive": applied["serial_to_exclusive"],
                "earliest": f"=Text(Min({inner}))",
                "latest": f"=Text(Max({inner}))",
                "earliest_number": f"=Num(Min({inner}))",
                "latest_number": f"=Num(Max({inner}))",
            })
        return probes

    @staticmethod
    def _read_controls(probe: Dict[str, str],
                       values: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Turn four evaluated probes into one statement about the period."""
        earliest, latest, earliest_num, latest_num = values
        check: Dict[str, Any] = {
            "field": probe["field"],
            "requested_from": probe["expected_from"],
            "requested_to": probe["expected_to"],
            "earliest_in_result": earliest.get("text"),
            "latest_in_result": latest.get("text"),
        }
        low = earliest_num.get("number")
        high = latest_num.get("number")
        if low is None or high is None:
            return check
        outside = (low < probe["serial_from"]
                   or high >= probe["serial_to_exclusive"])
        check["filter_applied"] = not outside
        if outside:
            check["note"] = (
                f"The result holds values of {probe['field']} outside "
                f"{probe['expected_from']}..{probe['expected_to']}, so the "
                f"period did not narrow it. Treat the numbers as unfiltered."
            )
        return check

    def run_queries(self, app_handle: int, app_id: str,
                    queries: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Run every query in the list, sharing three round-trips.

        Independent queries do not need to wait for each other: the Engine
        JSON API matches replies to requests by id, so all the session
        objects are created in one batch, all the layouts read in the next,
        and all the objects released in the third. A batch of five costs
        three round-trips rather than fifteen.

        A query that fails takes only itself down. Its entry carries the
        reason; the rest of the batch still answers.
        """
        started = time.monotonic()
        plans: List[Dict[str, Any]] = []
        for position, query in enumerate(queries):
            if not isinstance(query, dict):
                plans.append({"id": f"q{position + 1}", "error": (
                    f"A query must be an object, got {query!r}."),
                    "error_category": "invalid_argument"})
                continue
            try:
                plans.append(self._plan_query(app_handle, app_id, query, position))
            except Exception as exc:
                logger.exception("Planning query %d failed", position)
                plans.append({"id": str(query.get("id") or f"q{position + 1}"),
                              "error": str(exc), "error_category": "unexpected"})

        runnable = [plan for plan in plans if not plan.get("error")]

        # One validation batch for the whole request: every expression of
        # every query goes to Engine together.
        for plan in runnable:
            verdict = self._validate_cube_inputs(
                app_handle, plan["dimensions"], plan["measures"])
            if verdict.get("error"):
                plan.update(verdict)
        runnable = [plan for plan in plans if not plan.get("error")]

        prepared = []
        for plan in runnable:
            shaped = self._shape_cube(plan)
            if shaped.get("error"):
                plan.update(shaped)
                continue
            prepared.append((plan, shaped))
        if not prepared:
            return {"results": [self._query_reply(p, None, None) for p in plans],
                    "seconds": round(time.monotonic() - started, 3)}

        # Batch one: create every session object, and evaluate every
        # control probe alongside them.
        creates = [
            {"method": "CreateSessionObject", "params": [shaped["object"]],
             "handle": app_handle}
            for _, shaped in prepared
        ]
        probe_map: List[tuple] = []
        probe_requests = []
        for plan, _ in prepared:
            for probe in self._control_probes(plan):
                start = len(probe_requests)
                for key in ("earliest", "latest", "earliest_number",
                            "latest_number"):
                    probe_requests.append(
                        {"method": "EvaluateEx", "params": [probe[key]],
                         "handle": app_handle})
                probe_map.append((plan, probe, start))

        outcomes = self.send_requests_pipelined(
            creates + probe_requests, raise_on_error=False,
            timeout=self.ws_operation_timeout)
        create_outcomes = outcomes[:len(creates)]
        probe_outcomes = outcomes[len(creates):]

        for plan, probe, start in probe_map:
            values = []
            for outcome in probe_outcomes[start:start + 4]:
                if isinstance(outcome, Exception):
                    values.append({"text": None, "number": None})
                    continue
                value = (outcome or {}).get("qValue") or {}
                values.append({"text": value.get("qText"),
                               "number": value.get("qNumber")})
            plan.setdefault("period_check", []).append(
                self._read_controls(probe, values))

        handles = []
        for (plan, shaped), outcome in zip(prepared, create_outcomes):
            if isinstance(outcome, Exception):
                plan["error"] = str(outcome)
                plan["error_category"] = "engine_api_error"
                continue
            handle = ((outcome or {}).get("qReturn") or {}).get("qHandle")
            if handle is None:
                plan["error"] = "Engine created no object for this query"
                plan["error_category"] = "engine_api_error"
                continue
            handles.append((plan, shaped, handle))

        # Batch two: read every layout.
        layouts = self.send_requests_pipelined(
            [{"method": "GetLayout", "params": [], "handle": handle}
             for _, _, handle in handles],
            raise_on_error=False, timeout=self.ws_operation_timeout,
        ) if handles else []

        for (plan, shaped, handle), layout in zip(handles, layouts):
            if isinstance(layout, Exception):
                plan["error"] = str(layout)
                plan["error_category"] = "engine_api_error"
                continue
            cube = ((layout or {}).get("qLayout") or {}).get("qHyperCube")
            if not cube:
                plan["error"] = "Engine returned no hypercube for this query"
                plan["error_category"] = "engine_api_error"
                continue
            plan["cube"] = cube
            plan["shaped"] = shaped

        # Batch three: release every object. A leak here would pin the
        # result set in Engine memory for the rest of the session, which is
        # long-lived by design.
        if handles:
            try:
                self.send_requests_pipelined(
                    [{"method": "DestroySessionObject",
                      "params": [shaped["object"]["qInfo"]["qId"]],
                      "handle": app_handle}
                     for _, shaped, _ in handles],
                    raise_on_error=False, timeout=self.ws_operation_timeout,
                )
            except Exception as exc:
                logger.warning("Releasing batch session objects failed: %s", exc)

        results = [
            self._query_reply(plan, plan.get("cube"), plan.get("shaped"))
            for plan in plans
        ]
        return {
            "results": results,
            "queries_run": len([r for r in results if not r.get("error")]),
            "queries_failed": len([r for r in results if r.get("error")]),
            "seconds": round(time.monotonic() - started, 3),
        }

    def _shape_cube(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        """Resolve limits and sorting, then build the object definition."""
        dimensions = [dict(d, sort_by={"qSortByNumeric": 0, "qSortByAscii": 1,
                                       "qSortByExpression": 0, "qExpression": ""})
                      for d in plan["dimensions"]]
        measures = [dict(m, sort_by={"qSortByNumeric": -1})
                    for m in plan["measures"]]
        n_dims = len(dimensions)
        n_cols = n_dims + len(measures)
        column_names = self._column_names(dimensions, measures)

        limit = plan.get("limit")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            limit = DEFAULT_QUERY_LIMIT
        limit = min(limit, self.HARD_MAX_ROWS)
        if n_cols and n_cols * limit > self.HARD_MAX_CELLS:
            limit = max(1, self.HARD_MAX_CELLS // n_cols)

        sort_index = None
        direction = None
        if plan.get("sort_by") is not None and n_cols:
            direction = self._normalize_sort_order(plan.get("sort_order"))
            if direction is None:
                return {"error": (
                    f"sort_order={plan.get('sort_order')!r} is not a sort "
                    f"direction."),
                    "error_category": "invalid_sort",
                    "allowed_values": ["desc", "asc"]}
            sort_index = self._resolve_sort_column(
                plan["sort_by"], dimensions, measures)
            if sort_index is None:
                return {"error": (
                    f"sort_by={plan['sort_by']!r} names no column of this "
                    f"query."),
                    "error_category": "invalid_sort",
                    "available_columns": column_names}

        order = list(range(n_cols))
        if sort_index is not None and direction is not None:
            order = [sort_index] + [i for i in range(n_cols) if i != sort_index]
            if sort_index >= n_dims:
                measures[sort_index - n_dims]["sort_by"] = {
                    "qSortByNumeric": direction}
            else:
                dimensions[sort_index]["sort_by"] = {
                    "qSortByNumeric": direction, "qSortByAscii": direction,
                    "qSortByExpression": 0, "qExpression": ""}

        offset = max(0, int(plan.get("offset") or 0))
        return {
            "object": {
                "qInfo": {"qId": f"query-{uuid.uuid4().hex[:12]}",
                          "qType": "HyperCube"},
                "qHyperCubeDef": self._hypercube_def(
                    dimensions, measures, offset, limit, order,
                    suppress_zero=False, exclude_null_dimensions=True),
            },
            "column_names": column_names,
            "n_dims": n_dims,
            "limit": limit,
            "offset": offset,
            "sorted_by": column_names[sort_index] if sort_index is not None else None,
            "sort_order": (("desc" if direction == -1 else "asc")
                           if sort_index is not None else None),
        }

    def _query_reply(self, plan: Dict[str, Any], cube: Optional[Dict[str, Any]],
                     shaped: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """The answer to one query in the batch."""
        if plan.get("error"):
            reply = {k: v for k, v in plan.items()
                     if k in ("id", "error", "error_category", "hint",
                              "did_you_mean", "unknown_fields",
                              "unknown_values", "allowed_values",
                              "accepted_forms", "next_actions",
                              "invalid_expressions", "available_columns")}
            return reply
        if cube is None or shaped is None:
            return {"id": plan.get("id"),
                    "error": "This query did not run",
                    "error_category": "engine_api_error"}

        column_names = shaped["column_names"]
        n_dims = shaped["n_dims"]
        temporal = self._temporal_columns(cube)
        rows = self._matrix_to_rows(cube.get("qDataPages") or [],
                                    column_names, temporal)
        total_rows = (cube.get("qSize") or {}).get("qcy", 0)

        warnings: List[str] = []
        for column in self._measure_columns_are_empty(rows, n_dims, column_names):
            warnings.append(
                f"Every value of {column!r} came back 0 or '-'. Qlik returns "
                f"that for an aggregation over no rows; check the filters."
            )
        if not rows:
            warnings.append(
                "No rows matched. The grouping fields have no values under "
                "these filters."
            )
        for check in plan.get("period_check", []):
            if check.get("filter_applied") is False:
                warnings.append(check["note"])

        reply: Dict[str, Any] = {
            "id": plan["id"],
            "columns": column_names,
            "rows": rows,
            "returned_rows": len(rows),
            "total_rows": total_rows,
            "grand_total": [
                cell.get("qText")
                if (cell.get("qNum") in (None, "NaN")
                    or (n_dims + index) in temporal)
                else cell.get("qNum")
                for index, cell in enumerate(cube.get("qGrandTotalRow") or [])
            ],
            "sorted_by": shaped["sorted_by"],
            "sort_order": shaped["sort_order"],
        }
        if plan.get("filters_applied"):
            reply["filters_applied"] = plan["filters_applied"]
        if plan.get("period_check"):
            reply["period_check"] = plan["period_check"]
        if total_rows > len(rows):
            reply["has_more"] = True
            reply["next_offset"] = shaped["offset"] + len(rows)
            if shaped["sorted_by"] is None:
                warnings.append(
                    f"{total_rows} groups exist and {len(rows)} came back, "
                    f"in no particular order. Set `sort_by` to a measure "
                    f"label to get the largest ones."
                )
        if warnings:
            reply["warnings"] = warnings
        return reply
