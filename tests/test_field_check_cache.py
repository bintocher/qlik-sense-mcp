"""Checking field names should not cost a round-trip we already paid for.

Every hypercube validates the names it was given. That check went to Engine
each time, even though the caller had almost always just read the data model
— measured over 440 hypercube calls in the benchmark, `get_app_details` came
first in nine cases out of ten.

Three limits keep the saving honest, and each exists because of a way this
can be wrong:

  - the model must have been read *recently*. A reload can delete a field,
    and Qlik answers a query naming a deleted field with a plausible number
    instead of an error — so a stale model must not vouch for anything;
  - the cache may confirm a name, never deny one. It holds table fields and
    Qlik knows others besides, so "not in the list" is not "does not exist";
  - nothing is read here. With no model in hand the old path runs unchanged.
"""

import time

import pytest

from qlik_sense_mcp_server.engine_api import QlikEngineAPI


class _Engine(QlikEngineAPI):
    """Answers field-description batches; counts them."""

    def __init__(self, model_fields=(), engine_fields=(), age=0.0):
        self._cached_app_id = "app-1"
        self.asked = []
        self.reads = 0
        if model_fields:
            self._schema_store()["app-1"] = {
                "model": {"fields": [{"field_name": n} for n in model_fields]},
                "reload_stamp": "stamp", "hits": 0,
                "read_at": time.monotonic() - age,
            }
        self._engine = set(engine_fields) | set(model_fields)

    def get_fields(self, app_handle):
        self.reads += 1
        return {"fields": [{"field_name": n} for n in sorted(self._engine)]}

    def send_requests_pipelined(self, requests, raise_on_error=True):
        names = [r["params"][0] for r in requests]
        self.asked.append(names)
        return [{"qReturn": {"qName": n}} if n in self._engine else {"qReturn": {}}
                for n in names]


class TestKnownNamesCostNothing:
    def test_a_recently_read_name_is_not_asked_about(self):
        engine = _Engine(model_fields=["region_name", "amount"])
        assert engine._fields_exist(1, ["region_name"]) == {"region_name": True}
        assert engine.asked == [], "лишний запрос к Qlik за известным полем"

    def test_a_whole_cached_batch_costs_no_round_trip(self):
        engine = _Engine(model_fields=["a", "b", "c"])
        assert engine._fields_exist(1, ["a", "b", "c"]) == {
            "a": True, "b": True, "c": True}
        assert engine.asked == []


class TestStaleModelDoesNotVouch:
    """A reload between the read and the query deletes fields silently."""

    def test_an_old_model_sends_the_name_to_engine(self):
        engine = _Engine(model_fields=["amount"], age=QlikEngineAPI.FIELD_TRUST_SECONDS + 1)
        engine._fields_exist(1, ["amount"])
        assert engine.asked == [["amount"]], "старая модель поручилась за поле"

    def test_a_field_deleted_by_a_reload_is_reported_absent(self):
        engine = _Engine(model_fields=["amount"],
                         age=QlikEngineAPI.FIELD_TRUST_SECONDS + 1)
        engine._engine = set()  # приложение перезагрузили, поля больше нет
        assert engine._fields_exist(1, ["amount"]) == {"amount": False}

    def test_a_model_just_inside_the_window_still_counts(self):
        engine = _Engine(model_fields=["amount"],
                         age=QlikEngineAPI.FIELD_TRUST_SECONDS - 5)
        assert engine._fields_exist(1, ["amount"]) == {"amount": True}
        assert engine.asked == []


class TestUnknownNamesStillGoToEngine:
    def test_a_name_missing_from_the_cache_is_verified(self):
        """System fields exist without appearing in the table model."""
        engine = _Engine(model_fields=["amount"], engine_fields=["$Table"])
        assert engine._fields_exist(1, ["amount", "$Table"]) == {
            "amount": True, "$Table": True}
        assert engine.asked == [["$Table"]]

    def test_a_genuinely_absent_name_is_reported_absent(self):
        engine = _Engine(model_fields=["amount"])
        assert engine._fields_exist(1, ["amount", "no_such_field"])["no_such_field"] is False


class TestColdPathIsUnchanged:
    def test_no_model_means_no_extra_read(self):
        """Fetching the model here would cost more than the check saves."""
        engine = _Engine(engine_fields=["amount"])
        assert engine._fields_exist(1, ["amount"]) == {"amount": True}
        assert engine.reads == 0, "модель читалась ради проверки имени"
        assert engine.asked == [["amount"]]


class TestFailureKeepsWhatItKnows:
    class _Broken(_Engine):
        def send_requests_pipelined(self, requests, raise_on_error=True):
            raise RuntimeError("Engine unavailable")

    def test_cached_verdicts_survive_an_engine_failure(self):
        engine = self._Broken(model_fields=["amount"])
        assert engine._fields_exist(1, ["amount", "mystery"]) == {"amount": True}
