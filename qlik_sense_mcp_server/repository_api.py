"""Qlik Sense Repository API client."""

import httpx
from typing import Dict, List, Any, Optional
from .config import QlikSenseConfig


class QlikRepositoryAPI:
    """
    Client for Qlik Sense Repository API (REST-based).

    Handles HTTP requests to Repository Service for managing applications,
    users, tasks, data connections, and other Qlik Sense resources.
    """

    def __init__(self, config: QlikSenseConfig):
        self.config = config
        self.base_url = f"{config.server_url}:{config.repository_port}/qrs"
        self.proxy_url = f"{config.server_url}:{config.proxy_port}"

        # Setup HTTP client with certificates
        client_kwargs = {
            "timeout": 30.0
        }

        if config.client_cert_path and config.client_key_path:
            client_kwargs["cert"] = (config.client_cert_path, config.client_key_path)

        # SSL verification
        if config.verify_ssl and config.ca_cert_path:
            client_kwargs["verify"] = config.ca_cert_path
        else:
            client_kwargs["verify"] = config.verify_ssl

        self.client = httpx.Client(**client_kwargs)

        # Default headers with XSRF protection
        self.xrf_key = "0123456789abcdef"
        self.headers = {
            "X-Qlik-User": f"UserDirectory={config.user_directory}; UserId={config.user_id}",
            "Content-Type": "application/json",
            "X-Qlik-Xrfkey": self.xrf_key
        }

        # Cache for Qlik ticket
        self._qlik_ticket = None

    def _add_xrf_params(self, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """Add XSRF key to parameters."""
        if params is None:
            params = {}
        params["xrfkey"] = self.xrf_key
        return params

    def _get_qlik_ticket(self) -> str:
        """Get Qlik Sense ticket using the same approach as qs_ticket.py."""
        if self._qlik_ticket:
            return self._qlik_ticket

        import json
        import random
        import string

        # Generate XRF key
        characters = string.ascii_letters + string.digits
        xrf = ''.join(random.sample(characters, 16))

        # Prepare payload and headers
        payload = {
            'UserDirectory': self.config.user_directory,
            'UserId': self.config.user_id
        }
        json_payload = json.dumps(payload)

        headers = {
            'content-type': 'application/json',
            'X-Qlik-Xrfkey': xrf,
        }

        # Construct URL - extract server name from server_url
        server_name = self.config.server_url.replace("https://", "").replace("http://", "")
        url = f'https://{server_name}:4243/qps/ticket?Xrfkey={xrf}'

        try:
            response = self.client.post(url, data=json_payload, headers=headers)
            response.raise_for_status()
            ticket = response.json().get('Ticket')
            if ticket:
                self._qlik_ticket = ticket
            return ticket
        except Exception as e:
            raise Exception(f"Failed to get Qlik ticket: {str(e)}")



    def get_apps(self, filter_query: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get list of applications."""
        url = f"{self.base_url}/app"
        params = {"xrfkey": self.xrf_key}
        if filter_query:
            params["filter"] = filter_query

        response = self.client.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_app_by_id(self, app_id: str) -> Dict[str, Any]:
        """
        Get detailed information for specific application.

        Args:
            app_id: Application ID

        Returns:
            Application object with complete metadata
        """
        url = f"{self.base_url}/app/{app_id}"
        params = self._add_xrf_params()
        response = self.client.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_users(self, filter_query: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get list of users."""
        url = f"{self.base_url}/user"
        params = self._add_xrf_params()
        if filter_query:
            params["filter"] = filter_query

        response = self.client.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_streams(self) -> List[Dict[str, Any]]:
        """Get list of streams."""
        url = f"{self.base_url}/stream"
        params = self._add_xrf_params()
        response = self.client.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_data_connections(self, filter_query: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get list of data connections."""
        url = f"{self.base_url}/dataconnection"
        params = self._add_xrf_params()
        if filter_query:
            params["filter"] = filter_query

        response = self.client.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_tasks(self, task_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get list of tasks (reload, external program, etc.)."""
        if task_type == "reload":
            url = f"{self.base_url}/reloadtask"
        elif task_type == "external":
            url = f"{self.base_url}/externalprogramtask"
        else:
            url = f"{self.base_url}/task"

        params = self._add_xrf_params()
        response = self.client.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def start_task(self, task_id: str) -> Dict[str, Any]:
        """Start a task execution."""
        url = f"{self.base_url}/task/{task_id}/start/synchronous"
        params = self._add_xrf_params()
        response = self.client.post(url, headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_extensions(self) -> List[Dict[str, Any]]:
        """Get list of extensions."""
        url = f"{self.base_url}/extension"
        params = self._add_xrf_params()
        response = self.client.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_content_libraries(self) -> List[Dict[str, Any]]:
        """Get list of content libraries."""
        url = f"{self.base_url}/contentlibrary"
        params = self._add_xrf_params()
        response = self.client.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_app_metadata(self, app_id: str) -> Dict[str, Any]:
        """Get comprehensive app metadata including data model, sheets, and detailed objects."""
        try:
            # Get Qlik ticket for authentication
            ticket = self._get_qlik_ticket()
            if not ticket:
                raise Exception("Failed to get authentication ticket")

            # Get data metadata
            url = f"{self.config.server_url}/api/v1/apps/{app_id}/data/metadata"
            headers = {"Content-Type": "application/json"}
            params = {"qlikTicket": ticket}

            response = self.client.get(url, headers=headers, params=params)
            response.raise_for_status()
            data_metadata = response.json()

            # Get comprehensive objects information
            try:
                # Get all objects from Repository API
                app_objects = self.get_app_objects(app_id)

                # Get detailed sheet information using Engine API
                detailed_sheets = self._get_detailed_sheets_info(app_id)

                # Analyze data model
                fields_info = self._analyze_fields(data_metadata.get('fields', []))
                tables_info = self._analyze_tables(data_metadata.get('tables', []))

                # Get app basic info
                app_info = self.get_app_by_id(app_id)

                return {
                    "app_overview": {
                        "name": app_info.get("name", "Unknown"),
                        "app_id": app_id,
                        "owner": app_info.get("owner", {}).get("name", "Unknown"),
                        "created": app_info.get("createdDate", "Unknown"),
                        "modified": app_info.get("modifiedDate", "Unknown"),
                        "published": app_info.get("published", False),
                        "stream": app_info.get("stream", {}).get("name") if app_info.get("stream") else None
                    },
                    "data_model": {
                        "summary": {
                            "total_fields": len([f for f in data_metadata.get('fields', []) if not f.get('is_system', False)]),
                            "total_tables": len([t for t in data_metadata.get('tables', []) if not t.get('is_system', False)]),
                            "total_rows": sum(t.get('no_of_rows', 0) for t in data_metadata.get('tables', []) if not t.get('is_system', False)),
                            "app_size_bytes": data_metadata.get('static_byte_size', 0)
                        },
                        "fields_by_type": fields_info["by_type"],
                        "tables": tables_info["user_tables"]
                    },
                    "sheets_and_objects": detailed_sheets,
                    "performance_info": {
                        "reload_meta": data_metadata.get('reload_meta', {}),
                        "last_reload_status": "Success" if data_metadata.get('reload_meta') else "Unknown"
                    },
                    "raw_data": {
                        "all_objects_count": len(app_objects),
                        "objects_by_type": self._group_objects_by_type(app_objects),
                        "data_metadata": data_metadata
                    }
                }

            except Exception as objects_error:
                # Fallback without detailed objects data
                fields_info = self._analyze_fields(data_metadata.get('fields', []))
                tables_info = self._analyze_tables(data_metadata.get('tables', []))
                app_info = self.get_app_by_id(app_id)

                return {
                    "app_overview": {
                        "name": app_info.get("name", "Unknown"),
                        "app_id": app_id,
                        "error": f"Could not get detailed objects info: {str(objects_error)}"
                    },
                    "data_model": {
                        "summary": {
                            "total_fields": len([f for f in data_metadata.get('fields', []) if not f.get('is_system', False)]),
                            "total_tables": len([t for t in data_metadata.get('tables', []) if not t.get('is_system', False)]),
                            "total_rows": sum(t.get('no_of_rows', 0) for t in data_metadata.get('tables', []) if not t.get('is_system', False))
                        },
                        "fields_by_type": fields_info["by_type"],
                        "tables": tables_info["user_tables"]
                    },
                    "sheets_and_objects": {"error": str(objects_error)},
                    "raw_data": {"data_metadata": data_metadata}
                }

        except Exception as e:
            return {
                "error": f"Failed to get app metadata: {str(e)}",
                "app_id": app_id,
                "attempted_url": f"{self.config.server_url}/api/v1/apps/{app_id}/data/metadata"
            }

    def _get_detailed_sheets_info(self, app_id: str) -> Dict[str, Any]:
        """Get detailed information about sheets and their objects using Engine API."""
        try:
            from .engine_api import QlikEngineAPI
            engine_api = QlikEngineAPI(self.config)

            # Get sheets with objects from Engine API
            sheets_result = engine_api.get_sheets_with_objects(app_id)

            if "error" in sheets_result:
                raise Exception(sheets_result["error"])

            detailed_sheets = []
            total_objects = 0
            objects_by_type = {}

            for sheet_data in sheets_result.get("sheets", []):
                sheet = sheet_data.get("sheet_info", {})
                sheet_objects = sheet_data.get("objects", [])

                sheet_info = {
                    "sheet_id": sheet.get("qInfo", {}).get("qId", ""),
                    "sheet_name": sheet.get("qMeta", {}).get("title", ""),
                    "description": sheet.get("qMeta", {}).get("description", ""),
                    "created": sheet.get("qMeta", {}).get("createdDate", ""),
                    "modified": sheet.get("qMeta", {}).get("modifiedDate", ""),
                    "published": sheet.get("qMeta", {}).get("published", False),
                    "objects": []
                }

                # Process objects on this sheet
                if isinstance(sheet_objects, list):
                    for obj in sheet_objects:
                        if isinstance(obj, dict) and "qInfo" in obj:
                            obj_info = {
                                "object_id": obj.get("qInfo", {}).get("qId", ""),
                                "object_type": obj.get("qInfo", {}).get("qType", ""),
                                "title": obj.get("qMeta", {}).get("title", ""),
                                "description": obj.get("qMeta", {}).get("description", ""),
                                "size": obj.get("qMeta", {}).get("qSize", -1),
                                "created": obj.get("qMeta", {}).get("createdDate", ""),
                                "modified": obj.get("qMeta", {}).get("modifiedDate", "")
                            }

                            # Count objects by type
                            obj_type = obj_info["object_type"]
                            if obj_type:
                                objects_by_type[obj_type] = objects_by_type.get(obj_type, 0) + 1
                                total_objects += 1

                            sheet_info["objects"].append(obj_info)

                # Add error info if present
                if sheet_data.get("objects_error"):
                    sheet_info["objects_error"] = sheet_data["objects_error"]

                sheet_info["objects_count"] = len(sheet_info["objects"])
                detailed_sheets.append(sheet_info)

            return {
                "summary": {
                    "total_sheets": len(detailed_sheets),
                    "total_objects": total_objects,
                    "objects_by_type": objects_by_type
                },
                "sheets": detailed_sheets
            }

        except Exception as e:
            # Fallback to Repository API only
            try:
                app_objects = self.get_app_objects(app_id)
                sheets = [obj for obj in app_objects if obj.get('objectType') == 'sheet']

                return {
                    "summary": {
                        "total_sheets": len(sheets),
                        "total_objects": len(app_objects),
                        "objects_by_type": self._group_objects_by_type(app_objects),
                        "note": "Using Repository API fallback - limited detail"
                    },
                    "sheets": [
                        {
                            "sheet_id": s.get("id"),
                            "sheet_name": s.get("name", ""),
                            "objects": [],
                            "objects_count": 0,
                            "note": "Objects detail not available via Repository API"
                        }
                        for s in sheets
                    ],
                    "engine_api_error": str(e)
                }
            except Exception as fallback_error:
                return {
                    "error": f"Both Engine API and Repository API failed: {str(e)} | {str(fallback_error)}"
                }

    def _analyze_fields(self, fields: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Analyze fields and group by type."""
        field_types = {}
        user_fields = [f for f in fields if not f.get('is_system', False)]

        for field in user_fields:
            if field.get('is_numeric'):
                field_type = 'numeric'
                if '$timestamp' in field.get('tags', []):
                    field_type = 'timestamp'
                elif '$date' in field.get('tags', []):
                    field_type = 'date'
                elif '$integer' in field.get('tags', []):
                    field_type = 'integer'
            else:
                field_type = 'text'

            if field_type not in field_types:
                field_types[field_type] = []

            field_types[field_type].append({
                "name": field.get('name'),
                "cardinal": field.get('cardinal', 0),
                "total_count": field.get('total_count', 0),
                "src_tables": field.get('src_tables', [])
            })

        return {
            "by_type": {k: len(v) for k, v in field_types.items()},
            "details": field_types,
            "total_user_fields": len(user_fields)
        }

    def _analyze_tables(self, tables: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Analyze tables information."""
        user_tables = [t for t in tables if not t.get('is_system', False)]

        return {
            "user_tables": [
                {
                    "name": t.get('name'),
                    "rows": t.get('no_of_rows', 0),
                    "fields": t.get('no_of_fields', 0),
                    "size_bytes": t.get('byte_size', 0)
                }
                for t in user_tables
            ],
            "total_user_tables": len(user_tables)
        }

    def _group_objects_by_type(self, objects: List[Dict[str, Any]]) -> Dict[str, int]:
        """Group objects by type and count them."""
        type_counts = {}
        for obj in objects:
            obj_type = obj.get("objectType", "unknown")
            type_counts[obj_type] = type_counts.get(obj_type, 0) + 1
        return type_counts

    def get_app_objects(self, app_id: str, object_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get objects from specific app."""
        url = f"{self.base_url}/app/object"
        params = self._add_xrf_params({"filter": f"app.id eq {app_id}"})
        if object_type:
            params["filter"] += f" and objectType eq '{object_type}'"

        response = self.client.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_app_data_lineage(self, app_id: str) -> Dict[str, Any]:
        """Get data lineage for app."""
        url = f"{self.base_url}/app/{app_id}/datalineage"
        params = self._add_xrf_params()
        response = self.client.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_reload_tasks_for_app(self, app_id: str) -> List[Dict[str, Any]]:
        """Get reload tasks for specific app."""
        url = f"{self.base_url}/reloadtask"
        params = self._add_xrf_params({"filter": f"app.id eq {app_id}"})
        response = self.client.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def get_task_executions(self, task_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Get execution history for a task."""
        url = f"{self.base_url}/executionresult"
        params = self._add_xrf_params({
            "filter": f"reloadTask.id eq {task_id}",
            "orderby": "startTime desc"
        })
        response = self.client.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        results = response.json()
        return results[:limit] if isinstance(results, list) else [results]

    def get_app_privileges(self, app_id: str) -> List[Dict[str, Any]]:
        """Get privileges for app."""
        url = f"{self.base_url}/app/{app_id}/privilege"
        params = self._add_xrf_params()
        response = self.client.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def close(self):
        """Close the HTTP client."""
        self.client.close()
