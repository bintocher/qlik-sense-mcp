"""Qlik Sense Repository API client."""

import json
import ssl
import asyncio
from typing import Dict, List, Any, Optional
import httpx
import logging
from .config import QlikSenseConfig

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

        # XSRF key for Qlik Sense API
        self.xrfkey = "0123456789abcdef"

        # Create httpx client with certificates and SSL context
        self.client = httpx.Client(
            verify=ssl_context if self.config.verify_ssl else False,
            cert=cert,
            timeout=30.0,
            headers={
                "X-Qlik-User": f"UserDirectory={self.config.user_directory}; UserId={self.config.user_id}",
                "X-Qlik-Xrfkey": self.xrfkey,
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

            # Add xrfkey parameter to all requests
            params = kwargs.get('params', {})
            params['xrfkey'] = self.xrfkey
            kwargs['params'] = params

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
                                   limit: int = 25,
                                   offset: int = 0,
                                   name: Optional[str] = None,
                                   stream: Optional[str] = None,
                                   published: Optional[bool] = True) -> Dict[str, Any]:
        """
        Get minimal list of apps with essential fields and proper filtering/pagination.

        Returns only: guid, name, description, stream, modified_dttm, reload_dttm.
        Supports case-insensitive wildcard filters for name and stream, and published flag.
        Enforces default limit=25 and maximum limit=50.
        """
        if limit is None or limit < 1:
            limit = 25
        if limit > 50:
            limit = 50
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

        if name:
            lowered = name.lower().replace('*', '')
            minimal_apps = [a for a in minimal_apps if lowered in (a.get("name", "").lower())]
        if stream:
            lowered_stream = stream.lower().replace('*', '')
            minimal_apps = [a for a in minimal_apps if lowered_stream in (a.get("stream", "").lower())]
        if published is not None:
            if published:
                minimal_apps = [a for a in minimal_apps if a.get("stream", "") != ""]
            else:
                minimal_apps = [a for a in minimal_apps if a.get("stream", "") == ""]

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

            # Add xrfkey parameter
            params = {'xrfkey': self.xrfkey}

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

    def close(self):
        """Close the HTTP client."""
        self.client.close()
