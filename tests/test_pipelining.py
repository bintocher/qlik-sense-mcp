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

    def test_results_land_in_request_order_even_when_responses_are_reversed(self):
        sock = _FakeSocket(
            {"GetLayout": {"qLayout": {"title": "x"}}}, reverse=True,
        )
        eng = _engine(sock)
        requests = [{"method": "GetLayout", "params": [], "handle": h} for h in (10, 20, 30)]
        results = eng.send_requests_pipelined(requests)
        # All three succeed and come back in *request* order, not arrival order.
        assert results == [{"qLayout": {"title": "x"}}] * 3

    def test_unique_ids_assigned_per_request(self):
        sock = _FakeSocket({"GetObject": {}})
        eng = _engine(sock)
        eng.send_requests_pipelined(
            [{"method": "GetObject", "params": {}, "handle": 0} for _ in range(4)]
        )
        ids = [f["id"] for f in sock.sent_frames]
        assert len(set(ids)) == 4

    def test_raise_on_error_true_aggregates_and_raises(self):
        sock = _FakeSocket(
            {"GetObject": {"qReturn": {"qHandle": 1}}},
            error_methods={"GetLayout"},
        )
        eng = _engine(sock)
        with pytest.raises(Exception, match="1/2 requests failed"):
            eng.send_requests_pipelined([
                {"method": "GetObject", "params": {}, "handle": 0},
                {"method": "GetLayout", "params": [], "handle": 1},
            ])

    def test_raise_on_error_false_returns_exception_in_place(self):
        sock = _FakeSocket(
            {"GetObject": {"qReturn": {"qHandle": 1}}},
            error_methods={"GetLayout"},
        )
        eng = _engine(sock)
        results = eng.send_requests_pipelined(
            [
                {"method": "GetObject", "params": {}, "handle": 0},
                {"method": "GetLayout", "params": [], "handle": 1},
            ],
            raise_on_error=False,
        )
        assert results[0] == {"qReturn": {"qHandle": 1}}
        assert isinstance(results[1], Exception)

    def test_empty_batch_is_a_noop(self):
        eng = _engine(_FakeSocket({}))
        assert eng.send_requests_pipelined([]) == []

    def test_stray_notification_frames_are_skipped(self):
        """A frame with no `id` (an Engine notification like OnConnected)
        must not be mistaken for a batch response."""

        class _SocketWithNotification(_FakeSocket):
            def send(self, data):
                if not self.sent_frames:
                    # Inject a notification ahead of the real response.
                    self._queue.append({"jsonrpc": "2.0", "method": "OnConnected", "params": {}})
                super().send(data)

        sock = _SocketWithNotification({"GetObject": {"qReturn": {"qHandle": 1}}})
        eng = _engine(sock)
        results = eng.send_requests_pipelined(
            [{"method": "GetObject", "params": {}, "handle": 0}]
        )
        assert results == [{"qReturn": {"qHandle": 1}}]


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
            if requests[0]["method"] == "GetObject":
                return [{"qReturn": {"qHandle": 100 + i}} for i in range(len(requests))]
            return [{"qLayout": {"title": f"obj{i}"}} for i in range(len(requests))]

        eng.send_request = fake_send_request
        eng.send_requests_pipelined = fake_pipelined
        eng._extract_fields_from_object = lambda layout: []

        result = eng._get_sheet_objects_detailed(app_handle=1, sheet_id="SH01")

        assert len(result) == 3
        assert [r["object_title"] for r in result] == ["obj0", "obj1", "obj2"]
        # Exactly 2 batched calls total, regardless of how many children.
        assert len(pipelined_calls) == 2
        assert pipelined_calls[0] == ["GetObject"] * 3
        assert pipelined_calls[1] == ["GetLayout"] * 3

    def test_one_bad_child_does_not_drop_the_rest(self):
        eng = QlikEngineAPI.__new__(QlikEngineAPI)

        def fake_send_request(method, params=None, handle=-1, timeout=None):
            if method == "GetObject":
                return {"qReturn": {"qHandle": 999}}
            if method == "GetLayout":
                return self._sheet_layout(["good1", "bad", "good2"])
            raise AssertionError(method)

        def fake_pipelined(requests, timeout=None, raise_on_error=True):
            if requests[0]["method"] == "GetObject":
                # "bad" fails to resolve a handle at all.
                return [
                    {"qReturn": {"qHandle": 100}},
                    Exception("GetObject failed"),
                    {"qReturn": {"qHandle": 102}},
                ]
            # Only good1/good2 made it to the GetLayout wave.
            assert len(requests) == 2
            return [{"qLayout": {"title": "good1"}}, {"qLayout": {"title": "good2"}}]

        eng.send_request = fake_send_request
        eng.send_requests_pipelined = fake_pipelined
        eng._extract_fields_from_object = lambda layout: []

        result = eng._get_sheet_objects_detailed(app_handle=1, sheet_id="SH01")

        assert [r["object_id"] for r in result] == ["good1", "good2"]
