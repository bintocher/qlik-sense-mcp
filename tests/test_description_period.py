"""The description against the data.

A description is written once and outlives the reloads. On a large app it
said "Loaded period: 2 months" over twenty months of data, and a model
quoted it back as the answer. `get_app_details` now says when the dates or
the span a description names match none of the date fields' ranges.
"""

import datetime
import json

from qlik_sense_mcp_server.tools import overview, repository
from tests.test_details_warnings import _Engine, _Repo

RANGES = [("Date", datetime.date(2025, 1, 1), datetime.date(2026, 9, 29)),
          ("Month", datetime.date(2025, 1, 1), datetime.date(2026, 9, 30))]


class TestTheCheck:
    def test_a_span_no_date_field_has_is_named(self):
        warning = overview.description_disagrees(
            "Bets by sport. Loaded period: 2 months.", RANGES)
        assert "'2 months'" in warning
        assert "Date 2025-01-01..2026-09-29" in warning

    def test_a_span_in_russian_is_read_too(self):
        assert overview.description_disagrees(
            "Загруженный период: 2 месяца.", RANGES)

    def test_a_span_that_matches_a_range_is_not(self):
        assert overview.description_disagrees(
            "Last 21 months of data.", RANGES) is None

    def test_a_date_outside_every_range_is_named(self):
        warning = overview.description_disagrees("Data since 01.01.2020", RANGES)
        assert "'01.01.2020'" in warning

    def test_a_date_inside_a_range_is_not(self):
        assert overview.description_disagrees(
            "Data since 2025-01-01, version 2.3.3", RANGES) is None

    def test_no_dates_no_warning(self):
        assert overview.description_disagrees("Sales by region", RANGES) is None
        assert overview.description_disagrees("2 months", []) is None


class TestTheRanges:
    def test_min_max_and_listed_values_both_give_a_range(self):
        tables = [{"fields": [
            {"name": "Day", "type": "date", "min": "01.01.2025", "max": "29.09.2026"},
            {"name": "Month", "type": "date", "values": ["2025-01", "2026-09"]},
            {"name": "Serial", "type": "date", "min": "45658", "max": "46294"},
            {"name": "Amount", "type": "number", "min": "1", "max": "2"},
        ]}]
        found = {name: (low, high) for name, low, high in overview.date_ranges(tables)}
        assert found["Day"] == (datetime.date(2025, 1, 1), datetime.date(2026, 9, 29))
        assert found["Month"] == (datetime.date(2025, 1, 1), datetime.date(2026, 9, 30))
        assert found["Serial"][0] == datetime.date(2025, 1, 1)
        assert "Amount" not in found


def test_get_app_details_carries_the_warning(monkeypatch):
    class _DescribedRepo(_Repo):
        def get_app_by_id(self, app_id):
            return dict(super().get_app_by_id(app_id),
                        description="Loaded period: 2 months")

    fields = [
        {"field_name": "Date", "table_name": "Facts", "distinct_values": 600,
         "rows_count": 1000, "tags": ["$date", "$numeric"]},
    ]
    monkeypatch.setattr(repository.context, "engine_api", _Engine(fields))
    monkeypatch.setattr(repository.context, "repo_api", _DescribedRepo())
    monkeypatch.setattr(repository, "_values_and_ranges", lambda handle, fields: (
        {}, {"Date": {"lowest": ["01.01.2025"], "highest": ["29.09.2026"]}}))
    repository._DETAILS_CACHE.clear()
    reply = json.loads(repository.get_app_details("app-1"))
    assert any("description disagrees" in w for w in reply["warnings"])
