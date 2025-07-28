"""Qlik Sense Repository API client."""

import json
import ssl
import asyncio
from typing import Dict, List, Any, Optional
import httpx
import logging
from .config import QlikSenseConfig
from .cache import get_cached_app_metadata, set_cached_app_metadata

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

        # Create httpx client with certificates and SSL context
        self.client = httpx.Client(
            verify=ssl_context if self.config.verify_ssl else False,
            cert=cert,
            timeout=30.0,
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

    def get_apps(self, filter_query: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get list of apps from Repository API."""
        endpoint = "app/full"
        if filter_query:
            endpoint += f"?filter={filter_query}"

        result = self._make_request("GET", endpoint)

        if isinstance(result, list):
            return result
        elif isinstance(result, dict):
            if "error" in result:
                return []
            # Handle wrapped response
            return result.get("apps", result.get("data", []))
        else:
            return []

    def get_app_by_id(self, app_id: str) -> Dict[str, Any]:
        """Get specific app by ID."""
        return self._make_request("GET", f"app/{app_id}")

    def get_users(self, filter_query: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get list of users."""
        endpoint = "user/full"
        if filter_query:
            endpoint += f"?filter={filter_query}"

        result = self._make_request("GET", endpoint)
        return result if isinstance(result, list) else []

    def get_streams(self) -> List[Dict[str, Any]]:
        """Get list of streams."""
        result = self._make_request("GET", "stream/full")
        return result if isinstance(result, list) else []

    def get_data_connections(self, filter_query: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get list of data connections."""
        endpoint = "dataconnection/full"
        if filter_query:
            endpoint += f"?filter={filter_query}"

        result = self._make_request("GET", endpoint)
        return result if isinstance(result, list) else []

    def get_tasks(self, task_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get list of tasks."""
        if task_type == "reload":
            endpoint = "reloadtask/full"
        elif task_type == "external":
            endpoint = "externalprogramtask/full"
        else:
            # Get both types
            reload_tasks = self._make_request("GET", "reloadtask/full")
            external_tasks = self._make_request("GET", "externalprogramtask/full")

            tasks = []
            if isinstance(reload_tasks, list):
                tasks.extend([{**task, "task_type": "reload"} for task in reload_tasks])
            if isinstance(external_tasks, list):
                tasks.extend([{**task, "task_type": "external"} for task in external_tasks])

            return tasks

        result = self._make_request("GET", endpoint)
        task_type_label = task_type or "unknown"

        if isinstance(result, list):
            return [{**task, "task_type": task_type_label} for task in result]
        else:
            return []

    def start_task(self, task_id: str) -> Dict[str, Any]:
        """Start a task execution."""
        return self._make_request("POST", f"task/{task_id}/start")

    def get_extensions(self) -> List[Dict[str, Any]]:
        """Get list of extensions."""
        result = self._make_request("GET", "extension/full")
        return result if isinstance(result, list) else []

    def get_content_libraries(self) -> List[Dict[str, Any]]:
        """Get list of content libraries."""
        result = self._make_request("GET", "contentlibrary/full")
        return result if isinstance(result, list) else []

    def get_app_metadata(self, app_id: str) -> Dict[str, Any]:
        """Get detailed app metadata including data model information."""
        return self._make_request("GET", f"app/{app_id}/data/metadata")

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
