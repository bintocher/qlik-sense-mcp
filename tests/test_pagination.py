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

    def test_total_comes_from_the_count_endpoint(self):
        """250 apps behind a server that only ever returns one page of them."""
        qrs = _Qrs(total=250, rows=[_app_row(i) for i in range(25)])
        result = qrs.get_comprehensive_apps(limit=25, offset=0)
        assert result["pagination"]["total_found"] == 250

    def test_has_more_reflects_the_real_total(self):
        qrs = _Qrs(total=250, rows=[_app_row(i) for i in range(250)])
        page = qrs.get_comprehensive_apps(limit=25, offset=200)["pagination"]
        assert page["has_more"] is True
        assert page["next_offset"] == 225

    def test_last_page_says_so(self):
        qrs = _Qrs(total=30, rows=[_app_row(i) for i in range(30)])
        page = qrs.get_comprehensive_apps(limit=25, offset=25)["pagination"]
        assert page["returned"] == 5
        assert page["has_more"] is False
        assert page["next_offset"] is None

    def test_empty_result_is_not_a_broken_page(self):
        qrs = _Qrs(total=0, rows=[])
        result = qrs.get_comprehensive_apps(limit=25, offset=0)
        assert result["apps"] == []
        assert result["pagination"]["total_found"] == 0
        assert result["pagination"]["has_more"] is False


class TestFilters:
    def test_published_filter_reaches_both_count_and_page(self):
        qrs = _Qrs(total=1, rows=[_app_row(0)])
        qrs.get_comprehensive_apps(limit=25, offset=0, published=True)
        assert "published eq true" in qrs.count_filter
        assert "published eq true" in qrs.table_params["filter"]

    def test_no_filter_when_publication_state_is_irrelevant(self):
        """published=None must not narrow the query at all."""
        qrs = _Qrs(total=1, rows=[_app_row(0)])
        qrs.get_comprehensive_apps(limit=25, offset=0, published=None)
        assert qrs.count_filter is None
        assert "filter" not in qrs.table_params

    def test_name_filter_is_escaped(self):
        qrs = _Qrs(total=0, rows=[])
        qrs.get_comprehensive_apps(limit=25, offset=0, name="O'Brien")
        assert "O''Brien" in qrs.count_filter

    def test_unpublished_app_reports_no_stream(self):
        qrs = _Qrs(total=1, rows=[_app_row(0, published=False, stream="Finance")])
        result = qrs.get_comprehensive_apps(limit=25, offset=0, published=False)
        assert result["apps"][0]["stream"] == ""


class TestFailuresAreNotEmptyPages:
    def test_count_failure_is_propagated(self):
        qrs = _Qrs(count_error="HTTP 500: Internal Server Error")
        result = qrs.get_comprehensive_apps(limit=25, offset=0)
        assert "error" in result
        assert "apps" not in result, "a failed call must not look like zero apps"

    def test_table_failure_is_propagated(self):
        qrs = _Qrs(total=10, table_error="HTTP 403: Forbidden")
        result = qrs.get_comprehensive_apps(limit=25, offset=0)
        assert "error" in result
        assert "apps" not in result


class TestRequestPlumbing:
    """Exercises the real _make_request, which the mocks above bypass.

    A None `params` — what an unfiltered count passes — used to blow up
    inside the catch-all and come back as "'NoneType' object does not
    support item assignment", which says nothing about anything.
    """

    class _Response:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self):
            return {"value": 7}

        def raise_for_status(self):
            pass

    class _Client:
        def __init__(self):
            self.calls = []

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return TestRequestPlumbing._Response()

    @pytest.fixture
    def api(self):
        api = QlikRepositoryAPI.__new__(QlikRepositoryAPI)
        api.client = self._Client()

        class _Cfg:
            auth_mode = "certificate"
            qlik_base_host = "https://qlik.example.com"
            repository_port = 4242
            virtual_proxy_prefix = ""
        api.config = _Cfg()
        api.jwt_session = None
        return api

    def test_none_params_are_accepted(self, api):
        assert api._count("app") == 7
        sent = api.client.calls[0][2]["params"]
        assert "xrfkey" in sent

    def test_filter_is_passed_through(self, api):
        api._count("app", "published eq true")
        sent = api.client.calls[0][2]["params"]
        assert sent["filter"] == "published eq true"
        assert "xrfkey" in sent

    def test_xrfkey_goes_into_the_header_too(self, api):
        api._count("app")
        kwargs = api.client.calls[0][2]
        assert kwargs["headers"]["X-Qlik-Xrfkey"] == kwargs["params"]["xrfkey"]


class TestPublishedTriState:
    @pytest.mark.parametrize("value,expected", [
        ("true", True), ("True", True), ("1", True), ("yes", True),
        ("false", False), ("0", False), ("no", False),
        ("both", None), ("all", None), ("", None), (None, None),
    ])
    def test_parsing(self, value, expected):
        assert srv._to_tribool(value) is expected

    def test_both_reaches_the_repository_as_no_filter(self, monkeypatch):
        """'both' used to fold into the default True and hide unpublished apps."""
        captured = {}

        class _Repo:
            def get_comprehensive_apps(self, limit, offset, name, stream, published):
                captured["published"] = published
                return {"apps": [], "pagination": {}}

        monkeypatch.setattr(context, "repo_api", _Repo())
        fn = getattr(srv.get_apps, "fn", srv.get_apps)
        json.loads(fn(published="both"))
        assert captured["published"] is None
