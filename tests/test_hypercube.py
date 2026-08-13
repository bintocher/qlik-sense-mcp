"""Tests for hypercube sorting, limits and the request echo (since v1.6.0)."""

import json

import pytest

from qlik_sense_mcp_server.engine_api import QlikEngineAPI
from qlik_sense_mcp_server import server as srv


DIMS = [{"field": "clientid"}]
MEASURES = [{"expression": "Sum(ggr)", "label": "GGR"}]


class TestSortOrderNormalisation:
    @pytest.mark.parametrize("value", ["desc", "DESC", " Descending ", "top", -1, "-1"])
    def test_descending_aliases(self, value):
        assert QlikEngineAPI._normalize_sort_order(value) == -1

    @pytest.mark.parametrize("value", ["asc", "ASCENDING", "bottom", 1, "1"])
    def test_ascending_aliases(self, value):
        assert QlikEngineAPI._normalize_sort_order(value) == 1

    @pytest.mark.parametrize("value", ["sideways", "", None, 5, True, False, 0])
    def test_rejects_unknown(self, value):
        assert QlikEngineAPI._normalize_sort_order(value) is None


class TestColumnResolution:
    def test_columns_are_dimensions_then_measures(self):
        names = QlikEngineAPI._column_names(DIMS, MEASURES)
        assert names == ["clientid", "GGR"]

    def test_measure_label_resolves_to_measure_column(self):
        assert QlikEngineAPI._resolve_sort_column("GGR", DIMS, MEASURES) == 1

    def test_measure_expression_resolves(self):
        assert QlikEngineAPI._resolve_sort_column("Sum(ggr)", DIMS, MEASURES) == 1

    def test_dimension_field_resolves(self):
        assert QlikEngineAPI._resolve_sort_column("clientid", DIMS, MEASURES) == 0

    def test_matching_ignores_case_and_brackets(self):
        assert QlikEngineAPI._resolve_sort_column("[ggr]", DIMS, MEASURES) == 1
        assert QlikEngineAPI._resolve_sort_column("CLIENTID", DIMS, MEASURES) == 0

    def test_measure_wins_over_same_named_dimension(self):
        dims = [{"field": "Amount"}]
        measures = [{"expression": "Sum(Amount)", "label": "Amount"}]
        assert QlikEngineAPI._resolve_sort_column("Amount", dims, measures) == 1

    def test_auto_generated_measure_name(self):
        measures = [{"expression": "Sum(x)"}]
        assert QlikEngineAPI._resolve_sort_column("Measure_0", DIMS, measures) == 1

    def test_integer_index_passthrough_and_bounds(self):
        assert QlikEngineAPI._resolve_sort_column(1, DIMS, MEASURES) == 1
        assert QlikEngineAPI._resolve_sort_column(9, DIMS, MEASURES) is None

    def test_unknown_name(self):
        assert QlikEngineAPI._resolve_sort_column("Revenue", DIMS, MEASURES) is None


class TestMatrixToRows:
    def test_numbers_stay_numbers_and_nan_falls_back_to_text(self):
        pages = [{"qMatrix": [
            [{"qText": "North", "qNum": "NaN"}, {"qText": "1 250", "qNum": 1250.0}],
            [{"qText": "South", "qNum": "NaN"}, {"qText": "800", "qNum": 800.0}],
        ]}]
        rows = QlikEngineAPI._matrix_to_rows(pages, ["Region", "Sales"])
        assert rows == [["North", 1250.0], ["South", 800.0]]

    def test_handles_empty_pages(self):
        assert QlikEngineAPI._matrix_to_rows([], ["a"]) == []
        assert QlikEngineAPI._matrix_to_rows(None, ["a"]) == []


