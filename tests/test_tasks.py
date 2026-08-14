"""Reload-task queries: status codes, server-side filtering, failures.

The status codes here are not guesses — they come from the server's own
`TaskExecutionStatus` enum (6 Aborted, 7 FinishedSuccess, 8 FinishedFail,
11 Error). Filtering failures on 8 alone, as the code used to, quietly
excluded aborted and errored runs: precisely the tasks someone asking
"what broke?" needs to see.
"""

import json

import pytest

from qlik_sense_mcp_server import server as srv
from qlik_sense_mcp_server.tools import context
from qlik_sense_mcp_server.repository_api import QlikRepositoryAPI


class _Qrs(QlikRepositoryAPI):
    def __init__(self, rows=None, error=None, page_size_rows=None):
        self.requests = []
        self._rows = rows if rows is not None else []
        self._error = error
        self._page_rows = page_size_rows

    def _make_request(self, method, endpoint, **kwargs):
        self.requests.append((method, endpoint, kwargs))
        if self._error:
            return {"error": self._error}
        if endpoint.endswith("/table"):
            names = [c["name"] for c in kwargs["json"]["columns"]]
            skip = kwargs["params"]["skip"]
            take = kwargs["params"]["take"]
            rows = self._page_rows if self._page_rows is not None else self._rows
            return {"columnNames": names, "rows": rows[skip:skip + take]}
        if endpoint.endswith("/count"):
            return {"value": len(self._rows)}
        raise AssertionError(f"unexpected call {endpoint}")

    @property
    def filters(self):
        return [(kwargs.get("params") or {}).get("filter")
                for _, endpoint, kwargs in self.requests if endpoint.endswith("/table")]


def _task_row(name="Daily reload", status=7, app="Sales"):
    # Order must match QlikRepositoryAPI._TASK_COLUMNS.
    return ["task-guid", name, True, 0, "app-guid", app,
            "2026-08-20T06:00:00.000Z", status,
            "2026-08-10T06:00:00.000Z", "2026-08-10T06:04:00.000Z",
            240000, "details", "exec-guid"]


class TestFailureStatuses:
    def test_failed_covers_fail_error_and_aborted(self):
        qrs = _Qrs(rows=[])
        qrs.get_failed_tasks()
        applied = qrs.filters[0]
        for code in (8, 11, 6):
            assert f"status eq {code}" in applied, f"status {code} not covered"




class TestOperationalStatus:
    def test_rows_are_mapped_to_the_documented_shape(self):
        qrs = _Qrs(rows=[_task_row(status=8)])
        tasks = qrs.get_task_operational_status()
        assert tasks[0]["name"] == "Daily reload"
        assert tasks[0]["app_name"] == "Sales"
        assert tasks[0]["last_execution_result"]["status"] == 8
        assert tasks[0]["last_execution_result"]["duration_seconds"] == 240000







class TestExecutionResults:
    def test_top_is_a_server_side_take(self):
        qrs = _Qrs(rows=[])
        qrs.get_execution_results("task-guid", top=3)
        params = qrs.requests[0][2]["params"]
        assert params["take"] == 3
        assert params["sortColumn"] == "startTime"
        assert params["orderAscending"] == "false"
        assert "taskID eq task-guid" in params["filter"]





