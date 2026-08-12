"""Qlik Sense Repository API client."""

import ssl
from typing import Dict, List, Any, Optional
import httpx
import logging
from .config import (
    QlikSenseConfig,
    DEFAULT_HTTP_TIMEOUT,
    DEFAULT_APPS_LIMIT,
    MAX_APPS_LIMIT,
    AUTH_MODE_JWT,
)
from .jwt_session import JwtSession, JwtBootstrapError
from .utils import generate_xrfkey
from .exceptions import QlikConnectionError

logger = logging.getLogger(__name__)


class QlikRepositoryAPI:
    """Client for Qlik Sense Repository API using httpx."""

    def __init__(self, config: QlikSenseConfig, jwt_session: Optional[JwtSession] = None):
        self.config = config
        self.jwt_session = jwt_session  # required when config.auth_mode == jwt

        # In JWT mode the session holder is required up front — failing here
        # gives a clear message instead of a confusing 401 on the first call.
        if self.config.auth_mode == AUTH_MODE_JWT and jwt_session is None:
            raise QlikConnectionError(
                "JWT mode requires a JwtSession — pass jwt_session=... to "
                "QlikRepositoryAPI(). See server._init_clients for the canonical wiring."
            )

        # Setup SSL verification
        if self.config.verify_ssl:
            ssl_context = ssl.create_default_context()
            if self.config.ca_cert_path:
                ssl_context.load_verify_locations(self.config.ca_cert_path)
        else:
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        # Timeouts from env (seconds)
        timeout_val = DEFAULT_HTTP_TIMEOUT

        # Build per-mode client config. JWT mode uses neither client certs
        # nor X-Qlik-User impersonation — identity comes from the signed
        # bearer token validated by the VP, and after the first bootstrap
        # call the cookie jar authenticates the rest.
        if self.config.auth_mode == AUTH_MODE_JWT:
            cert = None
            default_headers = {"Content-Type": "application/json"}
        else:
            cert = None
            if self.config.client_cert_path and self.config.client_key_path:
                cert = (self.config.client_cert_path, self.config.client_key_path)
            default_headers = {
                "X-Qlik-User": f"UserDirectory={self.config.user_directory}; UserId={self.config.user_id}",
                "Content-Type": "application/json",
            }

        self.client = httpx.Client(
            verify=ssl_context if self.config.verify_ssl else False,
            cert=cert,
            timeout=timeout_val,
            headers=default_headers,
        )

    def _get_api_url(self, endpoint: str) -> str:
        """
        Build full QRS URL for an endpoint.

        Certificate mode:  https://host:4242/qrs/<endpoint>      (direct QRS)
        JWT mode:          https://host/<vp_prefix>/qrs/<endpoint> (via VP, port 443)
        """
        if self.config.auth_mode == AUTH_MODE_JWT:
            return f"{self.config.qlik_base_host}/{self.config.virtual_proxy_prefix}/qrs/{endpoint}"
        base_url = f"{self.config.qlik_base_host}:{self.config.repository_port}"
        return f"{base_url}/qrs/{endpoint}"

    def _make_request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        """Make HTTP request to Repository API."""
        try:
            url = self._get_api_url(endpoint)

            # Generate dynamic xrfkey for each request
            xrfkey = generate_xrfkey()

            # Add xrfkey parameter to all requests. `or {}` and not a
            # default: callers legitimately pass params=None, and mutating
            # that used to raise TypeError inside the catch-all below,
            # surfacing as a bare "'NoneType' object does not support item
            # assignment" error envelope.
            params = kwargs.get('params') or {}
            params['xrfkey'] = xrfkey
            kwargs['params'] = params

            # Add xrfkey header
            headers = kwargs.get('headers') or {}
            headers['X-Qlik-Xrfkey'] = xrfkey

            # JWT mode: make sure the session cookie is bootstrapped, then
            # attach the anti-CSWSH header. Bootstrap Set-Cookie lands in
            # self.client.cookies automatically because we pass the same
            # client into ensure().
            if self.config.auth_mode == AUTH_MODE_JWT and self.jwt_session is not None:
                try:
                    self.jwt_session.ensure(self.client)
                except JwtBootstrapError as bootstrap_exc:
                    logger.error("JWT bootstrap failed: %s", bootstrap_exc)
                    return {"error": f"JWT bootstrap failed: {bootstrap_exc}"}
                if self.jwt_session.csrf_token:
                    headers["qlik-csrf-token"] = self.jwt_session.csrf_token

            kwargs['headers'] = headers

            response = self.client.request(method, url, **kwargs)

            # If the session cookie expired mid-flight, invalidate and retry
            # once — this is cheaper than a proactive per-request check and
            # covers the case where the server killed the session early.
            if (response.status_code == 401
                    and self.config.auth_mode == AUTH_MODE_JWT
                    and self.jwt_session is not None):
                logger.info("QRS returned 401, refreshing JWT session and retrying once")
                self.jwt_session.invalidate()
                self.client.cookies.clear()
                try:
                    self.jwt_session.ensure(self.client)
                except JwtBootstrapError as bootstrap_exc:
                    logger.error("JWT re-bootstrap after 401 failed: %s", bootstrap_exc)
                    return {"error": f"JWT re-bootstrap failed: {bootstrap_exc}"}
                if self.jwt_session.csrf_token:
                    headers["qlik-csrf-token"] = self.jwt_session.csrf_token
                response = self.client.request(method, url, **kwargs)

            response.raise_for_status()

            if response.headers.get("content-type", "").startswith("application/json"):
                return response.json()
            else:
                return {"raw_response": response.text}

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error {e.response.status_code}: {e.response.text}")
            return {"error": f"HTTP {e.response.status_code}: {e.response.text}"}
        except Exception as e:
            logger.error(f"Request error: {str(e)}")
            return {"error": str(e)}

    def get_comprehensive_apps(self,
                                   limit: int = DEFAULT_APPS_LIMIT,
                                   offset: int = 0,
                                   name: Optional[str] = None,
                                   stream: Optional[str] = None,
                                   published: Optional[bool] = True) -> Dict[str, Any]:
        """
        Get minimal list of apps with essential fields and proper filtering/pagination.

        Returns only: guid, name, description, stream, modified_dttm, reload_dttm.
        Supports case-insensitive wildcard filters for name and stream, and published flag.
        """
        if limit is None or limit < 1:
            limit = DEFAULT_APPS_LIMIT
        if limit > MAX_APPS_LIMIT:
            limit = MAX_APPS_LIMIT
        if offset is None or offset < 0:
            offset = 0

        filters: List[str] = []
        if published is not None:
            filters.append(f"published eq {'true' if published else 'false'}")
        if name:
            raw_name = name.replace('*', '')
            safe_name = raw_name.replace("'", "''")
            filters.append(f"name so '{safe_name}'")
        if stream:
            raw_stream = stream.replace('*', '')
            safe_stream = raw_stream.replace("'", "''")
            filters.append(f"stream.name so '{safe_stream}'")

        query_filter = " and ".join(filters) if filters else None

        # Paging is done by QRS, not here. `app/full` ignores skip/take and
        # is itself truncated at the server's MaxRecordLimit (100 by
        # default), so slicing its result client-side both loses every app
        # past that cap and reports the cap as the total. `app/table` takes
        # skip/take/sortColumn, and `app/count` answers how many rows the
        # filter really matches.
        total_found = self._count("app", query_filter)
        if isinstance(total_found, dict):  # error envelope
            return total_found

        page = self._table(
            "App", "app",
            columns=[
                ("guid", "id"),
                ("name", "name"),
                ("description", "description"),
                ("stream", "stream.name"),
                ("published", "published"),
                ("modified_dttm", "modifiedDate"),
                ("reload_dttm", "lastReloadTime"),
            ],
            query_filter=query_filter,
            skip=offset,
            take=limit,
            sort_column="modifiedDate",
            ascending=False,
        )
        if isinstance(page, dict) and "error" in page:
            return page

        minimal_apps: List[Dict[str, Any]] = []
        for row in page:
            # An unpublished app can still carry a stream reference; the
            # tool has always reported a stream only for published apps.
            is_published = bool(row.get("published"))
            minimal_apps.append({
                "guid": row.get("guid") or "",
                "name": row.get("name") or "",
                "description": row.get("description") or "",
                "stream": (row.get("stream") or "") if is_published else "",
                "modified_dttm": row.get("modified_dttm") or "",
                "reload_dttm": row.get("reload_dttm") or "",
            })

        returned = len(minimal_apps)
        has_more = (offset + returned) < total_found
        return {
            "apps": minimal_apps,
            "pagination": {
                "limit": limit,
                "offset": offset,
                "returned": returned,
                "total_found": total_found,
                "has_more": has_more,
                "next_offset": (offset + returned) if has_more else None,
            },
        }

    def _count(self, entity: str, query_filter: Optional[str] = None) -> Any:
        """Number of rows matching a filter, straight from QRS.

        Returns an error envelope unchanged so callers can pass a failure
        through instead of reporting it as "nothing matched".
        """
        params = {"filter": query_filter} if query_filter else None
        result = self._make_request("GET", f"{entity}/count", params=params)
        if isinstance(result, dict) and "error" in result:
            return result

        # QRS documents /count as returning an Integer, and Qlik 31.60
        # answers with {"value": N}. Accept both rather than betting on
        # one: getting this wrong fails every paged read before it starts.
        # bool is an int subclass, hence the explicit exclusion.
        if isinstance(result, int) and not isinstance(result, bool):
            value = result
        elif isinstance(result, dict) and isinstance(result.get("value"), int) \
                and not isinstance(result.get("value"), bool):
            value = result["value"]
        else:
            return {"error": f"Unexpected reply from {entity}/count: {result!r}"}

        if value < 0:
            return {"error": f"Negative count from {entity}/count: {value}"}
        return value

    def _table(self, entity_type: str, endpoint: str,
               columns: List[tuple], query_filter: Optional[str] = None,
               skip: int = 0, take: int = 25,
               sort_column: Optional[str] = None,
               ascending: bool = True) -> Any:
        """Fetch one server-side page via the QRS table endpoint.

        `columns` maps the key we want in the result to the QRS property
        path. Returns a list of dicts, or the error envelope unchanged.
        """
        body = {
            "entity": entity_type,
            "columns": [
                {"name": key, "columnType": "Property", "definition": definition}
                for key, definition in columns
            ],
        }
        params: Dict[str, Any] = {"skip": skip, "take": take,
                                  "orderAscending": "true" if ascending else "false"}
        if sort_column:
            params["sortColumn"] = sort_column
        if query_filter:
            params["filter"] = query_filter

        result = self._make_request("POST", f"{endpoint}/table",
                                    params=params, json=body)
        if isinstance(result, dict) and "error" in result:
            return result
        if not isinstance(result, dict) or "rows" not in result:
            return {"error": f"Unexpected reply from {endpoint}/table: {result!r}"}

        names = result.get("columnNames") or [key for key, _ in columns]
        return [dict(zip(names, row)) for row in result.get("rows", [])]

    def get_about(self) -> Dict[str, Any]:
        """Get Qlik Sense server information via QRS /qrs/about endpoint."""
        return self._make_request("GET", "about")

    def get_app_by_id(self, app_id: str) -> Dict[str, Any]:
        """Get specific app by ID."""
        return self._make_request("GET", f"app/{app_id}")

    def start_task(self, task_id: str) -> Dict[str, Any]:
        """
        Start a task execution.

        Note: This method is not exported via MCP API as it's an administrative function,
        not an analytical tool. Available for internal use only.
        """
        return self._make_request("POST", f"task/{task_id}/start")

    # ── Task management methods ──

    def get_reload_task_by_id(self, task_id: str) -> Dict[str, Any]:
        """Get single reload task by ID."""
        return self._make_request("GET", f"reloadtask/{task_id}")

    # QRS TaskExecutionStatus, read from the server's own enum endpoint
    # (`/qrs/enum/...`) rather than assumed: 6 Aborted, 7 FinishedSuccess,
    # 8 FinishedFail, 11 Error. Filtering failures on 8 alone silently
    # ignored aborted and errored runs — exactly the ones an operator is
    # looking for when they ask what broke.
    FAILED_EXECUTION_STATUSES = (6, 8, 11)
    SUCCESS_EXECUTION_STATUS = 7
    # 1 Triggered, 2 Started, 3 Queued, 13 DistributionQueue,
    # 14 DistributionRunning — a task in any of these is still working.
    RUNNING_EXECUTION_STATUSES = (1, 2, 3, 13, 14)

    # The whole enum, so a reply can say what a status means. `status: 8`
    # is not something a reader should have to look up, and the two codes
    # that matter most — 8 FinishedFail and 6 Aborted — look nothing alike
    # as numbers while meaning almost the same thing to an operator.
    EXECUTION_STATUS_NAMES = {
        0: "NeverStarted", 1: "Triggered", 2: "Started", 3: "Queued",
        4: "AbortInitiated", 5: "Aborting", 6: "Aborted",
        7: "FinishedSuccess", 8: "FinishedFail", 9: "Skipped",
        10: "Retry", 11: "Error", 12: "Reset",
        13: "DistributionQueue", 14: "DistributionRunning",
    }

    @classmethod
    def execution_status_name(cls, status: Any) -> str:
        """Human-readable name for a QRS TaskExecutionStatus code."""
        return cls.EXECUTION_STATUS_NAMES.get(status, f"Unknown({status})")

    _TASK_COLUMNS = [
        ("id", "id"),
        ("name", "name"),
        ("enabled", "enabled"),
        ("task_type", "taskType"),
        ("app_id", "app.id"),
        ("app_name", "app.name"),
        ("next_execution", "operational.nextExecution"),
        ("status", "operational.lastExecutionResult.status"),
        ("start_time", "operational.lastExecutionResult.startTime"),
        ("stop_time", "operational.lastExecutionResult.stopTime"),
        ("duration", "operational.lastExecutionResult.duration"),
        ("details", "operational.lastExecutionResult.details"),
        ("execution_id", "operational.lastExecutionResult.id"),
    ]

    def get_task_operational_status(self,
                                    query_filter: Optional[str] = None) -> Any:
        """Every reload task with its last execution result.

        Reads through `reloadtask/table` page by page rather than
        `reloadtask/full`, which QRS truncates at MaxRecordLimit — a task
        past that cap simply did not exist as far as this server was
        concerned, and `get_failed_tasks` would report "none failed".
        """
        rows = self._read_all("ReloadTask", "reloadtask",
                              self._TASK_COLUMNS, query_filter,
                              sort_column="name")
        if isinstance(rows, dict):  # error envelope
            return rows

        return [{
            "id": row.get("id") or "",
            "name": row.get("name") or "",
            "enabled": bool(row.get("enabled")),
            "task_type": row.get("task_type") or 0,
            "app_id": row.get("app_id") or "",
            "app_name": row.get("app_name") or "",
            "next_execution": row.get("next_execution") or "",
            "last_execution_result": {
                "status": row.get("status") if row.get("status") is not None else -1,
                "start_time": row.get("start_time") or "",
                "stop_time": row.get("stop_time") or "",
                "duration_seconds": row.get("duration") or 0,
                "details": row.get("details") or "",
                "execution_id": row.get("execution_id") or "",
            },
        } for row in rows]

    def get_failed_tasks(self) -> Any:
        """Tasks whose last execution failed, was aborted or errored."""
        return self.get_tasks_by_status(self.FAILED_EXECUTION_STATUSES)

    def get_tasks_by_status(self, statuses) -> Any:
        """Tasks whose last execution ended in one of `statuses`.

        Filtering happens in QRS, so a task past the record cap is still
        found — the previous "read everything, filter here" approach could
        not see it at all.
        """
        status_filter = " or ".join(
            f"operational.lastExecutionResult.status eq {int(code)}"
            for code in statuses
        )
        return self.get_task_operational_status(query_filter=f"({status_filter})")

    def _read_all(self, entity_type: str, endpoint: str,
                  columns: List[tuple], query_filter: Optional[str] = None,
                  sort_column: Optional[str] = None,
                  page_size: int = 500, hard_cap: int = 10000) -> Any:
        """Page through a QRS table endpoint until every row is read.

        `hard_cap` stops a runaway loop on a server that ignores skip;
        reaching it is logged, never silently truncated.
        """
        # What QRS says the filter matches, so a short page can be told
        # apart from the end of the data. A `/table` call that stops early
        # for its own reasons would otherwise truncate the list in silence.
        expected = self._count(endpoint, query_filter)
        if isinstance(expected, dict):  # error envelope
            return expected

        collected: List[Dict[str, Any]] = []
        skip = 0
        # The cap counts rows read, not the offset reached: with `skip`
        # the last page before the limit was refused one row early.
        while len(collected) < hard_cap:
            page = self._table(entity_type, endpoint, columns, query_filter,
                               skip=skip, take=page_size,
                               sort_column=sort_column, ascending=True)
            if isinstance(page, dict):  # error envelope
                return page
            collected.extend(page)
            if len(collected) >= expected:
                # Everything the filter matches is in hand; stop before
                # spending another request on an empty page.
                return collected
            if len(page) < page_size:
                if len(collected) < expected:
                    return {
                        "error": f"{endpoint} returned {len(collected)} of "
                                 f"{expected} matching rows and then stopped; "
                                 f"the result would have been partial",
                        "rows_read": len(collected),
                        "rows_expected": expected,
                    }
                return collected
            skip += page_size
        # A cap the caller cannot see is indistinguishable from a complete
        # answer, so it travels back with the data instead of only going
        # to the server log.
        logger.warning("%s: stopped reading at the %d-row cap; results are partial",
                       endpoint, hard_cap)
        return {
            "error": f"Read stopped at the {hard_cap}-row safety cap while "
                     f"paging {endpoint}; the result would have been partial",
            "rows_read": len(collected),
        }

    def create_reload_task(self, app_id: str, task_name: str,
                           enabled: bool = True) -> Dict[str, Any]:
        """Create a new reload task for an app."""
        body = {
            "task": {
                "app": {"id": app_id},
                "name": task_name,
                "taskType": 0,
                "enabled": enabled,
                "taskSessionTimeout": 1440,
                "maxRetries": 0,
                "isManuallyTriggered": False,
            }
        }
        return self._make_request("POST", "reloadtask/create", json=body)

    # Fields a caller may change on a reload task. QRS takes the whole
    # object on PUT, so without a whitelist an `updates` dict could
    # overwrite `id`, `createdDate` or the operational section.
    UPDATABLE_TASK_FIELDS = frozenset({
        "name", "enabled", "taskSessionTimeout", "maxRetries", "tags",
    })

    def update_reload_task(self, task_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Update an existing reload task. Pass fields to change (name, enabled, etc.)."""
        rejected = set(updates) - self.UPDATABLE_TASK_FIELDS
        if rejected:
            return {"error": f"Fields not updatable on a reload task: "
                             f"{', '.join(sorted(rejected))}",
                    "allowed_values": sorted(self.UPDATABLE_TASK_FIELDS)}

        current = self.get_reload_task_by_id(task_id)
        if isinstance(current, dict) and "error" in current:
            return current
        current.update(updates)

        result = self._make_request("PUT", f"reloadtask/{task_id}", json=current)
        # QRS rejects a PUT whose modifiedDate is stale — someone else
        # changed the task between our GET and PUT. Reported as a conflict
        # rather than a generic HTTP error, because the caller's answer is
        # different: re-read and try again, not "the server is broken".
        if isinstance(result, dict) and "error" in result and "409" in str(result["error"]):
            return {"error": f"The task changed between read and write "
                             f"(QRS 409): {result['error']}",
                    "error_category": "conflict"}
        return result

    UPDATABLE_SCHEDULE_FIELDS = frozenset({
        "name", "enabled", "startDate", "expirationDate", "timeZone",
        "daylightSavingTime", "incrementDescription", "incrementOption",
        "schemaFilterDescription",
    })

    def update_schema_trigger(self, trigger_id: str,
                              updates: Dict[str, Any]) -> Dict[str, Any]:
        """Change one schedule trigger, leaving the rest of the task alone.

        Without this the only way to stop a schedule was disabling the whole
        task, which also stops every other trigger attached to it.
        """
        rejected = set(updates) - self.UPDATABLE_SCHEDULE_FIELDS
        if rejected:
            return {"error": f"Fields not updatable on a schedule trigger: "
                             f"{', '.join(sorted(rejected))}",
                    "allowed_values": sorted(self.UPDATABLE_SCHEDULE_FIELDS)}

        current = self._make_request("GET", f"schemaevent/{trigger_id}")
        if isinstance(current, dict) and "error" in current:
            return current
        current.update(updates)
        result = self._make_request("PUT", f"schemaevent/{trigger_id}", json=current)
        if isinstance(result, dict) and "error" in result and "409" in str(result["error"]):
            return {"error": f"The trigger changed between read and write "
                             f"(QRS 409): {result['error']}",
                    "error_category": "conflict"}
        return result

    def delete_schema_trigger(self, trigger_id: str) -> Dict[str, Any]:
        """Delete one schedule trigger."""
        return self._make_request("DELETE", f"schemaevent/{trigger_id}")

    def delete_reload_task(self, task_id: str) -> Dict[str, Any]:
        """Delete a reload task."""
        return self._make_request("DELETE", f"reloadtask/{task_id}")

    def get_schema_triggers(self, task_id: str) -> List[Dict[str, Any]]:
        """Get schedule triggers for a task."""
        result = self._make_request(
            "GET", "schemaevent/full",
            params={"filter": f"reloadTask.id eq {task_id}"}
        )
        if isinstance(result, list):
            return result
        # "this task has no schedule" and "QRS did not answer" are
        # different facts, and only one of them means the operator can
        # stop looking.
        if isinstance(result, dict) and "error" in result:
            return result
        return {"error": f"Unexpected reply from schemaevent/full: {result!r}"}

    # QRS SchemaEvent.incrementOption. Note this is NOT the same numbering
    # the tool layer used to send: there is no "minutely" option, and daily
    # is 2, not 3. A wrong code produces a trigger QMC renders under the
    # wrong type with a schedule that never comes round.
    SCHEDULE_REPEAT_OPTIONS = {
        "once": 0,
        "hourly": 1,
        "daily": 2,
        "weekly": 3,
        "monthly": 4,
    }

    # schemaFilterDescription is one string of exactly 8 space-separated
    # positions describing WHEN firing is allowed (a window), not how often:
    #   Minute Hour WeekDayPrefix WeekDay WeeklyInterval DayOfMonth Month MonthlyInterval
    # "-" in the third position means "no week prefix" — it is a value, not
    # a separator. All-wildcards means "no window restriction", which is
    # what a plain interval schedule wants.
    OPEN_SCHEMA_FILTER = "* * - * * * * *"

    @staticmethod
    def _increment_description(minutes: int) -> str:
        """QRS increment as "minutes hours days weeks".

        The old code wrote the interval into the *days* position
        (`f"0 0 {increment_minutes} 0"`), so "every 1440 minutes" asked
        Qlik for every 1440 days. Splitting into whole units keeps QMC's
        rendering sane: 1440 minutes shows up as "1 day", not "1440".
        """
        minutes = max(int(minutes or 0), 0)
        weeks, rest = divmod(minutes, 7 * 24 * 60)
        days, rest = divmod(rest, 24 * 60)
        hours, mins = divmod(rest, 60)
        return f"{mins} {hours} {days} {weeks}"

    def create_schema_trigger(self, task_id: str, name: str,
                              time_zone: str = "Europe/Moscow",
                              start_date: str = "2026-04-01T00:00:00.000Z",
                              repeat: str = "daily",
                              increment_minutes: int = 1440,
                              enabled: bool = True,
                              schema_filter: Optional[str] = None,
                              daylight_saving_time: int = 0) -> Dict[str, Any]:
        """Create a schedule trigger (SchemaEvent) for a reload task.

        `repeat` is one of SCHEDULE_REPEAT_OPTIONS. `increment_minutes` is
        the interval between runs and is ignored for "once".
        """
        key = (repeat or "").strip().lower()
        if key not in self.SCHEDULE_REPEAT_OPTIONS:
            return {"error": f"Unknown repeat {repeat!r}",
                    "allowed_values": sorted(self.SCHEDULE_REPEAT_OPTIONS)}

        body = {
            "name": name,
            "enabled": enabled,
            "eventType": 0,  # Schema
            "reloadTask": {"id": task_id},
            "timeZone": time_zone,
            # Since Qlik November 2025 the IANA zone and DST are separate
            # fields and an offset inside startDate is deprecated.
            # Measured: with Europe/Berlin and a 03:00 local
            # start in July: 0 fires at 01:00 UTC (the zone's DST rules are
            # observed), 1 fires at 02:00 UTC (standard time year-round).
            # 0 is therefore the right default — the schedule stays at the
            # wall-clock time the user asked for.
            "daylightSavingTime": daylight_saving_time,
            "startDate": start_date,
            "expirationDate": "9999-12-30T23:59:59.000Z",
            "schemaFilterDescription": [schema_filter or self.OPEN_SCHEMA_FILTER],
            "incrementDescription": self._increment_description(increment_minutes),
            "incrementOption": self.SCHEDULE_REPEAT_OPTIONS[key],
            # No "operational" key: QRS rejects an empty one outright with
            # 400 "invalid property ... operational with EMPTY GuID", and
            # it fills the section in itself. Sending it is what made this
            # tool fail on every single call.
        }
        return self._make_request("POST", "schemaevent", json=body)

    def get_execution_results(self, task_id: str, top: int = 10) -> Any:
        """Execution results for a reload task, newest first.

        `top` is a server-side `take`, not a slice of whatever QRS felt
        like returning: `executionresult/full` is capped at MaxRecordLimit
        and ordering is applied before that cap, so slicing locally could
        silently drop the newest runs on a busy task.
        """
        if top is None or top < 1:
            top = 10

        # The table endpoint picks the right N rows in the right order, but
        # flattens each to the columns asked for. Script-log retrieval needs
        # fields that do not survive that (scriptLogAvailable,
        # scriptLogLocation, the nested details), so the ids come from the
        # table and the objects themselves from `executionresult/{id}`.
        page = self._table(
            "ExecutionResult", "executionresult",
            columns=[("id", "id")],
            query_filter=f"taskID eq {task_id}",
            skip=0,
            take=top,
            sort_column="startTime",
            ascending=False,
        )
        if isinstance(page, dict):  # error envelope
            return page

        executions = []
        for row in page:
            full = self._make_request("GET", f"executionresult/{row['id']}")
            if isinstance(full, dict) and "error" in full:
                return full
            executions.append(full)
        return executions

    def get_script_log_by_task_id(self, task_id: str) -> str:
        """Get script log for a reload task.

        Tries multiple approaches to download the log, handling both
        single-node and multi-node environments:
        1. QRS scriptlog endpoint + tempContent download
        2. Direct file read from shared persistence (ArchivedLogs UNC path)
        3. Fallback to execution details if log unavailable
        """
        try:
            executions = self.get_execution_results(task_id, top=5)
            if isinstance(executions, dict) and "error" in executions:
                # Iterating an error envelope walks its keys and blows up on
                # the first `.get`. Say what actually happened instead.
                return f"Could not read execution history: {executions['error']}"
            if not executions:
                return "No execution results found for this task."

            target_exec = None
            for ex in executions:
                if ex.get("scriptLogAvailable", False):
                    target_exec = ex
                    break

            if not target_exec:
                return self._format_execution_fallback(executions[0])

            file_ref_id = target_exec.get("fileReferenceID", "")
            null_ref = "00000000-0000-0000-0000-000000000000"

            # --- Approach 1: QRS scriptlog + tempContent ---
            if file_ref_id and file_ref_id != null_ref:
                try:
                    url = self._get_api_url(f"reloadtask/{task_id}/scriptlog")
                    xrfkey = generate_xrfkey()
                    resp = self.client.get(
                        url,
                        params={"xrfkey": xrfkey, "fileReferenceId": file_ref_id},
                        headers={"X-Qlik-Xrfkey": xrfkey},
                        follow_redirects=True,
                    )
                    if resp.status_code == 200:
                        ct = resp.headers.get("content-type", "")
                        if "json" not in ct and len(resp.text) > 50:
                            return resp.text
                        if "json" in ct:
                            temp_id = resp.json().get("value", "")
                            if temp_id:
                                log_text = self._download_temp_content(temp_id)
                                if log_text:
                                    return log_text
                except Exception as e1:
                    logger.debug("QRS scriptlog download failed: %s", e1)

            # --- Approach 2: Direct file read from shared persistence ---
            log_location = target_exec.get("scriptLogLocation", "")
            if log_location:
                log_text = self._read_script_log_from_share(log_location)
                if log_text:
                    return log_text

            # --- Fallback ---
            return self._format_execution_fallback(target_exec)

        except Exception as e:
            logger.error("get_script_log_by_task_id error: %s", e)
            return f"Error fetching script log: {e}"

    def _get_archived_logs_root(self) -> Optional[str]:
        """Get ArchivedLogs root folder from service cluster config."""
        try:
            result = self._make_request("GET", "servicecluster/full")
            if isinstance(result, list) and result:
                props = result[0].get("settings", {}).get("sharedPersistenceProperties", {})
                return props.get("archivedLogsRootFolder", "")
        except Exception as e:
            logger.debug("Failed to get archived logs root: %s", e)
        return None

    def _read_script_log_from_share(self, log_location: str) -> Optional[str]:
        """Try to read script log file from shared persistence ArchivedLogs."""
        try:
            root = self._get_archived_logs_root()
            if not root:
                return None

            # Build full path: root + log_location
            # log_location example: "node.local\Script\appid.timestamp.log"
            # root example: "\\server\qlikshare\ArchivedLogs"
            import pathlib
            log_path = pathlib.PureWindowsPath(root) / log_location
            full_path = str(log_path)

            logger.debug("Trying to read script log from: %s", full_path)

            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except FileNotFoundError:
            logger.debug("Script log file not found at shared path: %s", log_location)
        except PermissionError:
            logger.debug("Permission denied reading script log: %s", log_location)
        except Exception as e:
            logger.debug("Failed to read script log from share: %s", e)
        return None

    def _download_temp_content(self, temp_id: str) -> Optional[str]:
        """Download content from QRS tempContent by ID."""
        try:
            xrfkey = generate_xrfkey()
            url = self._get_api_url(f"tempContent/{temp_id}")
            resp = self.client.get(
                url,
                params={"xrfkey": xrfkey},
                headers={"X-Qlik-Xrfkey": xrfkey},
                follow_redirects=True,
            )
            if resp.status_code == 200:
                ct = resp.headers.get("content-type", "")
                if "json" not in ct and len(resp.text) > 50:
                    return resp.text
        except Exception as e:
            logger.debug("tempContent download failed: %s", e)
        return None

    def _format_execution_fallback(self, execution: Dict[str, Any]) -> str:
        """Format execution details as fallback when script log is unavailable."""
        details = execution.get("details", [])
        messages = []
        if isinstance(details, list):
            for d in details:
                if isinstance(d, dict):
                    messages.append(d.get("message", ""))
        else:
            messages.append(str(details))

        file_ref_id = execution.get("fileReferenceID", "")
        null_ref = "00000000-0000-0000-0000-000000000000"
        file_location = ""
        if file_ref_id and file_ref_id != null_ref:
            fr = self._make_request("GET", f"filereference/{file_ref_id}")
            if isinstance(fr, dict) and "location" in fr:
                file_location = fr["location"]

        lines = [
            "Script log download not available.",
            f"Execution status: {execution.get('status')}",
            f"Start: {execution.get('startTime', '')}",
            f"Stop: {execution.get('stopTime', '')}",
            f"Duration: {execution.get('duration', 0)}ms",
            f"Node: {execution.get('executingNodeName', '')}",
            "",
            "Execution details:",
        ]
        for msg in messages:
            lines.append(f"  - {msg}")
        if file_location:
            lines.append(f"\nScript log file path on server: {file_location}")
        return "\n".join(lines)

    _COMPOSITE_EVENT_COLUMNS = [
        ("id", "id"),
        ("name", "name"),
        ("enabled", "enabled"),
        ("reloadTaskId", "reloadTask.id"),
        ("reloadTaskName", "reloadTask.name"),
    ]

    def get_all_composite_events(self) -> Any:
        """Every composite event (one task triggering another).

        Read page by page: `compositeevent/full` is cut off at the QRS
        record limit, and a dependency past that cut simply did not exist
        as far as the caller was concerned — the chain came back short
        with no indication that anything was missing.

        Composite rules (which upstream task, and on which outcome) do
        not survive the table endpoint, so they are fetched per event.
        """
        events = self._read_all("CompositeEvent", "compositeevent",
                                self._COMPOSITE_EVENT_COLUMNS,
                                sort_column="name")
        if isinstance(events, dict):  # error envelope
            return events

        detailed = []
        for event in events:
            full = self._make_request("GET", f"compositeevent/{event['id']}")
            if isinstance(full, dict) and "error" in full:
                return full
            detailed.append(full)
        return detailed

    def close(self):
        """Close the HTTP client."""
        self.client.close()