class _FakeEngine(QlikEngineAPI):
    """Captures the hypercube definition without touching the network."""

    def __init__(self):
        self.sent = []
        self.ws_operation_timeout = 30.0
        self.ws_timeout_seconds = 30.0
        # Stand-in for a live socket: cleanup is skipped when it is None.
        self.ws = object()

    def send_request(self, method, params=None, handle=-1, timeout=None):
        self.sent.append((method, params))
        if method == "CreateSessionObject":
            return {"qReturn": {"qHandle": 7}}
        if method == "GetLayout":
            return {"qLayout": {"qHyperCube": {
                "qSize": {"qcx": 2, "qcy": 4200},
                "qDataPages": [{"qMatrix": [
                    [{"qText": "42", "qNum": 42.0}, {"qText": "999", "qNum": 999.0}],
                ]}],
                "qGrandTotalRow": [{"qText": "1000", "qNum": 1000.0}],
            }}}
        return {}

    @property
    def cube_def(self):
        for method, params in self.sent:
            if method == "CreateSessionObject":
                return params[0]["qHyperCubeDef"]
        raise AssertionError("CreateSessionObject was never sent")


class TestHypercubeDefinition:
    def test_sorting_by_measure_puts_measure_column_first(self):
        eng = _FakeEngine()
        eng.create_hypercube(1, DIMS, MEASURES, 10, sort_by="GGR", sort_order="desc")
        cube = eng.cube_def
        # The whole point: measure column (index 1) leads the sort order.
        assert cube["qInterColumnSortOrder"] == [1, 0]
        assert cube["qMeasures"][0]["qSortBy"] == {"qSortByNumeric": -1}

    def test_ascending_flips_only_the_direction(self):
        eng = _FakeEngine()
        eng.create_hypercube(1, DIMS, MEASURES, 10, sort_by="GGR", sort_order="asc")
        assert eng.cube_def["qInterColumnSortOrder"] == [1, 0]
        assert eng.cube_def["qMeasures"][0]["qSortBy"] == {"qSortByNumeric": 1}

    def test_sorting_by_dimension_sets_both_numeric_and_ascii(self):
        eng = _FakeEngine()
        eng.create_hypercube(1, DIMS, MEASURES, 10, sort_by="clientid", sort_order="asc")
        cube = eng.cube_def
        assert cube["qInterColumnSortOrder"] == [0, 1]
        criteria = cube["qDimensions"][0]["qDef"]["qSortCriterias"][0]
        assert criteria["qSortByNumeric"] == 1
        assert criteria["qSortByAscii"] == 1

    @pytest.mark.parametrize("expression,expected", [
        ("Sum(ggr)", "Sum(ggr)"),                      # this tool's documented shape
        ({"qv": "Sum(ggr)"}, "Sum(ggr)"),              # Qlik's own native shape
        ("", ""),
    ])
    def test_dimension_sort_expression_is_never_double_wrapped(self, expression, expected):
        """{"qv": ...} used to become {"qv": {"qv": ...}}, which Engine ignores."""
        eng = _FakeEngine()
        dims = [{"field": "clientid", "sort_by": {
            "qSortByExpression": -1, "qExpression": expression}}]
        eng.create_hypercube(1, dims, MEASURES, 10)
        criteria = eng.cube_def["qDimensions"][0]["qDef"]["qSortCriterias"][0]
        assert criteria["qExpression"] == {"qv": expected}

    def test_without_sort_by_the_legacy_order_is_preserved(self):
        eng = _FakeEngine()
        eng.create_hypercube(1, DIMS, MEASURES, 10)
        assert eng.cube_def["qInterColumnSortOrder"] == [0, 1]

    def test_cube_wide_missing_suppression_is_never_used(self):
        """Measured: qSuppressMissing drops exactly the
        NULL-dimension row and nothing else — the same row qNullSuppression
        already handles, but outside the caller's control. Two switches for
        one behaviour is how an explicit "keep the NULL group" ended up
        being overridden."""
        eng = _FakeEngine()
        eng.create_hypercube(1, DIMS, MEASURES, 10, sort_by="GGR")
        assert eng.cube_def["qSuppressMissing"] is False

    def test_keeping_null_groups_leaves_every_suppression_off(self):
        eng = _FakeEngine()
        eng.create_hypercube(1, DIMS, MEASURES, 10, sort_by="GGR",
                             exclude_null_dimensions=False)
        assert eng.cube_def["qSuppressMissing"] is False
        assert eng.cube_def["qDimensions"][0]["qNullSuppression"] is False
        # The ranking itself must be untouched by that.
        assert eng.cube_def["qInterColumnSortOrder"] == [1, 0]

    def test_suppress_zero_is_opt_in(self):
        eng = _FakeEngine()
        eng.create_hypercube(1, DIMS, MEASURES, 10, suppress_zero=True)
        assert eng.cube_def["qSuppressZero"] is True

    def test_null_dimension_rows_are_kept_by_default(self):
        """Facts with no value for the grouping field are still facts, so
        dropping them is the caller's statement to make. Both tools answer
        the same way; one of them used to drop them unasked."""
        eng = _FakeEngine()
        eng.create_hypercube(1, DIMS, MEASURES, 10, sort_by="GGR")
        assert eng.cube_def["qDimensions"][0]["qNullSuppression"] is False

    def test_null_dimension_rows_can_be_kept(self):
        eng = _FakeEngine()
        eng.create_hypercube(1, DIMS, MEASURES, 10, exclude_null_dimensions=False)
        assert eng.cube_def["qDimensions"][0]["qNullSuppression"] is False

    def test_null_suppression_applies_to_every_dimension(self):
        eng = _FakeEngine()
        dims = [{"field": "clientid"}, {"field": "Region"}]
        eng.create_hypercube(1, dims, MEASURES, 10,
                             exclude_null_dimensions=True)
        assert all(d["qNullSuppression"] is True for d in eng.cube_def["qDimensions"])

    def test_page_height_follows_limit(self):
        eng = _FakeEngine()
        eng.create_hypercube(1, DIMS, MEASURES, 25)
        assert eng.cube_def["qInitialDataFetch"][0]["qHeight"] == 25

    def test_input_dicts_are_not_mutated(self):
        eng = _FakeEngine()
        dims = [{"field": "clientid"}]
        measures = [{"expression": "Sum(ggr)", "label": "GGR"}]
        eng.create_hypercube(1, dims, measures, 10, sort_by="GGR")
        assert dims == [{"field": "clientid"}]
        assert measures == [{"expression": "Sum(ggr)", "label": "GGR"}]


