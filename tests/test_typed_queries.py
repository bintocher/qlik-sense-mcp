"""Typed queries: the caller names fields, the server writes the Qlik.

Two things are being protected here. One is that a batch of independent
queries costs three round-trips rather than three per query — the Engine
JSON API matches replies to requests by id, so nothing has to wait. The
other is that a filter on a period reports what it actually selected, so
a filter that failed to apply is visible in the answer instead of hiding
behind a plausible number.
"""

import pytest

from qlik_sense_mcp_server.engine.queries import AGGREGATIONS
from qlik_sense_mcp_server.engine_api import QlikEngineAPI


# Names Qlik knows as functions rather than as fields. The double has to
# know them for the same reason the real CheckExpression does: `Null()` in
# a division guard is not a missing field.
_QLIK_FUNCTIONS = {
    "Sum", "Count", "Avg", "Min", "Max", "Text", "Num", "If", "DISTINCT",
    "Median", "Stdev", "Aggr", "Fractile", "Null", "TOTAL", "Index",
    "Upper", "Right", "Len", "P", "E", "Only", "Mode",
}


class _Engine(QlikEngineAPI):
    """A whole Engine, as far as a batch of queries can tell.

    Records every batch it was sent, so the tests can count round-trips.
    """

    ws_operation_timeout = 30.0

    def __init__(self, known=("Region", "Amount", "OrderDate", "Category"),
                 rows=None, period_bounds=(40544, 40908), syntax_errors=None):
        self.known = set(known)
        self.syntax_errors = syntax_errors or {}
        self.rows = rows if rows is not None else [["North", 10.0]]
        self.period_bounds = period_bounds
        self.batches = []
        self.destroyed = []
        self.pages = {}
        self.cube_defs = []
        self.short_page = 0
        self.stops_sending = False

    def send_requests_pipelined(self, requests, raise_on_error=True, timeout=None):
        self.batches.append([r["method"] for r in requests])
        replies = []
        for request in requests:
            replies.append(self._reply(request))
        return replies

    def _reply(self, request):
        method = request["method"]
        params = request.get("params") or []
        handle = request.get("handle")
        if method == "ExpandExpression":
            return {"qExpandedExpression": params[0]}
        if method == "CheckExpression":
            complaint = self.syntax_errors.get(params[0], "")
            missing = [] if complaint else [
                w for w in self._names(params[0]) if w not in self.known]
            return {"qErrorMsg": complaint, "qBadFieldNames": [
                {"qFrom": params[0].index(name), "qCount": len(name)}
                for name in missing]}
        if method == "GetFieldsFromExpression":
            return {"qFieldNames": [n for n in self._names(params[0])
                                    if n in self.known]}
        if method == "EvaluateEx":
            return {"qValue": self._evaluate(params[0])}
        if method == "CreateSessionObject":
            fetch = ((params[0] or {}).get("qHyperCubeDef") or {}).get(
                "qInitialDataFetch") or [{}]
            self.cube_defs.append(
                (params[0] or {}).get("qHyperCubeDef") or {})
            handle = 100 + len(self.pages)
            # Per object, not per stub: a batch holds several queries with
            # pages of their own, and one shared attribute gave them all
            # the page of whichever object was created last.
            self.pages[handle] = (fetch[0].get("qTop", 0),
                                  fetch[0].get("qHeight"))
            return {"qReturn": {"qHandle": handle}}
        if method == "GetHyperCubeData":
            page = (params or [None, [{}]])[1][0]
            top, height = page.get("qTop", 0), page.get("qHeight", 0)
            if self.stops_sending:
                return {"qDataPages": [{"qMatrix": []}]}
            more = self.rows[top:top + height]
            if self.short_page:
                more = more[:self.short_page]
            return {"qDataPages": [{"qMatrix": [
                [{"qText": str(v),
                  "qNum": v if isinstance(v, (int, float)) else "NaN"}
                 for v in row] for row in more]}]}
        if method == "GetLayout":
            return {"qLayout": {"qHyperCube": self._cube(handle)}}
        if method == "DestroySessionObject":
            self.destroyed.append(params[0])
            return {"qReturn": True}
        raise AssertionError(f"unexpected Engine call {method}")

    def _evaluate(self, expression):
        if "Min(" in expression:
            value = self.period_bounds[0]
        elif "Max(" in expression:
            value = self.period_bounds[1]
        else:
            return {"qText": "3", "qNumber": 3, "qIsNumeric": True}
        if expression.startswith("=Text("):
            return {"qText": f"day-{value}", "qNumber": "NaN"}
        return {"qText": str(value), "qNumber": value, "qIsNumeric": True}

    def _cube(self, handle=None):
        width = len(self.rows[0]) if self.rows else 1
        top, height = self.pages.get(handle, (0, None))
        shown = self.rows[top:top + height] if height else self.rows[top:]
        if self.short_page:
            shown = shown[:self.short_page]
        return {
            "qSize": {"qcy": len(self.rows), "qcx": width},
            "qDimensionInfo": [{"qNumFormat": {"qType": "A"}}],
            "qMeasureInfo": [{"qNumFormat": {"qType": "F"}}] * (width - 1),
            "qGrandTotalRow": [{"qNum": 99.0, "qText": "99"}],
            "qDataPages": [{"qMatrix": [
                [{"qText": str(v), "qNum": v if isinstance(v, (int, float)) else "NaN"}
                 for v in row] for row in shown]}],
        }

    @staticmethod
    def _names(expression):
        """Names Qlik would read as fields.

        Measured on the real CheckExpression: a value in single quotes is
        a literal and never a field, so `{<Region={'North'}>}` reports
        nothing about North.
        """
        import re
        without_literals = re.sub(r"'[^']*'", "", expression)
        return [m.group(1) or m.group(2) for m in
                re.finditer(r"\[([^\]]+)\]|\b([A-Z][A-Za-z]+)\b",
                            without_literals)
                if (m.group(1) or m.group(2)) not in _QLIK_FUNCTIONS]

    def get_field_description(self, app_handle, field_name):
        """Qlik's tags decide whether a bound is a day or a value."""
        tags = ["$numeric", "$date"] if "Date" in field_name else ["$numeric"]
        return {"name": field_name, "tags": tags}

    def get_fields(self, app_handle):
        return {"fields": [{"field_name": n} for n in sorted(self.known)]}


def _query(**kwargs):
    base = {"group_by": ["Region"],
            "metrics": [{"field": "Amount", "agg": "sum"}]}
    base.update(kwargs)
    return base


class TestExpressionsAreWritten:
    @pytest.mark.parametrize("aggregation, fragment", [
        ("sum", "Sum([Amount])"),
        ("count", "Count([Amount])"),
        ("count_distinct", "Count(DISTINCT [Amount])"),
        ("avg", "Avg([Amount])"),
        ("median", "Median([Amount])"),
    ])
    def test_each_aggregation_becomes_its_qlik_function(self, aggregation, fragment):
        engine = _Engine()
        plan = engine._plan_query(1, "app", _query(
            metrics=[{"field": "Amount", "agg": aggregation}]), 0)
        assert plan["measures"][0]["expression"] == fragment

    def test_the_default_label_names_the_aggregation_and_the_field(self):
        plan = _Engine()._plan_query(1, "app", _query(), 0)
        assert plan["measures"][0]["label"] == "sum_Amount"

    def test_an_aggregation_the_server_cannot_write_lists_the_ones_it_can(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"field": "Amount", "agg": "variance"}]), 0)
        assert plan["error_category"] == "invalid_argument"
        assert plan["allowed_values"] == sorted(AGGREGATIONS)

    def test_a_filter_is_folded_into_every_measure(self):
        engine = _Engine()
        plan = engine._plan_query(1, "app", _query(
            metrics=[{"field": "Amount", "agg": "sum"},
                     {"field": "Amount", "agg": "count"}],
            filters=[{"field": "OrderDate", "period": "2011"}]), 0)
        assert all("{<" in m["expression"] for m in plan["measures"])

    def test_a_written_expression_is_accepted_alongside_metrics(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[], measures=[{"expression": "Sum([Amount])/Count([Amount])",
                                   "label": "AOV"}]), 0)
        assert plan["measures"][0]["label"] == "AOV"

    def test_a_query_with_no_measure_says_how_to_add_one(self):
        plan = _Engine()._plan_query(1, "app", {"group_by": ["Region"]}, 0)
        assert plan["error_category"] == "invalid_argument"
        assert "metrics" in plan["hint"]

    def test_a_grouping_field_with_no_name_is_refused(self):
        plan = _Engine()._plan_query(1, "app", _query(group_by=[""]), 0)
        assert plan["error_category"] == "invalid_argument"


class TestBatching:
    def test_five_queries_cost_three_round_trips(self):
        engine = _Engine()
        engine.run_queries(1, "app", [_query() for _ in range(5)])
        # Validation runs first (expand, check), then create, layout,
        # destroy — three batches for the queries themselves however many
        # there are.
        engine_batches = [b for b in engine.batches
                          if b[0] in ("CreateSessionObject", "GetLayout",
                                      "DestroySessionObject")]
        assert len(engine_batches) == 3
        assert len(engine_batches[0]) == 5

    def test_every_query_gets_its_own_answer(self):
        engine = _Engine()
        result = engine.run_queries(1, "app", [_query(), _query()])
        assert len(result["results"]) == 2
        assert result["queries_run"] == 2

    def test_an_id_the_caller_gave_comes_back_with_its_answer(self):
        engine = _Engine()
        result = engine.run_queries(1, "app", [_query(id="by_region")])
        assert result["results"][0]["id"] == "by_region"

    def test_queries_without_ids_are_numbered(self):
        engine = _Engine()
        result = engine.run_queries(1, "app", [_query(), _query()])
        assert [r["id"] for r in result["results"]] == ["q1", "q2"]

    def test_one_bad_query_does_not_take_the_others_down(self):
        engine = _Engine()
        result = engine.run_queries(1, "app", [
            _query(),
            _query(metrics=[{"field": "Nope", "agg": "sum"}]),
            _query(),
        ])
        assert result["queries_failed"] == 1
        assert result["queries_run"] == 2
        assert result["results"][1]["error_category"] == "field_not_found"

    def test_every_object_is_released(self):
        engine = _Engine()
        engine.run_queries(1, "app", [_query(), _query()])
        assert len(engine.destroyed) == 2

    def test_a_query_that_is_not_an_object_is_refused_by_itself(self):
        engine = _Engine()
        result = engine.run_queries(1, "app", ["give me sales", _query()])
        assert result["results"][0]["error_category"] == "invalid_argument"
        assert result["queries_run"] == 1


