"""A filter that is reported as applied must actually be applied.

The failure this covers is the worst kind this server exists to prevent:
a reply that carries both a wrong number and a control value saying the
number is right. It happened for a measure the caller wrote by hand — the
filter went into the control probes but never into the expression, so the
query aggregated every row while `period_check` reported the period.
"""

import pytest

from tests.test_typed_queries import _Engine, _query


class TestHandWrittenMeasures:
    def test_a_marked_measure_gets_the_filter(self):
        engine = _Engine()
        plan = engine._plan_query(1, "app", _query(
            metrics=[],
            measures=[{"expression": "Sum({filter} [Amount])", "label": "Revenue"}],
            filters=[{"field": "OrderDate", "period": "2011"}]), 0)
        assert "{<" in plan["measures"][0]["expression"]
        assert "{filter}" not in plan["measures"][0]["expression"]

    def test_an_unmarked_measure_with_a_filter_is_refused(self):
        """Rather than aggregating everything and reporting a period."""
        engine = _Engine()
        plan = engine._plan_query(1, "app", _query(
            metrics=[],
            measures=[{"expression": "Sum([Amount])", "label": "Revenue"}],
            filters=[{"field": "OrderDate", "period": "2011"}]), 0)
        assert plan["error_category"] == "invalid_argument"
        assert "where the filter goes" in plan["error"]

    def test_the_refusal_offers_both_ways_out(self):
        engine = _Engine()
        plan = engine._plan_query(1, "app", _query(
            metrics=[],
            measures=[{"expression": "Sum([Amount])"}],
            filters=[{"field": "OrderDate", "period": "2011"}]), 0)
        actions = " ".join(plan["next_actions"])
        assert "{filter}" in actions
        assert "metric" in actions

    def test_without_filters_a_hand_written_measure_is_untouched(self):
        engine = _Engine()
        plan = engine._plan_query(1, "app", _query(
            metrics=[], measures=[{"expression": "Sum([Amount])/Count([Amount])",
                                   "label": "AOV"}]), 0)
        assert plan["measures"][0]["expression"] == "Sum([Amount])/Count([Amount])"

    def test_a_batch_reports_the_filter_only_where_it_applies(self):
        """The whole batch must not fail because one query is malformed."""
        engine = _Engine()
        result = engine.run_queries(1, "app", [
            _query(filters=[{"field": "OrderDate", "period": "2011"}]),
            _query(metrics=[], measures=[{"expression": "Sum([Amount])"}],
                   filters=[{"field": "OrderDate", "period": "2011"}]),
        ])
        assert result["queries_run"] == 1
        assert result["results"][1]["error_category"] == "invalid_argument"


class TestBatchIsolation:
    def test_a_malformed_offset_only_fails_its_own_query(self):
        engine = _Engine()
        result = engine.run_queries(1, "app", [
            _query(), _query(offset="next"), _query()])
        assert result["queries_run"] == 2
        assert result["results"][1]["error"]

    def test_an_unknown_field_fails_only_the_query_that_named_it(self):
        engine = _Engine()
        result = engine.run_queries(1, "app", [
            _query(),
            _query(metrics=[{"field": "Nope", "agg": "sum"}]),
            _query(),
        ])
        assert result["queries_run"] == 2
        assert result["results"][1]["error_category"] == "field_not_found"
        assert "error" not in result["results"][0]
        assert "error" not in result["results"][2]


class TestValidationIsOneBatch:
    def test_five_queries_are_checked_together(self):
        """Checking each query separately turned a three-round-trip batch
        into thirteen."""
        engine = _Engine()
        engine.run_queries(1, "app", [_query() for _ in range(5)])
        expands = [b for b in engine.batches if b[0] == "ExpandExpression"]
        checks = [b for b in engine.batches if b[0] == "CheckExpression"]
        assert len(expands) == 1
        assert len(checks) == 1

    def test_the_whole_batch_costs_five_round_trips(self):
        """Expand, check, create, layout, destroy — however many queries
        the call carries."""
        engine = _Engine()
        engine.run_queries(1, "app", [_query() for _ in range(5)])
        assert len(engine.batches) == 5


