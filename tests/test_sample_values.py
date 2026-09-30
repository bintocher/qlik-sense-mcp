"""Show the caller what the values look like, so it stops guessing them.

Qlik answers a wrong guess with a number, not an error: a measure filtered
on `region_name={'Moscow'}` where the data says `Moskva` returns a clean
table of zeros, and a date filtered as a serial number where the field
displays `01.01.2024` returns the same. Measured against a real LLM, that
guessing cost ten tool calls and two minutes on a question that takes two
once the values are in front of it.

So `get_app_details` lists the values of low-cardinality fields outright
and shows a few dates in their display format. Cost on a 10M-row app:
+0.3s and +600 characters.
"""

import pytest

from qlik_sense_mcp_server.engine import QlikEngineAPI
from qlik_sense_mcp_server.tools import repository


class _Engine(QlikEngineAPI):
    """Answers list-object batches; records what was asked for."""

    def __init__(self, values=None, fail=False):
        self.values = values or {}
        self.fail = fail
        self.asked = []
        self.destroyed = 0

    def send_requests_pipelined(self, requests, raise_on_error=True):
        if self.fail:
            raise RuntimeError("Engine unavailable")
        method = requests[0]["method"]
        if method == "CreateSessionObject":
            out = []
            for req in requests:
                definition = req["params"][0]
                field = definition["qListObjectDef"]["qDef"]["qFieldDefs"][0]
                height = definition["qListObjectDef"]["qInitialDataFetch"][0]["qHeight"]
                self.asked.append((field, height))
                out.append({"qReturn": {"qHandle": 100 + len(out)}})
            return out
        if method == "GetLayout":
            out = []
            for field, _height in self.asked:
                rows = [[{"qText": v}] for v in self.values.get(field, [])]
                out.append({"qLayout": {"qListObject": {"qDataPages": [{"qMatrix": rows}]}}})
            return out
        if method == "DestroySessionObject":
            self.destroyed += len(requests)
            return [{} for _ in requests]
        raise AssertionError(method)


class TestBatchRead:
    def test_values_come_back_per_field(self):
        engine = _Engine({"Region": ["North", "South"], "Year": ["2024", "2025"]})
        result = engine.get_field_values_batch(1, [("Region", 25), ("Year", 25)])
        assert result == {"Region": ["North", "South"], "Year": ["2024", "2025"]}






class TestAttachToFields:
    @staticmethod
    def _fields():
        return [
            {"field_name": f"flag_{i}", "distinct_values": 2, "tags": ["$numeric"]}
            for i in range(40)
        ] + [
            {"field_name": "region_name", "distinct_values": 10, "tags": ["$text"]},
            {"field_name": "client_id", "distinct_values": 200_000, "tags": ["$text"]},
            {"field_name": "order_date", "distinct_values": 730, "tags": ["$date", "$numeric"]},
            {"field_name": "empty_field", "distinct_values": 0, "tags": []},
        ]

    def test_every_short_field_gets_its_values(self, monkeypatch):
        """No cap on how many: 48 short fields left out cost 34 extra calls."""
        engine = _Engine({"region_name": ["Moskva", "Kazan"]})
        monkeypatch.setattr(repository.context, "engine_api", engine)
        monkeypatch.setattr(engine, "get_field_edges_batch",
                            lambda handle, names, count=5: {"order_date": {
                                "lowest": ["01.01.2024"], "highest": ["31.12.2025"]}},
                            raising=False)
        values, ranges = repository._values_and_ranges(1, self._fields())
        assert values["region_name"] == ["Moskva", "Kazan"]
        asked = [name for name, _height in engine.asked]
        assert len(asked) == 41 and "client_id" not in asked and "empty_field" not in asked
        assert ranges == {"order_date": {"lowest": ["01.01.2024"], "highest": ["31.12.2025"]}}








class TestFieldEdges:
    """A field too wide to list still has to show its shape.

    `client_id` has 200 000 values: listing them is useless, omitting them
    leaves the caller guessing what a value looks like. The two ends answer
    both questions actually asked — what is the format, what is the range.
    """

    class _Edges(QlikEngineAPI):
        def __init__(self, values):
            self.values = values
            self.sorts = []

        def send_requests_pipelined(self, requests, raise_on_error=True):
            method = requests[0]["method"]
            if method == "CreateSessionObject":
                out = []
                for i, req in enumerate(requests):
                    definition = req["params"][0]["qListObjectDef"]
                    sort = definition["qDef"]["qSortCriterias"][0]
                    self.sorts.append(sort["qSortByNumeric"])
                    out.append({"qReturn": {"qHandle": 100 + i}})
                return out
            if method == "GetLayout":
                out = []
                for i, direction in enumerate(self.sorts):
                    values = (self.values if direction == 1
                              else list(reversed(self.values)))
                    rows = [[{"qText": v}] for v in values[:5]]
                    out.append({"qLayout": {"qListObject": {
                        "qDataPages": [{"qMatrix": rows}]}}})
                return out
            return [{} for _ in requests]

    def test_both_ends_are_returned(self):
        engine = self._Edges(["1", "2", "3", "4", "5", "6"])
        edges = engine.get_field_edges_batch(1, ["fact_id"])
        assert edges["fact_id"]["lowest"][0] == "1"
        assert edges["fact_id"]["highest"][0] == "6"



