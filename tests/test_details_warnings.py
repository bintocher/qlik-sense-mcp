"""`get_app_details` warns about volume, and about nothing else.

It used to add a hint naming the first date-like fields and offering the
first as the example filter. On an app with a shift calendar the first one
was the shifted date, and a filter on it tripled the figures. Which date
field means "the period" is not the server's call.
"""

import json
from contextlib import contextmanager

from qlik_sense_mcp_server.tools import repository


class _Engine:
    def __init__(self, fields):
        self._fields = fields

    @contextmanager
    def transaction(self):
        yield

    def ensure_app(self, app_id, **kwargs):
        return 1

    def cached_fields(self, app_handle, app_id, reload_stamp=None):
        return {"fields": self._fields}


class _Repo:
    def get_app_by_id(self, app_id):
        return {"id": app_id, "name": "app", "lastReloadTime": "2026-01-01"}


def _details(monkeypatch, fields):
    monkeypatch.setattr(repository.context, "engine_api", _Engine(fields))
    monkeypatch.setattr(repository.context, "repo_api", _Repo())
    monkeypatch.setattr(repository, "_attach_sample_values", lambda handle, fields: None)
    repository._DETAILS_CACHE.clear()
    return json.loads(repository.get_app_details("app-1"))


def test_no_warning_names_a_date_field_or_suggests_a_filter(monkeypatch):
    fields = [
        {"field_name": "ShiftDate", "table_name": "ShiftCalendar", "distinct_values": 30,
         "rows_count": 90, "tags": ["$date", "$numeric"]},
        {"field_name": "Date", "table_name": "Calendar", "distinct_values": 700,
         "rows_count": 700, "tags": ["$date", "$timestamp"]},
        {"field_name": "Amount", "table_name": "Facts", "distinct_values": 2_000_000,
         "rows_count": 600_000_000, "tags": ["$numeric"]},
    ]
    reply = _details(monkeypatch, fields)
    text = json.dumps(reply["warnings"], ensure_ascii=False)
    assert reply["warnings"], "the volume warnings stay"
    for word in ("ShiftDate", "Date field", '"period"', "engine_get_field_range"):
        assert word not in text