class TestImpossibleDates:
    @pytest.mark.parametrize("bound", ["2024-02-30", "2024-13", "31.04.2024",
                                       "0000-00-00"])
    def test_a_date_that_does_not_exist_is_refused_as_a_bound(self, bound):
        engine = _Engine()
        plan = engine._plan_query(1, "app", _query(
            filters=[{"field": "OrderDate", "period": bound}]), 0)
        assert plan["error_category"] == "invalid_period"
        assert plan["accepted_forms"]

    @pytest.mark.parametrize("bound", ["2024-02-29", "2024-12", "29.02.2024"])
    def test_a_date_that_does_exist_still_works(self, bound):
        engine = _Engine()
        plan = engine._plan_query(1, "app", _query(
            filters=[{"field": "OrderDate", "period": bound}]), 0)
        assert plan.get("error_category") != "invalid_period"


class TestEachQueryIsJudgedOnItsOwn:
    """Two mistakes of different kinds in one batch, and a name that is a
    substring of a good one — the cases a batch-wide verdict gets wrong."""

    def test_two_mistakes_of_different_kinds_both_land(self):
        engine = _Engine(known=("Region", "Amount"),
                         syntax_errors={"Sum([Amount]) AS t": "Garbage after"})
        result = engine.run_queries(1, "app", [
            _query(metrics=[], measures=[{"expression": "Sum([Amount]) AS t"}]),
            _query(metrics=[{"field": "Nope", "agg": "sum"}]),
            _query(),
        ])
        assert result["results"][0]["error_category"] == "invalid_expression"
        assert result["results"][1]["error_category"] == "field_not_found"
        assert "error" not in result["results"][2]
        assert result["queries_run"] == 1

    def test_a_good_name_containing_a_bad_one_still_runs(self):
        """`NopeAmount` exists; `Nope` does not. Blaming by substring
        refused the query that used the real field."""
        engine = _Engine(known=("Region", "NopeAmount"))
        result = engine.run_queries(1, "app", [
            _query(metrics=[{"field": "Nope", "agg": "sum"}]),
            _query(metrics=[{"field": "NopeAmount", "agg": "sum"}]),
        ])
        assert result["results"][0]["error_category"] == "field_not_found"
        assert "error" not in result["results"][1]

    def test_engine_is_still_asked_only_once_for_the_batch(self):
        engine = _Engine()
        engine.run_queries(1, "app", [_query() for _ in range(4)])
        assert len([b for b in engine.batches if b[0] == "CheckExpression"]) == 1


class TestBatchSize:
    def test_a_batch_over_the_cap_is_refused_before_anything_opens(self):
        from qlik_sense_mcp_server.engine.queries import MAX_QUERIES_PER_CALL

        engine = _Engine()
        result = engine.run_queries(
            1, "app", [_query() for _ in range(MAX_QUERIES_PER_CALL + 1)])
        assert result["error_category"] == "limit_exceeded"
        assert engine.batches == []

    def test_a_batch_at_the_cap_runs(self):
        from qlik_sense_mcp_server.engine.queries import MAX_QUERIES_PER_CALL

        engine = _Engine()
        result = engine.run_queries(
            1, "app", [_query() for _ in range(MAX_QUERIES_PER_CALL)])
        assert result["queries_run"] == MAX_QUERIES_PER_CALL


class TestLimitIsNotGuessed:
    @pytest.mark.parametrize("limit", [0, -1, True, "10", 2.5])
    def test_a_limit_that_is_not_a_row_count_is_refused(self, limit):
        engine = _Engine()
        reply = engine.run_queries(
            1, "app", [_query(limit=limit)])["results"][0]
        assert reply["error_category"] == "invalid_limit"

    def test_an_absent_limit_still_defaults(self):
        from qlik_sense_mcp_server.engine.queries import DEFAULT_QUERY_LIMIT

        engine = _Engine()
        plan = engine._plan_query(1, "app", _query(), 0)
        plan.pop("limit")
        plan["limit"] = None
        assert engine._shape_cube(plan)["limit"] == DEFAULT_QUERY_LIMIT


