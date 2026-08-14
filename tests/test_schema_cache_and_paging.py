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

    def test_a_reload_invalidates_it(self):
        """The reload timestamp is the only thing that changes the model."""
        engine = _Engine()
        engine.cached_fields(1, "app-1", "2026-08-11T03:00:00Z")
        engine.cached_fields(1, "app-1", "2026-08-12T03:00:00Z")
        assert engine.reads == 2

    def test_apps_do_not_share_an_entry(self):
        engine = _Engine()
        engine.cached_fields(1, "app-1", "x")
        engine.cached_fields(2, "app-2", "x")
        assert engine.reads == 2

    def test_forgetting_forces_a_reread(self):
        engine = _Engine()
        engine.cached_fields(1, "app-1", "x")
        engine.forget_schema("app-1")
        engine.cached_fields(1, "app-1", "x")
        assert engine.reads == 2

    def test_two_clients_do_not_share_a_cache(self):
        """A class-level dict would let one client answer another's question."""
        first, second = _Engine(), _Engine(fields=("Other",))
        first.cached_fields(1, "app-1", "x")
        model = second.cached_fields(1, "app-1", "x")
        assert [f["field_name"] for f in model["fields"]] == ["Other"]

    def test_an_empty_model_is_not_cached(self):
        """A failed read must not become the remembered truth."""
        engine = _Engine(fields=())
        engine.cached_fields(1, "app-1", "x")
        engine.cached_fields(1, "app-1", "x")
        assert engine.reads == 2

    def test_stale_entries_expire_even_without_a_reload(self, monkeypatch):
        engine = _Engine()
        engine.cached_fields(1, "app-1", "x")
        import time
        monkeypatch.setattr(
            time, "monotonic",
            lambda: engine._schema_store()["app-1"]["read_at"]
            + engine.SCHEMA_CACHE_TTL_SECONDS + 1)
        engine.cached_fields(1, "app-1", "x")
        assert engine.reads == 2


class TestDetailsCache:
    """`get_app_details` is the call every conversation starts with."""

    def test_forgetting_one_app_leaves_the_others(self):
        repository._DETAILS_CACHE.clear()
        repository._DETAILS_CACHE["a"] = {"reload_stamp": "x", "result": {"n": 1}}
        repository._DETAILS_CACHE["b"] = {"reload_stamp": "x", "result": {"n": 2}}
        repository.forget_app_details("a")
        assert set(repository._DETAILS_CACHE) == {"b"}

    def test_forgetting_everything(self):
        repository._DETAILS_CACHE["a"] = {"reload_stamp": "x", "result": {}}
        repository.forget_app_details()
        assert repository._DETAILS_CACHE == {}


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

    def test_the_last_page_says_there_is_not(self):
        result = self._engine(total_rows=5).create_hypercube(1, DIMS, MEASURES, 5)
        assert result["has_more"] is False
        assert result["next_offset"] is None

    def test_an_offset_is_echoed(self):
        result = self._engine(total_rows=20).create_hypercube(
            1, DIMS, MEASURES, 5, offset=5)
        assert result["offset"] == 5

    def test_the_page_starts_where_asked(self):
        engine = self._engine(total_rows=20)
        engine.create_hypercube(1, DIMS, MEASURES, 5, offset=10)
        created = [p for m, p in engine.sent if m == "CreateSessionObject"]
        fetch = created[0][0]["qHyperCubeDef"]["qInitialDataFetch"][0]
        assert fetch["qTop"] == 10

    def test_a_negative_offset_is_refused(self):
        """A page before the first is a request nobody can answer, and
        answering the first page instead returns data for a different
        question."""
        result = self._engine().create_hypercube(1, DIMS, MEASURES, 5, offset=-3)
        assert result["error_category"] == "invalid_argument"


class TestWholeAppSearch:
    class _Searcher(QlikEngineAPI):
        def __init__(self, groups):
            self.groups = groups
            self.asked = None
            self.ws_operation_timeout = 30.0

        def send_request(self, method, params=None, handle=-1, timeout=None):
            assert method == "SearchResults"
            self.asked = params
            return {"qResult": {"qSearchGroupArray": self.groups}}

    def test_a_match_names_the_field_and_the_real_spelling(self):
        engine = self._Searcher([{"qItems": [
            {"qIdentifier": "region_name",
             "qItemMatches": [{"qText": "Moskva"}]}]}])
        result = engine.search_app(1, "Mos")
        assert result["matches"][0]["field"] == "region_name"
        assert result["matches"][0]["values"] == ["Moskva"]

    def test_no_match_is_an_answer_not_an_error(self):
        engine = self._Searcher([])
        assert engine.search_app(1, "Moscow")["matches"] == []

    def test_named_fields_are_passed_to_engine(self):
        """Whole-app search takes ~30s on 10M rows; a named field is instant."""
        engine = self._Searcher([])
        engine.search_app(1, "Mos", fields=["region_name"])
        assert engine.asked[0]["qSearchFields"] == ["region_name"]

    def test_searching_everywhere_by_default(self):
        engine = self._Searcher([])
        engine.search_app(1, "Mos")
        assert engine.asked[0]["qSearchFields"] == []
