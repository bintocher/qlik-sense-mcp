"""Schedule triggers must be ones Qlik can actually run.

Every field here was wrong before, and the failure mode was silence: QRS
rejected the whole call with 400 because of an empty `operational`, and had
it got past that, `incrementOption` used a numbering that does not exist,
the interval landed in the *days* position, and `schemaFilterDescription`
received an enum code where an 8-position window string belongs. A caller
would have been told a schedule exists that never fires.

Position/enum values are taken from the server's own enum endpoint and from
a working trigger read back off a live Qlik, not from guesswork.
"""

import json

import pytest

from qlik_sense_mcp_server import server as srv
from qlik_sense_mcp_server.tools import context
from qlik_sense_mcp_server.repository_api import QlikRepositoryAPI


class _Qrs(QlikRepositoryAPI):
    def __init__(self, reply=None):
        self.sent = []
        self._reply = reply if reply is not None else {"id": "new-guid"}

    def _make_request(self, method, endpoint, **kwargs):
        self.sent.append((method, endpoint, kwargs))
        return self._reply

    @property
    def body(self):
        return self.sent[0][2]["json"]


class TestIncrementDescription:
    @pytest.mark.parametrize("minutes,expected", [
        (1440, "0 0 1 0"),      # daily
        (60, "0 1 0 0"),        # hourly
        (15, "15 0 0 0"),       # quarter-hourly
        (10080, "0 0 0 1"),     # weekly
        (90, "30 1 0 0"),       # an hour and a half
        (0, "0 0 0 0"),
    ])
    def test_interval_is_split_into_units(self, minutes, expected):
        """"minutes hours days weeks" — the interval used to land in days."""
        assert QlikRepositoryAPI._increment_description(minutes) == expected

    def test_daily_is_one_day_not_1440_of_something(self):
        qrs = _Qrs()
        qrs.create_schema_trigger("t", "n", repeat="daily", increment_minutes=1440)
        assert qrs.body["incrementDescription"] == "0 0 1 0"


class TestScheduleBody:
    def test_repeat_maps_to_the_qrs_increment_option(self):
        for repeat, option in [("once", 0), ("hourly", 1), ("daily", 2),
                               ("weekly", 3), ("monthly", 4)]:
            qrs = _Qrs()
            qrs.create_schema_trigger("t", "n", repeat=repeat)
            assert qrs.body["incrementOption"] == option, repeat

    def test_schema_filter_is_an_eight_position_window_string(self):
        qrs = _Qrs()
        qrs.create_schema_trigger("t", "n", repeat="daily")
        [window] = qrs.body["schemaFilterDescription"]
        assert len(window.split(" ")) == 8, f"not 8 positions: {window!r}"
        assert window.split(" ")[2] == "-", "third position is the week prefix"

    def test_custom_window_is_passed_through(self):
        qrs = _Qrs()
        qrs.create_schema_trigger("t", "n", repeat="hourly",
                                  schema_filter="45 3-21 - * * * * *")
        assert qrs.body["schemaFilterDescription"] == ["45 3-21 - * * * * *"]

    def test_no_empty_operational_section(self):
        """QRS answers 400 "operational with EMPTY GuID" and creates nothing."""
        qrs = _Qrs()
        qrs.create_schema_trigger("t", "n")
        assert "operational" not in qrs.body

    def test_task_and_timezone_are_set(self):
        qrs = _Qrs()
        qrs.create_schema_trigger("task-guid", "Nightly",
                                  time_zone="Europe/Moscow",
                                  start_date="2026-09-01T03:00:00.000Z")
        body = qrs.body
        assert body["reloadTask"] == {"id": "task-guid"}
        assert body["timeZone"] == "Europe/Moscow"
        assert body["startDate"] == "2026-09-01T03:00:00.000Z"
        assert body["eventType"] == 0
        assert body["daylightSavingTime"] == 0

    def test_unknown_repeat_is_refused_at_the_repository_layer(self):
        qrs = _Qrs()
        result = qrs.create_schema_trigger("t", "n", repeat="fortnightly")
        assert "error" in result
        assert not qrs.sent, "nothing may be sent to QRS for an unusable repeat"


