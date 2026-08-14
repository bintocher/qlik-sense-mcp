"""Tests for COMMENT FIELD / COMMENT TABLE propagation (since v1.7.2).

Qlik carries a field's business description in `qComment` — set by
`COMMENT FIELD x WITH '...'` in the load script — and returns it both in
`GetTablesAndKeys` (per field and per table) and in `GetFieldDescription`.
Before v1.7.2 the server dropped it, so an LLM had to guess what a column
meant from its name.
"""

import contextlib
import json
from types import SimpleNamespace

import pytest

from qlik_sense_mcp_server.engine_api import QlikEngineAPI
from qlik_sense_mcp_server import server as srv
from qlik_sense_mcp_server.tools import context


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



FIELDS_PAYLOAD = {
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
}


class TestAppDetailsPayload:
    """`get_app_details` emits `comment` only where the script set one."""

    @pytest.fixture(autouse=True)
    def _stub_apis(self, monkeypatch):
        # Without Qlik env vars the module-level clients are None (that is
        # how CI runs), so replace the whole client objects rather than
        # patching attributes on them.
        monkeypatch.setattr(context, "repo_api",
            SimpleNamespace(
                get_app_by_id=lambda app_id: {
                    "id": app_id, "name": "App", "published": False
                }
            ),
        )
        monkeypatch.setattr(context, "engine_api",
            SimpleNamespace(
                ensure_app=lambda *a, **kw: 1,
                get_fields=lambda handle: FIELDS_PAYLOAD,
                # Engine-backed tools run inside the client's transaction,
                # which serialises them against the shared socket.
                transaction=contextlib.nullcontext,
            ),
        )

    def _details(self):
        return json.loads(srv.get_app_details(app_id="a1b2c3d4-1111-2222-3333-444455556666"))

    def test_commented_field_carries_comment(self):
        fields = {f["name"]: f for f in self._details()["fields"]}
        assert fields["Amount"]["comment"] == "Order amount, net of refunds"


