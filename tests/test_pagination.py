"""QRS paging is done by the server, and the totals must be the real ones.

`app/full` ignores skip/take and is itself truncated at the QRS
MaxRecordLimit (100 by default), so the old code — fetch everything, slice
locally — dropped every app past that cap and then reported the cap as
`total_found`. A client paging through `has_more` walked off the end of the
data without a single error.
"""

import json

import pytest

from qlik_sense_mcp_server import server as srv
from qlik_sense_mcp_server.tools import context
from qlik_sense_mcp_server.repository_api import QlikRepositoryAPI


class _Qrs(QlikRepositoryAPI):
    """Records requests and replays canned QRS answers."""

    def __init__(self, total=0, rows=None, count_error=None, table_error=None):
        self.requests = []
        self._total = total
        self._rows = rows if rows is not None else []
        self._count_error = count_error
        self._table_error = table_error

    def _make_request(self, method, endpoint, **kwargs):
        self.requests.append((method, endpoint, kwargs))
        if endpoint.endswith("/count"):
            if self._count_error:
                return {"error": self._count_error}
            return {"value": self._total}
        if endpoint.endswith("/table"):
            if self._table_error:
                return {"error": self._table_error}
            skip = kwargs["params"]["skip"]
            take = kwargs["params"]["take"]
            names = [c["name"] for c in kwargs["json"]["columns"]]
            window = self._rows[skip:skip + take]
            return {"columnNames": names, "rows": window}
        raise AssertionError(f"unexpected QRS call: {method} {endpoint}")

    @property
    def table_params(self):
        for method, endpoint, kwargs in self.requests:
            if endpoint.endswith("/table"):
                return kwargs["params"]
        raise AssertionError("no table request was made")

    @property
    def count_filter(self):
        for method, endpoint, kwargs in self.requests:
            if endpoint.endswith("/count"):
                return (kwargs.get("params") or {}).get("filter")
        raise AssertionError("no count request was made")


def _app_row(i, published=True, stream="Finance"):
    return [f"guid-{i}", f"App {i}", f"desc {i}", stream, published,
            "2026-08-01T00:00:00.000Z", "2026-08-02T00:00:00.000Z"]


class TestServerSidePaging:
    def test_page_is_requested_from_qrs_not_sliced_locally(self):
        qrs = _Qrs(total=250, rows=[_app_row(i) for i in range(250)])
        result = qrs.get_comprehensive_apps(limit=25, offset=100)

        assert qrs.table_params["skip"] == 100
        assert qrs.table_params["take"] == 25
        assert [a["name"] for a in result["apps"]][:2] == ["App 100", "App 101"]






class TestFilters:
    def test_published_filter_reaches_both_count_and_page(self):
        qrs = _Qrs(total=1, rows=[_app_row(0)])
        qrs.get_comprehensive_apps(limit=25, offset=0, published=True)
        assert "published eq true" in qrs.count_filter
        assert "published eq true" in qrs.table_params["filter"]





class TestFailuresAreNotEmptyPages:
    def test_count_failure_is_propagated(self):
        qrs = _Qrs(count_error="HTTP 500: Internal Server Error")
        result = qrs.get_comprehensive_apps(limit=25, offset=0)
        assert "error" in result
        assert "apps" not in result, "a failed call must not look like zero apps"







