"""A call whose Engine socket is gone is made once more on a new one.

Measured on a live server: right after the JWT session renewed, one call
failed with "Not connected to Engine API" (the proxy had closed the socket,
which still read as open), the next with OpenDoc 1003 "App not found", and
the third worked. The calls were not concurrent: they ran one after the
other under the Engine lock. Two calls that do arrive together are
serialised, the second waiting for the first to open the app.
"""

import json
import threading
import time
from contextlib import contextmanager

from qlik_sense_mcp_server.tools import helpers


class _Engine:
    def __init__(self):
        self._lock = threading.RLock()
        self.disconnects = 0
        self.inside = 0
        self.overlapped = False

    @contextmanager
    def transaction(self):
        with self._lock:
            yield self

    def disconnect(self):
        self.disconnects += 1

    def ensure_app(self):
        self.inside += 1
        if self.inside > 1:
            self.overlapped = True
        time.sleep(0.05)
        self.inside -= 1
        return 1


def _with_engine(monkeypatch):
    engine = _Engine()
    monkeypatch.setattr(helpers.context, "engine_api", engine)
    return engine


def test_a_call_that_lost_its_socket_is_made_once_more(monkeypatch):
    engine = _with_engine(monkeypatch)
    replies = iter([
        helpers._err("Not connected to Engine API", error_type="ConnectionError"),
        helpers._ok({"rows": [[1]]}),
    ])
    tool = helpers._engine_serialised(lambda: next(replies))
    assert json.loads(tool()) == {"rows": [[1]]}
    assert engine.disconnects == 1


def test_open_doc_1003_after_a_new_session_is_tried_again(monkeypatch):
    engine = _with_engine(monkeypatch)
    calls = []

    def tool_body():
        calls.append(1)
        if len(calls) == 1:
            return helpers._err("Engine API error for method 'OpenDoc' (handle=-1): "
                                "{'code': 1003, 'parameter': '', 'message': 'App not found'}")
        return helpers._ok({"done": True})
    assert json.loads(helpers._engine_serialised(tool_body)()) == {"done": True}
    assert len(calls) == 2 and engine.disconnects == 1


def test_a_raised_connection_error_is_tried_again(monkeypatch):
    _with_engine(monkeypatch)
    calls = []

    def tool_body():
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("Not connected to Engine API")
        return helpers._ok({"done": True})
    assert json.loads(helpers._engine_serialised(tool_body)()) == {"done": True}


def test_a_timeout_or_a_wrong_query_is_not_repeated(monkeypatch):
    engine = _with_engine(monkeypatch)
    for error in ("WebSocket recv() timed out after 15.0s waiting for response",
                  "Unknown field(s) in measures: [X]"):
        calls = []

        def tool_body():
            calls.append(1)
            return helpers._err(error)
        helpers._engine_serialised(tool_body)()
        assert len(calls) == 1
    assert engine.disconnects == 0


def test_two_calls_at_once_open_the_app_one_after_the_other(monkeypatch):
    engine = _with_engine(monkeypatch)
    tool = helpers._engine_serialised(lambda: helpers._ok({"handle": engine.ensure_app()}))
    results = []
    threads = [threading.Thread(target=lambda: results.append(json.loads(tool())))
               for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [{"handle": 1}, {"handle": 1}]
    assert engine.overlapped is False
