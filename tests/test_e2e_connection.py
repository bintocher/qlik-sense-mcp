"""End-to-end: the WebSocket must be reused, not re-opened.

Qlik allows a limited number of concurrent Engine sessions per user (5 by
default) and a closed socket does not free its session immediately — it
lingers until the virtual proxy's inactivity timeout. So a client that
reconnects per call does not merely lose the cache: after a handful of
calls Qlik starts refusing new sessions outright with a fatal greeting,
and every tool fails for minutes afterwards.

That is not hypothetical. A WebSocket ping in the liveness check made
every second call reconnect, and a live session hit the cap within a
couple of minutes of ordinary use. None of the offline tests noticed,
because through a direct Engine socket the very same ping is harmless.
"""

import time

import pytest

pytestmark = pytest.mark.e2e


@pytest.fixture
def reconnect_counter(live):
    """Count how many times the client opens a fresh Engine connection."""
    engine = live.engine_api
    original = engine.connect
    calls = []

    def counting_connect(*args, **kwargs):
        calls.append(kwargs.get("app_id") or (args[0] if args else None))
        return original(*args, **kwargs)

    engine.connect = counting_connect
    yield calls
    engine.connect = original


class TestConnectionReuse:
    def test_repeated_calls_open_exactly_one_connection(self, call, app_id, reconnect_counter):
        for _ in range(5):
            call("get_app_script", app_id=app_id)
        assert len(reconnect_counter) <= 1, (
            f"5 calls against one app opened {len(reconnect_counter)} connections; "
            "each one burns a Qlik session that lingers past the socket close")

    def test_second_call_is_not_slower_than_the_first(self, call, app_id):
        """A wedged socket shows up as a call that hangs for the whole timeout."""
        started = time.monotonic()
        call("get_app_script", app_id=app_id)
        first = time.monotonic() - started

        started = time.monotonic()
        call("get_app_script", app_id=app_id)
        second = time.monotonic() - started

        assert second < max(first * 5, 10.0), (
            f"second call took {second:.1f}s against {first:.1f}s for the first — "
            "the cached socket is not answering")

    def test_mixed_tools_share_the_connection(self, call, app_id, text_field, reconnect_counter):
        call("get_app_script", app_id=app_id)
        call("get_app_variables", app_id=app_id)
        call("get_app_field", app_id=app_id, field_name=text_field["name"], limit=3)
        call("engine_create_hypercube", app_id=app_id,
             dimensions=[{"field": text_field["name"]}],
             measures=[{"expression": "Count(1)", "label": "cnt"}], limit=5)
        assert len(reconnect_counter) <= 1, (
            "different tools against the same app must ride the same socket")

    def test_switching_apps_reconnects_once_and_still_works(self, call, live, app_id,
                                                            sheet_app_id, reconnect_counter):
        if sheet_app_id == app_id:
            pytest.skip("need a second app — set QLIK_E2E_SHEET_APP")
        first = call("get_app_script", app_id=app_id)
        other = call("get_app_script", app_id=sheet_app_id)
        back = call("get_app_script", app_id=app_id)

        assert first["app_id"] == back["app_id"] == app_id
        assert other["app_id"] == sheet_app_id
        # Switching documents is allowed to reconnect, but only per switch:
        # Qlik must never be left holding two open documents for this session.
        assert len(reconnect_counter) <= 3
        assert live.engine_api._cached_app_id == app_id


class TestLivenessCheck:
    def test_idle_connection_is_revalidated_without_breaking(self, call, live, app_id,
                                                             can_read_script):
        """Force the idle path: the probe must not wedge the socket."""
        call("get_app_script", app_id=app_id)
        engine = live.engine_api
        engine._last_successful_io -= (engine.ws_idle_probe_after + 1)

        assert engine._is_connected() is True
        # And the connection is still usable right after being probed —
        # this is precisely what a WebSocket ping breaks through a proxy.
        started = time.monotonic()
        result = call("get_app_script", app_id=app_id)
        if can_read_script:
            assert result["script_length"] > 0
        assert time.monotonic() - started < 10.0, "call after the liveness probe hung"

    def test_probe_does_not_consume_the_next_reply(self, call, live, app_id, text_field):
        engine = live.engine_api
        call("get_app_script", app_id=app_id)
        engine._last_successful_io -= (engine.ws_idle_probe_after + 1)
        engine._is_connected()
        # A probe that left an unread frame behind would make this call
        # read the probe's answer and then wait forever for its own.
        values = call("get_app_field", app_id=app_id, field_name=text_field["name"], limit=3)
        assert values.get("field_values")


class TestPipelinedBatch:
    def test_batched_sheet_objects_match_a_sequential_read(self, call, live, sheet_app_id):
        """The pipelined path must return exactly what one-by-one reads do."""
        sheets = call("get_app_sheets", app_id=sheet_app_id)
        if not sheets["sheets"]:
            pytest.skip("no sheets in the app")
        sheet_id = sheets["sheets"][0]["sheet_id"]

        batched = call("get_app_sheet_objects", app_id=sheet_app_id, sheet_id=sheet_id)
        items = batched.get("objects") or []
        if len(items) < 2:
            pytest.skip("need a sheet with at least 2 objects")

        # Same objects, read individually through the non-batched path.
        engine = live.engine_api
        handle = engine.ensure_app(sheet_app_id)
        sheet = engine.send_request("GetObject", {"qId": sheet_id}, handle=handle)
        sheet_layout = engine.send_request(
            "GetLayout", [], handle=sheet["qReturn"]["qHandle"])
        expected = {child["qInfo"]["qId"]
                    for child in sheet_layout["qLayout"]["qChildList"]["qItems"]}

        assert {item["object_id"] for item in items} <= expected
        assert len(items) >= min(2, len(expected))

    def test_batch_keeps_the_socket_healthy(self, call, live, sheet_app_id, app_id):
        """A half-drained batch would poison every later call on this socket."""
        sheets = call("get_app_sheets", app_id=sheet_app_id)
        if not sheets["sheets"]:
            pytest.skip("no sheets in the app")
        call("get_app_sheet_objects", app_id=sheet_app_id,
             sheet_id=sheets["sheets"][0]["sheet_id"])
        # Anything left unread from the batch would surface here.
        assert call("get_app_script", app_id=sheet_app_id)["app_id"] == sheet_app_id
