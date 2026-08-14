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




class TestColumnResolution:
    def test_columns_are_dimensions_then_measures(self):
        names = QlikEngineAPI._column_names(DIMS, MEASURES)
        assert names == ["clientid", "GGR"]










class TestMatrixToRows:
    def test_numbers_stay_numbers_and_nan_falls_back_to_text(self):
        pages = [{"qMatrix": [
            [{"qText": "North", "qNum": "NaN"}, {"qText": "1 250", "qNum": 1250.0}],
            [{"qText": "South", "qNum": "NaN"}, {"qText": "800", "qNum": 800.0}],
        ]}]
        rows = QlikEngineAPI._matrix_to_rows(pages, ["Region", "Sales"])
        assert rows == [["North", 1250.0], ["South", 800.0]]



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







