"""Suggesting a near miss must not cost more than the answer it helps.

`_known_field_names` runs only on the error path, to turn "unknown field"
into "did you mean". Two things it must not do: read the whole data model
when one has already been read, and write what it reads into the schema
cache — a model stored from here carries no reload stamp, and
`get_app_details` would then reread the model it already had.
"""

from qlik_sense_mcp_server.engine_api import QlikEngineAPI


class _Engine(QlikEngineAPI):
    def __init__(self, cached=None, on_disk=("Region", "Sales")):
        self._cached_app_id = "app"
        self.on_disk = list(on_disk)
        self.reads = 0
        if cached is not None:
            self._schema_store()["app"] = {
                "model": {"fields": [{"field_name": n} for n in cached]},
                "reload_stamp": "stamp", "read_at": 0.0, "hits": 0,
            }

    def get_fields(self, app_handle):
        self.reads += 1
        return {"fields": [{"field_name": n} for n in self.on_disk]}


class TestReadingTheModel:
    def test_a_model_already_read_is_reused(self):
        engine = _Engine(cached=["Region", "Category"])
        assert engine._known_field_names(1) == ["Region", "Category"]
        assert engine.reads == 0

    def test_with_nothing_cached_the_model_is_read_once(self):
        engine = _Engine(cached=None)
        assert engine._known_field_names(1) == ["Region", "Sales"]
        assert engine.reads == 1

    def test_reading_it_here_does_not_fill_the_cache(self):
        """A model stored without a reload stamp would make the next
        `get_app_details` reread what it already had."""
        engine = _Engine(cached=None)
        engine._known_field_names(1)
        assert "app" not in engine._schema_store()

    def test_a_failed_read_costs_the_suggestion_and_nothing_else(self):
        class _Broken(_Engine):
            def get_fields(self, app_handle):
                raise RuntimeError("Engine is not answering")

        assert _Broken(cached=None)._known_field_names(1) == []

    def test_a_join_key_appears_once(self):
        engine = _Engine(cached=["region_code", "region_code", "region_name"])
        assert engine._known_field_names(1) == ["region_code", "region_name"]

    def test_an_instance_without_the_cache_still_answers(self):
        """Test doubles and subclasses that skip __init__ used to lose
        their suggestions to an AttributeError."""
        engine = _Engine.__new__(_Engine)
        engine.on_disk = ["Region"]
        engine.reads = 0
        assert engine._known_field_names(1) == ["Region"]
