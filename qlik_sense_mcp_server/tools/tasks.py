"""Reload-task tools — certificate mode only.

QRS task administration needs repository-admin rights that a JWT analyst
identity does not have, so these are registered only when the server runs
with a client certificate.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from . import context
from .context import _cert_only_tool
from .helpers import (
    _check,
    _err,
    _ok,
    _timed,
    _wildcard_to_regex,
)


@_cert_only_tool()
@_timed
def get_tasks(
    status_filter: Optional[str] = None,
    name_filter: Optional[str] = None,
    app_filter: Optional[str] = None,
) -> str:
    """
    List Qlik Sense reload tasks with their last execution status.

    Use this as the entry point for anything reload/schedule-related. For
    "find all broken reloads" prefer `status_filter="failed"` — it's faster
    because it uses a QRS query filter instead of fetching everything and
    client-side filtering.

    Args:
        status_filter: Last-execution status. Accepts:
            - `"failed"` — tasks whose last run errored out
            - `"success"` — tasks that last finished cleanly (status code 7)
            - `"running"` — currently executing (best-effort, can be stale)
            - `"all"` or `None` (default) — everything
        name_filter: Wildcard filter on task name. Supports `*` and `%` as
            multi-char wildcards. Case-insensitive. Example: `"Daily*"`.
        app_filter: Wildcard filter on the target app name (the one this task
            reloads). Same syntax as `name_filter`.

    Returns:
        JSON `{ "tasks": [...], "count": N }`. Each task has id, name,
        app_name, enabled, last_execution_result (status, start_time,
        stop_time, details).
    
    Example (everything that is currently broken — fastest path):
        Call: {"status_filter": "failed"}
        Returns: {"tool_call_seconds": 0.9, "count": 1,
                  "tasks": [{"id": "c3d4e5f6-1111-...",
                             "name": "Reload Sales Dashboard",
                             "app_name": "Sales Dashboard", "enabled": true,
                             "last_execution_result": {"status": 8,
                                 "start_time": "2026-07-28T03:00:00.000Z",
                                 "stop_time": "2026-07-28T03:04:12.000Z",
                                 "details": ["..."]}}]}

    Example (wildcard filter on the task name):
        Call: {"name_filter": "Daily*"}
        Returns: {"tool_call_seconds": 1.2, "tasks": ["..."], "count": 3}

    QRS status codes: 7 = finished OK, 8 = failed.
    """
    e = _check()
    if e:
        return e
    # Status filtering runs in QRS, so a task past the server's record cap
    # is still found. An unrecognised value used to fall through and return
    # everything, which reads as "no task matches that state".
    status_groups = {
        "failed": context.repo_api.FAILED_EXECUTION_STATUSES,
        "success": (context.repo_api.SUCCESS_EXECUTION_STATUS,),
        "running": context.repo_api.RUNNING_EXECUTION_STATUSES,
    }
    wanted = (status_filter or "").strip().lower()
    if wanted == "all":
        wanted = ""
    if wanted and wanted not in status_groups:
        return _err(
            f"Unknown status_filter {status_filter!r}",
            error_category="invalid_argument",
            allowed_values=sorted(status_groups) + ["(omit for every task)"],
        )
    if wanted:
        tasks = context.repo_api.get_tasks_by_status(status_groups[wanted])
    else:
        tasks = context.repo_api.get_task_operational_status()
    if isinstance(tasks, dict) and "error" in tasks:
        # Never turn a Repository failure into "there are no tasks".
        return _err(f"Repository lookup failed: {tasks['error']}",
                    error_category="repository_error")
    if name_filter:
        rx = _wildcard_to_regex(name_filter, False)
        tasks = [t for t in tasks if rx.match(t.get("name", ""))]
    if app_filter:
        rx = _wildcard_to_regex(app_filter, False)
        tasks = [t for t in tasks if rx.match(t.get("app_name", ""))]
    return _ok({"tasks": tasks, "count": len(tasks)})



@_cert_only_tool()
@_timed
def get_task_details(task_id: str) -> str:
    """
    Fetch full QRS reload-task object by task ID — all properties, not just
    the summary from `get_tasks`.

    Args:
        task_id: Task GUID from `get_tasks`.

    Returns:
        Raw QRS JSON for `/qrs/reloadtask/{id}` — includes enabled, maxRetries,
        taskSessionTimeout, preloadNodes, app reference, tags, privileges, etc.
    
    Example:
        Call: {"task_id": "c3d4e5f6-1111-2222-3333-444455556666"}
        Returns: {"tool_call_seconds": 0.4, "id": "c3d4e5f6-1111-...",
                  "name": "Reload Sales Dashboard", "taskType": 0,
                  "enabled": true, "taskSessionTimeout": 1440,
                  "maxRetries": 0,
                  "app": {"id": "a1b2...", "name": "Sales Dashboard"},
                  "schemaPath": "ReloadTask"}
    """
    e = _check()
    if e:
        return e
    return _ok(context.repo_api.get_reload_task_by_id(task_id))



@_cert_only_tool()
@_timed
def start_task(task_id: str) -> str:
    """
    Trigger a reload task to start immediately.

    This is a write operation that affects the live Qlik Sense server — only
    call it when the user has explicitly asked to start/retry a reload.
    Returns right after the task is queued; use `get_task_executions` to
    track progress afterwards.

    Args:
        task_id: Task GUID to start. Required.

    Returns:
        QRS response to `/qrs/task/{id}/start`.
    
    Example (WRITE operation — confirm with the user before calling):
        Call: {"task_id": "c3d4e5f6-1111-2222-3333-444455556666"}
        Returns: {"tool_call_seconds": 0.35, "raw_response": ""}

    QRS answers 204 No Content, so an empty `raw_response` means the task
    was queued successfully. Poll `get_task_executions` for progress.
    """
    e = _check()
    if e:
        return e
    return _ok(context.repo_api.start_task(task_id))



@_cert_only_tool()
@_timed
def create_task(app_id: str, task_name: str, enabled: bool = True) -> str:
    """
    Create a new reload task for a Qlik application.

    Write operation — only call with explicit user intent. The created task
    has NO schedule by default; attach one separately via
    `create_task_schedule`.

    Args:
        app_id: Application GUID the task will reload. Required.
        task_name: Human-readable task name (must be unique in QRS). Required.
        enabled: Whether the task is enabled after creation. Default `True`.

    Returns:
        Created QRS task object, including the new task `id`.
    
    Example (WRITE operation — confirm with the user before calling):
        Call: {"app_id": "a1b2...", "task_name": "Reload Sales (nightly)",
               "enabled": true}
        Returns: {"tool_call_seconds": 0.6, "id": "c3d4e5f6-1111-...",
                  "name": "Reload Sales (nightly)", "enabled": true,
                  "app": {"id": "a1b2..."}}

    The new task has NO schedule — attach one with `create_task_schedule`.
    """
    e = _check()
    if e:
        return e
    return _ok(context.repo_api.create_reload_task(app_id, task_name, enabled))



@_cert_only_tool()
@_timed
def update_task(task_id: str, name: Optional[str] = None, enabled: Optional[bool] = None) -> str:
    """
    Update properties of an existing reload task. Write operation.

    Args:
        task_id: Task GUID to update. Required.
        name: New task name. Pass `None` to keep the current one.
        enabled: New enabled state. Pass `None` to keep the current one.

    Returns:
        Updated QRS task object.
    
    Example (WRITE operation — confirm with the user before calling):
        Call: {"task_id": "c3d4e5f6-1111-...", "enabled": false}
        Returns: {"tool_call_seconds": 0.7, "id": "c3d4e5f6-1111-...",
                  "name": "Reload Sales Dashboard", "enabled": false,
                  "modifiedDate": "2026-07-28T10:00:00.000Z"}
    """
    e = _check()
    if e:
        return e
    updates: Dict[str, Any] = {}
    if name is not None:
        updates["name"] = name
    if enabled is not None:
        updates["enabled"] = enabled
    return _ok(context.repo_api.update_reload_task(task_id, updates))



@_cert_only_tool()
@_timed
def delete_task(task_id: str) -> str:
    """
    Permanently delete a reload task. DESTRUCTIVE write operation — only call
    after explicit user confirmation. There is no undo.

    Args:
        task_id: Task GUID to delete. Required.

    Returns:
        QRS delete response.
    
    Example (DESTRUCTIVE — never call without explicit user confirmation):
        Call: {"task_id": "c3d4e5f6-1111-2222-3333-444455556666"}
        Returns: {"tool_call_seconds": 0.4, "raw_response": ""}
    """
    e = _check()
    if e:
        return e
    return _ok(context.repo_api.delete_reload_task(task_id))



@_cert_only_tool()
@_timed
def get_task_schedule(task_id: str) -> str:
    """
    List schedule triggers (cron-like time triggers) attached to a reload task.

    Args:
        task_id: Task GUID. Required.

    Returns:
        JSON `{ "task_id": ..., "triggers": [...], "count": N }`. Each trigger
        describes its repetition rule (daily/hourly/etc.), start time, time
        zone, and enabled state.
    
    Example:
        Call: {"task_id": "c3d4e5f6-1111-2222-3333-444455556666"}
        Returns: {"tool_call_seconds": 0.5, "task_id": "c3d4e5f6-1111-...",
                  "count": 1,
                  "triggers": [{"id": "e5f6a7b8-1111-...",
                                "name": "Nightly 03:00", "enabled": true,
                                "timeZone": "Europe/Moscow",
                                "startDate": "2026-04-01T00:00:00.000Z",
                                "incrementDescription": "0 0 1440 0"}]}
    """
    e = _check()
    if e:
        return e
    triggers = context.repo_api.get_schema_triggers(task_id)
    if isinstance(triggers, dict) and "error" in triggers:
        # An unscheduled task and an unreachable QRS both used to answer
        # "count: 0", which is the difference between "nobody scheduled
        # this" and "we could not find out".
        return _err(f"Repository lookup failed: {triggers['error']}",
                    error_category="repository_error")
    return _ok({"task_id": task_id, "triggers": triggers, "count": len(triggers)})



@_cert_only_tool()
@_timed
def create_task_schedule(
    task_id: str,
    name: str,
    repeat: str = "daily",
    interval_minutes: int = 1440,
    start_date: Optional[str] = None,
    time_zone: str = "Europe/Moscow",
    enabled: bool = True,
    time_window: Optional[str] = None,
) -> str:
    """
    Attach a new schedule trigger to a reload task. Write operation.

    Args:
        task_id: Task GUID to attach the schedule to. Required.
        name: Display name of the schedule trigger. Required.
        repeat: Repetition rule. One of `"once"`, `"hourly"`, `"daily"`
            (default), `"weekly"`, `"monthly"`. Case-insensitive. An
            unknown value is rejected rather than silently treated as
            daily. QRS has no "minutely" option — use `"hourly"` with
            `interval_minutes` below 60.
        interval_minutes: Interval between runs in minutes. Default 1440
            (once a day). Only meaningful for repeating schedules; ignored
            for `"once"`.
        start_date: First run, as `"YYYY-MM-DDThh:mm:ss.000"`. The time is
            read in `time_zone`, NOT in UTC — a trailing `Z` is accepted but
            does not make it UTC, so write the wall-clock time the user
            means. Defaults to the next midnight in that zone; a `once`
            schedule with a start date in the past never fires, which is
            why there is no fixed default any more.
        time_zone: IANA time-zone name. Default `"Europe/Moscow"`.
        enabled: Whether the schedule is active immediately. Default `True`.
        time_window: Optional restriction on WHEN the schedule may fire, as
            Qlik's 8-position string: `"Minute Hour WeekDayPrefix WeekDay
            WeeklyInterval DayOfMonth Month MonthlyInterval"`, `*` for "any"
            and `-` in the third position for "no week prefix". The interval
            says how often, this says when it is allowed — e.g.
            `"45 3-21 - * * * * *"` with `repeat="hourly"` fires hourly at
            :45 between 03:45 and 21:45. Omit for no restriction.

    Returns:
        QRS response with the created schema event/trigger object.
    
    Example (WRITE operation — confirm with the user; always set
    start_date to a real future time instead of keeping the default):
        Call: {"task_id": "c3d4e5f6-1111-...", "name": "Nightly 03:00",
               "repeat": "daily", "interval_minutes": 1440,
               "start_date": "2026-08-01T03:00:00.000Z",
               "time_zone": "Europe/Moscow", "enabled": true}
        Returns: {"tool_call_seconds": 0.6, "id": "e5f6a7b8-1111-...",
                  "name": "Nightly 03:00", "enabled": true,
                  "startDate": "2026-08-01T03:00:00.000Z"}
    """
    e = _check()
    if e:
        return e
    key = (repeat or "").strip().lower()
    if key not in context.repo_api.SCHEDULE_REPEAT_OPTIONS:
        # A typo used to fall back to "daily" silently, so a schedule the
        # caller believed was hourly quietly ran once a day.
        return _err(
            f"Unknown repeat {repeat!r}",
            error_category="invalid_argument",
            allowed_values=sorted(context.repo_api.SCHEDULE_REPEAT_OPTIONS),
        )
    if key != "once" and (interval_minutes is None or interval_minutes < 1):
        return _err(
            f"interval_minutes must be at least 1 for a {key} schedule, "
            f"got {interval_minutes}",
            error_category="invalid_argument",
        )
    if time_window is not None and len(time_window.split(" ")) != 8:
        return _err(
            f"time_window must have exactly 8 space-separated positions, "
            f"got {len(time_window.split(' '))}",
            error_category="invalid_argument",
            hint='Format: "Minute Hour WeekDayPrefix WeekDay WeeklyInterval '
                 'DayOfMonth Month MonthlyInterval", e.g. "45 3-21 - * * * * *".',
        )
    result = context.repo_api.create_schema_trigger(
        task_id, name, time_zone, start_date or _next_midnight(), key,
        interval_minutes, enabled, schema_filter=time_window)
    if isinstance(result, dict) and "error" in result:
        return _err(f"Failed to create schedule: {result['error']}",
                    error_category="repository_error")
    return _ok(result)



@_cert_only_tool()
@_timed
def get_task_executions(task_id: str, top: int = 10) -> str:
    """
    Get execution history (results) for a reload task, newest first.

    Use this after `start_task` to verify the reload worked, or to show a
    reliability timeline for a flaky task.

    Args:
        task_id: Task GUID. Required.
        top: How many most-recent executions to return. Default 10.

    Returns:
        JSON `{ "task_id": ..., "executions": [...], "count": N }`. Entries
        are raw QRS objects in camelCase: status (7 = OK, 8 = failed),
        startTime, stopTime, duration (MILLISECONDS), executingNodeName,
        scriptLogAvailable, details — plus `status_name`, this server's
        decoding of the status number ("FinishedSuccess", "FinishedFail",
        "Aborted", "Error", ...).
    
    Example (raw QRS objects, newest first — note the camelCase keys and
    that `duration` is in MILLISECONDS):
        Call: {"task_id": "c3d4e5f6-1111-...", "top": 2}
        Returns: {"tool_call_seconds": 0.6, "count": 2,
                  "executions": [{"id": "d4e5f6a7-1111-...", "status": 7,
                      "startTime": "2026-07-28T03:00:00.000Z",
                      "stopTime": "2026-07-28T03:04:12.000Z",
                      "duration": 252000, "executingNodeName": "Central",
                      "scriptLogAvailable": true, "details": ["..."]}]}
    """
    e = _check()
    if e:
        return e
    if top is not None and top < 1:
        return _err(
            f"top must be a positive number of executions, got {top}",
            error_category="invalid_argument",
            hint="Omit top for the default of 10 most recent executions.",
        )
    results = context.repo_api.get_execution_results(task_id, top)
    if isinstance(results, dict) and "error" in results:
        return _err(f"Repository lookup failed: {results['error']}",
                    error_category="repository_error")
    # QRS reports the outcome as a bare number. Adding the name costs
    # nothing and saves the reader looking up whether 8 is worse than 6
    # (it is not — both are failures).
    for entry in results:
        if isinstance(entry, dict) and "status" in entry:
            entry["status_name"] = context.repo_api.execution_status_name(entry["status"])
    return _ok({"task_id": task_id, "executions": results, "count": len(results)})



@_cert_only_tool()
@_timed
def get_task_script_log(task_id: str) -> str:
    """
    Download the full script log (stdout of the last reload run) for a task.

    Use this to diagnose a reload failure — search the log for "Error",
    "not found", timestamps around the failure, etc. Can be LARGE (several MB
    for long scripts); prefer `get_failed_tasks_with_logs` if you only need
    the tail of failures.

    Args:
        task_id: Task GUID. Required.

    Returns:
        Raw log text (not JSON).
    
    Example (returns PLAIN TEXT, not JSON — it arrives wrapped in the
    "result" key and can be several MB):
        Call: {"task_id": "c3d4e5f6-1111-2222-3333-444455556666"}
        Returns: {"tool_call_seconds": 1.85,
                  "result": "2026-07-28 03:00:01 Execution started.\n..."}
    """
    e = _check()
    if e:
        return e
    log_text = context.repo_api.get_script_log_by_task_id(task_id)
    return log_text



@_cert_only_tool()
@_timed
def get_failed_tasks_with_logs() -> str:
    """
    Get all currently-failed reload tasks together with the last ~50 lines of
    their script logs — in a single call, no parameters.

    This is the fastest way to answer "what's broken on the Qlik server right
    now" / "show me today's reload failures". Prefer this over combining
    `get_tasks(status_filter="failed")` + `get_task_script_log` for every
    task.

    Returns:
        JSON `{ "failed_tasks": [{task_id, task_name, app_name, last_start,
        last_stop, details, log_tail}, ...], "count": N }`.
    
    Example:
        Call: {}
        Returns: {"tool_call_seconds": 4.2, "count": 1,
                  "failed_tasks": [{"task_id": "c3d4e5f6-1111-...",
                      "task_name": "Reload Sales Dashboard",
                      "app_name": "Sales Dashboard",
                      "last_start": "2026-07-28T03:00:00.000Z",
                      "last_stop": "2026-07-28T03:04:12.000Z",
                      "details": ["..."],
                      "log_tail": "...Error: Field 'AmountX' not found..."}]}
    """
    e = _check()
    if e:
        return e
    failed = context.repo_api.get_failed_tasks()
    if isinstance(failed, dict) and "error" in failed:
        # "0 failed tasks" and "could not ask" must not look the same.
        return _err(f"Repository lookup failed: {failed['error']}",
                    error_category="repository_error")
    results = []
    for t in failed:
        tid = t.get("id", "")
        log_text = context.repo_api.get_script_log_by_task_id(tid) if tid else "No task ID"
        # Extract last ~50 lines of log to find error
        log_lines = log_text.strip().split("\n") if log_text else []
        tail = log_lines[-50:] if len(log_lines) > 50 else log_lines
        results.append({
            "task_id": tid,
            "task_name": t.get("name", ""),
            "app_name": t.get("app_name", ""),
            "last_start": t.get("last_execution_result", {}).get("start_time", ""),
            "last_stop": t.get("last_execution_result", {}).get("stop_time", ""),
            "details": t.get("last_execution_result", {}).get("details", ""),
            "log_tail": "\n".join(tail),
        })
    return _ok({"failed_tasks": results, "count": len(results)})



@_cert_only_tool()
@_timed
def get_task_dependencies(task_id: str, direction: str = "downstream") -> str:
    """
    Resolve the full transitive dependency chain of a reload task, following
    composite events (one task's successful finish triggering another).

    Use `direction="downstream"` to answer "what else will run after this
    task succeeds / what is the blast radius". Use `direction="upstream"` to
    answer "what must have finished before this task can start".

    The result is flattened (not a tree) — each entry carries a `depth`
    field so you can reconstruct hierarchy if needed. Cycles are broken
    using a visited set.

    Args:
        task_id: Root task GUID. Required.
        direction: `"downstream"` (default) — tasks this one triggers;
            `"upstream"` — tasks that trigger this one.

    Returns:
        JSON `{ "root_task_id": ..., "direction": ..., "dependencies":
        [{id, name, depth}, ...], "count": N }`.
    
    Example (downstream — what runs after this task succeeds):
        Call: {"task_id": "c3d4e5f6-1111-..."}
        Returns: {"tool_call_seconds": 1.0,
                  "root_task_id": "c3d4e5f6-1111-...",
                  "direction": "downstream", "count": 2,
                  "dependencies": [{"id": "c3d4...67",
                                    "name": "Reload Finance Mart",
                                    "depth": 1},
                                   {"id": "c3d4...68",
                                    "name": "Reload Finance Dashboard",
                                    "depth": 2}]}

    Example (upstream — what must finish before this task can start):
        Call: {"task_id": "c3d4...68", "direction": "upstream"}
        Returns: {"tool_call_seconds": 1.0, "direction": "upstream",
                  "dependencies": [{"id": "c3d4...67",
                                    "name": "Reload Finance Mart",
                                    "depth": 1}], "count": 1}
    """
    e = _check()
    if e:
        return e

    all_events = context.repo_api.get_all_composite_events()
    if isinstance(all_events, dict) and "error" in all_events:
        # "no dependencies" is a real and common answer; an unreachable
        # QRS must not be able to impersonate it.
        return _err(f"Repository lookup failed: {all_events['error']}",
                    error_category="repository_error")

    # Build lookup: trigger_task_id -> list of dependent tasks
    # and reverse: dependent_task_id -> list of trigger tasks
    downstream_map: Dict[str, List[Dict[str, Any]]] = {}
    upstream_map: Dict[str, List[Dict[str, Any]]] = {}

    for evt in all_events:
        dependent_task = evt.get("reloadTask") or evt.get("externalProgramTask")
        if not dependent_task:
            continue
        dep_id = dependent_task.get("id", "")
        dep_name = dependent_task.get("name", "")

        rules = evt.get("compositeRules", [])
        for rule in rules:
            trigger_task = rule.get("reloadTask") or rule.get("externalProgramTask")
            if not trigger_task:
                continue
            trig_id = trigger_task.get("id", "")
            trig_name = trigger_task.get("name", "")

            downstream_map.setdefault(trig_id, []).append({"id": dep_id, "name": dep_name})
            upstream_map.setdefault(dep_id, []).append({"id": trig_id, "name": trig_name})

    visited = set()
    result = []

    def walk(tid: str, depth: int):
        if tid in visited:
            return
        visited.add(tid)
        lookup = downstream_map if direction == "downstream" else upstream_map
        for child in lookup.get(tid, []):
            result.append({"id": child["id"], "name": child["name"], "depth": depth})
            walk(child["id"], depth + 1)

    walk(task_id, 1)
    return _ok({"root_task_id": task_id, "direction": direction, "dependencies": result, "count": len(result)})




@_cert_only_tool()
@_timed
def update_task_schedule(
    trigger_id: str,
    name: Optional[str] = None,
    enabled: Optional[bool] = None,
    repeat: Optional[str] = None,
    interval_minutes: Optional[int] = None,
    start_date: Optional[str] = None,
    time_zone: Optional[str] = None,
    time_window: Optional[str] = None,
) -> str:
    """
    Change one schedule trigger. Write operation.

    Use this to retime or pause a single schedule. Disabling a task with
    `update_task(enabled=False)` stops every trigger it has; this stops one.

    Args:
        trigger_id: Trigger GUID from `get_task_schedule` (`triggers[*].id`).
            Required — this is NOT the task id.
        name: New display name. Omit to keep.
        enabled: Turn this one trigger on or off. Omit to keep.
        repeat: New repetition rule (`once`/`hourly`/`daily`/`weekly`/
            `monthly`). Omit to keep.
        interval_minutes: New interval between runs. Omit to keep.
        start_date: New first-run timestamp, ISO-8601. Omit to keep.
        time_zone: New IANA time-zone name. Omit to keep.
        time_window: New 8-position window string (see
            `create_task_schedule`). Omit to keep.

    Returns:
        The updated QRS schema-event object.

    Example (pause one schedule without touching the task):
        Call: {"trigger_id": "e5f6a7b8-1111-...", "enabled": false}
        Returns: {"tool_call_seconds": 0.5, "id": "e5f6a7b8-1111-...",
                  "enabled": false, "name": "Nightly 03:00"}
    """
    e = _check()
    if e:
        return e

    updates: Dict[str, Any] = {}
    if name is not None:
        updates["name"] = name
    if enabled is not None:
        updates["enabled"] = bool(enabled)
    if start_date is not None:
        updates["startDate"] = start_date
    if time_zone is not None:
        updates["timeZone"] = time_zone
    if repeat is not None:
        key = repeat.strip().lower()
        if key not in context.repo_api.SCHEDULE_REPEAT_OPTIONS:
            return _err(
                f"Unknown repeat {repeat!r}",
                error_category="invalid_argument",
                allowed_values=sorted(context.repo_api.SCHEDULE_REPEAT_OPTIONS),
            )
        # The two fields describe one schedule together: incrementOption
        # says "hourly", incrementDescription says "every 90 minutes".
        # Changing one and keeping the other leaves QMC rendering a
        # schedule nobody asked for — e.g. daily→hourly with the old
        # "0 0 1 0" means "hourly, every 1 day".
        if key != "once" and interval_minutes is None:
            return _err(
                f"Changing repeat to {key!r} also needs interval_minutes — "
                f"the repetition type and the interval describe one schedule "
                f"together, and keeping the old interval would produce a "
                f"schedule you did not ask for",
                error_category="invalid_argument",
            )
        updates["incrementOption"] = context.repo_api.SCHEDULE_REPEAT_OPTIONS[key]
    if interval_minutes is not None:
        if interval_minutes < 1:
            return _err(
                f"interval_minutes must be at least 1, got {interval_minutes}",
                error_category="invalid_argument",
            )
        updates["incrementDescription"] = \
            context.repo_api._increment_description(interval_minutes)
    if time_window is not None:
        if len(time_window.split(" ")) != 8:
            return _err(
                f"time_window must have exactly 8 space-separated positions, "
                f"got {len(time_window.split(' '))}",
                error_category="invalid_argument",
            )
        updates["schemaFilterDescription"] = [time_window]

    if not updates:
        return _err(
            "Nothing to update — pass at least one field to change",
            error_category="invalid_argument",
        )

    result = context.repo_api.update_schema_trigger(trigger_id, updates)
    if isinstance(result, dict) and "error" in result:
        return _err(f"Failed to update schedule: {result['error']}",
                    error_category=result.get("error_category", "repository_error"))
    return _ok(result)


@_cert_only_tool()
@_timed
def delete_task_schedule(trigger_id: str) -> str:
    """
    Delete one schedule trigger from a reload task. Write operation.

    Removes just this trigger; the task and its other triggers stay.

    Args:
        trigger_id: Trigger GUID from `get_task_schedule` (`triggers[*].id`).
            Required — this is NOT the task id.

    Returns:
        `{"deleted": "<trigger_id>"}` on success.

    Example (WRITE operation — confirm with the user before calling):
        Call: {"trigger_id": "e5f6a7b8-1111-2222-3333-444455556666"}
        Returns: {"tool_call_seconds": 0.3,
                  "deleted": "e5f6a7b8-1111-2222-3333-444455556666"}
    """
    e = _check()
    if e:
        return e
    result = context.repo_api.delete_schema_trigger(trigger_id)
    if isinstance(result, dict) and "error" in result:
        return _err(f"Failed to delete schedule: {result['error']}",
                    error_category="repository_error")
    return _ok({"deleted": trigger_id})


def _next_midnight() -> str:
    """Tomorrow at 00:00, in the format QRS expects.

    A schedule whose start date is in the past never runs when `repeat` is
    `once`, and reads oddly in QMC for the others. The old hard-coded
    default was a date that had already passed by the time anyone used it.
    """
    tomorrow = datetime.now(timezone.utc).date() + timedelta(days=1)
    return f"{tomorrow.isoformat()}T00:00:00.000"
