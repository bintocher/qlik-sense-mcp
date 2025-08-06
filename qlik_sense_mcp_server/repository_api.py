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
                                   limit: int = 50, 
                                   offset: int = 0,
                                   name_filter: Optional[str] = None,
                                   app_id_filter: Optional[str] = None,
                                   include_unpublished: bool = True) -> Dict[str, Any]:
        """
        Get comprehensive list of apps with streams, metadata and ownership information.
        
        Args:
            limit: Maximum number of apps to return (default: 50, max: 1000)
            offset: Number of apps to skip for pagination (default: 0)
            name_filter: Filter apps by name (case-insensitive partial match)
            app_id_filter: Filter by specific app ID/GUID
            include_unpublished: Include unpublished apps (default: True)
        """
        # 1. Build filter query
        filters = []
        
        if app_id_filter:
            filters.append(f"id eq {app_id_filter}")
        
        if name_filter:
            # Use contains for partial name matching
            filters.append(f"name so '{name_filter}'")
            
        if not include_unpublished:
            filters.append("stream ne null")
        
        # 2. Get applications list
        endpoint = "app/full"
        if filters:
            filter_query = " and ".join(filters)
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

                # Обогащаем информацию о потоке (стриме)
                stream_id = enriched_app["stream_info"]["stream_id"]
                if stream_id and stream_id in streams_dict:
                    stream_info = streams_dict[stream_id]
                    enriched_app["stream_info"]["stream_name"] = stream_info.get("name", "")
                    enriched_app["stream_info"]["stream_owner"] = stream_info.get("owner", {})

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

        # 4. Apply additional client-side filtering if needed
        if name_filter:
            # Additional case-insensitive filtering on client side for better matching
            enriched_apps = [
                app for app in enriched_apps 
                if name_filter.lower() in app.get("basic_info", {}).get("name", "").lower()
            ]
        
        # 5. Calculate totals before pagination
        total_found = len(enriched_apps)
        total_published = len([app for app in enriched_apps if app.get("stream_info", {}).get("published", False)])
        total_private = total_found - total_published
        
        # 6. Apply pagination
        paginated_apps = enriched_apps[offset:offset + limit]
        
        # 7. Build final response with pagination metadata
        return {
            "apps": paginated_apps,
            "streams": streams,
            "pagination": {
                "limit": limit,
                "offset": offset,
                "returned": len(paginated_apps),
                "total_found": total_found,
                "has_more": offset + limit < total_found,
                "next_offset": offset + limit if offset + limit < total_found else None
            },
            "filters": {
                "name_filter": name_filter,
                "app_id_filter": app_id_filter,
                "include_unpublished": include_unpublished
            },
            "summary": {
                "total_apps": total_found,
                "published_apps": total_published,
                "private_apps": total_private,
                "total_streams": len(streams),
                "showing": f"{offset + 1}-{min(offset + limit, total_found)} of {total_found}"
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
        """
        Start a task execution.

        Note: This method is not exported via MCP API as it's an administrative function,
        not an analytical tool. Available for internal use only.
        """
        return self._make_request("POST", f"task/{task_id}/start")

    def get_app_metadata(self, app_id: str) -> Dict[str, Any]:
        """Get detailed app metadata using Engine REST API."""
        try:
            # Используем Engine REST API вместо QRS
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
