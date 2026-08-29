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



class TestScheduleBody:
    def test_repeat_maps_to_the_qrs_increment_option(self):
        for repeat, option in [("once", 0), ("hourly", 1), ("daily", 2),
                               ("weekly", 3), ("monthly", 4)]:
            qrs = _Qrs()
            qrs.create_schema_trigger("t", "n", repeat=repeat)
            assert qrs.body["incrementOption"] == option, repeat







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
        # The refusal names what is wrong and stops there: a caller that
        # needs the list of options asks for it.
        assert "minutely" in result["error"]
        assert not qrs.sent











