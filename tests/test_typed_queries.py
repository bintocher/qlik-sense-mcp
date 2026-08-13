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

    def send_requests_pipelined(self, requests, raise_on_error=True, timeout=None):
        self.batches.append([r["method"] for r in requests])
        replies = []
        for request in requests:
            replies.append(self._reply(request))
        return replies

    def _reply(self, request):
        method = request["method"]
        params = request.get("params") or []
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
            return {"qReturn": {"qHandle": 100 + len(self.batches)}}
        if method == "GetLayout":
            return {"qLayout": {"qHyperCube": self._cube()}}
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

    def _cube(self):
        width = len(self.rows[0]) if self.rows else 1
        return {
            "qSize": {"qcy": len(self.rows), "qcx": width},
            "qDimensionInfo": [{"qNumFormat": {"qType": "A"}}],
            "qMeasureInfo": [{"qNumFormat": {"qType": "F"}}] * (width - 1),
            "qGrandTotalRow": [{"qNum": 99.0, "qText": "99"}],
            "qDataPages": [{"qMatrix": [
                [{"qText": str(v), "qNum": v if isinstance(v, (int, float)) else "NaN"}
                 for v in row] for row in self.rows]}],
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
                if (m.group(1) or m.group(2)) not in
                ("Sum", "Count", "Avg", "Min", "Max", "Text", "Num", "If",
                 "DISTINCT", "Median", "Stdev")]

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
        engine._cube = lambda: dict(
            _Engine._cube(engine), qSize={"qcy": 500, "qcx": 2})
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
