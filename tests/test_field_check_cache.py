"""Checking field names should not cost a round-trip we already paid for.

Every hypercube validates the names it was given. That check went to Engine
each time, even though the caller had almost always just read the data model
— measured over 440 hypercube calls in the benchmark, `get_app_details` came
first in nine cases out of ten.

The cache is used in one direction only. A name it lists is known. A name it
does not list is still asked about, because the cached model holds table
fields and Qlik knows others besides — answering "no such field" from an
incomplete list would refuse a valid query.
"""

import pytest

from qlik_sense_mcp_server.engine_api import QlikEngineAPI


class _Engine(QlikEngineAPI):
    """Answers field-description batches; counts them."""

    def __init__(self, model_fields=(), engine_fields=()):
        self._model = list(model_fields)
        self._engine = set(engine_fields) | set(model_fields)
        self.asked = []

    def cached_fields(self, app_handle, app_id, reload_stamp=None):
        return {"fields": [{"field_name": n} for n in self._model]}

    def send_requests_pipelined(self, requests, raise_on_error=True):
        names = [r["params"][0] for r in requests]
        self.asked.append(names)
        return [{"qReturn": {"qName": n}} if n in self._engine else {"qReturn": {}}
                for n in names]


class TestKnownNamesCostNothing:
    def test_a_cached_name_is_not_asked_about(self):
        engine = _Engine(model_fields=["region_name", "amount"])
        assert engine._fields_exist(1, ["region_name"]) == {"region_name": True}
        assert engine.asked == [], "лишний запрос к Qlik за известным полем"

    def test_a_whole_cached_batch_costs_no_round_trip(self):
        engine = _Engine(model_fields=["a", "b", "c"])
        verdicts = engine._fields_exist(1, ["a", "b", "c"])
        assert verdicts == {"a": True, "b": True, "c": True}
        assert engine.asked == []


class TestUnknownNamesStillGoToEngine:
    def test_a_name_missing_from_the_cache_is_verified(self):
        """System fields exist without appearing in the table model."""
        engine = _Engine(model_fields=["amount"], engine_fields=["$Table"])
        verdicts = engine._fields_exist(1, ["amount", "$Table"])
        assert verdicts == {"amount": True, "$Table": True}
        assert engine.asked == [["$Table"]], "спрошено не то, что нужно"

    def test_a_genuinely_absent_name_is_reported_absent(self):
        engine = _Engine(model_fields=["amount"])
        verdicts = engine._fields_exist(1, ["amount", "no_such_field"])
        assert verdicts["no_such_field"] is False

    def test_only_the_unknown_part_is_asked_about(self):
        engine = _Engine(model_fields=["a", "b"], engine_fields=["c"])
        engine._fields_exist(1, ["a", "b", "c"])
        assert engine.asked == [["c"]]


class TestFailureKeepsWhatItKnows:
    class _Broken(_Engine):
        def send_requests_pipelined(self, requests, raise_on_error=True):
            raise RuntimeError("Engine unavailable")

    def test_cached_verdicts_survive_an_engine_failure(self):
        """The check is a guard; losing it must not lose what we knew."""
        engine = self._Broken(model_fields=["amount"])
        verdicts = engine._fields_exist(1, ["amount", "mystery"])
        assert verdicts == {"amount": True}

    def test_an_empty_cache_still_asks(self):
        engine = _Engine(model_fields=[], engine_fields=["amount"])
        assert engine._fields_exist(1, ["amount"]) == {"amount": True}
        assert engine.asked == [["amount"]]