class TestHypercubeResponse:
    def test_compact_response_shape(self):
        eng = _FakeEngine()
        result = eng.create_hypercube(1, DIMS, MEASURES, 10, sort_by="GGR")
        assert result["columns"] == ["clientid", "GGR"]
        assert result["rows"] == [[42.0, 999.0]]
        assert result["sorted_by"] == "GGR"
        assert result["sort_order"] == "desc"
        assert result["grand_total"] == [1000.0]
        assert "timings" in result
        # Raw layout is opt-in — it costs several times more tokens.
        assert "hypercube_data" not in result

    def test_raw_layout_opt_in(self):
        eng = _FakeEngine()
        result = eng.create_hypercube(1, DIMS, MEASURES, 10, include_raw_layout=True)
        assert "hypercube_data" in result
        assert result["hypercube_handle"] == 7

    def test_session_object_is_destroyed(self):
        eng = _FakeEngine()
        eng.create_hypercube(1, DIMS, MEASURES, 10)
        assert any(m == "DestroySessionObject" for m, _ in eng.sent)

    def test_session_object_is_destroyed_when_get_layout_raises(self):
        """A leak here pins the result set in Engine memory for the whole session."""
        class _Exploding(_FakeEngine):
            def send_request(self, method, params=None, handle=-1, timeout=None):
                if method == "GetLayout":
                    self.sent.append((method, params))
                    raise Exception("Engine API error: bad expression")
                return super().send_request(method, params, handle, timeout)

        eng = _Exploding()
        result = eng.create_hypercube(1, DIMS, MEASURES, 10)
        assert result["error_category"] == "engine_api_error"
        assert any(m == "DestroySessionObject" for m, _ in eng.sent)

    def test_no_cleanup_attempted_once_the_socket_is_dead(self):
        """After a timeout the socket is force-closed; there is nobody to talk to."""
        class _TimingOut(_FakeEngine):
            def send_request(self, method, params=None, handle=-1, timeout=None):
                if method == "GetLayout":
                    self.sent.append((method, params))
                    raise TimeoutError("WebSocket recv() timed out")
                return super().send_request(method, params, handle, timeout)

        eng = _TimingOut()
        result = eng.create_hypercube(1, DIMS, MEASURES, 10)
        assert result["error_category"] == "socket_timeout"
        assert eng.ws is None
        assert not any(m == "DestroySessionObject" for m, _ in eng.sent)

    def test_session_object_is_destroyed_on_malformed_layout(self):
        class _Malformed(_FakeEngine):
            def send_request(self, method, params=None, handle=-1, timeout=None):
                if method == "GetLayout":
                    self.sent.append((method, params))
                    return {"unexpected": "shape"}
                return super().send_request(method, params, handle, timeout)

        eng = _Malformed()
        eng.ws = object()
        result = eng.create_hypercube(1, DIMS, MEASURES, 10)
        assert result["error"] == "No hypercube in layout"
        assert any(m == "DestroySessionObject" for m, _ in eng.sent)




