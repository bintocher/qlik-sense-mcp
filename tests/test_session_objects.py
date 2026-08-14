"""Session objects are always destroyed, and failures are never silence.

Engine holds a session object — and its result set — in memory until the
client destroys it or the session ends. Cleanup written after the read
instead of in a `finally` leaks the object on every early return and every
exception, which is most of the paths that matter.

The other half of this file is about what an empty list means. `get_sheets`
and `_get_user_variables` used to answer `[]` for both "this app has none"
and "the call failed", so a broken Engine looked like a tidy, empty app.
"""

import pytest

from qlik_sense_mcp_server.engine_api import QlikEngineAPI
from qlik_sense_mcp_server.exceptions import QlikEngineError


class _Engine(QlikEngineAPI):
    """Records traffic; answers CreateSessionObject and GetLayout."""

    def __init__(self, layout=None, fail_on=None, handle=None):
        self.ws = object()
        self.ws_timeout_seconds = 30.0
        self.sent = []
        self._layout = layout if layout is not None else {"qLayout": {}}
        self._fail_on = fail_on
        self._handle = 7 if handle is None else handle

    def send_request(self, method, params=None, handle=-1, timeout=None):
        self.sent.append((method, params))
        if method == self._fail_on:
            raise Exception(f"Engine API error: {method} refused")
        if method == "CreateSessionObject":
            if self._handle is False:
                return {"qReturn": {}}
            return {"qReturn": {"qHandle": self._handle}}
        if method == "GetLayout":
            return self._layout
        return {}

    @property
    def created_ids(self):
        return [p[0]["qInfo"]["qId"] for m, p in self.sent if m == "CreateSessionObject"]

    @property
    def destroyed_ids(self):
        return [p[0] for m, p in self.sent if m == "DestroySessionObject"]


class TestSessionObjectContext:
    def test_object_is_destroyed_after_a_clean_read(self):
        eng = _Engine()
        with eng.session_object(1, {"qInfo": {"qType": "SheetList"}}) as handle:
            assert handle == 7
        assert eng.destroyed_ids == eng.created_ids








class TestSheets:
    def test_sheets_are_returned_and_the_object_cleaned_up(self):
        eng = _Engine(layout={"qLayout": {"qAppObjectList": {"qItems": [
            {"qInfo": {"qId": "s1"}}, {"qInfo": {"qId": "s2"}}]}}})
        sheets = eng.get_sheets(1)
        assert len(sheets) == 2
        assert eng.destroyed_ids == eng.created_ids






class TestUserVariables:
    def _layout(self, items):
        return {"qLayout": {"qVariableList": {"qItems": items}}}

    def test_user_variables_are_mapped(self):
        eng = _Engine(layout=self._layout([
            {"qName": "vYear", "qDefinition": "=Year(Today())", "qIsScriptCreated": True},
            {"qName": "vUi", "qDefinition": "42", "qIsScriptCreated": False},
        ]))
        variables = eng._get_user_variables(1)
        assert [v["name"] for v in variables] == ["vYear", "vUi"]
        assert variables[0]["is_script_created"] is True
        assert eng.destroyed_ids == eng.created_ids



