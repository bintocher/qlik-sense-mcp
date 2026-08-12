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
        import re
        return [m.group(1) or m.group(2) for m in
                re.finditer(r"\[([^\]]+)\]|\b([A-Z][A-Za-z]+)\b", expression)
                if (m.group(1) or m.group(2)) not in
                ("Sum", "Count", "Avg", "Min", "Max", "Text", "Num", "If",
                 "DISTINCT", "Median", "Stdev")]

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

    def test_a_limit_wider_than_the_cell_cap_is_brought_down(self):
        engine = _Engine()
        plan = engine._plan_query(1, "app", _query(limit=99999), 0)
        shaped = engine._shape_cube(plan)
        assert shaped["limit"] * 2 <= engine.HARD_MAX_CELLS
