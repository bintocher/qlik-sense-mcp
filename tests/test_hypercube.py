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

    def test_without_sort_by_the_legacy_order_is_preserved(self):
        eng = _FakeEngine()
        eng.create_hypercube(1, DIMS, MEASURES, 10)
        assert eng.cube_def["qInterColumnSortOrder"] == [0, 1]

    def test_measure_sort_suppresses_missing_rows(self):
        eng = _FakeEngine()
        eng.create_hypercube(1, DIMS, MEASURES, 10, sort_by="GGR")
        assert eng.cube_def["qSuppressMissing"] is True

    def test_suppress_zero_is_opt_in(self):
        eng = _FakeEngine()
        eng.create_hypercube(1, DIMS, MEASURES, 10, suppress_zero=True)
        assert eng.cube_def["qSuppressZero"] is True

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

    def test_ranked_truncation_warning_explains_it_is_expected(self):
        eng = _FakeEngine()
        result = eng.create_hypercube(1, DIMS, MEASURES, 10, sort_by="GGR")
        assert "highest" in result["truncation_warning"]

    def test_unsorted_truncation_warning_flags_arbitrary_rows(self):
        eng = _FakeEngine()
        result = eng.create_hypercube(1, DIMS, MEASURES, 10)
        assert "NO sort was requested" in result["truncation_warning"]


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
