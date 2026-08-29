"""Tests for send_requests_pipelined() and its use in _get_sheet_objects_detailed().

The whole point of pipelining is sending N requests before reading any
response, then matching replies back to requests by `id` regardless of the
order they arrive in — that's the behavior these tests exercise directly
against a fake socket, not through send_request() (which is untouched).
"""

import json

import pytest

from qlik_sense_mcp_server.engine_api import QlikEngineAPI


class _FakeSocket:
    """Stand-in for `websocket.WebSocket`.

    `send()` just records the frame and queues a scripted response for it
    (looked up by method name). `recv()` pops from that queue — in
    `reverse` mode it pops last-in-first-out, so responses come back in the
    opposite order from how the requests were sent, proving id-matching
    doesn't depend on send order.
    """

    def __init__(self, responses_by_method, reverse=False, error_methods=()):
        self.sent_frames = []
        self._responses_by_method = responses_by_method
        self._reverse = reverse
        self._error_methods = set(error_methods)
        self._queue = []
        self.sock = None  # so _set_socket_timeout's `self.ws.sock` check is a no-op

    def send(self, data):
        frame = json.loads(data)
        self.sent_frames.append(frame)
        if frame["method"] in self._error_methods:
            response = {"jsonrpc": "2.0", "id": frame["id"],
                        "error": {"message": f"{frame['method']} failed"}}
        else:
            result = self._responses_by_method.get(frame["method"], {})
            response = {"jsonrpc": "2.0", "id": frame["id"], "result": result}
        self._queue.append(response)

    def recv(self):
        return json.dumps(self._queue.pop() if self._reverse else self._queue.pop(0))

    def settimeout(self, *_a, **_kw):
        pass


def _engine(sock):
    eng = QlikEngineAPI.__new__(QlikEngineAPI)
    eng.ws = sock
    eng.request_id = 0
    eng.ws_timeout_seconds = 30.0
    return eng


class TestSendRequestsPipelined:
    def test_sends_all_before_reading_any_response(self):
        """The defining property of pipelining: every request is on the
        wire before we start waiting on responses."""
        sock = _FakeSocket({"GetObject": {"qReturn": {"qHandle": 1}}})
        eng = _engine(sock)
        eng.send_requests_pipelined(
            [{"method": "GetObject", "params": {"qId": f"obj{i}"}, "handle": 0}
             for i in range(5)]
        )
        assert len(sock.sent_frames) == 5
        assert [f["params"]["qId"] for f in sock.sent_frames] == [f"obj{i}" for i in range(5)]








class TestSheetObjectsUsesPipelining:
    """`_get_sheet_objects_detailed` should issue exactly 2 pipelined
    batches for N child objects, not 2N sequential send_request calls."""

    def _sheet_layout(self, child_ids):
        return {
            "qLayout": {
                "qChildList": {
                    "qItems": [
                        {"qInfo": {"qId": cid, "qType": "chart"}} for cid in child_ids
                    ]
                }
            }
        }

    def test_all_children_resolved_via_two_pipelined_waves(self, monkeypatch):
        eng = QlikEngineAPI.__new__(QlikEngineAPI)
        pipelined_calls = []

        def fake_send_request(method, params=None, handle=-1, timeout=None):
            if method == "GetObject":
                return {"qReturn": {"qHandle": 999}}  # sheet handle itself
            if method == "GetLayout":
                return self._sheet_layout(["c1", "c2", "c3"])
            raise AssertionError(f"unexpected send_request call: {method}")

        def fake_pipelined(requests, timeout=None, raise_on_error=True):
            pipelined_calls.append([r["method"] for r in requests])
            method = requests[0]["method"]
            if method == "GetObject":
                return [{"qReturn": {"qHandle": 100 + i}} for i in range(len(requests))]
            if method == "GetProperties":
                return [{"qProp": {"qHyperCubeDef": {}}} for _ in requests]
            return [{"qLayout": {"title": f"obj{i}"}} for i in range(len(requests))]

        eng.send_request = fake_send_request
        eng.send_requests_pipelined = fake_pipelined
        eng._extract_fields_from_object = lambda layout: []

        result = eng._get_sheet_objects_detailed(app_handle=1, sheet_id="SH01")

        assert len(result) == 3
        assert [r["object_title"] for r in result] == ["obj0", "obj1", "obj2"]
        # The point is that the cost is a constant number of round-trips, not
        # one per object: handles, layouts and properties, each in one batch.
        # (Properties are needed because the layout has no measure
        # expressions — Engine simply does not put them there.)
        assert [len(batch) for batch in pipelined_calls] == [3, 3, 3]
        assert [batch[0] for batch in pipelined_calls] == [
            "GetObject", "GetLayout", "GetProperties"]

