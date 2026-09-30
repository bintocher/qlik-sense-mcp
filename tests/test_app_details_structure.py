"""`get_app_details`: the model as tables and links, within 20k tokens.

On a 97-field app the old reply was 57k characters: 698 bookmarks (a third
of it, half named by a GUID), 67 measures with their whole expressions, and
a field list without the values of 48 short fields and without a single
link between tables. A client diverted it to a file, and the model worked
without it.
"""

import json
from contextlib import contextmanager

import pytest

from qlik_sense_mcp_server.engine import QlikEngineAPI
from qlik_sense_mcp_server.tools import overview, repository


def _field(name, table, distinct, rows, tags=("$numeric",), key_type="NOT_KEY"):
    return {"field_name": name, "table_name": table, "distinct_values": distinct,
            "rows_count": rows, "table_rows": rows, "tags": list(tags), "key_type": key_type}


def _model():
    """A fact table, a second fact, a dimension table, a regular calendar,
    a shift calendar chained behind a link table, and an island."""
    fields = [
        _field("OrderId", "Orders", 50_000_000, 900_000_000),
        _field("ClientId", "Orders", 1_000_000, 900_000_000),
        _field("DateKey", "Orders", 700, 900_000_000, ("$date", "$numeric")),
        _field("ShiftKey", "Orders", 30, 900_000_000, ("$date", "$numeric")),
        _field("Amount", "Orders", 2_000_000, 900_000_000),
        _field("OrderId", "Payments", 50_000_000, 200_000_000, key_type="PERFECT_KEY"),
        _field("PaidAmount", "Payments", 900_000, 200_000_000),
        _field("ClientId", "Clients", 1_000_000, 1_000_000, key_type="PRIMARY_KEY"),
        _field("Segment", "Clients", 4, 1_000_000, ("$text",)),
        _field("DateKey", "Calendar", 700, 700, ("$date", "$numeric"), "PERFECT_KEY"),
        _field("Date", "Calendar", 700, 700, ("$date", "$numeric")),
        _field("ShiftKey", "ShiftLink", 30, 90, ("$date", "$numeric")),
        _field("LinkDate", "ShiftLink", 21, 90, ("$date", "$numeric")),
        _field("LinkDate", "ShiftCalendar", 21, 400, ("$date", "$numeric")),
        _field("Accumulation", "ShiftCalendar", 8, 400, ("$text", "$hidden")),
        _field("Selector", "Choice", 3, 3, ("$text",)),
    ]
    # Pad to 100 fields on the dimension table.
    for i in range(100 - len({(f["field_name"]) for f in fields})):
        fields.append(_field(f"Attr{i:02}", "Clients", 3 + i % 40, 1_000_000, ("$text",)))
    return fields


