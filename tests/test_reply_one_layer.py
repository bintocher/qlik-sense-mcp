"""A reply reaches the client as JSON, not as JSON inside a string.

Tools return their JSON document as text. The SDK infers structured output
from the `-> str` annotation and, left to itself, also sends that text as
`structuredContent: {"result": "<json>"}`; a client that prefers the
structured copy shows the model an escaped string a third longer than the
reply. Tools are registered without structured output, so the text content
is the only copy.
"""

import asyncio
import json

from qlik_sense_mcp_server import server as srv
from qlik_sense_mcp_server.tools import context


def test_no_tool_declares_an_output_schema():
    tools = asyncio.run(context.mcp.list_tools())
    assert tools
    assert [t.name for t in tools if getattr(t, "outputSchema", None)] == []


def test_a_call_answers_with_the_json_text_alone(monkeypatch):
    class _Repo:
        def get_about(self):
            return {"buildVersion": "1.2.3"}

    monkeypatch.setattr(context, "repo_api", _Repo())
    result = asyncio.run(context.mcp.call_tool("get_about", {}))
    # 1.x returns either the content list or (content, structured);
    # structured must be absent either way.
    if isinstance(result, tuple):
        content, structured = result
        assert not structured
    else:
        content = result
    assert len(content) == 1
    reply = json.loads(content[0].text)
    assert reply["buildVersion"] == "1.2.3"
    assert srv.get_about  # the plain function stays importable