class TestPeriodControl:
    def test_the_answer_says_what_period_it_covers(self):
        engine = _Engine(period_bounds=(40544, 40908))
        result = engine.run_queries(1, "app", [_query(
            filters=[{"field": "OrderDate", "period": "2011"}])])
        check = result["results"][0]["period_check"][0]
        assert check["requested_from"] == "2011-01-01"
        assert check["requested_to"] == "2011-12-31"
        assert check["filter_applied"] is True

    def test_a_value_outside_the_period_says_the_filter_did_not_apply(self):
        """The failure this exists for: Qlik drops a condition it cannot
        honour and answers with the unfiltered total."""
        engine = _Engine(period_bounds=(40000, 41000))
        result = engine.run_queries(1, "app", [_query(
            filters=[{"field": "OrderDate", "period": "2011"}])])
        reply = result["results"][0]
        assert reply["period_check"][0]["filter_applied"] is False
        assert any("did not narrow" in w for w in reply["warnings"])

    def test_the_control_values_read_as_the_field_displays_them(self):
        engine = _Engine()
        result = engine.run_queries(1, "app", [_query(
            filters=[{"field": "OrderDate", "period": "2011"}])])
        check = result["results"][0]["period_check"][0]
        assert check["earliest_in_result"].startswith("day-")

    def test_a_query_without_a_period_carries_no_period_check(self):
        engine = _Engine()
        result = engine.run_queries(1, "app", [_query()])
        assert "period_check" not in result["results"][0]

    def test_what_each_filter_resolved_to_is_reported(self):
        engine = _Engine()
        result = engine.run_queries(1, "app", [_query(
            filters=[{"field": "OrderDate", "period": "2011"}])])
        applied = result["results"][0]["filters_applied"][0]
        assert applied["field"] == "OrderDate"
        assert applied["form"] in ("numeric", "expression")


class TestResultShape:
    def test_rows_and_columns_come_back_named(self):
        engine = _Engine(rows=[["North", 10.0], ["South", 20.0]])
        reply = engine.run_queries(1, "app", [_query()])["results"][0]
        assert reply["columns"] == ["Region", "sum_Amount"]
        assert reply["rows"] == [["North", 10.0], ["South", 20.0]]

    def test_the_grand_total_covers_every_group(self):
        engine = _Engine()
        reply = engine.run_queries(1, "app", [_query()])["results"][0]
        assert reply["grand_total"] == [99.0]

    def test_an_all_zero_measure_is_called_out(self):
        engine = _Engine(rows=[["North", 0], ["South", 0]])
        reply = engine.run_queries(1, "app", [_query()])["results"][0]
        assert any("came back 0" in w for w in reply["warnings"])

    def test_an_unsorted_cut_result_says_the_rows_are_arbitrary(self):
        engine = _Engine(rows=[["North", 10.0]])
        engine._cube = lambda handle=None: dict(
            _Engine._cube(engine, handle), qSize={"qcy": 500, "qcx": 2})
        reply = engine.run_queries(1, "app", [_query()])["results"][0]
        assert any("no particular order" in w for w in reply["warnings"])
        assert reply["has_more"] is True

    def test_sorting_by_a_column_that_does_not_exist_is_refused(self):
        engine = _Engine()
        reply = engine.run_queries(
            1, "app", [_query(sort_by="Profit")])["results"][0]
        assert reply["error_category"] == "invalid_sort"
        assert reply["available_columns"] == ["Region", "sum_Amount"]

    def test_a_limit_wider_than_the_cell_cap_is_refused(self):
        """Not quietly reduced: a cut limit returns fewer rows than were
        asked for, and nothing in the reply says which happened."""
        engine = _Engine()
        plan = engine._plan_query(1, "app", _query(limit=5000), 0)
        shaped = engine._shape_cube(plan)
        assert shaped["error_category"] == "cell_cap_exceeded"
        assert "limit=" in shaped["hint"]

    def test_a_limit_above_the_row_ceiling_is_refused(self):
        engine = _Engine()
        plan = engine._plan_query(1, "app", _query(limit=99999), 0)
        assert engine._shape_cube(plan)["error_category"] == "limit_exceeded"

    @pytest.mark.parametrize("offset", [-1, "next", 2.5, True])
    def test_an_offset_that_is_not_a_row_number_is_refused(self, offset):
        engine = _Engine()
        plan = engine._plan_query(1, "app", _query(offset=offset), 0)
        assert engine._shape_cube(plan)["error_category"] == "invalid_argument"

    def test_the_null_group_is_kept_unless_asked_otherwise(self):
        """Dropping it is a statement about the data, so the caller makes
        it. Facts with no value for the grouping field are still facts."""
        engine = _Engine()
        plan = engine._plan_query(1, "app", _query(), 0)
        shaped = engine._shape_cube(plan)
        assert shaped["object"]["qHyperCubeDef"]["qDimensions"][0][
            "qNullSuppression"] is False

    def test_the_caller_can_ask_for_it_to_be_dropped(self):
        engine = _Engine()
        plan = engine._plan_query(
            1, "app", _query(exclude_null_dimensions=True), 0)
        shaped = engine._shape_cube(plan)
        assert shaped["object"]["qHyperCubeDef"]["qDimensions"][0][
            "qNullSuppression"] is True


class TestNestedAggregations:
    """An aggregation over groups answers a different question than the
    same aggregation over rows.

    Measured on four rows - issue A with 1 and 2 days, B with 10, C with
    100: `Median([days])` returned 6, `Median(Aggr(Sum([days]), [issue]))`
    returned 10. Asking the first when the second was meant gives a number
    that looks entirely reasonable.
    """

    def test_the_motivating_example_is_written_as_qlik_writes_it(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"field": "tis_days", "inner_agg": "sum",
                      "per": "IssueId", "agg": "fractile", "p": 0.85}]), 0)
        assert plan["measures"][0]["expression"] == (
            "Fractile(Aggr(Sum([tis_days]), [IssueId]), 0.85)")

    def test_the_filter_goes_into_the_inner_aggregation(self):
        """It is the only function there that reads rows."""
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"field": "tis_days", "inner_agg": "sum",
                      "per": "IssueId", "agg": "median"}],
            filters=[{"field": "Region", "values": ["North"]}]), 0)
        expression = plan["measures"][0]["expression"]
        assert expression.startswith("Median(Aggr(Sum({<")
        assert expression.endswith("[tis_days]), [IssueId]))")

    def test_grouping_by_several_fields(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"field": "Amount", "inner_agg": "sum",
                      "per": ["IssueId", "Region"], "agg": "avg"}]), 0)
        assert plan["measures"][0]["expression"] == (
            "Avg(Aggr(Sum([Amount]), [IssueId], [Region]))")

    def test_a_flat_fractile_needs_no_grouping(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"field": "Amount", "agg": "fractile", "p": 0.5}]), 0)
        assert plan["measures"][0]["expression"] == "Fractile([Amount], 0.5)"

    def test_a_fractile_without_its_fraction_is_refused(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"field": "Amount", "agg": "fractile"}]), 0)
        assert plan["error_category"] == "invalid_argument"
        assert "0.85" in plan["hint"]

    def test_grouping_without_an_inner_aggregation_is_refused(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"field": "Amount", "per": "IssueId", "agg": "median"}]), 0)
        assert plan["error_category"] == "invalid_argument"
        assert "inner_agg" in plan["hint"]

    def test_an_inner_aggregation_with_nothing_to_group_by_is_refused(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"field": "Amount", "inner_agg": "sum",
                      "agg": "median"}]), 0)
        assert plan["error_category"] == "invalid_argument"
        assert "per" in plan["hint"]

    def test_count_distinct_over_groups_is_refused_with_the_reason(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"field": "Amount", "inner_agg": "sum",
                      "per": "IssueId", "agg": "count_distinct"}]), 0)
        assert plan["error_category"] == "invalid_argument"
        assert "counts the groups" in plan["hint"]

    def test_a_fractile_inside_aggr_is_refused(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"field": "Amount", "inner_agg": "fractile",
                      "per": "IssueId", "agg": "median"}]), 0)
        assert plan["error_category"] == "invalid_argument"
        assert "fractile" not in plan["allowed_values"]

    def test_the_same_field_twice_in_per_is_refused(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"field": "Amount", "inner_agg": "sum",
                      "per": ["IssueId", "IssueId"], "agg": "median"}]), 0)
        assert plan["error_category"] == "invalid_argument"
        assert "twice" in plan["error"]

    def test_the_label_says_both_aggregations(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"field": "Amount", "inner_agg": "sum",
                      "per": "IssueId", "agg": "median"}]), 0)
        assert plan["measures"][0]["label"] == "median_sum_Amount"

    def test_an_unknown_aggregation_points_at_the_way_out(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"field": "Amount", "agg": "variance"}]), 0)
        assert "engine_create_hypercube" in plan["hint"]