class TestToolLayer:
    @pytest.fixture
    def tool(self, monkeypatch):
        def _install(reply=None):
            qrs = _Qrs(reply)
            monkeypatch.setattr(context, "repo_api", qrs)
            return qrs
        return _install

    def _call(self, **kwargs):
        fn = getattr(srv.create_task_schedule, "fn", srv.create_task_schedule)
        return json.loads(fn(**kwargs))

    def test_typo_in_repeat_is_rejected_not_silently_daily(self, tool):
        """"minutely" is not a QRS option; it used to fall back to daily."""
        qrs = tool()
        result = self._call(task_id="t", name="n", repeat="minutely")
        assert result["error_category"] == "invalid_argument"
        assert "hourly" in result["allowed_values"]
        assert not qrs.sent

    def test_repeat_is_case_insensitive(self, tool):
        qrs = tool()
        self._call(task_id="t", name="n", repeat="DAILY")
        assert qrs.body["incrementOption"] == 2

    @pytest.mark.parametrize("interval", [0, -30])
    def test_repeating_schedule_needs_a_real_interval(self, tool, interval):
        qrs = tool()
        result = self._call(task_id="t", name="n", repeat="hourly",
                            interval_minutes=interval)
        assert result["error_category"] == "invalid_argument"
        assert not qrs.sent

    def test_once_ignores_the_interval(self, tool):
        qrs = tool()
        result = self._call(task_id="t", name="n", repeat="once", interval_minutes=0)
        assert "error" not in result
        assert qrs.body["incrementOption"] == 0

    def test_time_window_reaches_qrs(self, tool):
        """Without this the caller can only set how often, never when."""
        qrs = tool()
        self._call(task_id="t", name="n", repeat="hourly",
                   time_window="45 3-21 - * * * * *")
        assert qrs.body["schemaFilterDescription"] == ["45 3-21 - * * * * *"]

    def test_malformed_time_window_is_refused(self, tool):
        qrs = tool()
        result = self._call(task_id="t", name="n", time_window="45 3-21 *")
        assert result["error_category"] == "invalid_argument"
        assert not qrs.sent, "a window Qlik cannot parse must not be sent"

    def test_no_window_means_no_restriction(self, tool):
        qrs = tool()
        self._call(task_id="t", name="n", repeat="daily")
        assert qrs.body["schemaFilterDescription"] == ["* * - * * * * *"]

    def test_the_default_start_date_is_in_the_future(self, tool):
        """A `once` schedule dated in the past never fires; the old default
        was a fixed date that had long since passed."""
        from datetime import datetime, timezone

        qrs = tool()
        self._call(task_id="t", name="n", repeat="once")
        start = qrs.body["startDate"]
        assert start > datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    def test_an_explicit_start_date_is_used_verbatim(self, tool):
        qrs = tool()
        self._call(task_id="t", name="n", start_date="2027-01-01T03:00:00.000")
        assert qrs.body["startDate"] == "2027-01-01T03:00:00.000"

    def test_qrs_rejection_is_surfaced(self, tool):
        tool(reply={"error": "HTTP 400: invalid property"})
        result = self._call(task_id="t", name="n", repeat="daily")
        assert result["error_category"] == "repository_error"
        assert "invalid property" in result["error"]


