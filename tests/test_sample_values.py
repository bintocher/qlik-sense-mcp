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

from qlik_sense_mcp_server.engine_api import QlikEngineAPI
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

    def test_the_requested_height_is_honoured(self):
        engine = _Engine({"Dates": ["01.01.2024"]})
        engine.get_field_values_batch(1, [("Dates", 3)])
        assert engine.asked == [("Dates", 3)]

    def test_session_objects_are_destroyed(self):
        """They hold their result set in Engine memory until they are not."""
        engine = _Engine({"Region": ["North"]})
        engine.get_field_values_batch(1, [("Region", 25)])
        assert engine.destroyed == 1

    def test_nothing_wanted_costs_no_calls(self):
        engine = _Engine()
        assert engine.get_field_values_batch(1, []) == {}
        assert engine.asked == []

    def test_a_field_with_no_values_is_simply_absent(self):
        engine = _Engine({"Region": ["North"], "Empty": []})
        result = engine.get_field_values_batch(1, [("Region", 25), ("Empty", 25)])
        assert "Empty" not in result


class TestAttachToFields:
    @staticmethod
    def _fields():
        return [
            {"name": "region_name", "distinct_values": 10, "tags": ["$text"]},
            {"name": "client_id", "distinct_values": 200_000, "tags": ["$text"]},
            {"name": "order_date", "distinct_values": 730, "tags": ["$date", "$numeric"]},
            {"name": "empty_field", "distinct_values": 0, "tags": []},
        ]

    def test_a_small_field_gets_its_values(self, monkeypatch):
        engine = _Engine({"region_name": ["Moskva", "Kazan"], "order_date": ["01.01.2024"]})
        monkeypatch.setattr(repository.context, "engine_api", engine)
        fields = self._fields()
        repository._attach_sample_values(1, fields)
        assert fields[0]["values"] == ["Moskva", "Kazan"]

    def test_a_high_cardinality_field_is_left_alone(self, monkeypatch):
        """200k values are not an aid, they are a wall of text."""
        engine = _Engine({"region_name": ["Moskva"]})
        monkeypatch.setattr(repository.context, "engine_api", engine)
        fields = self._fields()
        repository._attach_sample_values(1, fields)
        assert "values" not in fields[1] and "sample" not in fields[1]

    def test_a_date_field_shows_its_display_format(self, monkeypatch):
        """The format is the whole point: 01.01.2024 is not 45292."""
        engine = _Engine({"region_name": ["Moskva"],
                          "order_date": ["01.01.2024", "02.01.2024", "03.01.2024"]})
        monkeypatch.setattr(repository.context, "engine_api", engine)
        fields = self._fields()
        repository._attach_sample_values(1, fields)
        assert fields[2]["sample"] == ["01.01.2024", "02.01.2024", "03.01.2024"]

    def test_a_date_field_is_sampled_not_listed(self, monkeypatch):
        engine = _Engine({"order_date": ["01.01.2024"] * 10})
        monkeypatch.setattr(repository.context, "engine_api", engine)
        fields = self._fields()
        repository._attach_sample_values(1, fields)
        assert "values" not in fields[2]

    def test_an_empty_field_is_not_asked_about(self, monkeypatch):
        engine = _Engine()
        monkeypatch.setattr(repository.context, "engine_api", engine)
        repository._attach_sample_values(1, self._fields())
        assert all(name != "empty_field" for name, _ in engine.asked)

    def test_the_number_of_sampled_fields_is_capped(self, monkeypatch):
        engine = _Engine()
        monkeypatch.setattr(repository.context, "engine_api", engine)
        many = [{"name": f"f{i}", "distinct_values": 5, "tags": []} for i in range(50)]
        repository._attach_sample_values(1, many)
        assert len(engine.asked) == repository.SAMPLE_VALUES_MAX_FIELDS

    def test_a_failure_leaves_the_reply_intact(self, monkeypatch):
        """This is a convenience; it must never be why the answer fails."""
        monkeypatch.setattr(repository.context, "engine_api", _Engine(fail=True))
        fields = self._fields()
        repository._attach_sample_values(1, fields)
        assert all("values" not in f for f in fields)
