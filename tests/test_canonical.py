"""One text for one question, and Engine's codes in words.

Engine's result cache is shared by every user of an app and finds a result
again only for an expression written character for character the same way.
Two logically equal questions now go out as the same text. Engine answers a
heavy question with a bare number code; the reply now says what it means.
"""

import datetime
import json

import pytest

from qlik_sense_mcp_server.engine.canonical import (
    ENGINE_ERROR_MEANINGS, canonical_expression, explain_engine_error)
from qlik_sense_mcp_server.tools.helpers import _err, _ok
from tests.test_typed_queries import _Engine, _query

NOW = datetime.datetime(2026, 9, 30, 14, 5)


class TestOneText:
    def test_filters_in_any_order_write_the_same_modifier(self):
        engine = _Engine()
        first = engine._plan_query(1, "app", _query(filters=[
            {"field": "Region", "values": ["North"]},
            {"field": "Category", "values": ["North"]}]), 0)
        second = engine._plan_query(1, "app", _query(filters=[
            {"field": "Category", "values": ["North"]},
            {"field": "Region", "values": ["North"]}]), 0)
        assert (first["measures"][0]["expression"]
                == second["measures"][0]["expression"]
                == "Sum({<[Category]={'North'},[Region]={'North'}>} [Amount])")

    def test_hand_written_spacing_and_case_come_out_the_same(self):
        engine = _Engine()
        texts = [engine._plan_query(1, "app", _query(
            metrics=[], measures=[{"expression": text, "label": "x"}]), 0)
            ["measures"][0]["expression"]
            for text in ("sum( [Amount] )  /  COUNT(  [Amount])",
                         "Sum([Amount]) / Count([Amount])")]
        assert texts[0] == texts[1] == "Sum([Amount]) / Count([Amount])"

    def test_literals_and_names_are_left_alone(self):
        text = "sum({<[Region  A]={'north  x'}, B={\">=1  <2\"}>} [Amount])"
        assert canonical_expression(text, NOW) == (
            "Sum({<[Region  A]={'north  x'}, B={\">=1  <2\"}>} [Amount])")

    def test_a_comment_keeps_its_line_break(self):
        text = "Sum(A) // old\n+ sum(B)"
        assert canonical_expression(text, NOW) == "Sum(A) // old\n+ Sum(B)"

    def test_today_and_now_become_the_day(self):
        assert canonical_expression("Sum({<D={\">=$(=Today())\"}>} A) + Today(1)",
                                    NOW).endswith("A) + MakeDate(2026, 9, 30)")
        assert canonical_expression("Now()", NOW) == (
            "Timestamp(MakeDate(2026, 9, 30) + MakeTime(14, 5))")

    def test_an_unknown_function_keeps_its_case(self):
        assert canonical_expression("myFunc( A )", NOW) == "myFunc(A)"


class TestEngineCodes:
    @pytest.mark.parametrize("code", sorted(ENGINE_ERROR_MEANINGS))
    def test_each_code_gets_its_meaning(self, code):
        text = explain_engine_error(
            f"Engine API error for method 'GetLayout' (handle=3): "
            f"{{'code': {code}, 'parameter': '', 'message': 'x'}}")
        assert text.endswith(ENGINE_ERROR_MEANINGS[code])

    def test_the_meanings_differ(self):
        assert len(set(ENGINE_ERROR_MEANINGS.values())) == len(ENGINE_ERROR_MEANINGS)

    def test_an_unknown_code_is_left_as_it_was(self):
        assert explain_engine_error("{'code': 2, 'message': 'x'}") == \
            "{'code': 2, 'message': 'x'}"

    def test_a_refusal_in_a_batch_carries_the_meaning(self):
        reply = json.loads(_ok({"results": [
            {"id": "q1", "error": "{'code': 7004, 'message': 'Aborted'}"}]}))
        assert "time limit" in reply["results"][0]["error"]

    def test_a_tool_error_carries_it_too(self):
        assert "10000 cells" in json.loads(_err("{'qErrorCode': 7009}"))["error"]
