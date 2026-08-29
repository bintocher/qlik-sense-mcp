"""Three things that stop the caller repeating itself.

A model asking about one app asks the same structural questions over and
over: what are the fields, what do the values look like, is this name
real. None of that changes until the app reloads, so it is read once.

Paging replaces a refusal. A result wider than the row cap used to end the
conversation — the caller was told to redesign a query that was fine.

And a value can be looked up without knowing which field holds it, which
is the one thing a caller cannot work out on its own.
"""

import pytest

from qlik_sense_mcp_server.engine_api import QlikEngineAPI
from qlik_sense_mcp_server.tools import repository


class _Engine(QlikEngineAPI):
    """Counts how often the data model is actually read."""

    def __init__(self, fields=("Region", "Sales")):
        self.reads = 0
        self._fields = fields

    def get_fields(self, app_handle):
        self.reads += 1
        return {"fields": [{"field_name": n} for n in self._fields],
                "tables_count": 1, "total_fields": len(self._fields)}


class TestSchemaCache:
    def test_the_model_is_read_once_per_app(self):
        engine = _Engine()
        for _ in range(5):
            engine.cached_fields(1, "app-1", "2026-08-11T03:00:00Z")
        assert engine.reads == 1








class TestDetailsCache:
    """`get_app_details` is the call every conversation starts with."""

    def test_forgetting_one_app_leaves_the_others(self):
        repository._DETAILS_CACHE.clear()
        repository._DETAILS_CACHE["a"] = {"reload_stamp": "x", "result": {"n": 1}}
        repository._DETAILS_CACHE["b"] = {"reload_stamp": "x", "result": {"n": 2}}
        repository.forget_app_details("a")
        assert set(repository._DETAILS_CACHE) == {"b"}



DIMS = [{"field": "Region"}]
MEASURES = [{"expression": "Sum(Sales)", "label": "v"}]


class TestPaging:
    """A page, not a refusal."""

    @staticmethod
    def _engine(total_rows=20):
        from tests.test_hypercube import _PagingEngine
        return _PagingEngine(total_rows=total_rows, first_page=5, page_size=5)

    def test_the_reply_says_there_is_more(self):
        result = self._engine(total_rows=20).create_hypercube(1, DIMS, MEASURES, 5)
        assert result["has_more"] is True
        assert result["next_offset"] == 5






