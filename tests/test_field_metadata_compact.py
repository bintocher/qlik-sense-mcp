"""What a field says about itself, in as few characters as it takes.

Two things a caller needs and cannot infer: the comment from the load
script — the only human description a column has — and the tags, which say
whether it is a date, a number or a key. Both are returned. Everything a
field shares with its table is not: `rows` repeats the table's row count on
every field, and `is_key` is false for most of them.

Tags arrive from Qlik as `["$numeric", "$integer"]` and go out as
`"numeric integer"`: same words, a third of the characters.
"""

import json
from contextlib import contextmanager

import pytest

from qlik_sense_mcp_server.tools import repository


class _Engine:
    def __init__(self, fields):
        self._fields = fields

    @contextmanager
    def transaction(self):
        yield

    def ensure_app(self, app_id, **kwargs):
        return 1

    def connect(self, app_id=None):
        return None

    def cached_fields(self, app_handle, app_id, reload_stamp=None):
        return {"fields": self._fields, "tables_count": 1,
                "total_fields": len(self._fields)}

    def get_fields(self, app_handle):
        return self.cached_fields(app_handle, "")


class _Repo:
    def get_app_by_id(self, app_id):
        return {"id": app_id, "name": "app", "lastReloadTime": "2026-01-01"}


@pytest.fixture
def details(monkeypatch):
    def build(fields):
        monkeypatch.setattr(repository.context, "engine_api", _Engine(fields))
        monkeypatch.setattr(repository.context, "repo_api", _Repo())
        monkeypatch.setattr(repository, "_attach_sample_values",
                            lambda handle, fields: None)
        repository._DETAILS_CACHE.clear()
        return json.loads(repository.get_app_details("app-1"))
    return build


def _field(**kwargs):
    base = {"field_name": "amount", "table_name": "Fact", "is_key": False,
            "distinct_values": 100, "rows_count": 1000, "tags": ["$numeric"]}
    base.update(kwargs)
    return base


class TestWhatIsKept:
    def test_the_load_script_comment_reaches_the_caller(self, details):
        """The only human description a column ever has."""
        reply = details([_field(comment="Сумма заказа без НДС")])
        assert reply["fields"][0]["comment"] == "Сумма заказа без НДС"




class TestWhatIsDropped:
    def test_no_comment_means_no_key(self, details):
        """An empty comment on every field is noise, not information."""
        reply = details([_field()])
        assert "comment" not in reply["fields"][0]





class TestTagText:
    def test_the_dollar_sign_is_dropped(self):
        assert repository._tags_text(["$date"]) == "date"


