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




































































