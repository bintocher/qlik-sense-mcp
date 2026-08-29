"""Searching and paging a field happen in Engine, over the whole field.

The old path read the first 500-5000 values, matched them in Python and
sliced the result. On any field bigger than that prefix the answer was
wrong in a way nothing in the reply revealed: a value at position 150k did
not exist, `offset` past the prefix returned an empty page, and passing
both search parameters made the second silently overwrite the first.

Case is the one thing Qlik cannot filter for us. Verified:
`Match()` respects case but takes no wildcards, `WildMatch()` takes
wildcards but ignores case. So a case-sensitive search asks Engine for the
case-insensitive superset and narrows it here — which is safe, because the
exact answer is always a subset of it.
"""

import json

import pytest

from qlik_sense_mcp_server import server as srv
from qlik_sense_mcp_server.tools import context
from qlik_sense_mcp_server.engine_api import QlikEngineAPI


class _Engine(QlikEngineAPI):
    """Serves a fixed list of values as if Engine had matched them."""

    def __init__(self, values=None):
        self.ws = object()
        self.ws_timeout_seconds = 30.0
        self.sent = []
        self.pages = []
        self._values = values if values is not None else []

    def transaction(self):
        import contextlib
        return contextlib.nullcontext()

    def ensure_app(self, app_id, no_data=False):
        return 1

    def get_field_description(self, app_handle, field_name):
        return {"name": field_name, "comment": ""}

    def send_request(self, method, params=None, handle=-1, timeout=None):
        self.sent.append((method, params))
        if method == "CreateSessionObject":
            return {"qReturn": {"qHandle": 7}}
        if method == "GetLayout":
            # Only the size matters here; rows come from GetHyperCubeData.
            return {"qLayout": {"qHyperCube": {"qSize": {"qcx": 1, "qcy": len(self._values)}}}}
        if method == "GetHyperCubeData":
            page = params[1][0]
            self.pages.append((page["qTop"], page["qHeight"]))
            window = self._values[page["qTop"]:page["qTop"] + page["qHeight"]]
            return {"qDataPages": [{"qMatrix": [[{"qText": v}] for v in window]}]}
        return {}

    @property
    def cube_def(self):
        for method, params in self.sent:
            if method == "CreateSessionObject":
                return params[0]["qHyperCubeDef"]
        raise AssertionError("no hypercube was created")

    @property
    def expression(self):
        return self.cube_def["qDimensions"][0]["qDef"]["qFieldDefs"][0]


class TestSearchIsPushedToEngine:
    def test_filter_becomes_a_calculated_dimension(self):
        eng = _Engine(values=["ACME Ltd"])
        eng.search_field_values(1, "Customer", "ACME*", limit=10)
        assert eng.expression.startswith("=If(")
        assert "WildMatch" in eng.expression
        assert "ACME*" in eng.expression
        assert eng.cube_def["qDimensions"][0]["qNullSuppression"] is True, (
            "non-matching values evaluate to NULL and must be suppressed, "
            "otherwise paging counts them")







class TestCaseSensitiveSearch:
    def test_engine_is_still_asked_for_the_wildcard_superset(self):
        """Match() cannot do wildcards, so WildMatch selects, we narrow."""
        eng = _Engine(values=["ACME", "acme"])
        eng.search_field_values(1, "Customer", "AC*", limit=10, case_sensitive=True)
        assert "WildMatch" in eng.expression









class TestWildcardSyntaxIsQliks:
    """Only `*` and `?` are wildcards; everything else is literal.

    fnmatch would also read `[...]` as a character class, so `Order[12]`
    would match the value `Order[1]` — a match Qlik would never make.
    """

    def test_brackets_are_literal(self):
        eng = _Engine(values=["Order[1]", "Order[2]", "Order[12]"])
        result = eng.search_field_values(1, "F", "Order[12]", limit=10,
                                         case_sensitive=True)
        assert [v["value"] for v in result["values"]] == ["Order[12]"]







