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

    def get_comprehensive_apps(self, filter_query: Optional[str] = None) -> Dict[str, Any]:
        """Get comprehensive list of apps with streams, metadata and ownership information."""
        # 1. Получаем список приложений
        endpoint = "app/full"
        if filter_query:
            endpoint += f"?filter={filter_query}"

        apps_result = self._make_request("GET", endpoint)

        # Normalize apps result
        if isinstance(apps_result, list):
            apps = apps_result
        elif isinstance(apps_result, dict):
            if "error" in apps_result:
                apps = []
            else:
                apps = apps_result.get("apps", apps_result.get("data", []))
        else:
            apps = []

        # 2. Получаем список streams
        streams = self.get_streams()
        streams_dict = {stream.get("id"): stream for stream in streams}

        # 3. Обогащаем каждое приложение
        enriched_apps = []
        for app in apps:
            try:
                app_id = app.get("id", "")

                # Базовая информация
                enriched_app = {
                    "basic_info": {
                        "id": app_id,
                        "name": app.get("name", ""),
                        "description": app.get("description", ""),
                        "filename": app.get("filename", ""),
                        "owner": app.get("owner", {}),
                        "created_date": app.get("createdDate", ""),
                        "modified_date": app.get("modifiedDate", ""),
                        "privileges": app.get("privileges", [])
                    },
                    "stream_info": {
                        "published": app.get("published", False),
                        "publish_time": app.get("publishTime", ""),
                        "stream_id": app.get("stream", {}).get("id", "") if app.get("stream") else "",
                        "stream_name": "",
                        "stream_owner": {}
                    },
                    "size_info": {
                        "file_size_bytes": app.get("fileSize", 0),
                        "static_byte_size": app.get("qStaticByteSize", 0),
                        "last_reload_time": app.get("lastReloadTime", "")
                    }
                }

                # Обогащаем информацией о stream
                stream_id = enriched_app["stream_info"]["stream_id"]
                if stream_id and stream_id in streams_dict:
                    stream_info = streams_dict[stream_id]
                    enriched_app["stream_info"]["stream_name"] = stream_info.get("name", "")
                    enriched_app["stream_info"]["stream_owner"] = stream_info.get("owner", {})

                # Получаем метаданные приложения (если нужно детальную информацию)
                if app_id:
                    app_metadata = self.get_app_metadata(app_id)
                    if isinstance(app_metadata, dict) and "error" not in app_metadata:
                        enriched_app["metadata"] = {
                            "reload_meta": app_metadata.get("reload_meta", {}),
                            "tables_count": len(app_metadata.get("tables", [])),
                            "fields_count": len(app_metadata.get("fields", [])),
                            "has_section_access": app_metadata.get("has_section_access", False),
                            "is_direct_query_mode": app_metadata.get("is_direct_query_mode", False)
                        }
                    else:
                        enriched_app["metadata"] = {
                            "reload_meta": {},
                            "tables_count": 0,
                            "fields_count": 0,
                            "has_section_access": False,
                            "is_direct_query_mode": False,
                            "metadata_error": app_metadata.get("error", "Unable to fetch metadata")
                        }

                enriched_apps.append(enriched_app)

            except Exception as e:
                # Если ошибка при обработке конкретного приложения, включаем базовую информацию
                enriched_apps.append({
                    "basic_info": {
                        "id": app.get("id", ""),
                        "name": app.get("name", ""),
                        "error": f"Failed to enrich app data: {str(e)}"
                    }
                })

        # 4. Формируем финальный ответ
        return {
            "apps": enriched_apps,
            "streams": streams,
            "summary": {
                "total_apps": len(enriched_apps),
                "published_apps": len([app for app in enriched_apps if app.get("stream_info", {}).get("published", False)]),
                "private_apps": len([app for app in enriched_apps if not app.get("stream_info", {}).get("published", False)]),
                "total_streams": len(streams)
            }
        }

    def get_app_by_id(self, app_id: str) -> Dict[str, Any]:
        """Get specific app by ID."""
        return self._make_request("GET", f"app/{app_id}")

    def get_streams(self) -> List[Dict[str, Any]]:
        """Get list of streams."""
        result = self._make_request("GET", "stream/full")
        return result if isinstance(result, list) else []

    def start_task(self, task_id: str) -> Dict[str, Any]:
        """Start a task execution."""
        return self._make_request("POST", f"task/{task_id}/start")

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
