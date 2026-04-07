"""Qlik Sense Repository API client."""

import json
import ssl
import asyncio
from typing import Dict, List, Any, Optional
import httpx
import logging
import os
from .config import (
    QlikSenseConfig,
    DEFAULT_HTTP_TIMEOUT,
    DEFAULT_APPS_LIMIT,
    MAX_APPS_LIMIT,
)
from .utils import generate_xrfkey
from .exceptions import QlikRepositoryError

logger = logging.getLogger(__name__)


class QlikRepositoryAPI:
    """Client for Qlik Sense Repository API using httpx."""

    def __init__(self, config: QlikSenseConfig):
        self.config = config

        # Setup SSL verification
        if self.config.verify_ssl:
            ssl_context = ssl.create_default_context()
            if self.config.ca_cert_path:
                ssl_context.load_verify_locations(self.config.ca_cert_path)
        else:
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        # Setup client certificates if provided
        cert = None
        if self.config.client_cert_path and self.config.client_key_path:
            cert = (self.config.client_cert_path, self.config.client_key_path)

        # Timeouts from env (seconds)
        http_timeout_env = os.getenv("QLIK_HTTP_TIMEOUT")
        try:
            timeout_val = float(http_timeout_env) if http_timeout_env else DEFAULT_HTTP_TIMEOUT
        except ValueError:
            timeout_val = DEFAULT_HTTP_TIMEOUT

        # Create httpx client with certificates and SSL context
        self.client = httpx.Client(
            verify=ssl_context if self.config.verify_ssl else False,
            cert=cert,
            timeout=timeout_val,
            headers={
                "X-Qlik-User": f"UserDirectory={self.config.user_directory}; UserId={self.config.user_id}",
                "Content-Type": "application/json",
            },
        )

    def _get_api_url(self, endpoint: str) -> str:
        """Get full API URL for endpoint."""
        base_url = f"{self.config.server_url}:{self.config.repository_port}"
        return f"{base_url}/qrs/{endpoint}"

    def _make_request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        """Make HTTP request to Repository API."""
        try:
            url = self._get_api_url(endpoint)

            # Generate dynamic xrfkey for each request
            xrfkey = generate_xrfkey()

            # Add xrfkey parameter to all requests
            params = kwargs.get('params', {})
            params['xrfkey'] = xrfkey
            kwargs['params'] = params

            # Add xrfkey header
            headers = kwargs.get('headers', {})
            headers['X-Qlik-Xrfkey'] = xrfkey
            kwargs['headers'] = headers

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

        params: Dict[str, Any] = {}
        if filters:
            params["filter"] = " and ".join(filters)
        params["orderby"] = "modifiedDate desc"

        apps_result = self._make_request("GET", "app/full", params=params)

        if isinstance(apps_result, list):
            apps = apps_result
        elif isinstance(apps_result, dict):
            if "error" in apps_result:
                apps = []
            else:
                apps = apps_result.get("data", []) or apps_result.get("apps", [])
        else:
            apps = []

        minimal_apps: List[Dict[str, Any]] = []
        for app in apps:
            try:
                is_published = bool(app.get("published", False))
                stream_name = app.get("stream", {}).get("name", "") if is_published else ""
                minimal_apps.append({
                    "guid": app.get("id", ""),
                    "name": app.get("name", ""),
                    "description": app.get("description") or "",
                    "stream": stream_name or "",
                    "modified_dttm": app.get("modifiedDate", "") or "",
                    "reload_dttm": app.get("lastReloadTime", "") or "",
                })
            except Exception:
                continue

        total_found = len(minimal_apps)
        paginated_apps = minimal_apps[offset:offset + limit]

        return {
            "apps": paginated_apps,
            "pagination": {
                "limit": limit,
                "offset": offset,
                "returned": len(paginated_apps),
                "total_found": total_found,
                "has_more": (offset + limit) < total_found,
                "next_offset": (offset + limit) if (offset + limit) < total_found else None,
            },
        }

    def get_about(self) -> Dict[str, Any]:
        """Get Qlik Sense server information via QRS /qrs/about endpoint."""
        return self._make_request("GET", "about")

    def get_app_by_id(self, app_id: str) -> Dict[str, Any]:
        """Get specific app by ID."""
        return self._make_request("GET", f"app/{app_id}")

    def get_streams(self) -> List[Dict[str, Any]]:
        """Get list of streams."""
        result = self._make_request("GET", "stream/full")
        return result if isinstance(result, list) else []

    def start_task(self, task_id: str) -> Dict[str, Any]:
        """
        Start a task execution.

        Note: This method is not exported via MCP API as it's an administrative function,
        not an analytical tool. Available for internal use only.
        """
        return self._make_request("POST", f"task/{task_id}/start")

    def get_app_metadata(self, app_id: str) -> Dict[str, Any]:
        """Get detailed app metadata using Engine REST API."""
        try:
            base_url = f"{self.config.server_url}"
            url = f"{base_url}/api/v1/apps/{app_id}/data/metadata"

            # Generate dynamic xrfkey for each request
            xrfkey = generate_xrfkey()
            params = {'xrfkey': xrfkey}

            response = self.client.request("GET", url, params=params)
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

    def get_app_reload_tasks(self, app_id: str) -> List[Dict[str, Any]]:
        """Get reload tasks for specific app."""
        filter_query = f"app.id eq {app_id}"
        endpoint = f"reloadtask/full?filter={filter_query}"

        result = self._make_request("GET", endpoint)
        return result if isinstance(result, list) else []

    def get_task_executions(self, task_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Get execution history for a task."""
        endpoint = f"executionresult/full?filter=executionId eq {task_id}&orderby=startTime desc"
        if limit:
            endpoint += f"&limit={limit}"

        result = self._make_request("GET", endpoint)
        return result if isinstance(result, list) else []

    def get_app_objects(self, app_id: str, object_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get app objects (sheets, charts, etc.)."""
        filter_query = f"app.id eq {app_id}"
        if object_type:
            filter_query += f" and objectType eq '{object_type}'"

        endpoint = f"app/object/full?filter={filter_query}"

        result = self._make_request("GET", endpoint)
        return result if isinstance(result, list) else []

    def get_reload_tasks_for_app(self, app_id: str) -> List[Dict[str, Any]]:
        """Get all reload tasks associated with an app."""
        filter_query = f"app.id eq {app_id}"
        endpoint = f"reloadtask/full?filter={filter_query}"

        result = self._make_request("GET", endpoint)
        return result if isinstance(result, list) else []

    # ── Task management methods ──

    def get_all_reload_tasks(self, filter_str: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all reload tasks with optional QRS filter."""
        endpoint = "reloadtask/full"
        params: Dict[str, Any] = {}
        if filter_str:
            params["filter"] = filter_str
        params["orderby"] = "name asc"
        result = self._make_request("GET", endpoint, params=params)
        return result if isinstance(result, list) else []

    def get_reload_task_by_id(self, task_id: str) -> Dict[str, Any]:
        """Get single reload task by ID."""
        return self._make_request("GET", f"reloadtask/{task_id}")

    def get_task_operational_status(self) -> List[Dict[str, Any]]:
        """Get operational status of all tasks (includes last execution result)."""
        result = self._make_request("GET", "reloadtask/full")
        if not isinstance(result, list):
            return []
        tasks_with_status = []
        for task in result:
            status = task.get("operational", {})
            last_result = status.get("lastExecutionResult", {})
            tasks_with_status.append({
                "id": task.get("id", ""),
                "name": task.get("name", ""),
                "enabled": task.get("enabled", False),
                "task_type": task.get("taskType", 0),
                "app_id": task.get("app", {}).get("id", ""),
                "app_name": task.get("app", {}).get("name", ""),
                "next_execution": status.get("nextExecution", ""),
                "last_execution_result": {
                    "status": last_result.get("status", -1),
                    "start_time": last_result.get("startTime", ""),
                    "stop_time": last_result.get("stopTime", ""),
                    "duration_seconds": last_result.get("duration", 0),
                    "details": last_result.get("details", ""),
                    "execution_id": last_result.get("id", ""),
                },
            })
        return tasks_with_status

    def get_failed_tasks(self) -> List[Dict[str, Any]]:
        """Get tasks whose last execution actually failed (status=8 FinishedFail)."""
        all_tasks = self.get_task_operational_status()
        return [t for t in all_tasks if t.get("last_execution_result", {}).get("status") == 8]

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

    def update_reload_task(self, task_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Update an existing reload task. Pass fields to change (name, enabled, etc.)."""
        current = self.get_reload_task_by_id(task_id)
        if isinstance(current, dict) and "error" in current:
            return current
        current.update(updates)
        return self._make_request("PUT", f"reloadtask/{task_id}", json=current)

    def delete_reload_task(self, task_id: str) -> Dict[str, Any]:
        """Delete a reload task."""
        return self._make_request("DELETE", f"reloadtask/{task_id}")

    def get_schema_triggers(self, task_id: str) -> List[Dict[str, Any]]:
        """Get schedule triggers for a task."""
        result = self._make_request(
            "GET", "schemaevent/full",
            params={"filter": f"reloadTask.id eq {task_id}"}
        )
        return result if isinstance(result, list) else []

    def create_schema_trigger(self, task_id: str, name: str,
                              time_zone: str = "Europe/Moscow",
                              start_date: str = "2026-04-01T00:00:00.000Z",
                              repeat_opt: int = 0,
                              increment_minutes: int = 1440,
                              enabled: bool = True) -> Dict[str, Any]:
        """Create a schedule trigger (SchemaEvent) for a reload task.

        repeat_opt: 0=Once, 1=Minutely, 2=Hourly, 3=Daily, 4=Weekly, 5=Monthly
        increment_minutes: interval in minutes between executions
        """
        body = {
            "name": name,
            "enabled": enabled,
            "eventType": 0,  # Schema
            "reloadTask": {"id": task_id},
            "timeZone": time_zone,
            "startDate": start_date,
            "expirationDate": "9999-12-30T23:59:59.000Z",
            "schemaFilterDescription": [repeat_opt],
            "incrementDescription": f"0 0 {increment_minutes} 0",
            "incrementOption": 1,
            "operational": {},
        }
        return self._make_request("POST", "schemaevent", json=body)

    def get_execution_results(self, task_id: str, top: int = 10) -> List[Dict[str, Any]]:
        """Get execution results for a reload task, newest first."""
        result = self._make_request(
            "GET", "executionresult/full",
            params={
                "filter": f"taskID eq {task_id}",
                "orderby": "startTime desc",
            }
        )
        if isinstance(result, list):
            return result[:top]
        return []

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
            f"Script log download not available.",
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

    def get_all_composite_events(self) -> List[Dict[str, Any]]:
        """Get all composite events (task dependency triggers) from QRS."""
        result = self._make_request("GET", "compositeevent/full")
        return result if isinstance(result, list) else []

    def close(self):
        """Close the HTTP client."""
        self.client.close()
