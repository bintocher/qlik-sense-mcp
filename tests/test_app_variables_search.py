"""get_app_variables finds a variable by a part of its name.

A model searched for "BK_LS" and got nothing back next to a variable named
"vBK_LS": the search was a whole-text pattern, so a part matched nothing,
and the model concluded the variable was not in the app.
"""

import json
from contextlib import contextmanager

import pytest

from qlik_sense_mcp_server.tools import engine as engine_tools


class _Engine:
    @contextmanager
    def transaction(self):
        yield

    def ensure_app(self, app_id, **kwargs):
        return 1

    def _get_user_variables(self, app_handle):
        return [
            {"name": "vBK_LS", "text_value": "=If(GetFieldSelections([Side]) = 'B', 'B', 'A')",
             "is_script_created": False},
            {"name": "vToday", "text_value": "=Today()", "is_script_created": True},
        ]


@pytest.fixture
def variables(monkeypatch):
    monkeypatch.setattr(engine_tools.context, "engine_api", _Engine())
    monkeypatch.setattr(engine_tools, "_check", lambda: None)

    def run(**kwargs):
        return json.loads(engine_tools.get_app_variables("app-1", **kwargs))
    return run


def test_a_part_of_the_name_finds_the_variable(variables):
    reply = variables(search_string="BK_LS")
    assert reply["variables_from_ui"] == {
        "vBK_LS": "=If(GetFieldSelections([Side]) = 'B', 'B', 'A')"}
    assert reply["total_found"] == 1


def test_a_part_of_the_value_finds_it_too(variables):
    assert list(variables(search_string="today()")["variables_from_script"]) == ["vToday"]


def test_a_pattern_with_a_star_is_matched_whole(variables):
    assert variables(search_string="LS*")["total_found"] == 0
    assert variables(search_string="v*")["total_found"] == 2
