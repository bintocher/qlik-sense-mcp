"""The load script comes a page of lines at a time, or around a word.

A large app's script runs to thousands of lines, and whole it is one of the
largest replies a model can ask for - mostly about things the question is
not. Pages joined back must be the script exactly, or a model reading page
by page sees something the app does not run.
"""

import json
from contextlib import contextmanager

import pytest

from qlik_sense_mcp_server.tools import engine as tools
from qlik_sense_mcp_server.tools.engine import script_page

SCRIPT = "\r\n".join(
    [f"// section {i}" if i % 50 == 0 else f"LOAD a{i}, b{i} FROM t{i}.qvd;" for i in range(1030)]
) + "\r\n"


class _Engine:
    @contextmanager
    def transaction(self):
        yield

    def ensure_app(self, app_id, **kwargs):
        return 1

    def get_script(self, handle):
        return SCRIPT


@pytest.fixture
def call(monkeypatch):
    monkeypatch.setattr(tools.context, "engine_api", _Engine())
    monkeypatch.setattr(tools.context, "repo_api", object())
    return lambda **kw: json.loads(tools.get_app_script("app-1", **kw))


def test_pages_join_back_into_the_script(call):
    pages, offset = [], 0
    while True:
        reply = call(offset=offset)
        pages.append(reply["qScript"])
        assert reply["total_lines"] == 1031
        if "next_offset" not in reply:
            break
        offset = reply["next_offset"]
    assert len(pages) == 3
    assert "\n".join(pages) == SCRIPT
    assert reply["script_length"] == len(SCRIPT)


def test_the_first_page_is_400_lines(call):
    reply = call()
    assert reply["returned_lines"] == 400
    assert reply["next_offset"] == 400


def test_search_returns_the_hits_with_five_lines_around():
    page = script_page(SCRIPT, 0, 400, "SECTION 1")
    hit_lines = [i for i, line in enumerate(SCRIPT.split("\n")) if "section 1" in line.lower()]
    assert page["match_count"] == len(hit_lines)
    first = page["matches"][0]
    low, high = (int(x) for x in first["lines"].split("-"))
    assert low == hit_lines[0] - 5 and high == hit_lines[0] + 5
    assert "section 1" in first["text"].lower()
    assert "qScript" not in page


def test_neighbouring_hits_merge_into_one_block():
    script = "\n".join(["x"] * 20 + ["hit"] + ["x"] * 3 + ["hit"] + ["x"] * 20)
    page = script_page(script, 0, 400, "hit")
    assert page["match_count"] == 2
    assert [m["lines"] for m in page["matches"]] == ["15-29"]


def test_search_pages_by_blocks():
    script = "\n".join(("hit" if i % 100 == 0 else "x") for i in range(1000))
    page = script_page(script, 0, 25, "hit")
    assert len(page["matches"]) == 2
    assert page["next_offset"] == 195
    rest = script_page(script, page["next_offset"], 25, "hit")
    assert rest["matches"][0]["lines"] == "195-205"


def test_a_bad_offset_is_refused(call):
    reply = call(offset=-1)
    assert reply["error_category"] == "invalid_argument"