class TestObjectsAreAlwaysReleased:
    def test_a_transport_failure_mid_batch_still_releases(self):
        """A leak pins each result set in Engine memory for the rest of a
        session this server keeps open by design."""
        class _Failing(_Engine):
            def send_requests_pipelined(self, requests, raise_on_error=True,
                                        timeout=None):
                if requests[0]["method"] == "GetLayout":
                    raise TimeoutError("WebSocket recv() timed out")
                return super().send_requests_pipelined(
                    requests, raise_on_error, timeout)

        engine = _Failing()
        with pytest.raises(TimeoutError):
            engine.run_queries(1, "app", [_query(), _query()])
        assert len(engine.destroyed) == 2

    def test_nothing_is_sent_when_the_socket_is_gone(self):
        class _Dead(_Engine):
            ws = None

            def send_requests_pipelined(self, requests, raise_on_error=True,
                                        timeout=None):
                if requests[0]["method"] == "GetLayout":
                    raise TimeoutError("WebSocket recv() timed out")
                return super().send_requests_pipelined(
                    requests, raise_on_error, timeout)

        engine = _Dead()
        with pytest.raises(TimeoutError):
            engine.run_queries(1, "app", [_query()])
        assert engine.destroyed == []


class TestFieldNamesAreOneIdentifier:
    """A metric names a field. Written into an expression unchecked, a
    name carrying a bracket turns one aggregation into two — and the
    second one carries no filter while `period_check` reports success."""

    @pytest.mark.parametrize("field", [
        "Amount]) + Sum([Amount",
        "Amount] , [Region",
        "[Amount]) * 2 + Count([Amount",
    ])
    def test_a_name_with_a_bracket_is_refused_in_a_metric(self, field):
        engine = _Engine()
        plan = engine._plan_query(1, "app", _query(
            metrics=[{"field": field, "agg": "sum"}]), 0)
        assert plan["error_category"] == "invalid_argument"

    def test_a_name_with_a_bracket_is_refused_in_a_grouping(self):
        engine = _Engine()
        plan = engine._plan_query(1, "app", _query(
            group_by=["Region]) + ([Amount"]), 0)
        assert plan["error_category"] == "invalid_argument"

    def test_an_ordinary_name_still_works_bracketed_or_not(self):
        engine = _Engine()
        assert "error" not in engine._plan_query(1, "app", _query(
            group_by=["[Region]"], metrics=[{"field": "[Amount]", "agg": "sum"}]), 0)


class TestExpressionBudget:
    def test_one_query_holding_too_many_measures_is_refused(self):
        from qlik_sense_mcp_server.engine.queries import MAX_EXPRESSIONS_PER_CALL

        engine = _Engine()
        result = engine.run_queries(1, "app", [_query(
            metrics=[{"field": "Amount", "agg": "sum", "label": f"m{i}"}
                     for i in range(MAX_EXPRESSIONS_PER_CALL + 1)])])
        assert result["error_category"] == "limit_exceeded"
        assert engine.batches == []

    def test_an_ordinary_batch_is_well_inside_it(self):
        engine = _Engine()
        result = engine.run_queries(1, "app", [_query() for _ in range(5)])
        assert result["queries_run"] == 5


class TestMarkerWithoutFilters:
    def test_a_marker_with_no_filter_is_refused_rather_than_sent(self):
        """Qlik has no meaning for it and would read it as text."""
        engine = _Engine()
        plan = engine._plan_query(1, "app", _query(
            metrics=[], measures=[{"expression": "Sum({filter} [Amount])"}]), 0)
        assert plan["error_category"] == "invalid_argument"
        assert "drop the marker" in plan["hint"]


