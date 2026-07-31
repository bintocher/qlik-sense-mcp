"""Tests for COMMENT FIELD / COMMENT TABLE propagation (since v1.7.2).

Qlik carries a field's business description in `qComment` — set by
`COMMENT FIELD x WITH '...'` in the load script — and returns it both in
`GetTablesAndKeys` (per field and per table) and in `GetFieldDescription`.
Before v1.7.2 the server dropped it, so an LLM had to guess what a column
meant from its name.
"""

import json

import pytest

from qlik_sense_mcp_server.engine_api import QlikEngineAPI
from qlik_sense_mcp_server import server as srv


TABLES_AND_KEYS = {
    "qtr": [
        {
            "qName": "Orders",
            "qComment": "Order facts",
            "qNoOfRows": 2,
            "qFields": [
                {
                    "qName": "Amount",
                    "qComment": "Order amount, net of refunds",
                    "qnRows": 2,
                    "qnTotalDistinctValues": 2,
                    "qTags": ["$numeric"],
                },
                {
                    "qName": "OrderId",
                    "qnRows": 2,
                    "qnTotalDistinctValues": 2,
                    "qTags": ["$integer"],
                },
            ],
        }
    ]
}


@pytest.fixture
def engine(monkeypatch):
    api = QlikEngineAPI.__new__(QlikEngineAPI)
    monkeypatch.setattr(
        api, "send_request", lambda *a, **kw: TABLES_AND_KEYS, raising=False
    )
    return api


class TestGetFields:
    def test_field_comment_is_kept(self, engine):
        fields = engine.get_fields(app_handle=1)["fields"]
        by_name = {f["field_name"]: f for f in fields}
        assert by_name["Amount"]["comment"] == "Order amount, net of refunds"

    def test_missing_comment_is_empty_string(self, engine):
        fields = engine.get_fields(app_handle=1)["fields"]
        by_name = {f["field_name"]: f for f in fields}
        assert by_name["OrderId"]["comment"] == ""

    def test_table_comment_travels_with_every_field(self, engine):
        fields = engine.get_fields(app_handle=1)["fields"]
        assert {f["table_comment"] for f in fields} == {"Order facts"}


class TestGetFieldDescription:
    def test_maps_qcomment(self, monkeypatch):
        api = QlikEngineAPI.__new__(QlikEngineAPI)
        payload = {
            "qReturn": {
                "qName": "Amount",
                "qComment": "Order amount, net of refunds",
                "qSrcTables": ["Orders"],
                "qCardinal": 2,
                "qTotalCount": 2,
                "qIsNumeric": True,
                "qTags": ["$numeric"],
                "qByteSize": 22,
            }
        }
        monkeypatch.setattr(api, "send_request", lambda *a, **kw: payload, raising=False)
        described = api.get_field_description(1, "Amount")
        assert described["comment"] == "Order amount, net of refunds"
        assert described["src_tables"] == ["Orders"]

    def test_unknown_field_returns_empty_dict(self, monkeypatch):
        api = QlikEngineAPI.__new__(QlikEngineAPI)

        def boom(*a, **kw):
            raise RuntimeError("Invalid parameters")

        monkeypatch.setattr(api, "send_request", boom, raising=False)
        assert api.get_field_description(1, "nope") == {}


class TestAppDetailsPayload:
    """`get_app_details` emits `comment` only where the script set one."""

    @pytest.fixture(autouse=True)
    def _stub_apis(self, monkeypatch):
        monkeypatch.setattr(srv, "_check", lambda: None)
        monkeypatch.setattr(
            srv.repo_api,
            "get_app_by_id",
            lambda app_id: {"id": app_id, "name": "App", "published": False},
            raising=False,
        )
        monkeypatch.setattr(srv.engine_api, "ensure_app", lambda *a, **kw: 1, raising=False)
        monkeypatch.setattr(
            srv.engine_api,
            "get_fields",
            lambda handle: {
                "fields": [
                    {
                        "field_name": "Amount",
                        "table_name": "Orders",
                        "comment": "Order amount, net of refunds",
                        "table_comment": "Order facts",
                        "rows_count": 2,
                        "distinct_values": 2,
                        "tags": ["$numeric"],
                    },
                    {
                        "field_name": "OrderId",
                        "table_name": "Orders",
                        "comment": "",
                        "table_comment": "Order facts",
                        "rows_count": 2,
                        "distinct_values": 2,
                        "tags": ["$integer"],
                    },
                ]
            },
            raising=False,
        )

    def _details(self):
        return json.loads(srv.get_app_details(app_id="a1b2c3d4-1111-2222-3333-444455556666"))

    def test_commented_field_carries_comment(self):
        fields = {f["name"]: f for f in self._details()["fields"]}
        assert fields["Amount"]["comment"] == "Order amount, net of refunds"

    def test_uncommented_field_has_no_comment_key(self):
        fields = {f["name"]: f for f in self._details()["fields"]}
        assert "comment" not in fields["OrderId"]

    def test_table_comment_is_reported_once(self):
        tables = self._details()["tables"]
        assert [t["comment"] for t in tables] == ["Order facts"]
