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
        """Expand, check, create, layout, destroy — however many queries."""
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