class TestPerMetricFilters:
    """A KPI needs its numerator and its denominator in one answer."""

    @staticmethod
    def _kpi():
        return _query(
            metrics=[
                {"field": "Amount", "agg": "count_distinct", "label": "sliced"},
                {"field": "Amount", "agg": "count_distinct", "label": "all",
                 "filters": []},
            ],
            filters=[{"field": "Region", "values": ["North"]}])

    def test_a_metric_without_filters_takes_the_query_filter(self):
        plan = _Engine()._plan_query(1, "app", self._kpi(), 0)
        assert "{<" in plan["measures"][0]["expression"]

    def test_an_empty_list_means_no_filter_at_all(self):
        plan = _Engine()._plan_query(1, "app", self._kpi(), 0)
        assert plan["measures"][1]["expression"] == "Count(DISTINCT [Amount])"

    def test_a_metric_can_narrow_differently(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"field": "Amount", "agg": "sum",
                      "filters": [{"field": "Category", "values": ["Books"]}]}],
            filters=[{"field": "Region", "values": ["North"]}]), 0)
        assert "[Category]" in plan["measures"][0]["expression"]
        assert "[Region]" not in plan["measures"][0]["expression"]

    def test_the_answer_says_which_measure_used_which_slice(self):
        result = _Engine().run_queries(1, "app", [self._kpi()])
        reported = result["results"][0]["measure_filters"]
        assert [entry["label"] for entry in reported] == ["all"]
        assert reported[0]["filters_applied"] == []

    def test_the_same_filter_asked_twice_is_built_once(self):
        engine = _Engine()
        same = [{"field": "Region", "values": ["North"]}]
        engine._plan_query(1, "app", _query(
            metrics=[{"field": "Amount", "agg": "sum", "filters": same},
                     {"field": "Amount", "agg": "count", "filters": same}],
            filters=same), 0)
        asked = [b for b in engine.batches if b[0] == "EvaluateEx"]
        assert len(asked) == 1

    def test_filters_that_are_not_a_list_are_refused(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"field": "Amount", "agg": "sum",
                      "filters": "Region"}]), 0)
        assert plan["error_category"] == "invalid_argument"


class TestOneMeasureDoesNotColourTheNext:
    """The query filter is the base for every measure, and stays it.

    Writing the resolved modifier back into the shared variable made the
    first measure with its own filter the base for the next one, so a
    measure that stated no filter inherited its neighbour's — silently,
    with a plausible number to show for it.
    """

    def test_a_measure_without_filters_takes_the_querys(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[],
            measures=[
                {"expression": "Sum({filter} [Amount])",
                 "filters": [{"field": "Category", "values": ["Books"]}]},
                {"expression": "Count({filter} [Amount])"},
            ],
            filters=[{"field": "Region", "values": ["North"]}]), 0)
        second = plan["measures"][1]["expression"]
        assert "[Region]" in second
        assert "[Category]" not in second

    def test_the_measure_with_its_own_filter_keeps_it(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[],
            measures=[
                {"expression": "Sum({filter} [Amount])",
                 "filters": [{"field": "Category", "values": ["Books"]}]},
                {"expression": "Count({filter} [Amount])"},
            ],
            filters=[{"field": "Region", "values": ["North"]}]), 0)
        assert "[Category]" in plan["measures"][0]["expression"]

    def test_an_empty_filter_does_not_disarm_the_next_measure(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[],
            measures=[
                {"expression": "Count({filter} [Amount])", "filters": []},
                {"expression": "Sum({filter} [Amount])"},
            ],
            filters=[{"field": "Region", "values": ["North"]}]), 0)
        assert plan["measures"][0]["expression"] == "Count([Amount])"
        assert "[Region]" in plan["measures"][1]["expression"]


class TestFractionBounds:
    """Qlik answers a fraction outside 0..1 with a dash, not an error —
    measured — and a dash reads as a value."""

    @pytest.mark.parametrize("fraction", [1.5, -0.2, 2, -1])
    def test_a_fraction_outside_the_range_is_refused(self, fraction):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"field": "Amount", "agg": "fractile", "p": fraction}]), 0)
        assert plan["error_category"] == "invalid_argument"
        assert "between 0 and 1" in plan["error"]

    @pytest.mark.parametrize("fraction", [0, 0.5, 1])
    def test_the_ends_of_the_range_are_allowed(self, fraction):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"field": "Amount", "agg": "fractile", "p": fraction}]), 0)
        assert "error" not in plan

    def test_a_fraction_that_is_not_a_number_is_refused(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"field": "Amount", "agg": "fractile", "p": "85%"}]), 0)
        assert plan["error_category"] == "invalid_argument"

    def test_the_nested_form_checks_it_too(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"field": "Amount", "inner_agg": "sum", "per": "IssueId",
                      "agg": "fractile", "p": 1.5}]), 0)
        assert plan["error_category"] == "invalid_argument"


class TestAFailedQuestionIsNotAnAnswer:
    """`get_field_description` answers empty both for a missing field and
    for a call that failed. Refusing on the second would turn a dropped
    frame into "this field does not exist"."""

    def test_a_transport_failure_does_not_read_as_a_missing_field(self):
        class _Broken(_Engine):
            def send_request(self, method, params=None, handle=-1, timeout=None):
                if method == "GetFieldDescription":
                    raise ConnectionError("WebSocket recv() failed")
                return {}

        engine = _Broken()
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 3, "is_numeric": True, "error": None}
            for _ in exprs]
        result = engine.build_filters(
            1, "app", [{"field": "Region", "values": ["North"]}])
        assert "error" not in result


class TestAnAnswerIsNotAFailureToAsk:
    """Engine refusing a call and the call not getting through are
    different things, and the difference decides whether a field is
    reported missing."""

    def test_engine_saying_no_such_field_is_a_refusal(self):
        from qlik_sense_mcp_server.exceptions import QlikEngineError

        class _Missing(_Engine):
            def send_request(self, method, params=None, handle=-1, timeout=None):
                if method == "GetFieldDescription":
                    raise QlikEngineError("Engine API error: Invalid parameters")
                return {}

        result = _Missing().build_filters(
            1, "app", [{"field": "Regoin", "values": ["North"]}])
        assert result["error_category"] == "field_not_found"
        assert "[Regoin]" in result["error"]

    def test_a_dropped_frame_is_not(self):
        class _Broken(_Engine):
            def send_request(self, method, params=None, handle=-1, timeout=None):
                if method == "GetFieldDescription":
                    raise ConnectionError("WebSocket recv() failed")
                return {}

        engine = _Broken()
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 3, "is_numeric": True, "error": None}
            for _ in exprs]
        result = engine.build_filters(
            1, "app", [{"field": "Region", "values": ["North"]}])
        assert "error" not in result

    def test_a_field_that_exists_passes(self):
        engine = _Engine()
        engine.evaluate_expressions = lambda handle, exprs: [
            {"text": None, "number": 3, "is_numeric": True, "error": None}
            for _ in exprs]
        result = engine.build_filters(
            1, "app", [{"field": "Region", "values": ["North"]}])
        assert "error" not in result


class TestControlValuesFollowTheFilter:
    """A period stated inside a metric narrows that metric, and a period
    that fails to apply is what these probes exist to catch."""

    def test_a_period_on_a_metric_is_checked_too(self):
        engine = _Engine(period_bounds=(40544, 40908))
        result = engine.run_queries(1, "app", [_query(
            metrics=[{"field": "Amount", "agg": "sum",
                      "filters": [{"field": "OrderDate", "period": "2011"}]}])])
        checks = result["results"][0].get("period_check") or []
        assert [c["field"] for c in checks] == ["OrderDate"]

    def test_a_period_on_a_metric_that_did_not_apply_is_reported(self):
        engine = _Engine(period_bounds=(10, 90000))
        result = engine.run_queries(1, "app", [_query(
            metrics=[{"field": "Amount", "agg": "sum",
                      "filters": [{"field": "OrderDate", "period": "2011"}]}])])
        reply = result["results"][0]
        assert reply["period_check"][0]["filter_applied"] is False
        assert any("did not narrow" in w for w in reply["warnings"])

    def test_the_same_period_is_not_checked_twice(self):
        engine = _Engine(period_bounds=(40544, 40908))
        same = [{"field": "OrderDate", "period": "2011"}]
        result = engine.run_queries(1, "app", [_query(
            metrics=[{"field": "Amount", "agg": "sum", "filters": same},
                     {"field": "Amount", "agg": "count", "filters": same}],
            filters=same)])
        assert len(result["results"][0]["period_check"]) == 1


class TestShareOfTheTotal:
    """The same sum, once per group and once across all of them, in one
    row. Verified live on North 40 / South 60: the share came back 0.4 and
    0.6, and with total_except by region, each client's row carried its
    own region's 40 or 60."""

    def test_total_ignores_the_grouping(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"field": "Amount", "agg": "sum", "total": True}]), 0)
        assert plan["measures"][0]["expression"] == "Sum(TOTAL [Amount])"

    def test_total_keeps_the_filter_inside(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"field": "Amount", "agg": "sum", "total": True}],
            filters=[{"field": "Region", "values": ["North"]}]), 0)
        expression = plan["measures"][0]["expression"]
        assert expression.startswith("Sum(TOTAL {<")
        assert "[Region]" in expression

    def test_total_except_names_what_it_still_respects(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"field": "Amount", "agg": "sum",
                      "total_except": ["Region"]}]), 0)
        assert plan["measures"][0]["expression"] == (
            "Sum(TOTAL <[Region]> [Amount])")

    def test_total_except_takes_several_fields(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"field": "Amount", "agg": "sum",
                      "total_except": ["Region", "Category"]}]), 0)
        assert "TOTAL <[Region], [Category]>" in plan["measures"][0]["expression"]

    def test_an_empty_total_except_is_refused(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"field": "Amount", "agg": "sum", "total_except": []}]), 0)
        assert plan["error_category"] == "invalid_argument"
        assert "total" in plan["hint"]

    def test_a_share_is_an_ordinary_division(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"label": "share", "op": "divide", "of": [
                {"field": "Amount", "agg": "sum"},
                {"field": "Amount", "agg": "sum", "total": True}]}]), 0)
        expression = plan["measures"][0]["expression"]
        assert "Sum(TOTAL [Amount])" in expression
        assert expression.startswith("If(")

    def test_without_it_nothing_changes(self):
        plan = _Engine()._plan_query(1, "app", _query(), 0)
        assert "TOTAL" not in plan["measures"][0]["expression"]


