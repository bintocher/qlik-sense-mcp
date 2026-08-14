"""A failed call must not be reported as an empty or missing result.

These are the branches a live server will not produce on demand: QRS
returning 500, the proxy refusing the connection, the JWT bootstrap
failing. Every one of them used to surface as "App not found by provided
app_id" — which sends a model looking for a different app id when the real
answer is that Qlik is unreachable. Caught in an e2e run while the Qlik
proxy happened to be restarting.
"""

import json

import pytest

from qlik_sense_mcp_server import server as srv
from qlik_sense_mcp_server.tools import context


class _Repo:
    """Stands in for QlikRepositoryAPI, returning whatever the test wants."""

    def __init__(self, by_id=None, search=None):
        self._by_id = by_id
        self._search = search
        self.calls = []

    def get_app_by_id(self, app_id):
        self.calls.append(("get_app_by_id", app_id))
        return self._by_id

    def get_comprehensive_apps(self, *args, **kwargs):
        self.calls.append(("get_comprehensive_apps", args))
        return self._search


@pytest.fixture
def repo(monkeypatch):
    def _install(**kwargs):
        fake = _Repo(**kwargs)
        monkeypatch.setattr(context, "repo_api", fake)
        monkeypatch.setattr(context, "config", getattr(srv, "config", None) or object())
        # _check() only guards against an unconfigured server.
        return fake
    return _install


def _details(**kwargs):
    fn = getattr(srv.get_app_details, "fn", srv.get_app_details)
    return json.loads(fn(**kwargs))


class TestLookupByIdFailures:
    @pytest.mark.parametrize("failure", [
        "HTTP 500: Internal Server Error",
        "JWT bootstrap failed: csrftoken request failed: [WinError 10061]",
        "Server disconnected without sending a response.",
        "HTTP 403: Forbidden",
    ])
    def test_transport_failure_is_not_reported_as_a_missing_app(self, repo, failure):
        repo(by_id={"error": failure})
        result = _details(app_id="9b15dfb6-78da-4ff2-8eb2-aa2e469ec43e")

        assert result["error_category"] == "repository_error"
        assert failure in result["error"], "the original cause must survive"
        assert "not found" not in result["error"].lower(), (
            "a server outage reported as a missing app sends the caller "
            "hunting for a different id")




def result_hint(payload):
    return payload.get("hint")


class TestLookupByNameFailures:
    def test_search_failure_is_not_reported_as_no_matches(self, repo):
        repo(search={"error": "HTTP 500: Internal Server Error"})
        result = _details(name="Sales")
        assert result["error_category"] == "repository_error"
        assert "No apps found" not in result["error"]

