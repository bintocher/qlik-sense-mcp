"""A model with hundreds of fields must not bury the reply in key names.

Measured: 33 fields cost 7.6k characters as a list of objects, and the
repeated key names are most of that. A warehouse model with 300 fields would
cost proportionally more, for nothing — the caller reads the same content
from a header plus rows.

The threshold matters in both directions. Below it the readable form stays,
because that is the normal case and the size is not a problem there.
"""

import json
from contextlib import contextmanager

import pytest

from qlik_sense_mcp_server.tools import repository
from qlik_sense_mcp_server.tools.repository import (
    WIDE_MODEL_COLUMNS, WIDE_MODEL_FIELDS)


def _fields(count):
    return [{"name": f"field_{i}", "table": "Fact", "is_key": False,
             "distinct_values": i, "rows": 100, "tags": ["$numeric"],
             "comment": ""} for i in range(count)]


class _Engine:
    """Enough of the Engine surface for get_app_details to run."""

    def __init__(self, field_count):
        self.field_count = field_count
        self.sampled = 0

    @contextmanager
    def transaction(self):
        """The tool wraps its Engine work in one; here it does nothing."""
        yield

    def ensure_app(self, app_id, **kwargs):
        """The tool asks for an open app and gets a handle back."""
        return 1

    def connect(self, app_id=None):
        return None

    def open_doc(self, app_id, no_data=False):
        return {"qReturn": {"qHandle": 1}}

    def cached_fields(self, app_handle, app_id, reload_stamp=None):
        return {"fields": [{"field_name": f["name"], "table_name": "Fact",
                            "cardinal": f["distinct_values"], "rows_count": 100,
                            "tags": ["$numeric"], "is_key": False}
                           for f in _fields(self.field_count)],
                "tables_count": 1, "total_fields": self.field_count}

    def get_fields(self, app_handle):
        return self.cached_fields(app_handle, "")


@pytest.fixture
def stand(monkeypatch):
    def install(field_count):
        engine = _Engine(field_count)
        monkeypatch.setattr(repository.context, "engine_api", engine)
        monkeypatch.setattr(repository.context, "repo_api", _Repo())
        monkeypatch.setattr(repository, "_attach_sample_values",
                            lambda handle, fields: setattr(
                                engine, "sampled", engine.sampled + 1))
        repository._DETAILS_CACHE.clear()
        return engine
    return install


class _Repo:
    def get_app_by_id(self, app_id):
        return {"id": app_id, "name": "wide", "lastReloadTime": "2026-01-01"}


class TestWideModel:
    def test_a_wide_model_comes_back_as_columns_and_rows(self, stand):
        stand(WIDE_MODEL_FIELDS + 1)
        reply = json.loads(repository.get_app_details("app-1"))
        assert reply["fields"]["columns"] == WIDE_MODEL_COLUMNS
        assert len(reply["fields"]["rows"]) == WIDE_MODEL_FIELDS + 1

    def test_the_row_order_matches_the_header(self, stand):
        stand(WIDE_MODEL_FIELDS + 1)
        reply = json.loads(repository.get_app_details("app-1"))
        first = dict(zip(reply["fields"]["columns"], reply["fields"]["rows"][0]))
        assert first["name"] == "field_0"
        assert first["table"] == "Fact"

    def test_the_caller_is_told_why(self, stand):
        stand(WIDE_MODEL_FIELDS + 1)
        reply = json.loads(repository.get_app_details("app-1"))
        assert any("columns+rows" in w for w in reply["warnings"])

    def test_sampling_is_skipped_when_the_values_would_be_dropped(self, stand):
        engine = stand(WIDE_MODEL_FIELDS + 1)
        repository.get_app_details("app-1")
        assert engine.sampled == 0, "значения читались зря"


class TestNarrowModelIsUntouched:
    def test_an_ordinary_model_keeps_the_readable_form(self, stand):
        stand(WIDE_MODEL_FIELDS - 1)
        reply = json.loads(repository.get_app_details("app-1"))
        assert isinstance(reply["fields"], list)
        assert reply["fields"][0]["name"] == "field_0"

    def test_sampling_still_runs_below_the_threshold(self, stand):
        engine = stand(5)
        repository.get_app_details("app-1")
        assert engine.sampled == 1


class TestTheTableIsActuallySmaller:
    """A compact form that is not compact is worse than none at all."""

    def test_a_field_costs_less_in_the_table_than_as_an_object(self, stand):
        stand(WIDE_MODEL_FIELDS - 1)
        as_objects = len(repository.get_app_details("app-1"))
        per_object = as_objects / (WIDE_MODEL_FIELDS - 1)

        stand(WIDE_MODEL_FIELDS + 1)
        as_table = len(repository.get_app_details("app-1"))
        per_row = as_table / (WIDE_MODEL_FIELDS + 1)

        assert per_row < per_object, (
            f"таблица дороже списка: {per_row:.0f} против {per_object:.0f} "
            "знаков на поле")

    def test_rows_are_written_one_per_line(self, stand):
        """Indented rows were what made the table longer than the objects."""
        stand(WIDE_MODEL_FIELDS + 1)
        raw = repository.get_app_details("app-1")
        assert '["field_0"' in raw.replace(" ", ""), "строки печатаются с отступами"