class TestEveryValueCountsTowardsTheBudget:
    """Every filter asks Engine about its field, and every value asks
    whether the field holds it. Counting only the values stated at the top
    let a metric carry three hundred of its own."""

    def test_values_inside_a_metric_filter_count(self):
        from qlik_sense_mcp_server.engine.queries import MAX_EXPRESSIONS_PER_CALL

        engine = _Engine()
        result = engine.run_queries(1, "app", [_query(
            metrics=[{"field": "Amount", "agg": "sum",
                      "filters": [{"field": "Region", "values": [
                          f"v{i}" for i in range(MAX_EXPRESSIONS_PER_CALL)]}]}])])
        assert result["error_category"] == "limit_exceeded"
        assert engine.batches == []

    def test_values_inside_an_element_set_count(self):
        from qlik_sense_mcp_server.engine.queries import MAX_EXPRESSIONS_PER_CALL

        engine = _Engine()
        result = engine.run_queries(1, "app", [_query(
            filters=[{"field": "Client", "matching": {"filters": [
                {"field": "Region", "values": [
                    f"v{i}" for i in range(MAX_EXPRESSIONS_PER_CALL)]}]}}])])
        assert result["error_category"] == "limit_exceeded"

    def test_an_ordinary_query_is_well_inside_it(self):
        engine = _Engine()
        result = engine.run_queries(1, "app", [_query(
            filters=[{"field": "Region", "values": ["North", "South"]}])])
        assert result["queries_failed"] == 0


class TestTotalReachesEveryShape:
    def test_a_nested_aggregation_can_ignore_the_grouping(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"field": "Amount", "inner_agg": "sum", "per": "IssueId",
                      "agg": "median", "total": True}]), 0)
        assert plan["measures"][0]["expression"] == (
            "Median(TOTAL Aggr(Sum([Amount]), [IssueId]))")

    def test_a_part_of_an_operation_can_ignore_it_too(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"label": "share", "op": "divide", "of": [
                {"field": "Amount", "agg": "sum"},
                {"field": "Amount", "agg": "sum", "total": True}]}]), 0)
        assert "Sum(TOTAL [Amount])" in plan["measures"][0]["expression"]

    @pytest.mark.parametrize("value", [1, True, {"field": "X"}])
    def test_a_grouping_that_is_not_a_name_is_refused(self, value):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"field": "Amount", "inner_agg": "sum", "per": value,
                      "agg": "median"}]), 0)
        assert plan["error_category"] == "invalid_argument"

    @pytest.mark.parametrize("value", [1, {"field": "X"}])
    def test_a_total_except_that_is_not_a_name_is_refused(self, value):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"field": "Amount", "agg": "sum",
                      "total_except": value}]), 0)
        assert plan["error_category"] == "invalid_argument"


class TestScopeWithoutFilters:
    def test_a_metric_can_state_a_scope_alone(self):
        """"Everything, ignoring selections" is a statement in itself."""
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"field": "Amount", "agg": "sum",
                      "scope": {"ignore_selections": True}}]), 0)
        assert plan["measures"][0]["expression"] == "Sum({1} [Amount])"

    def test_the_query_scope_still_reaches_a_plain_metric(self):
        plan = _Engine()._plan_query(1, "app", dict(
            _query(), scope={"ignore_selections": True}), 0)
        assert "{1}" in plan["measures"][0]["expression"]


class TestControlValuesFollowThePartsToo:
    def test_a_period_inside_a_part_is_checked(self):
        engine = _Engine(period_bounds=(40544, 40908))
        result = engine.run_queries(1, "app", [_query(
            metrics=[{"label": "share", "op": "divide", "of": [
                {"field": "Amount", "agg": "sum",
                 "filters": [{"field": "OrderDate", "period": "2011"}]},
                {"field": "Amount", "agg": "sum", "filters": []}]}])])
        checks = result["results"][0].get("period_check") or []
        assert [c["field"] for c in checks] == ["OrderDate"]


class TestAScopeAppliesWhereverItIsStated:
    """The rule was written for a metric and reached only a metric, so the
    flagship shape of this round - "this part by its own scope, that part by
    the total" - quietly counted one part by the filters of the query."""

    def test_a_part_of_an_operation_can_state_a_scope_alone(self):
        plan = _Engine()._plan_query(1, "app", dict(_query(
            metrics=[{"label": "share", "op": "divide", "of": [
                {"field": "Amount", "agg": "sum"},
                {"field": "Amount", "agg": "sum",
                 "scope": {"ignore_selections": True}}]}]),
            filters=[{"field": "Region", "values": ["North"]}]), 0)
        expression = plan["measures"][0]["expression"]
        assert "Sum({1} [Amount])" in expression
        # ...and the other part still carries the filter of the query.
        assert "[Region]" in expression

    def test_a_hand_written_measure_can_state_a_scope_alone(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[],
            measures=[{"expression": "Sum({filter} [Amount])",
                       "scope": {"ignore_selections": True}}]), 0)
        assert plan["measures"][0]["expression"] == "Sum({1} [Amount])"

    def test_a_broken_scope_on_a_part_is_refused(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"label": "share", "op": "divide", "of": [
                {"field": "Amount", "agg": "sum"},
                {"field": "Amount", "agg": "sum",
                 "scope": {"bookmark": "BM", "ignore_selections": True}}]}]), 0)
        assert "error" in plan


class TestTheReplySaysWhichPartUsedWhichSlice:
    """`measure_filters` answers "which measure used which slice". A metric
    made of parts narrows each of them on its own, and the number alone does
    not show it."""

    def test_the_parts_of_an_operation_are_listed(self):
        engine = _Engine()
        result = engine.run_queries(1, "app", [_query(
            metrics=[{"label": "share", "op": "divide", "of": [
                {"field": "Amount", "agg": "sum",
                 "filters": [{"field": "Region", "values": ["North"]}]},
                {"field": "Amount", "agg": "sum", "filters": []}]}])])
        listed = result["results"][0].get("measure_filters") or []
        assert [item["label"] for item in listed] == ["share [1]", "share [2]"]
        assert listed[0]["filters_applied"] and not listed[1]["filters_applied"]

    def test_a_plain_metric_is_listed_as_before(self):
        engine = _Engine()
        result = engine.run_queries(1, "app", [_query(
            metrics=[{"label": "north", "field": "Amount", "agg": "sum",
                      "filters": [{"field": "Region", "values": ["North"]}]}])])
        listed = result["results"][0].get("measure_filters") or []
        assert [item["label"] for item in listed] == ["north"]


class TestTheReplyNamesTheSetItCountedOver:
    def test_the_query_scope_is_in_the_reply(self):
        engine = _Engine()
        result = engine.run_queries(1, "app", [dict(
            _query(), scope={"ignore_selections": True})])
        assert result["results"][0]["scope"] == "1"

    def test_a_measure_counted_over_a_bookmark_says_so(self):
        engine = _Engine()
        result = engine.run_queries(1, "app", [_query(
            metrics=[{"label": "bm", "field": "Amount", "agg": "sum",
                      "scope": {"bookmark": "BM01"}}])])
        listed = result["results"][0].get("measure_filters") or []
        assert listed == [{"label": "bm", "filters_applied": [],
                           "scope": "BM01"}]

    def test_a_query_without_a_scope_says_nothing(self):
        engine = _Engine()
        result = engine.run_queries(1, "app", [_query()])
        assert "scope" not in result["results"][0]


class TestThePartIsNamedByItsOwnPosition:
    """With a slice on the second part only, the reply used to call it
    "[1]" and point at the first."""

    def test_a_slice_on_the_second_part_is_named_second(self):
        engine = _Engine()
        result = engine.run_queries(1, "app", [_query(
            metrics=[{"label": "share", "op": "divide", "of": [
                {"field": "Amount", "agg": "sum"},
                {"field": "Amount", "agg": "sum",
                 "filters": [{"field": "Region", "values": ["North"]}]}]}])])
        listed = result["results"][0].get("measure_filters") or []
        assert [item["label"] for item in listed] == ["share [2]"]

    def test_a_nested_operation_keeps_its_own_parts(self):
        engine = _Engine()
        result = engine.run_queries(1, "app", [_query(
            metrics=[{"label": "share", "op": "divide", "of": [
                {"op": "add", "of": [
                    {"field": "Amount", "agg": "sum"},
                    {"field": "Amount", "agg": "sum",
                     "filters": [{"field": "Region", "values": ["North"]}]}]},
                {"field": "Amount", "agg": "sum", "total": True}]}])])
        listed = result["results"][0].get("measure_filters") or []
        assert [item["label"] for item in listed] == ["share [1][2]"]
        assert listed[0]["filters_applied"]

    def test_a_period_inside_a_nested_part_is_still_checked(self):
        engine = _Engine(period_bounds=(40544, 40908))
        result = engine.run_queries(1, "app", [_query(
            metrics=[{"label": "share", "op": "divide", "of": [
                {"op": "add", "of": [
                    {"field": "Amount", "agg": "sum"},
                    {"field": "Amount", "agg": "sum",
                     "filters": [{"field": "OrderDate", "period": "2011"}]}]},
                {"field": "Amount", "agg": "sum"}]}])])
        checks = result["results"][0].get("period_check") or []
        assert [c["field"] for c in checks] == ["OrderDate"]