class TestUnusableNumericBounds:
    @pytest.mark.parametrize("bound", [float("inf"), float("-inf"),
                                       float("nan"), "inf", "nan"])
    def test_a_bound_qlik_cannot_compare_is_refused(self, bound):
        engine = _Engine()
        plan = engine._plan_query(1, "app", _query(
            filters=[{"field": "Amount", "from": bound}]), 0)
        assert plan["error_category"] == "invalid_filter"


class TestEmptyIntersection:
    """Two filters that each select something can select nothing together.

    Engine answers Min/Max over an empty set with the string "NaN".
    Comparing that to a day number raised TypeError and took the whole
    batch down with it — the one thing a batch promises not to do.
    """

    class _Empty(_Engine):
        def _evaluate(self, expression):
            if "Min(" in expression or "Max(" in expression:
                return {"qText": "-", "qNumber": "NaN", "qIsNumeric": False}
            return super()._evaluate(expression)

    def test_the_batch_survives_and_says_so(self):
        engine = self._Empty()
        result = engine.run_queries(1, "app", [
            _query(filters=[{"field": "OrderDate", "period": "2011"}]),
            _query(),
        ])
        assert result["queries_run"] == 2
        check = result["results"][0]["period_check"][0]
        assert check["filter_applied"] is None
        assert "no rows" in check["note"]

    def test_a_number_that_arrives_as_text_is_still_a_number(self):
        engine = _Engine()
        engine._evaluate = lambda expr: {"qText": "5", "qNumber": "5",
                                         "qIsNumeric": True}
        values = engine.evaluate_expressions(1, ["=Count(1)"])
        assert values[0]["number"] == 5.0


class TestFilterValuesCountTowardsTheBudget:
    def test_a_filter_holding_too_many_values_is_refused(self):
        from qlik_sense_mcp_server.engine.queries import MAX_EXPRESSIONS_PER_CALL

        engine = _Engine()
        result = engine.run_queries(1, "app", [_query(
            filters=[{"field": "Region",
                      "values": [f"v{i}" for i in range(MAX_EXPRESSIONS_PER_CALL)]}])])
        assert result["error_category"] == "limit_exceeded"
        assert engine.batches == []

    def test_a_handful_of_values_still_works(self):
        engine = _Engine()
        result = engine.run_queries(1, "app", [_query(
            filters=[{"field": "Region", "values": ["North", "South"]}])])
        assert result["queries_failed"] == 0


class TestFilterFieldNames:
    def test_a_bracket_in_a_filter_field_is_refused(self):
        """It is written into a set modifier, where Qlik has no escape
        for one."""
        engine = _Engine()
        plan = engine._plan_query(1, "app", _query(
            filters=[{"field": "pri]ce", "from": 400}]), 0)
        assert plan["error_category"] == "invalid_filter"


class TestRangeControl:
    def test_a_numeric_range_reports_what_the_result_holds(self):
        engine = _Engine(period_bounds=(410, 480))
        result = engine.run_queries(1, "app", [_query(
            filters=[{"field": "Amount", "from": 400, "to": 500}])])
        check = result["results"][0]["period_check"][0]
        assert check["filter_applied"] is True

    def test_a_value_outside_the_range_says_the_filter_did_not_apply(self):
        engine = _Engine(period_bounds=(10, 9000))
        result = engine.run_queries(1, "app", [_query(
            filters=[{"field": "Amount", "from": 400, "to": 500}])])
        reply = result["results"][0]
        assert reply["period_check"][0]["filter_applied"] is False
        assert any("did not narrow" in w for w in reply["warnings"])

    def test_the_named_upper_bound_is_included(self):
        """`to: 500` means 500 counts; a period's upper bound does not."""
        engine = _Engine(period_bounds=(400, 500))
        result = engine.run_queries(1, "app", [_query(
            filters=[{"field": "Amount", "from": 400, "to": 500}])])
        assert result["results"][0]["period_check"][0]["filter_applied"] is True