class _Engine(QlikEngineAPI):
    """The Engine surface get_app_details reads, answered in memory."""

    def __init__(self, bookmarks=700, approved=5, measures=50, expression_chars=800):
        self.fields = _model()
        self.bookmarks = bookmarks
        self.approved = approved
        self.measure_count = measures
        self.expression_chars = expression_chars

    @contextmanager
    def transaction(self):
        yield

    def ensure_app(self, app_id, **kwargs):
        return 1

    def cached_fields(self, app_handle, app_id, reload_stamp=None):
        return {"fields": self.fields}

    def get_field_values_batch(self, app_handle, wanted):
        distinct = {f["field_name"]: f["distinct_values"] for f in self.fields}
        return {name: [f"{name} value {i}" for i in range(min(how_many, distinct[name]))]
                for name, how_many in wanted}

    def get_field_edges_batch(self, app_handle, names, count=5):
        return {name: {"lowest": ["01.01.2025"], "highest": ["29.09.2026"]} for name in names}

    def get_master_items(self, app_handle):
        body = "Sum({<Segment={'A'}>} Amount)"
        long = (body + " + ") * (self.expression_chars // (len(body) + 3))
        measures = []
        for i in range(self.measure_count):
            measures.append({"id": f"m{i}", "name": f"Measure {i}",
                             "expression": long[:self.expression_chars]})
        measures += [{"id": "rev", "name": "Revenue", "expression": "Sum(Amount)"},
                     {"id": "rev1", "name": "Revenue Y-1", "expression": "Sum({<Year={2024}>} Amount)"},
                     {"id": "rev2", "name": "Revenue  YoY, %", "expression": "[Revenue]/[Revenue Y-1]-1"},
                     {"id": "rev3", "name": "Revenue, %", "expression": "[Revenue]/Sum(total Amount)"},
                     {"id": "revx", "name": "Revenue per client", "expression": "[Revenue]/Count(ClientId)"}]
        return {"measures": measures,
                "dimensions": [{"id": "d1", "name": "Client segment",
                                "definition": "Client segment = Segment", "fields": ["Segment"]}]}

    @contextmanager
    def session_object(self, app_handle, definition, timeout=None):
        yield definition["qInfo"]["qId"]

    def send_request(self, method, params=None, handle=None, timeout=None):
        assert handle == "approved-bookmarks"
        items = []
        for i in range(self.bookmarks):
            approved = i < self.approved
            items.append({"qInfo": {"qId": f"b{i}"},
                          "qMeta": {"approved": approved, "published": True},
                          "qData": {"title": f"Base {i}" if approved else f"0000{i}-user",
                                    "description": "Current month" if i == 0 else ""}})
        return {"qLayout": {"qBookmarkList": {"qItems": items}}}

    def _known_sets(self, app_handle):
        return {"bookmarks": set(), "states": {"Compare"}}


class _Repo:
    def get_app_by_id(self, app_id):
        return {"id": app_id, "name": "Sales", "description": "Sales model",
                "lastReloadTime": "2026-09-29T03:00:00Z"}


@pytest.fixture
def details(monkeypatch):
    def run(**engine_options):
        monkeypatch.setattr(repository.context, "engine_api", _Engine(**engine_options))
        monkeypatch.setattr(repository.context, "repo_api", _Repo())
        repository._DETAILS_CACHE.clear()
        text = repository.get_app_details("app-1")
        return text, json.loads(text)
    return run


def test_the_reply_fits_the_budget(details):
    text, reply = details()
    assert sum(len(t["fields"]) for t in reply["tables"]) + sum(
        len(i.split(";")) for i in reply.get("islands", [])) == 100
    assert len(text) / overview.CHARS_PER_TOKEN <= overview.TOKEN_BUDGET
    assert reply["size_budget"]["limit"] == overview.TOKEN_BUDGET


def test_only_approved_bookmarks_and_nothing_about_the_rest(details):
    text, reply = details()
    assert reply["bookmarks"] == ["Base 0 - Current month", "Base 1", "Base 2", "Base 3", "Base 4"]
    assert "user" not in text and "named_sets" not in reply


def test_tables_carry_roles_and_links(details):
    _text, reply = details()
    roles = {t["name"]: t["role"] for t in reply["tables"]}
    assert roles == {"Orders": "fact", "Payments": "fact", "Clients": "dimension table",
                     "Calendar": "calendar", "ShiftLink": "calendar",
                     "ShiftCalendar": "calendar"}
    assert set(reply["links"]) == {
        "Orders -OrderId-> Payments", "Orders -ClientId-> Clients",
        "Orders -DateKey-> Calendar", "Orders -ShiftKey-> ShiftLink",
        "ShiftLink -LinkDate-> ShiftCalendar"}
    assert reply["islands"] == ["Choice (3 rows): Selector = Selector value 0 | "
                                "Selector value 1 | Selector value 2"]


def test_a_calendar_states_its_dates_against_its_rows(details):
    _text, reply = details()
    tables = {t["name"]: t for t in reply["tables"]}
    assert tables["Calendar"]["calendar"] == "rows 700, distinct dates 700 (DateKey)"
    assert tables["ShiftCalendar"]["calendar"] == (
        "rows 400, distinct dates 21 (LinkDate); fields with few values next to the date: "
        "Accumulation (8)")


def test_fields_carry_values_ranges_and_marks(details):
    _text, reply = details()
    fields = {f["name"]: f for t in reply["tables"] for f in t["fields"]}
    assert fields["Segment"]["values"][0] == "Segment value 0"
    assert fields["Date"]["min"] == "01.01.2025" and fields["Date"]["max"] == "29.09.2026"
    assert fields["OrderId"]["key"] is True
    assert fields["Accumulation"]["hidden"] is True
    # A key field is listed once, on the fact side.
    orders = [f["name"] for f in reply["tables"][0]["fields"]]
    assert "ClientId" in orders
    assert all("ClientId" not in [f["name"] for f in t["fields"]] for t in reply["tables"][1:])


def test_measures_are_cut_and_variants_folded(details):
    _text, reply = details()
    measures = {m["name"]: m for m in reply["measures"]}
    assert measures["Revenue"]["variants"] == ["Revenue Y-1", "Revenue  YoY, %", "Revenue, %"]
    assert "Revenue per client" in measures
    long = measures["Measure 0"]
    assert long["expression_truncated"] is True and long["id"] == "m0"
    assert long["fields"] == ["Amount", "Segment"]
    assert reply["dimensions"] == ["Client segment = Segment"]
    assert reply["alternate_states"] == ["Compare"]


def test_an_oversized_reply_is_trimmed_and_says_so(details):
    text, reply = details(measures=400)
    assert reply["size_budget"]["trimmed"]
    assert "measure expressions cut to 80 characters" in reply["size_budget"]["trimmed"]
    assert all(len(m["expression"]) <= 83 for m in reply["measures"])


def test_warnings_are_about_volume(details):
    _text, reply = details()
    assert len(reply["warnings"]) == 2
    assert "rows" in reply["warnings"][0] and "values" in reply["warnings"][1]


def test_get_app_object_returns_a_cut_measure_whole(monkeypatch):
    from qlik_sense_mcp_server.tools import engine as engine_tools

    class _Items(_Engine):
        def describe_app_object(self, app_handle, object_id):
            return None

        def _library_items_by_id(self, app_handle, item_ids, kinds=("measures", "dimensions")):
            return {"measures": [{"id": "m0", "name": "Measure 0", "expression": "x" * 800}],
                    "dimensions": []}

    monkeypatch.setattr(engine_tools.context, "engine_api", _Items())
    monkeypatch.setattr(engine_tools.context, "repo_api", _Repo())
    reply = json.loads(engine_tools.get_app_object("app-1", "m0"))
    assert reply["object_type"] == "master measure"
    assert reply["title"] == "Measure 0" and len(reply["expression"]) == 800