class TestAScopeThatNamesNothing:
    """`{}` names no set, and neither does every key left false. Reading
    the presence of the key rather than its content threw away the filters
    the query had already stated."""

    @pytest.mark.parametrize("scope", [{}, {"ignore_selections": False},
                                       {"bookmark": None}])
    def test_an_empty_scope_keeps_the_query_filters(self, scope):
        plan = _Engine()._plan_query(1, "app", _query(
            filters=[{"field": "Region", "values": ["North"]}],
            metrics=[{"field": "Amount", "agg": "sum", "scope": scope}]), 0)
        assert plan["measures"][0]["expression"] == (
            "Sum({<[Region]={'North'}>} [Amount])")

    def test_a_scope_that_names_something_still_replaces_them(self):
        plan = _Engine()._plan_query(1, "app", _query(
            filters=[{"field": "Region", "values": ["North"]}],
            metrics=[{"field": "Amount", "agg": "sum",
                      "scope": {"ignore_selections": True}}]), 0)
        assert plan["measures"][0]["expression"] == "Sum({1} [Amount])"


class TestTheScopeReachesThePartsOfAnOperation:
    """A part that adds a filter of its own used to fall back to the
    query's set, so the numerator and the denominator of one ratio were
    counted over different sets."""

    def test_the_scope_of_a_metric_reaches_its_parts(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"label": "share", "op": "divide",
                      "scope": {"ignore_selections": True}, "of": [
                          {"field": "Amount", "agg": "sum", "filters": [
                              {"field": "Region", "values": ["North"]}]},
                          {"field": "Amount", "agg": "sum"}]}]), 0)
        expression = plan["measures"][0]["expression"]
        assert "Sum({1<[Region]={'North'}>} [Amount])" in expression
        assert "Sum({1} [Amount])" in expression

    def test_a_part_may_still_state_its_own(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"label": "share", "op": "divide",
                      "scope": {"ignore_selections": True}, "of": [
                          {"field": "Amount", "agg": "sum",
                           "scope": {"bookmark": "BM01"}},
                          {"field": "Amount", "agg": "sum"}]}]), 0)
        expression = plan["measures"][0]["expression"]
        assert "Sum({BM01} [Amount])" in expression


class TestAnOperationInsideAnOperation:
    """Arithmetic reads by precedence, not by structure: written flat,
    `Sum(A) + Sum(B) / Sum(C)` is not the sum divided by the third."""

    def test_a_nested_operation_keeps_its_own_precedence(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"label": "n", "op": "divide", "of": [
                {"op": "add", "of": [{"field": "Amount", "agg": "sum"},
                                     {"field": "Amount", "agg": "count"}]},
                {"field": "Amount", "agg": "sum"}]}]), 0)
        assert "(Sum([Amount]) + Count([Amount])) / Sum([Amount])" in (
            plan["measures"][0]["expression"])

    def test_a_plain_part_is_not_wrapped(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"label": "n", "op": "divide", "of": [
                {"field": "Amount", "agg": "sum"},
                {"field": "Amount", "agg": "count"}]}]), 0)
        assert "Sum([Amount]) / Count([Amount])" in (
            plan["measures"][0]["expression"])


class TestOneValueIsAValue:
    """A single value written without a list used to be counted by its
    characters, and the count threw before the queries were told apart -
    taking the healthy neighbours of a batch down with it."""

    def test_a_single_value_does_not_break_the_batch(self):
        engine = _Engine()
        result = engine.run_queries(1, "app", [
            _query(filters=[{"field": "Region", "values": "North"}]),
            _query()])
        assert result["queries_failed"] == 0
        assert len(result["results"]) == 2


class TestAnInheritedScopeDoesNotEraseTheFilters:
    """An inherited scope arrives already built into the modifier, with the
    filters that came with it. Rebuilding it from the scope alone threw
    those filters away and answered over every row."""

    def test_a_metric_that_states_both_keeps_both_in_its_parts(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"label": "share", "op": "divide",
                      "scope": {"ignore_selections": True},
                      "filters": [{"field": "Region", "values": ["North"]}],
                      "of": [{"field": "Amount", "agg": "sum"},
                             {"field": "Amount", "agg": "sum",
                              "total": True}]}]), 0)
        expression = plan["measures"][0]["expression"]
        assert expression.count("{1<[Region]={'North'}>}") == 3

    def test_a_part_may_still_override_it(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"label": "share", "op": "divide",
                      "scope": {"ignore_selections": True},
                      "filters": [{"field": "Region", "values": ["North"]}],
                      "of": [{"field": "Amount", "agg": "sum"},
                             {"field": "Amount", "agg": "sum",
                              "scope": {"bookmark": "BM01"}}]}]), 0)
        expression = plan["measures"][0]["expression"]
        assert "Sum({BM01} [Amount])" in expression
        assert "{1<[Region]={'North'}>}" in expression


class TestAnEmptyScopeBesideFilters:
    """The same rule wherever it is written: a scope that names nothing
    does not cancel the set the query named."""

    @pytest.mark.parametrize("scope", [{}, {"ignore_selections": False}])
    def test_it_keeps_the_query_scope(self, scope):
        plan = _Engine()._plan_query(1, "app", dict(_query(
            metrics=[{"field": "Amount", "agg": "sum", "scope": scope,
                      "filters": [{"field": "Region", "values": ["North"]}]}]),
            scope={"ignore_selections": True}), 0)
        assert plan["measures"][0]["expression"] == (
            "Sum({1<[Region]={'North'}>} [Amount])")

    def test_a_part_of_an_operation_follows_the_same_rule(self):
        plan = _Engine()._plan_query(1, "app", dict(_query(
            metrics=[{"label": "n", "op": "divide", "of": [
                {"field": "Amount", "agg": "sum", "scope": {},
                 "filters": [{"field": "Region", "values": ["North"]}]},
                {"field": "Amount", "agg": "sum"}]}]),
            scope={"ignore_selections": True}), 0)
        assert "Sum({1<[Region]={'North'}>} [Amount])" in (
            plan["measures"][0]["expression"])


class TestThePartsCountTowardsTheCeiling:
    """A metric made of parts builds one expression per part, at any
    depth. Counting the list alone let one allowed metric carry an
    expression of any size into a shared connection."""

    def test_a_deep_metric_is_refused(self):
        from qlik_sense_mcp_server.engine.queries import (
            MAX_EXPRESSIONS_PER_CALL)

        deep = {"op": "add", "of": [{"field": "Amount", "agg": "sum"}
                                    for _ in range(MAX_EXPRESSIONS_PER_CALL)]}
        engine = _Engine()
        result = engine.run_queries(1, "app", [_query(metrics=[deep])])
        assert result["error_category"] == "limit_exceeded"
        assert engine.batches == []

    def test_nesting_counts_at_every_depth(self):
        from qlik_sense_mcp_server.engine.queries import (
            MAX_EXPRESSIONS_PER_CALL)

        inner = {"op": "add", "of": [{"field": "Amount", "agg": "sum"}
                                     for _ in range(20)]}
        deep = {"op": "add", "of": [dict(inner)
                                    for _ in range(MAX_EXPRESSIONS_PER_CALL
                                                   // 10)]}
        result = _Engine().run_queries(1, "app", [_query(metrics=[deep])])
        assert result["error_category"] == "limit_exceeded"

    def test_an_ordinary_operation_still_runs(self):
        result = _Engine().run_queries(1, "app", [_query(
            metrics=[{"label": "n", "op": "divide", "of": [
                {"field": "Amount", "agg": "sum"},
                {"field": "Amount", "agg": "count"}]}])])
        assert result["queries_failed"] == 0

    def test_the_field_an_element_set_reads_counts_too(self):
        from qlik_sense_mcp_server.engine.queries import _filter_cost

        cost = _filter_cost({"filters": [
            {"field": "Client", "matching": {
                "of_field": "Buyer",
                "filters": [{"field": "Year", "values": ["2023"]}]}}]})
        without = _filter_cost({"filters": [
            {"field": "Client", "matching": {
                "filters": [{"field": "Year", "values": ["2023"]}]}}]})
        assert cost == without + 1


class TestAScopeThatNamesNothingAnywhere:
    """Read from the description, not from the result: a scope naming no
    set neither replaces the query's filters nor triggers the branch that
    rebuilds them - wherever it is written."""

    def test_a_part_with_an_empty_scope_keeps_the_query_filters(self):
        plan = _Engine()._plan_query(1, "app", dict(_query(
            filters=[{"field": "Region", "values": ["North"]}],
            metrics=[{"label": "n", "op": "divide", "of": [
                {"field": "Amount", "agg": "sum", "scope": {}},
                {"field": "Amount", "agg": "sum"}]}]),
            scope={"ignore_selections": True}), 0)
        expression = plan["measures"][0]["expression"]
        assert "Sum({1} [Amount])" not in expression
        assert expression.count("{1<[Region]={'North'}>}") == 3

    def test_a_metric_with_an_empty_scope_keeps_them_too(self):
        plan = _Engine()._plan_query(1, "app", dict(_query(
            filters=[{"field": "Region", "values": ["North"]}],
            metrics=[{"field": "Amount", "agg": "sum", "scope": {}}]),
            scope={"ignore_selections": True}), 0)
        assert plan["measures"][0]["expression"] == (
            "Sum({1<[Region]={'North'}>} [Amount])")


class TestMetricsIsAList:
    """An object was walked by its keys, so each key became a metric while
    the ceiling counted none of them."""

    @pytest.mark.parametrize("stated", [
        {"a": {"field": "Amount", "agg": "sum"}}, "Amount", 5])
    def test_anything_else_is_refused(self, stated):
        result = _Engine().run_queries(1, "app", [
            {"group_by": ["Region"], "metrics": stated}])
        assert result["results"][0]["error_category"] == "invalid_argument"

    def test_a_list_still_works(self):
        result = _Engine().run_queries(1, "app", [_query()])
        assert result["queries_failed"] == 0


class TestCountingTheCostIsBounded:
    """A deeply nested request used to end the count with an interpreter
    error instead of the refusal the ceiling exists to give."""

    def test_a_deeply_nested_metric_is_refused(self):
        deep = {"field": "Amount", "agg": "sum"}
        for _ in range(400):
            deep = {"op": "add", "of": [deep,
                                        {"field": "Amount", "agg": "sum"}]}
        result = _Engine().run_queries(1, "app", [_query(metrics=[deep])])
        assert result["error_category"] == "limit_exceeded"

    def test_a_deeply_nested_filter_is_refused(self):
        deep = {"field": "Client", "values": ["a"]}
        for _ in range(400):
            deep = {"field": "Client", "matching": {"filters": [deep]}}
        result = _Engine().run_queries(1, "app", [_query(filters=[deep])])
        assert result["error_category"] == "limit_exceeded"

    def test_an_element_set_on_the_same_field_costs_nothing_extra(self):
        from qlik_sense_mcp_server.engine.queries import _filter_cost

        same = _filter_cost({"filters": [
            {"field": "Client", "matching": {
                "of_field": "Client",
                "filters": [{"field": "Year", "values": ["2023"]}]}}]})
        plain = _filter_cost({"filters": [
            {"field": "Client", "matching": {
                "filters": [{"field": "Year", "values": ["2023"]}]}}]})
        assert same == plain


class TestYesOrNoIsYesOrNo:
    """A key that means yes or no takes yes or no. A zero, or the word
    spelled out, used to switch on the very thing it named - every
    non-empty string is true."""

    @pytest.mark.parametrize("scope", [{"ignore_selections": 0},
                                       {"ignore_selections": "false"},
                                       {"current_selection": "no"},
                                       {"ignore_selections": 1}])
    def test_anything_but_a_boolean_is_refused(self, scope):
        result = _Engine().run_queries(1, "app", [_query(
            metrics=[{"field": "Amount", "agg": "sum", "scope": scope}])])
        assert result["results"][0]["error_category"] == "invalid_argument"

    def test_a_plain_no_keeps_the_query_filters(self):
        plan = _Engine()._plan_query(1, "app", _query(
            filters=[{"field": "Region", "values": ["North"]}],
            metrics=[{"field": "Amount", "agg": "sum",
                      "scope": {"ignore_selections": False}}]), 0)
        assert plan["measures"][0]["expression"] == (
            "Sum({<[Region]={'North'}>} [Amount])")


class TestAPartInheritsFromItsMetric:
    def test_an_empty_scope_beside_a_filter_inherits_the_metric_scope(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"label": "n", "op": "divide",
                      "scope": {"ignore_selections": True}, "of": [
                          {"field": "Amount", "agg": "sum", "scope": {},
                           "filters": [{"field": "Region",
                                        "values": ["North"]}]},
                          {"field": "Amount", "agg": "sum"}]}]), 0)
        assert "Sum({1<[Region]={'North'}>} [Amount])" in (
            plan["measures"][0]["expression"])