class _PagingEngine(_FakeEngine):
    """Engine that hands back short pages, as a loaded one does.

    GetLayout is allowed to trim qInitialDataFetch to whatever it feels
    like; the rest has to be collected with GetHyperCubeData. A client that
    trusts the first page returns fewer rows than asked for and says
    nothing about it.
    """

    def __init__(self, total_rows=10, first_page=2, page_size=3):
        super().__init__()
        self.total_rows = total_rows
        self.first_page = first_page
        self.page_size = page_size
        self.page_requests = []

    def _matrix(self, start, count):
        return [[{"qText": f"row{i}", "qNum": float(i)},
                 {"qText": str(i * 10), "qNum": float(i * 10)}]
                for i in range(start, min(start + count, self.total_rows))]

    def send_request(self, method, params=None, handle=-1, timeout=None):
        self.sent.append((method, params))
        if method == "CreateSessionObject":
            return {"qReturn": {"qHandle": 7}}
        if method == "GetLayout":
            return {"qLayout": {"qHyperCube": {
                "qSize": {"qcx": 2, "qcy": self.total_rows},
                "qDataPages": [{"qMatrix": self._matrix(0, self.first_page)}],
                "qGrandTotalRow": [{"qText": "1000", "qNum": 1000.0}],
            }}}
        if method == "GetHyperCubeData":
            page = params[1][0]
            self.page_requests.append((page["qTop"], page["qHeight"]))
            return {"qDataPages": [
                {"qMatrix": self._matrix(page["qTop"], min(page["qHeight"], self.page_size))}
            ]}
        return {}


