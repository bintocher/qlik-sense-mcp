"""search_app: which fields it asks Engine to search, and what it says back.

Engine answers a field name it does not have with "Invalid parameters" on
`Options.SearchFields`, which names no field; a model that guessed a field
could not tell that from a broken server. And a search over every field
spends its time in the ids and timestamps with millions of values, where a
value someone names almost never is.
"""

import pytest

from qlik_sense_mcp_server.engine import QlikEngineAPI
from qlik_sense_mcp_server.engine.fields import SEARCH_MAX_DISTINCT_VALUES


MODEL = {"fields": [
    {"field_name": "Region", "table_name": "Facts", "distinct_values": 12},
    {"field_name": "OrderID", "table_name": "Facts", "distinct_values": 10_000_000},
    {"field_name": "Customer", "table_name": "Facts", "distinct_values": 40_000},
    {"field_name": "Customer", "table_name": "Customers", "distinct_values": 50_000},
    {"field_name": "$Table", "table_name": "", "distinct_values": 2, "is_system": True},
]}


class _Engine(QlikEngineAPI):
    def __init__(self, model=MODEL):
        self.ws_operation_timeout = 30.0
        self._cached_app_id = "app-1"
        self.model = model
        self.searches = []

    def get_fields(self, app_handle):
        return self.model

    def send_request(self, method, params=None, handle=-1, timeout=None):
        assert method == "SearchResults"
        self.searches.append(params)
        return {"qResult": {"qSearchGroupArray": [{"qItems": [
            {"qIdentifier": "Region", "qItemMatches": [{"qText": "North"}],
             "qTotalNumberOfMatches": 1}]}]}}


class TestWhatIsSearched:
    def test_without_fields_large_ones_are_left_out_and_named(self):
        engine = _Engine()
        reply = engine.search_app(1, "Nor")
        options = engine.searches[0][0]
        assert sorted(options["qSearchFields"]) == ["Customer", "Region"]
        assert reply["fields_not_searched"] == ["OrderID"]
        assert reply["matches"][0]["values"] == ["North"]

    def test_a_key_field_counts_by_its_largest_table(self):
        model = {"fields": [
            {"field_name": "Key", "distinct_values": 10},
            {"field_name": "Key", "distinct_values": SEARCH_MAX_DISTINCT_VALUES + 1},
        ]}
        reply = _Engine(model).search_app(1, "x")
        assert reply["fields_not_searched"] == ["Key"]
        assert reply["matches"] == []

    def test_named_fields_are_searched_as_named(self):
        engine = _Engine()
        engine.search_app(1, "Nor", fields=["[OrderID]", "Region"])
        assert engine.searches[0][0]["qSearchFields"] == ["OrderID", "Region"]

    def test_an_unknown_field_is_refused_before_engine(self):
        engine = _Engine()
        reply = engine.search_app(1, "Revenue", fields=["MeasureName", "Region"])
        assert reply["error_category"] == "field_not_found"
        assert reply["unknown_fields"] == ["MeasureName"]
        assert engine.searches == []

    def test_no_model_means_no_empty_field_list(self):
        engine = _Engine(model={"fields": []})
        engine.search_app(1, "Nor")
        assert "qSearchFields" not in engine.searches[0][0]

    def test_page_asks_for_the_requested_counts(self):
        engine = _Engine()
        engine.search_app(1, "2026-08", max_fields=15, max_values=3)
        assert engine.searches[0][2] == {"qOffset": 0, "qCount": 15,
                                         "qMaxNbrFieldMatches": 3}

    @pytest.mark.parametrize("bad", [0, -1, "5", True])
    def test_counts_must_be_positive_numbers(self, bad):
        reply = _Engine().search_app(1, "x", max_fields=bad)
        assert reply["error_category"] == "invalid_argument"


class TestNotes:
    def test_reply_explains_selection_scope_and_slow_first_search(self):
        notes = " ".join(_Engine().search_app(1, "Nor")["search_notes"])
        assert "current selections" in notes
        assert "CreateSearchIndexOnReload" in notes