class TestEveryListIsAList:
    @pytest.mark.parametrize("key", ["group_by", "measures"])
    @pytest.mark.parametrize("stated", [{"a": 1}, 5])
    def test_anything_else_is_refused(self, key, stated):
        query = {"group_by": ["Region"],
                 "metrics": [{"field": "Amount", "agg": "sum"}]}
        query[key] = stated
        result = _Engine().run_queries(1, "app", [query])
        assert result["results"][0]["error_category"] == "invalid_argument"

    def test_one_field_named_plainly_still_works(self):
        result = _Engine().run_queries(1, "app", [
            {"group_by": "Region",
             "metrics": [{"field": "Amount", "agg": "sum"}]}])
        assert result["results"][0]["columns"] == ["Region", "sum_Amount"]


class TestTheCostOfAName:
    def test_the_same_field_in_brackets_costs_nothing_extra(self):
        from qlik_sense_mcp_server.engine.queries import _filter_cost

        bracketed = _filter_cost({"filters": [
            {"field": "Client", "matching": {
                "of_field": "[Client]",
                "filters": [{"field": "Year", "values": ["2023"]}]}}]})
        plain = _filter_cost({"filters": [
            {"field": "Client", "matching": {
                "filters": [{"field": "Year", "values": ["2023"]}]}}]})
        assert bracketed == plain

    def test_a_range_costs_what_the_calendar_path_costs(self):
        from qlik_sense_mcp_server.engine.queries import (
            _filter_cost, PERIOD_PROBE_COST)

        # Written as bounds or as a period, the same filter can take the
        # calendar path - the field decides, not the spelling - so both are
        # counted at what that path costs.
        periodic = _filter_cost({"filters": [
            {"field": "OrderDate", "period": "2024"}]})
        numeric = _filter_cost({"filters": [
            {"field": "price", "greater_than": 400}]})
        assert periodic == numeric == 1 + PERIOD_PROBE_COST


class TestAScopeWrittenDownIsAScopeChecked:
    """A scope naming no set is not applied, but it is still read: a key
    misspelled and holding zero used to pass unnoticed, and the answer came
    back over the query's set with nothing to say another was asked for."""

    @pytest.mark.parametrize("scope", [{"ignore_selection": 0},
                                       {"selection_back": -2},
                                       {"steps_back": 0}])
    def test_a_metric_scope_is_read(self, scope):
        result = _Engine().run_queries(1, "app", [_query(
            metrics=[{"field": "Amount", "agg": "sum", "scope": scope}])])
        assert result["results"][0]["error_category"] == "invalid_argument"

    def test_a_part_scope_is_read(self):
        result = _Engine().run_queries(1, "app", [_query(
            metrics=[{"label": "n", "op": "divide", "of": [
                {"field": "Amount", "agg": "sum",
                 "scope": {"ignore_selection": 0}},
                {"field": "Amount", "agg": "sum"}]}])])
        assert result["results"][0]["error_category"] == "invalid_argument"

    def test_a_hand_written_measure_scope_is_read(self):
        result = _Engine().run_queries(1, "app", [_query(
            metrics=[],
            measures=[{"expression": "Sum([Amount])",
                       "scope": {"bad_key": 0}}])])
        assert result["results"][0]["error_category"] == "invalid_argument"

    def test_a_readable_scope_still_runs(self):
        result = _Engine().run_queries(1, "app", [_query(
            metrics=[{"field": "Amount", "agg": "sum",
                      "scope": {"ignore_selections": True}}])])
        assert result["queries_failed"] == 0


class TestOneMeasureHasASize:
    """A division writes its denominator twice - once for the guard against
    zero - so divisions inside divisions double the text at every level.
    Sixteen levels is a megabyte of Qlik through a shared connection."""

    def test_a_doubling_metric_is_refused(self):
        deep = {"field": "Amount", "agg": "sum"}
        for _ in range(16):
            deep = {"op": "divide", "of": [{"field": "Amount", "agg": "sum"},
                                           dict(deep)]}
        plan = _Engine()._plan_query(1, "app", _query(metrics=[deep]), 0)
        assert plan["error_category"] == "limit_exceeded"
        assert "denominator" in plan["hint"]

    def test_an_ordinary_nesting_still_builds(self):
        deep = {"op": "divide", "of": [
            {"field": "Amount", "agg": "sum"},
            {"op": "add", "of": [{"field": "Amount", "agg": "sum"},
                                 {"field": "Amount", "agg": "count"}]}]}
        plan = _Engine()._plan_query(1, "app", _query(metrics=[deep]), 0)
        assert "expression" in plan["measures"][0]


class TestThePageEndsWhereTheRowsEnd:
    def test_the_last_page_is_not_called_incomplete(self):
        engine = _Engine(rows=[["North", 1], ["South", 2], ["East", 3]])
        result = engine.run_queries(1, "app", [_query(offset=2, limit=2)])
        reply = result["results"][0]
        # One row of three, taken from the third: the page ends where the
        # rows end, and the old formula compared the total against the size
        # of this page alone.
        assert reply["returned_rows"] == 1 and reply["total_rows"] == 3
        assert "has_more" not in reply

    def test_a_page_in_the_middle_says_there_is_more(self):
        engine = _Engine(rows=[["North", 1], ["South", 2], ["East", 3]])
        result = engine.run_queries(1, "app", [_query(offset=1, limit=1)])
        reply = result["results"][0]
        assert reply["returned_rows"] == 1
        assert reply["has_more"] is True and reply["next_offset"] == 2

    def test_an_offset_past_the_end_is_not_called_incomplete(self):
        engine = _Engine(rows=[["North", 1]])
        result = engine.run_queries(1, "app", [_query(offset=50, limit=10)])
        assert "has_more" not in result["results"][0]
        assert "next_offset" not in result["results"][0]


class TestAnEmptyNameIsAMistake:
    @pytest.mark.parametrize("scope", [{"bookmark": ""}, {"state": "  "},
                                       {"selection_back": 0},
                                       {"bookmark": 0}])
    def test_it_is_refused_rather_than_ignored(self, scope):
        result = _Engine().run_queries(1, "app", [_query(
            metrics=[{"field": "Amount", "agg": "sum", "scope": scope}])])
        assert result["results"][0]["error_category"] == "invalid_argument"