class TestPageCompletion:
    def test_missing_rows_are_fetched(self):
        eng = _PagingEngine(total_rows=10, first_page=2, page_size=3)
        result = eng.create_hypercube(1, DIMS, MEASURES, 10)
        assert len(result["rows"]) == 10, "short first page was taken at face value"
        assert result["returned_rows"] == 10

    def test_rows_stay_in_order(self):
        eng = _PagingEngine(total_rows=8, first_page=2, page_size=3)
        rows = eng.create_hypercube(1, DIMS, MEASURES, 8)["rows"]
        assert [r[0] for r in rows] == [float(i) for i in range(8)]

    def test_paging_starts_after_what_the_layout_returned(self):
        eng = _PagingEngine(total_rows=10, first_page=2, page_size=3)
        eng.create_hypercube(1, DIMS, MEASURES, 10)
        assert eng.page_requests[0][0] == 2, "must not re-read rows already in hand"

    def test_never_asks_for_more_than_the_limit(self):
        eng = _PagingEngine(total_rows=1000, first_page=2, page_size=3)
        result = eng.create_hypercube(1, DIMS, MEASURES, 7)
        assert len(result["rows"]) == 7
        # No request may reach past the requested limit, however many
        # rows the server holds.
        assert all(top + height <= 7 for top, height in eng.page_requests)

    def test_page_height_respects_the_cell_cap(self):
        """Qlik rejects a page over 10000 cells with error 7009."""
        eng = _PagingEngine(total_rows=5000, first_page=0, page_size=4000)
        eng.create_hypercube(1, DIMS, MEASURES, 5000)
        for _, height in eng.page_requests:
            assert height * 2 <= QlikEngineAPI.HARD_MAX_CELLS

    def test_no_extra_call_when_the_first_page_is_complete(self):
        eng = _PagingEngine(total_rows=5, first_page=5, page_size=5)
        eng.create_hypercube(1, DIMS, MEASURES, 5)
        assert eng.page_requests == []

    def test_server_having_more_rows_than_the_limit_is_not_paged_past_it(self):
        eng = _PagingEngine(total_rows=4200, first_page=10, page_size=100)
        result = eng.create_hypercube(1, DIMS, MEASURES, 10)
        assert eng.page_requests == []
        assert result["truncation_warning"]

    def test_an_empty_page_stops_the_loop(self):
        """An Engine that keeps answering with nothing must not spin."""
        class _Stubborn(_PagingEngine):
            def send_request(self, method, params=None, handle=-1, timeout=None):
                if method == "GetHyperCubeData":
                    self.page_requests.append((0, 0))
                    return {"qDataPages": [{"qMatrix": []}]}
                return super().send_request(method, params, handle, timeout)

        eng = _Stubborn(total_rows=100, first_page=2)
        result = eng.create_hypercube(1, DIMS, MEASURES, 50)
        assert len(eng.page_requests) == 1
        assert len(result["rows"]) == 2
        assert result["truncation_warning"]

    def test_a_failing_page_keeps_what_was_already_read(self):
        class _Failing(_PagingEngine):
            def send_request(self, method, params=None, handle=-1, timeout=None):
                if method == "GetHyperCubeData":
                    raise Exception("Engine API error: calc-pages-too-large")
                return super().send_request(method, params, handle, timeout)

        eng = _Failing(total_rows=100, first_page=3)
        result = eng.create_hypercube(1, DIMS, MEASURES, 50)
        assert len(result["rows"]) == 3
        assert "error" not in result

    def test_a_failed_read_is_not_presented_as_a_deliberate_top_n(self):
        """Partial because Engine refused, and partial because the caller
        asked for 10 of 4200, must not read the same in the reply."""
        class _Failing(_PagingEngine):
            def send_request(self, method, params=None, handle=-1, timeout=None):
                if method == "GetHyperCubeData":
                    raise Exception("Engine API error: calc-pages-too-large")
                return super().send_request(method, params, handle, timeout)

        eng = _Failing(total_rows=100, first_page=3)
        result = eng.create_hypercube(1, DIMS, MEASURES, 50, sort_by="GGR")
        warning = result["truncation_warning"]
        assert "INCOMPLETE" in warning
        assert "calc-pages-too-large" in warning
        assert "highest" not in warning, "a failed read is not a ranking"
        assert result["timings"]["page_fetch_error"]

    def test_a_complete_ranked_page_says_nothing_about_failure(self):
        eng = _PagingEngine(total_rows=4200, first_page=10, page_size=10)
        result = eng.create_hypercube(1, DIMS, MEASURES, 10, sort_by="GGR")
        assert "INCOMPLETE" not in result["truncation_warning"]
        assert "highest" in result["truncation_warning"]
        assert "page_fetch_error" not in result["timings"]

    def test_extra_fetches_are_reported_in_timings(self):
        eng = _PagingEngine(total_rows=10, first_page=2, page_size=3)
        timings = eng.create_hypercube(1, DIMS, MEASURES, 10)["timings"]
        assert timings["extra_page_fetches"] >= 1
        assert "extra_pages_seconds" in timings

    def test_ranked_truncation_warning_explains_it_is_expected(self):
        # A complete page out of a much larger result: the truncation is
        # what the caller asked for, and the wording must say so.
        eng = _PagingEngine(total_rows=4200, first_page=10, page_size=10)
        result = eng.create_hypercube(1, DIMS, MEASURES, 10, sort_by="GGR")
        assert "highest" in result["truncation_warning"]
        assert "INCOMPLETE" not in result["truncation_warning"]

    def test_unsorted_truncation_warning_flags_arbitrary_rows(self):
        eng = _PagingEngine(total_rows=4200, first_page=10, page_size=10)
        result = eng.create_hypercube(1, DIMS, MEASURES, 10)
        assert "NO sort was requested" in result["truncation_warning"]

    def test_a_page_that_never_arrives_is_not_a_ranking(self):
        """Asked for 10, Engine stopped at 1: that is a short read, not a top-N."""
        eng = _FakeEngine()          # answers one row, then nothing
        result = eng.create_hypercube(1, DIMS, MEASURES, 10, sort_by="GGR")
        assert "INCOMPLETE" in result["truncation_warning"]
        assert result["timings"]["page_fetch_error"]

