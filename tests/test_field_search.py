"""Searching and paging a field happen in Engine, over the whole field.

The old path read the first 500-5000 values, matched them in Python and
sliced the result. On any field bigger than that prefix the answer was
wrong in a way nothing in the reply revealed: a value at position 150k did
not exist, `offset` past the prefix returned an empty page, and passing
both search parameters made the second silently overwrite the first.

Case is the one thing Qlik cannot filter for us. Verified on 31.62:
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

    def test_paging_applies_to_the_matches(self):
        eng = _Engine(values=[f"v{i}" for i in range(500)])
        eng.search_field_values(1, "Customer", "v*", limit=25, offset=100)
        assert eng.pages == [(100, 25)]

    def test_percent_is_accepted_as_a_wildcard(self):
        eng = _Engine(values=[])
        eng.search_field_values(1, "Customer", "ACME%", limit=10)
        assert "ACME*" in eng.expression

    def test_quotes_in_the_pattern_are_escaped(self):
        """An apostrophe would otherwise end the Qlik string literal."""
        eng = _Engine(values=[])
        eng.search_field_values(1, "Customer", "O'Brien*", limit=10)
        assert "O''Brien*" in eng.expression

    def test_total_matches_comes_from_the_engine(self):
        eng = _Engine(values=[f"v{i}" for i in range(4321)])
        result = eng.search_field_values(1, "Customer", "*", limit=2)
        assert result["total_matches"] == 4321

    def test_the_session_object_is_destroyed(self):
        eng = _Engine(values=["a"])
        eng.search_field_values(1, "Customer", "a*", limit=10)
        assert any(m == "DestroySessionObject" for m, _ in eng.sent)


class TestCaseSensitiveSearch:
    def test_engine_is_still_asked_for_the_wildcard_superset(self):
        """Match() cannot do wildcards, so WildMatch selects, we narrow."""
        eng = _Engine(values=["ACME", "acme"])
        eng.search_field_values(1, "Customer", "AC*", limit=10, case_sensitive=True)
        assert "WildMatch" in eng.expression

    def test_only_exact_case_survives(self):
        eng = _Engine(values=["ACME", "acme", "Acme", "ACMExyz"])
        result = eng.search_field_values(1, "Customer", "ACME*", limit=10,
                                         case_sensitive=True)
        assert [v["value"] for v in result["values"]] == ["ACME", "ACMExyz"]

    def test_wildcards_keep_their_meaning(self):
        """`C1*9` must not degrade into "contains C19"."""
        eng = _Engine(values=["C1009", "C19", "C1x9", "c1009"])
        result = eng.search_field_values(1, "Customer", "C1*9", limit=10,
                                         case_sensitive=True)
        assert [v["value"] for v in result["values"]] == ["C1009", "C19", "C1x9"]

    def test_question_mark_matches_one_character(self):
        eng = _Engine(values=["ab", "axb", "axxb"])
        result = eng.search_field_values(1, "F", "a?b", limit=10, case_sensitive=True)
        assert [v["value"] for v in result["values"]] == ["axb"]

    def test_paging_applies_after_the_case_filter(self):
        values = []
        for i in range(30):
            values += [f"ACME{i}", f"acme{i}"]
        eng = _Engine(values=values)
        page = eng.search_field_values(1, "F", "ACME*", limit=5, offset=5,
                                       case_sensitive=True)
        assert [v["value"] for v in page["values"]] == [f"ACME{i}" for i in range(5, 10)]

    def test_exact_total_when_the_whole_superset_was_read(self):
        eng = _Engine(values=["ACME", "acme", "ACMEX"])
        result = eng.search_field_values(1, "F", "ACME*", limit=10, case_sensitive=True)
        assert result["total_matches"] == 2
        assert "total_matches_at_least" not in result

    def test_an_early_stop_does_not_quote_a_total_it_did_not_count(self):
        eng = _Engine(values=[f"ACME{i}" for i in range(1000)])
        result = eng.search_field_values(1, "F", "ACME*", limit=5, case_sensitive=True)
        assert "total_matches" not in result, (
            "only part of the superset was read, so no exact total exists")
        assert result["total_matches_at_least"] >= 5

    def test_scanning_is_capped(self, monkeypatch):
        """One search must not walk a 200k-value field end to end."""
        monkeypatch.setattr(QlikEngineAPI, "MAX_CASE_SENSITIVE_SCAN", 100)
        eng = _Engine(values=[f"acme{i}" for i in range(5000)])  # nothing matches
        result = eng.search_field_values(1, "F", "ACME*", limit=5, case_sensitive=True)
        assert result["search_truncated"] is True
        assert result["candidates_scanned"] <= 300


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

    def test_a_dot_is_literal(self):
        eng = _Engine(values=["a.b", "axb"])
        result = eng.search_field_values(1, "F", "a.b", limit=10, case_sensitive=True)
        assert [v["value"] for v in result["values"]] == ["a.b"]

    def test_regex_metacharacters_are_literal(self):
        eng = _Engine(values=["a+b", "aab", "a(b)"])
        result = eng.search_field_values(1, "F", "a+b", limit=10, case_sensitive=True)
        assert [v["value"] for v in result["values"]] == ["a+b"]

    def test_star_still_spans_anything(self):
        eng = _Engine(values=["ab", "axxxb", "b"])
        result = eng.search_field_values(1, "F", "a*b", limit=10, case_sensitive=True)
        assert [v["value"] for v in result["values"]] == ["ab", "axxxb"]


class TestPlainPagingIsPushedToEngine:
    def test_offset_is_the_page_top_not_a_local_slice(self):
        eng = _Engine(values=[])
        eng.get_field_values(1, "Customer", max_values=10, offset=5000)
        create = next(p for m, p in eng.sent if m == "CreateSessionObject")
        page = create[0]["qListObjectDef"]["qInitialDataFetch"][0]
        assert page["qTop"] == 5000
        assert page["qHeight"] == 10


class TestToolLayer:
    @pytest.fixture
    def engine(self, monkeypatch):
        def _install(values=None):
            eng = _Engine(values=values)
            monkeypatch.setattr(context, "engine_api", eng)
            monkeypatch.setattr(context, "repo_api", object())
            return eng
        return _install

    def _call(self, **kwargs):
        fn = getattr(srv.get_app_field, "fn", srv.get_app_field)
        return json.loads(fn(**kwargs))

    def test_search_result_is_returned_as_is(self, engine):
        engine(values=["C199990", "C199999"])
        result = self._call(app_id="app", field_name="client_id",
                            search_string="C19999*")
        assert result["field_values"] == ["C199990", "C199999"]
        assert result["total_matches"] == 2

    def test_no_local_filtering_of_the_page(self, engine):
        """Engine already applied the filter; re-filtering can only lose rows."""
        eng = engine(values=["match-1", "match-2"])
        result = self._call(app_id="app", field_name="f", search_string="nomatch*")
        assert result["field_values"] == ["match-1", "match-2"]
        assert eng.cube_def is not None

    def test_both_search_parameters_together_are_refused(self, engine):
        """They filtered the same values, and the second overwrote the first."""
        engine(values=[])
        result = self._call(app_id="app", field_name="f",
                            search_string="a*", search_number="1*")
        assert result["error_category"] == "invalid_argument"

    def test_truncation_flags_reach_the_caller(self, engine, monkeypatch):
        """A capped scan must not read as the complete answer."""
        monkeypatch.setattr(QlikEngineAPI, "MAX_CASE_SENSITIVE_SCAN", 100)
        engine(values=[f"acme{i}" for i in range(5000)])
        result = self._call(app_id="app", field_name="f",
                            search_string="ACME*", case_sensitive=True)
        assert result["search_truncated"] is True
        assert "total_matches" not in result
        assert result["candidates_scanned"] > 0

    def test_an_exact_count_is_passed_through(self, engine):
        engine(values=["ACME", "acme"])
        result = self._call(app_id="app", field_name="f",
                            search_string="ACME*", case_sensitive=True)
        assert result["total_matches"] == 1
        assert "search_truncated" not in result

    def test_unknown_field_is_still_refused_before_searching(self, engine, monkeypatch):
        eng = engine(values=[])
        monkeypatch.setattr(eng, "get_field_description", lambda *a, **kw: {})
        result = self._call(app_id="app", field_name="nope", search_string="a*")
        assert result["error_category"] == "field_not_found"