class TestEveryExpressionHasASize:
    def test_a_measure_written_by_hand_is_measured_too(self):
        from qlik_sense_mcp_server.engine.queries import MAX_EXPRESSION_CHARS

        long_one = "Sum([Amount]) + " * (MAX_EXPRESSION_CHARS // 10)
        result = _Engine().run_queries(1, "app", [_query(
            metrics=[], measures=[long_one + "Sum([Amount])"])])
        assert result["results"][0]["error_category"] == "limit_exceeded"

    def test_an_ordinary_measure_still_runs(self):
        result = _Engine().run_queries(1, "app", [_query(
            metrics=[], measures=["Sum([Amount])"])])
        assert result["queries_failed"] == 0


class TestEachQueryGetsItsOwnPage:
    """A batch holds several queries with pages of their own."""

    def test_two_queries_take_two_pages(self):
        engine = _Engine(rows=[["North", 1], ["South", 2], ["East", 3]])
        result = engine.run_queries(1, "app", [
            dict(_query(offset=0, limit=1), id="first"),
            dict(_query(offset=2, limit=1), id="second")])
        first, second = result["results"]
        assert first["rows"] == [["North", 1.0]]
        assert second["rows"] == [["East", 3.0]]
        assert first.get("has_more") is True
        assert "has_more" not in second


class TestYesOrNoEverywhere:
    @pytest.mark.parametrize("key", ["exclude_null_dimensions",
                                     "suppress_zero", "include_raw_layout"])
    @pytest.mark.parametrize("value", ["false", 0, 1, "no"])
    def test_a_query_key_takes_a_boolean(self, key, value):
        result = _Engine().run_queries(1, "app", [dict(_query(), **{key: value})])
        assert result["results"][0]["error_category"] == "invalid_argument"

    @pytest.mark.parametrize("value", ["false", 1, "yes"])
    def test_total_takes_a_boolean(self, value):
        result = _Engine().run_queries(1, "app", [_query(
            metrics=[{"field": "Amount", "agg": "sum", "total": value}])])
        assert result["results"][0]["error_category"] == "invalid_argument"

    def test_a_plain_boolean_still_works(self):
        result = _Engine().run_queries(1, "app", [dict(
            _query(metrics=[{"field": "Amount", "agg": "sum", "total": True}]),
            exclude_null_dimensions=True)])
        assert result["queries_failed"] == 0


class TestAKeyAcceptedIsAKeyObeyed:
    """Checked for type and then dropped on the floor: a caller that asked
    for the raw layout, or for zero-valued groups to go, got neither and no
    word about it."""

    def test_the_raw_layout_comes_back_when_asked_for(self):
        result = _Engine().run_queries(1, "app", [
            dict(_query(), include_raw_layout=True)])
        assert "hypercube_data" in result["results"][0]

    def test_it_stays_out_when_not_asked_for(self):
        result = _Engine().run_queries(1, "app", [_query()])
        assert "hypercube_data" not in result["results"][0]

    def test_suppress_zero_reaches_the_cube(self):
        engine = _Engine()
        engine.run_queries(1, "app", [dict(_query(), suppress_zero=True)])
        created = [b for b in engine.batches if "CreateSessionObject" in b]
        assert created
        assert engine.cube_defs[-1]["qSuppressZero"] is True

    def test_without_it_the_cube_keeps_zeroes(self):
        engine = _Engine()
        engine.run_queries(1, "app", [_query()])
        assert engine.cube_defs[-1]["qSuppressZero"] is False


class TestAShortPageIsReadOn:
    """Engine may hand back fewer rows than the page asked for. The
    hypercube path reads the rest; this one used to answer with fewer rows
    for the same question and no word about it."""

    def test_the_missing_rows_are_read(self):
        engine = _Engine(rows=[["North", 1], ["South", 2], ["East", 3]])
        engine.short_page = 1
        result = engine.run_queries(1, "app", [_query(limit=3)])
        reply = result["results"][0]
        assert reply["returned_rows"] == 3
        assert [row[0] for row in reply["rows"]] == ["North", "South", "East"]

    def test_a_full_page_asks_for_nothing_more(self):
        engine = _Engine(rows=[["North", 1], ["South", 2]])
        result = engine.run_queries(1, "app", [_query(limit=2)])
        assert not any("GetHyperCubeData" in batch for batch in engine.batches)


class TestReadingOnUntilThePageIsWhole:
    def test_several_short_replies_are_read_on(self):
        engine = _Engine(rows=[["A", 1], ["B", 2], ["C", 3], ["D", 4]])
        engine.short_page = 1
        reply = engine.run_queries(1, "app", [_query(limit=4)])["results"][0]
        assert reply["returned_rows"] == 4
        assert [row[0] for row in reply["rows"]] == ["A", "B", "C", "D"]

    def test_engine_giving_up_is_said_out_loud(self):
        engine = _Engine(rows=[["A", 1], ["B", 2], ["C", 3]])
        engine.short_page = 1
        engine.stops_sending = True
        reply = engine.run_queries(1, "app", [_query(limit=3)])["results"][0]
        assert reply["returned_rows"] == 1
        assert any("came back" in w and "offset=1" in w
                   for w in reply["warnings"])


class TestACombinationReachesEveryLevel:
    def test_a_metric_can_state_one(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"field": "Amount", "agg": "sum", "scope": {
                "combine": "union", "of": [
                    {"ignore_selections": True,
                     "filters": [{"field": "Region", "values": ["North"]}]},
                    {"ignore_selections": True,
                     "filters": [{"field": "Region", "values": ["South"]}]}]}}]
            ), 0)
        assert plan["measures"][0]["expression"] == (
            "Sum({(1<[Region]={'North'}>) + (1<[Region]={'South'}>)} "
            "[Amount])")

    def test_the_query_can_state_one(self):
        plan = _Engine()._plan_query(1, "app", dict(_query(), scope={
            "combine": "intersect", "of": [
                {"ignore_selections": True},
                {"current_selection": True}]}), 0)
        assert "(1) * ($)" in plan["measures"][0]["expression"]


class TestTheEstimateMatchesWhatIsWritten:
    """An allowed expression on the boundary used to be refused for length
    it never had."""

    @pytest.mark.parametrize("parts", [2, 3, 50, 400])
    def test_the_estimate_is_exact(self, parts):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"label": "n", "op": "divide", "of": [
                {"field": "Amount", "agg": "sum"} for _ in range(parts)]}]), 0)
        written = ["Sum([Amount])"] * parts
        planned = (sum(len(one) for one in written) + 3 * (parts - 1)
                   + sum(len(one) + 6 for one in written[1:])
                   + 4 * max(0, parts - 2) + 14)
        assert len(plan["measures"][0]["expression"]) == planned


class TestEachQuerySaysWhyItCameUpShort:
    def test_the_reason_is_that_query_own(self):
        engine = _Engine(rows=[["A", 1], ["B", 2], ["C", 3]])
        engine.short_page = 1
        engine.stops_sending = True
        result = engine.run_queries(1, "app", [
            dict(_query(limit=3), id="first"),
            dict(_query(limit=1), id="second")])
        first, second = result["results"]
        assert any("Engine stopped sending" in w for w in first["warnings"])
        assert second["returned_rows"] == 1
        assert not any("came back;" in w for w in second.get("warnings") or [])


class TestAListWhereAListBelongs:
    """The same rule at the top of a query as inside a metric or a set: an
    object read as "no filters" ran the query unfiltered and answered as if
    nothing were wrong."""

    @pytest.mark.parametrize("stated", [{}, {"field": "Region"}, "Region", 5])
    def test_filters_that_are_not_a_list_are_refused(self, stated):
        result = _Engine().run_queries(1, "app", [
            dict(_query(), filters=stated)])
        assert result["results"][0]["error_category"] == "invalid_filter"

    def test_a_list_still_works(self):
        result = _Engine().run_queries(1, "app", [_query(
            filters=[{"field": "Region", "values": ["North"]}])])
        assert result["queries_failed"] == 0


class TestACheckThatCouldNotRun:
    """Engine failing on the control expression is not "the filters select
    nothing together" - saying so passes unchecked numbers off as checked."""

    def test_the_reason_names_what_qlik_said(self):
        class _Failing(_Engine):
            def send_requests_pipelined(self, requests, raise_on_error=True,
                                        timeout=None):
                replies = super().send_requests_pipelined(
                    requests, raise_on_error, timeout)
                # Only the control probes - the ones that look at the
                # result. The probes that build the filter must still
                # work, or the query is refused before there is anything
                # to check.
                return [RuntimeError("the socket went away")
                        if (r["method"] == "EvaluateEx"
                            and "Min(" in str((r.get("params") or [""])[0]))
                        else reply
                        for r, reply in zip(requests, replies)]

        engine = _Failing(period_bounds=(40544, 40908))
        result = engine.run_queries(1, "app", [_query(
            filters=[{"field": "OrderDate", "period": "2011"}])])
        checks = result["results"][0].get("period_check") or []
        assert checks and "could not be checked" in checks[0]["note"]


class TestSetsCountTowardsTheCeiling:
    """Nesting combinations doubles the sets at every level; counting only
    the filters let one small request build hundreds of thousands of them
    on the connection every query shares."""

    def test_a_deeply_nested_combination_is_refused(self):
        scope = {"combine": "union", "of": [{"ignore_selections": True},
                                            {"current_selection": True}]}
        for _ in range(8):
            scope = {"combine": "union", "of": [dict(scope), dict(scope)]}
        result = _Engine().run_queries(1, "app", [
            dict(_query(), scope=scope)])
        assert result["error_category"] == "limit_exceeded"

    def test_an_ordinary_combination_still_runs(self):
        result = _Engine().run_queries(1, "app", [dict(_query(), scope={
            "combine": "union", "of": [{"ignore_selections": True},
                                       {"current_selection": True}]})])
        assert result["queries_failed"] == 0


