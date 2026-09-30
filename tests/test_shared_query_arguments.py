"""Arguments stated beside `queries` are shared by the queries.

A model sent its measures and filters once for three queries: two came
back "asks for no measure", and a query with measures of its own would have
been computed without the filters it had stated - they were ignored.
"""

import json
from contextlib import contextmanager

import pytest

from qlik_sense_mcp_server.tools import engine as engine_tools


class _Engine:
    def __init__(self):
        self.queries = None

    @contextmanager
    def transaction(self):
        yield self

    def ensure_app(self, app_id, no_data=False):
        return 1

    def run_queries(self, app_handle, app_id, queries):
        self.queries = queries
        return {"results": []}


@pytest.fixture
def engine(monkeypatch):
    fake = _Engine()
    monkeypatch.setattr(engine_tools.context, "engine_api", fake)
    monkeypatch.setattr(engine_tools, "_check", lambda: None)
    return fake


def test_queries_take_the_shared_measures_and_filters(engine):
    engine_tools.engine_query(
        "app", queries=[{"id": "total"}, {"id": "north",
                                          "filters": [{"field": "Region", "values": ["N"]}]}],
        measures=[{"master": "Revenue"}],
        filters=[{"field": "Month", "values": ["2026-08"]}])
    total, north = engine.queries
    assert total["measures"] == [{"master": "Revenue"}]
    assert total["filters"] == [{"field": "Month", "values": ["2026-08"]}]
    # A query's own key wins over the shared one.
    assert north["filters"] == [{"field": "Region", "values": ["N"]}]
    assert north["measures"] == [{"master": "Revenue"}]


def test_nothing_shared_leaves_the_queries_as_they_are(engine):
    engine_tools.engine_query("app", queries=[{"id": "a", "group_by": ["Region"]}])
    assert engine.queries == [{"id": "a", "group_by": ["Region"]}]
