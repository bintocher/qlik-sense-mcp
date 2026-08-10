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

    def test_success_is_not_treated_as_failure(self):
        qrs = _Qrs(rows=[])
        qrs.get_failed_tasks()
        assert "status eq 7" not in qrs.filters[0]

    def test_filtering_happens_in_qrs(self):
        """Client-side filtering could not see a task past the record cap."""
        qrs = _Qrs(rows=[])
        qrs.get_failed_tasks()
        assert qrs.filters[0], "no filter was sent to QRS"


class TestOperationalStatus:
    def test_rows_are_mapped_to_the_documented_shape(self):
        qrs = _Qrs(rows=[_task_row(status=8)])
        tasks = qrs.get_task_operational_status()
        assert tasks[0]["name"] == "Daily reload"
        assert tasks[0]["app_name"] == "Sales"
        assert tasks[0]["last_execution_result"]["status"] == 8
        assert tasks[0]["last_execution_result"]["duration_seconds"] == 240000

    def test_never_executed_task_reports_minus_one(self):
        row = _task_row()
        row[7] = None  # status
        qrs = _Qrs(rows=[row])
        tasks = qrs.get_task_operational_status()
        assert tasks[0]["last_execution_result"]["status"] == -1

    def test_paging_continues_past_the_first_page(self):
        """500 tasks must all come back, not just the first page."""
        qrs = _Qrs(rows=[_task_row(name=f"task {i}") for i in range(1200)])
        qrs._page_rows = qrs._rows
        tasks = qrs.get_task_operational_status()
        assert len(tasks) == 1200

    def test_a_short_read_is_reported_against_the_count(self):
        """A `/table` that stops early must not pass for the whole list."""
        class _ShortReader(_Qrs):
            def _make_request(self, method, endpoint, **kwargs):
                if endpoint.endswith("/count"):
                    return {"value": 900}      # QRS says 900 match
                return super()._make_request(method, endpoint, **kwargs)

        qrs = _ShortReader(rows=[_task_row(name=f"task {i}") for i in range(400)])
        result = qrs.get_task_operational_status()
        assert isinstance(result, dict) and "error" in result
        assert result["rows_read"] == 400
        assert result["rows_expected"] == 900

    def test_an_exact_multiple_of_the_page_size_is_not_a_truncation(self):
        """1000 rows at 500 per page is a complete read, not a capped one."""
        rows = [_task_row(name=f"task {i}") for i in range(1000)]
        qrs = _Qrs(rows=rows)
        qrs._page_rows = rows
        result = qrs.get_task_operational_status()
        assert isinstance(result, list)
        assert len(result) == 1000

    def test_failure_is_propagated_not_swallowed(self):
        qrs = _Qrs(error="HTTP 500: Internal Server Error")
        result = qrs.get_task_operational_status()
        assert isinstance(result, dict) and "error" in result


class TestExecutionResults:
    def test_top_is_a_server_side_take(self):
        qrs = _Qrs(rows=[])
        qrs.get_execution_results("task-guid", top=3)
        params = qrs.requests[0][2]["params"]
        assert params["take"] == 3
        assert params["sortColumn"] == "startTime"
        assert params["orderAscending"] == "false"
        assert "taskID eq task-guid" in params["filter"]

    @pytest.mark.parametrize("bad", [0, -5, None])
    def test_unusable_top_falls_back_to_the_default(self, bad):
        qrs = _Qrs(rows=[])
        qrs.get_execution_results("task-guid", top=bad)
        assert qrs.requests[0][2]["params"]["take"] == 10


class TestToolLayer:
    @pytest.fixture
    def tool(self, monkeypatch):
        def _install(repo):
            monkeypatch.setattr(context, "repo_api", repo)
        return _install

    def _call(self, name, **kwargs):
        fn = getattr(getattr(srv, name), "fn", getattr(srv, name))
        return json.loads(fn(**kwargs))

    def test_unknown_status_filter_is_refused(self, tool):
        tool(_Qrs(rows=[]))
        result = self._call("get_tasks", status_filter="brokn")
        assert result["error_category"] == "invalid_argument"
        assert "failed" in result["allowed_values"]

    def test_running_filter_is_supported(self, tool):
        qrs = _Qrs(rows=[])
        tool(qrs)
        self._call("get_tasks", status_filter="running")
        applied = qrs.filters[0]
        for code in (1, 2, 3):
            assert f"status eq {code}" in applied

    def test_all_means_no_status_filter(self, tool):
        qrs = _Qrs(rows=[_task_row()])
        tool(qrs)
        self._call("get_tasks", status_filter="all")
        assert qrs.filters[0] is None

    def test_repository_failure_is_not_an_empty_task_list(self, tool):
        tool(_Qrs(error="HTTP 503: Service Unavailable"))
        result = self._call("get_tasks")
        assert result["error_category"] == "repository_error"
        assert "tasks" not in result

    def test_failed_with_logs_does_not_claim_zero_on_failure(self, tool):
        tool(_Qrs(error="HTTP 500"))
        result = self._call("get_failed_tasks_with_logs")
        assert result["error_category"] == "repository_error"
        assert result.get("count") != 0

    def test_non_positive_top_is_refused(self, tool):
        tool(_Qrs(rows=[]))
        result = self._call("get_task_executions", task_id="t", top=0)
        assert result["error_category"] == "invalid_argument"