class TestOfMeansTwoDifferentThings:
    """The sets of a combination and the parts of an arithmetic metric
    share a key name. Counting the second as sets refused ordinary "share
    of the whole" queries well inside the ceiling."""

    def test_a_batch_of_shares_is_not_refused(self):
        from qlik_sense_mcp_server.engine.queries import (
            MAX_EXPRESSIONS_PER_CALL)

        share = {"label": "share", "op": "divide", "of": [
            {"field": "Amount", "agg": "sum"},
            {"field": "Amount", "agg": "sum", "total": True}]}
        many = [dict(share) for _ in range(MAX_EXPRESSIONS_PER_CALL // 4)]
        result = _Engine().run_queries(1, "app", [_query(metrics=many)])
        assert result.get("error_category") != "limit_exceeded"
        assert result["queries_failed"] == 0

    def test_sets_are_still_counted(self):
        scope = {"combine": "union", "of": [{"ignore_selections": True},
                                            {"current_selection": True}]}
        for _ in range(8):
            scope = {"combine": "union", "of": [dict(scope), dict(scope)]}
        result = _Engine().run_queries(1, "app", [dict(_query(), scope=scope)])
        assert result["error_category"] == "limit_exceeded"


class TestACheckThatNeverRanIsNotACheck:
    """The filter still goes out - a dropped frame is not the caller's
    mistake - but calling it checked would be a statement about data nobody
    looked at."""

    def test_the_reply_says_the_values_were_not_checked(self):
        class _Silent(_Engine):
            def _reply(self, request):
                if request["method"] == "EvaluateEx":
                    return {"qValue": {"qText": None, "qNumber": "NaN"}}
                return super()._reply(request)

        result = _Silent().run_queries(1, "app", [_query(
            filters=[{"field": "Region", "values": ["North"]}])])
        reply = result["results"][0]
        assert any("could not be checked" in w for w in reply["warnings"])

    def test_a_checked_filter_says_nothing_of_the_kind(self):
        result = _Engine().run_queries(1, "app", [_query(
            filters=[{"field": "Region", "values": ["North"]}])])
        assert not any("could not be checked" in w
                       for w in result["results"][0].get("warnings") or [])


class TestAMeasureIsAnExpressionOrAnObject:
    @pytest.mark.parametrize("stated", [5, ["Sum([Amount])"], None])
    def test_anything_else_is_refused(self, stated):
        result = _Engine().run_queries(1, "app", [_query(
            metrics=[], measures=[stated])])
        assert result["results"][0]["error_category"] == "invalid_argument"


class TestEveryProbeCountsTowardsTheCeiling:
    """A text search, a condition written as an expression and an element
    set each prove themselves with a probe of their own."""

    @pytest.mark.parametrize("filter_shape, each", [
        ({"field": "Region", "contains": "no"}, 2),
        ({"field": "Region", "match_expression": "Sum([Amount]) > 1"}, 4),
        ({"field": "Region", "matching": {"filters": []}}, 2),
    ])
    def test_the_cost_counts_them(self, filter_shape, each):
        from qlik_sense_mcp_server.engine.queries import _filter_cost

        assert _filter_cost({"filters": [filter_shape]}) == each


class TestANoteFromInsideAPart:
    def test_it_reaches_the_warnings(self):
        class _Silent(_Engine):
            def _reply(self, request):
                if request["method"] == "EvaluateEx":
                    return {"qValue": {"qText": None, "qNumber": "NaN"}}
                return super()._reply(request)

        result = _Silent().run_queries(1, "app", [_query(
            metrics=[{"label": "share", "op": "divide", "of": [
                {"field": "Amount", "agg": "sum", "filters": [
                    {"field": "Region", "values": ["North"]}]},
                {"field": "Amount", "agg": "sum"}]}])])
        reply = result["results"][0]
        assert any("could not be checked" in w for w in reply["warnings"])


class TestAMeasureThatIsNotAMeasure:
    @pytest.mark.parametrize("stated", [5, None, ["Sum([Amount])"]])
    def test_the_hypercube_refuses_it(self, stated):
        from tests.test_hypercube import _FakeEngine

        result = _FakeEngine().create_hypercube(
            1, [{"field": "Region"}], [stated], 10)
        assert result["error_category"] == "invalid_argument"


class TestAMeasureObjectWithoutAnExpression:
    @pytest.mark.parametrize("stated", [{}, {"label": "Revenue"},
                                        {"expression": "   "}])
    def test_the_hypercube_refuses_it(self, stated):
        from tests.test_hypercube import _FakeEngine

        result = _FakeEngine().create_hypercube(
            1, [{"field": "Region"}], [stated], 10)
        assert result["error_category"] == "invalid_argument"
        assert "expression" in result["error"]


class TestTwoWaysToIgnoreTheGrouping:
    """They say different things about the same metric, and taking one
    silently answers a question nobody asked."""

    def test_both_at_once_are_refused(self):
        result = _Engine().run_queries(1, "app", [_query(
            metrics=[{"field": "Amount", "agg": "sum", "total": True,
                      "total_except": ["Region"]}])])
        assert result["results"][0]["error_category"] == "invalid_argument"

    def test_either_alone_still_works(self):
        for metric in ({"field": "Amount", "agg": "sum", "total": True},
                       {"field": "Amount", "agg": "sum",
                        "total_except": ["Region"]}):
            result = _Engine().run_queries(1, "app", [_query(
                metrics=[metric])])
            assert result["queries_failed"] == 0


class TestTheArithmeticShapeIsCheckedToo:
    def test_both_ways_of_ignoring_the_grouping_are_refused(self):
        result = _Engine().run_queries(1, "app", [_query(
            metrics=[{"label": "n", "op": "divide", "total": True,
                      "total_except": ["Region"], "of": [
                          {"field": "Amount", "agg": "sum"},
                          {"field": "Amount", "agg": "sum"}]}])])
        assert result["results"][0]["error_category"] == "invalid_argument"

    def test_one_of_them_still_works(self):
        result = _Engine().run_queries(1, "app", [_query(
            metrics=[{"label": "n", "op": "divide", "total": True, "of": [
                {"field": "Amount", "agg": "sum"},
                {"field": "Amount", "agg": "sum"}]}])])
        assert result["queries_failed"] == 0


class TestTotalOnTheOperationItself:
    """Stated on the operation it belongs to every part of it: a share of
    the whole is the same share whichever aggregation is written first. It
    used to be accepted and then ignored."""

    def test_it_reaches_every_part(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"label": "n", "op": "divide", "total": True, "of": [
                {"field": "Amount", "agg": "sum"},
                {"field": "Amount", "agg": "count"}]}]), 0)
        expression = plan["measures"][0]["expression"]
        assert "Sum(TOTAL [Amount])" in expression
        assert "Count(TOTAL [Amount])" in expression

    def test_total_except_reaches_them_too(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"label": "n", "op": "divide",
                      "total_except": ["Region"], "of": [
                          {"field": "Amount", "agg": "sum"},
                          {"field": "Amount", "agg": "sum"}]}]), 0)
        assert "TOTAL <[Region]>" in plan["measures"][0]["expression"]

    def test_a_part_keeps_what_it_says_for_itself(self):
        plan = _Engine()._plan_query(1, "app", _query(
            metrics=[{"label": "n", "op": "divide", "total": True, "of": [
                {"field": "Amount", "agg": "sum", "total": False},
                {"field": "Amount", "agg": "sum"}]}]), 0)
        expression = plan["measures"][0]["expression"]
        assert "Sum([Amount]) / Sum(TOTAL [Amount])" in expression

    @pytest.mark.parametrize("stated", ["yes", 1, "false"])
    def test_the_type_is_checked_here_as_well(self, stated):
        result = _Engine().run_queries(1, "app", [_query(
            metrics=[{"label": "n", "op": "divide", "total": stated, "of": [
                {"field": "Amount", "agg": "sum"},
                {"field": "Amount", "agg": "sum"}]}])])
        assert result["results"][0]["error_category"] == "invalid_argument"


class TestOneValueHasALength:
    """The ceiling counts how many things a request names and says nothing
    about how long each of them is. One value of a few megabytes builds a
    modifier of the same size on the connection every query shares."""

    def test_an_over_long_value_is_refused(self):
        from qlik_sense_mcp_server.engine.filters import MAX_VALUE_CHARS

        result = _Engine().run_queries(1, "app", [_query(
            filters=[{"field": "Region",
                      "values": ["x" * (MAX_VALUE_CHARS + 1)]}])])
        assert result["results"][0]["error_category"] == "limit_exceeded"

    def test_many_short_values_are_measured_together(self):
        from qlik_sense_mcp_server.engine.filters import MAX_FILTER_CHARS

        result = _Engine().run_queries(1, "app", [_query(
            filters=[{"field": "Region",
                      "values": ["x" * 1000
                                 for _ in range(MAX_FILTER_CHARS // 1000 + 1)]}])])
        # Refused either as a whole or per query, but refused.
        stated = (result.get("error_category")
                  or result["results"][0].get("error_category"))
        assert stated == "limit_exceeded"

    def test_a_measure_keeps_its_own_wider_ceiling(self):
        """A hand-written measure may run to MAX_EXPRESSION_CHARS; the
        value ceiling is about values, and covering measures with it made
        the wider limit unreachable."""
        from qlik_sense_mcp_server.engine.filters import MAX_VALUE_CHARS

        result = _Engine().run_queries(1, "app", [_query(
            metrics=[],
            measures=["Sum([Amount])" + " + Sum([Amount])"
                      * (MAX_VALUE_CHARS // 16)])])
        assert result["queries_failed"] == 0

    def test_a_measure_past_its_own_ceiling_is_refused(self):
        from qlik_sense_mcp_server.engine.queries import MAX_EXPRESSION_CHARS

        result = _Engine().run_queries(1, "app", [_query(
            metrics=[],
            measures=["Sum([Amount])" + " + Sum([Amount])"
                      * (MAX_EXPRESSION_CHARS // 16)])])
        assert result["results"][0]["error_category"] == "limit_exceeded"

    def test_ordinary_values_still_pass(self):
        result = _Engine().run_queries(1, "app", [_query(
            filters=[{"field": "Region", "values": ["North"]}])])
        assert result["queries_failed"] == 0