class TestUpdateAndDelete:
    """One trigger at a time.

    Before these existed, the only way to stop a schedule through this
    server was `update_task(enabled=False)` — which stops the task and
    every other trigger attached to it.
    """

    @pytest.fixture
    def tool(self, monkeypatch):
        def _install(reply=None, current=None):
            class _Qrs2(_Qrs):
                def _make_request(self, method, endpoint, **kwargs):
                    self.sent.append((method, endpoint, kwargs))
                    if method == "GET":
                        return current if current is not None else {
                            "id": "trig", "name": "Nightly", "enabled": True,
                            "incrementOption": 2, "modifiedDate": "2026-08-10T00:00:00Z",
                        }
                    return self._reply

            qrs = _Qrs2(reply)
            monkeypatch.setattr(context, "repo_api", qrs)
            return qrs
        return _install

    def _update(self, **kwargs):
        fn = getattr(srv.update_task_schedule, "fn", srv.update_task_schedule)
        return json.loads(fn(**kwargs))

    def _delete(self, **kwargs):
        fn = getattr(srv.delete_task_schedule, "fn", srv.delete_task_schedule)
        return json.loads(fn(**kwargs))

    def test_disabling_one_trigger_keeps_the_rest_of_the_object(self, tool):
        qrs = tool()
        self._update(trigger_id="trig", enabled=False)
        body = qrs.sent[-1][2]["json"]
        assert body["enabled"] is False
        assert body["name"] == "Nightly", "unrelated fields must survive the PUT"

    def test_repeat_and_interval_are_translated(self, tool):
        qrs = tool()
        self._update(trigger_id="trig", repeat="hourly", interval_minutes=90)
        body = qrs.sent[-1][2]["json"]
        assert body["incrementOption"] == 1
        assert body["incrementDescription"] == "30 1 0 0"

    def test_window_is_validated_before_the_call(self, tool):
        qrs = tool()
        result = self._update(trigger_id="trig", time_window="45 3-21 *")
        assert result["error_category"] == "invalid_argument"
        assert not qrs.sent

    def test_an_empty_update_is_refused(self, tool):
        qrs = tool()
        result = self._update(trigger_id="trig")
        assert result["error_category"] == "invalid_argument"
        assert not qrs.sent, "a PUT that changes nothing still risks a conflict"

    def test_unknown_repeat_is_refused(self, tool):
        tool()
        assert self._update(trigger_id="trig",
                            repeat="fortnightly")["error_category"] == "invalid_argument"

    def test_a_concurrent_change_is_reported_as_a_conflict(self, tool):
        """QRS answers 409 when someone else touched the trigger meanwhile."""
        tool(reply={"error": "HTTP 409: Conflict"})
        result = self._update(trigger_id="trig", enabled=False)
        assert result["error_category"] == "conflict"

    def test_changing_repeat_alone_is_refused(self, tool):
        """incrementOption and incrementDescription describe one schedule.

        daily -> hourly while keeping "0 0 1 0" means "hourly, every 1 day",
        which is not a schedule anyone asked for.
        """
        qrs = tool()
        result = self._update(trigger_id="trig", repeat="hourly")
        assert result["error_category"] == "invalid_argument"
        assert "interval_minutes" in result["error"]
        assert not qrs.sent

    def test_repeat_with_an_interval_is_accepted(self, tool):
        qrs = tool()
        self._update(trigger_id="trig", repeat="hourly", interval_minutes=60)
        body = qrs.sent[-1][2]["json"]
        assert body["incrementOption"] == 1
        assert body["incrementDescription"] == "0 1 0 0"

    def test_switching_to_once_needs_no_interval(self, tool):
        qrs = tool()
        result = self._update(trigger_id="trig", repeat="once")
        assert "error" not in result
        assert qrs.sent[-1][2]["json"]["incrementOption"] == 0

    def test_delete_reports_what_it_removed(self, tool):
        qrs = tool(reply={"raw_response": ""})
        result = self._delete(trigger_id="trig")
        assert result["deleted"] == "trig"
        assert qrs.sent[-1][0] == "DELETE"

    def test_delete_failure_is_surfaced(self, tool):
        tool(reply={"error": "HTTP 403: Forbidden"})
        assert self._delete(trigger_id="trig")["error_category"] == "repository_error"