class TestHypercubeGuardRails:
    def test_unknown_sort_column_lists_available_columns(self):
        eng = _FakeEngine()
        result = eng.create_hypercube(1, DIMS, MEASURES, 10, sort_by="Revenue")
        assert result["error_category"] == "invalid_sort"
        assert result["available_columns"] == ["clientid", "GGR"]
        assert not eng.sent, "must fail before touching the Engine"

    def test_bad_sort_order_is_rejected(self):
        eng = _FakeEngine()
        result = eng.create_hypercube(1, DIMS, MEASURES, 10, sort_by="GGR",
                                      sort_order="sideways")
        assert result["error_category"] == "invalid_sort"

    def test_limit_above_hard_cap(self):
        eng = _FakeEngine()
        result = eng.create_hypercube(1, DIMS, MEASURES, 5001)
        assert result["error_category"] == "limit_exceeded"

    @pytest.mark.parametrize("bad_limit", [0, -7, 1.5, "10", True, None])
    def test_non_positive_limit_is_rejected(self, bad_limit):
        """max(1, ...) used to turn limit=0 into a single silently returned row."""
        eng = _FakeEngine()
        result = eng.create_hypercube(1, DIMS, MEASURES, bad_limit)
        assert result["error_category"] == "invalid_limit"
        assert not eng.sent, "must fail before touching the Engine"

    def test_cell_cap(self):
        eng = _FakeEngine()
        measures = [{"expression": f"Sum(f{i})", "label": f"M{i}"} for i in range(9)]
        result = eng.create_hypercube(1, DIMS, measures, 1000)
        assert result["error_category"] == "cell_cap_exceeded"
        assert result["hint"]


class TestRequestEcho:
    """A timeout must say WHICH query timed out, not just that it did."""

    def test_exception_reply_echoes_the_request(self):
        @srv._timed
        def boom(app_id: str, limit: int = 10):
            raise TimeoutError("WebSocket recv() timed out after 180.0s")

        parsed = json.loads(boom("app-1", limit=99))
        assert parsed["error_type"] == "TimeoutError"
        assert parsed["tool"] == "boom"
        assert parsed["request"] == {"app_id": "app-1", "limit": 99}

    def test_error_payload_reply_echoes_the_request(self):
        @srv._timed
        def failing(app_id: str, sort_by: str = None):
            return json.dumps({"error": "boom", "error_category": "socket_timeout"})

        parsed = json.loads(failing("app-2", sort_by="GGR"))
        assert parsed["request"] == {"app_id": "app-2", "sort_by": "GGR"}
        assert parsed["tool"] == "failing"

    def test_successful_reply_has_no_echo(self):
        @srv._timed
        def fine(app_id: str):
            return json.dumps({"rows": []})

        parsed = json.loads(fine("app-3"))
        assert "request" not in parsed
        assert "tool_call_seconds" in parsed


class TestEmptyResultIsExplained:
    """No rows at all is the strongest version of "this measure is empty".

    `suppress_zero=True` turns a full table of zeros into an empty result,
    and the per-column check needs rows to look at — so the warning went
    quiet exactly when the answer was emptiest.
    """

    def test_an_empty_result_is_flagged(self):
        eng = _PagingEngine(total_rows=0, first_page=0, page_size=3)
        result = eng.create_hypercube(1, DIMS, MEASURES, 10)
        assert result["rows"] == []
        assert any("no rows" in w for w in result["warnings"]), result["warnings"]

    def test_suppress_zero_is_named_as_a_possible_cause(self):
        eng = _PagingEngine(total_rows=0, first_page=0, page_size=3)
        result = eng.create_hypercube(1, DIMS, MEASURES, 10, suppress_zero=True)
        assert any("suppress_zero" in w for w in result["warnings"]), result["warnings"]

    def test_a_result_with_rows_is_not_flagged_as_empty(self):
        eng = _PagingEngine(total_rows=5, first_page=5, page_size=5)
        result = eng.create_hypercube(1, DIMS, MEASURES, 10)
        assert not any("no rows" in w for w in result.get("warnings", []))
