"""Main MCP Server for Qlik Sense APIs."""

import asyncio
import json
import sys
from typing import Any, Dict, List, Optional
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import ServerCapabilities
from mcp.types import CallToolResult, TextContent

from .config import QlikSenseConfig
from .repository_api import QlikRepositoryAPI
from .engine_api import QlikEngineAPI


class QlikSenseMCPServer:
    """MCP Server for Qlik Sense Enterprise APIs."""

    def __init__(self):
        try:
            self.config = QlikSenseConfig.from_env()
            self.config_valid = self._validate_config()
        except Exception as e:
            self.config = None
            self.config_valid = False

        # Initialize API clients safely
        self.repository_api = None
        self.engine_api = None

        if self.config_valid:
            try:
                self.repository_api = QlikRepositoryAPI(self.config)
                self.engine_api = QlikEngineAPI(self.config)
            except Exception as e:
                # API clients will be None, tools will return errors
                pass

        self.server = Server("qlik-sense-mcp-server")
        self._setup_handlers()

    def _validate_config(self) -> bool:
        """Validate that required configuration is present."""
        if not self.config:
            return False
        return bool(
            self.config.server_url and
            self.config.user_directory and
            self.config.user_id
        )

    def _setup_handlers(self):
        """Setup MCP server handlers."""

        @self.server.list_tools()
        async def handle_list_tools():
            """
            List all available MCP tools for Qlik Sense operations.

            Returns tool definitions with schemas for Repository API and Engine API operations
            including applications, users, data connections, analytics tools, and data export.
            """
            tools_list = [
                {"name": "get_apps", "description": "Get list of Qlik Sense applications", "inputSchema": {"type": "object", "properties": {"filter": {"type": "string", "description": "Optional filter query"}}}},
                {"name": "get_app_details", "description": "Get detailed information about a specific application", "inputSchema": {"type": "object", "properties": {"app_id": {"type": "string", "description": "Application ID"}}, "required": ["app_id"]}},
                {"name": "get_users", "description": "Get list of users in Qlik Sense", "inputSchema": {"type": "object", "properties": {"filter": {"type": "string", "description": "Optional filter query"}}}},
                {"name": "get_streams", "description": "Get list of streams in Qlik Sense", "inputSchema": {"type": "object", "properties": {}}},
                {"name": "get_data_connections", "description": "Get list of data connections", "inputSchema": {"type": "object", "properties": {"filter": {"type": "string", "description": "Optional filter query"}}}},
                {"name": "get_tasks", "description": "Get list of tasks", "inputSchema": {"type": "object", "properties": {"task_type": {"type": "string", "enum": ["reload", "external", "all"], "description": "Type of tasks"}}}},
                {"name": "start_task", "description": "Start execution of a task", "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string", "description": "Task ID"}}, "required": ["task_id"]}},
                {"name": "get_extensions", "description": "Get list of extensions", "inputSchema": {"type": "object", "properties": {}}},
                {"name": "get_content_libraries", "description": "Get list of content libraries", "inputSchema": {"type": "object", "properties": {}}},
                {"name": "get_app_metadata", "description": "Get app metadata via REST API - faster than Engine API for metadata queries", "inputSchema": {"type": "object", "properties": {"app_id": {"type": "string", "description": "Application ID"}}, "required": ["app_id"]}},
                {"name": "engine_get_doc_list", "description": "Get list of documents via Engine API", "inputSchema": {"type": "object", "properties": {}}},
                {"name": "engine_open_app", "description": "Open an app via Engine API", "inputSchema": {"type": "object", "properties": {"app_id": {"type": "string", "description": "Application ID"}}, "required": ["app_id"]}},

                {"name": "engine_get_script", "description": "Get load script from app", "inputSchema": {"type": "object", "properties": {"app_id": {"type": "string", "description": "Application ID"}}, "required": ["app_id"]}},
                {"name": "engine_get_fields", "description": "Get fields from app", "inputSchema": {"type": "object", "properties": {"app_id": {"type": "string", "description": "Application ID"}}, "required": ["app_id"]}},
                {"name": "engine_get_sheets", "description": "Get sheets from app", "inputSchema": {"type": "object", "properties": {"app_id": {"type": "string", "description": "Application ID"}}, "required": ["app_id"]}},
                {"name": "engine_get_table_data", "description": "Get data from specific table or all tables", "inputSchema": {"type": "object", "properties": {"app_id": {"type": "string", "description": "Application ID"}, "table_name": {"type": "string", "description": "Table name (optional - if not provided, returns info about all tables)"}, "max_rows": {"type": "integer", "description": "Maximum rows to return", "default": 1000}}, "required": ["app_id"]}},
                {"name": "engine_get_field_values", "description": "Get field values with frequency information", "inputSchema": {"type": "object", "properties": {"app_id": {"type": "string", "description": "Application ID"}, "field_name": {"type": "string", "description": "Field name"}, "max_values": {"type": "integer", "description": "Maximum values to return", "default": 100}, "include_frequency": {"type": "boolean", "description": "Include frequency information", "default": True}}, "required": ["app_id", "field_name"]}},
                {"name": "engine_get_field_statistics", "description": "Get comprehensive statistics for a field", "inputSchema": {"type": "object", "properties": {"app_id": {"type": "string", "description": "Application ID"}, "field_name": {"type": "string", "description": "Field name"}}, "required": ["app_id", "field_name"]}},
                {"name": "engine_get_data_model", "description": "Get complete data model with tables and associations", "inputSchema": {"type": "object", "properties": {"app_id": {"type": "string", "description": "Application ID"}}, "required": ["app_id"]}},
                {"name": "engine_create_hypercube", "description": "Create hypercube for data analysis", "inputSchema": {"type": "object", "properties": {"app_id": {"type": "string", "description": "Application ID"}, "dimensions": {"type": "array", "items": {"type": "string"}, "description": "List of dimension fields"}, "measures": {"type": "array", "items": {"type": "string"}, "description": "List of measure expressions"}, "max_rows": {"type": "integer", "description": "Maximum rows to return", "default": 1000}}, "required": ["app_id", "dimensions", "measures"]}},
                {"name": "engine_create_data_export", "description": "Export data in various formats (JSON, CSV, simple)", "inputSchema": {"type": "object", "properties": {"app_id": {"type": "string", "description": "Application ID"}, "table_name": {"type": "string", "description": "Table name (optional)"}, "fields": {"type": "array", "items": {"type": "string"}, "description": "List of fields to export (optional)"}, "format_type": {"type": "string", "enum": ["json", "csv", "simple"], "description": "Export format", "default": "json"}, "max_rows": {"type": "integer", "description": "Maximum rows to export", "default": 10000}, "filters": {"type": "object", "description": "Field filters (optional)"}}, "required": ["app_id"]}}
                ]
            return tools_list

        @self.server.call_tool()
        async def handle_call_tool(name: str, arguments: Dict[str, Any]):
            # Check configuration before processing any tool calls
            if not self.config_valid:
                error_msg = {
                    "error": "Qlik Sense configuration missing",
                    "message": "Please set the following environment variables:",
                    "required": [
                        "QLIK_SERVER_URL - Qlik Sense server URL",
                        "QLIK_USER_DIRECTORY - User directory",
                        "QLIK_USER_ID - User ID",
                        "QLIK_CLIENT_CERT_PATH - Path to client certificate",
                        "QLIK_CLIENT_KEY_PATH - Path to client key",
                        "QLIK_CA_CERT_PATH - Path to CA certificate"
                    ],
                    "example": "uvx --with-env QLIK_SERVER_URL=https://qlik.company.com qlik-sense-mcp-server"
                }
                return [TextContent(type="text", text=json.dumps(error_msg, indent=2))]
            """
            Handle MCP tool calls by routing to appropriate API handlers.

            Args:
                name: Tool name to execute
                arguments: Tool-specific parameters

            Returns:
                TextContent with JSON response from Qlik Sense APIs
            """
            try:
                if name == "get_apps":
                    filter_query = arguments.get("filter")
                    apps = await asyncio.to_thread(self.repository_api.get_apps, filter_query)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(apps, indent=2, ensure_ascii=False)
                        )
                    ]

                elif name == "get_app_details":
                    app_id = arguments["app_id"]
                    app = await asyncio.to_thread(self.repository_api.get_app_by_id, app_id)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(app, indent=2, ensure_ascii=False)
                        )
                    ]

                elif name == "get_users":
                    filter_query = arguments.get("filter")
                    users = await asyncio.to_thread(self.repository_api.get_users, filter_query)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(users, indent=2, ensure_ascii=False)
                        )
                    ]

                elif name == "get_streams":
                    streams = await asyncio.to_thread(self.repository_api.get_streams)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(streams, indent=2, ensure_ascii=False)
                        )
                    ]

                elif name == "get_data_connections":
                    filter_query = arguments.get("filter")
                    connections = await asyncio.to_thread(self.repository_api.get_data_connections, filter_query)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(connections, indent=2, ensure_ascii=False)
                        )
                    ]

                elif name == "get_tasks":
                    task_type = arguments.get("task_type", "all")
                    if task_type == "all":
                        task_type = None
                    tasks = await asyncio.to_thread(self.repository_api.get_tasks, task_type)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(tasks, indent=2, ensure_ascii=False)
                        )
                    ]

                elif name == "start_task":
                    task_id = arguments["task_id"]
                    result = await asyncio.to_thread(self.repository_api.start_task, task_id)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(result, indent=2, ensure_ascii=False)
                        )
                    ]

                elif name == "get_extensions":
                    extensions = await asyncio.to_thread(self.repository_api.get_extensions)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(extensions, indent=2, ensure_ascii=False)
                        )
                    ]

                elif name == "get_content_libraries":
                    libraries = await asyncio.to_thread(self.repository_api.get_content_libraries)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(libraries, indent=2, ensure_ascii=False)
                        )
                    ]

                elif name == "get_app_metadata":
                    app_id = arguments["app_id"]
                    metadata = await asyncio.to_thread(self.repository_api.get_app_metadata, app_id)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(metadata, indent=2, ensure_ascii=False)
                        )
                    ]

                # Engine API handlers
                elif name == "engine_get_doc_list":
                    def _get_doc_list():
                        try:
                            self.engine_api.connect()
                            return self.engine_api.get_doc_list()
                        except Exception as e:
                            return {"error": str(e), "docs": []}
                        finally:
                            self.engine_api.disconnect()

                    docs = await asyncio.to_thread(_get_doc_list)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(docs, indent=2, ensure_ascii=False)
                        )
                    ]

                elif name == "engine_open_app":
                    app_id = arguments["app_id"]

                    def _open_app():
                        try:
                            self.engine_api.connect()
                            return self.engine_api.open_doc(app_id)
                        except Exception as e:
                            return {"error": str(e)}
                        finally:
                            self.engine_api.disconnect()

                    result = await asyncio.to_thread(_open_app)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(result, indent=2, ensure_ascii=False)
                        )
                    ]

                elif name == "engine_get_script":
                    app_id = arguments["app_id"]

                    def _get_script():
                        app_handle = -1
                        try:
                            # Шаг 1: Создать engine (подключиться)
                            self.engine_api.connect()

                            # Шаг 2: Через engine сделать openapp (безопасно)
                            app_result = self.engine_api.open_doc_safe(app_id, no_data=True)

                            # Проверяем результат открытия
                            if "qReturn" not in app_result:
                                raise Exception(f"Failed to open app: invalid response {app_result}")

                            app_handle = app_result["qReturn"].get("qHandle", -1)
                            if app_handle == -1:
                                raise Exception(f"Failed to get app handle: {app_result}")

                            # Шаг 3: Только теперь внутри получаем script
                            script = self.engine_api.get_script(app_handle)

                            return {
                                "qScript": script,
                                "app_id": app_id,
                                "app_handle": app_handle,
                                "script_length": len(script) if script else 0
                            }

                        except Exception as e:
                            error_msg = str(e)
                            # Более детальная обработка ошибок
                            if "already open" in error_msg.lower():
                                error_msg = f"App {app_id} is already open in another session. Try again later or use a different session."
                            elif "failed to open app" in error_msg.lower():
                                error_msg = f"Could not open app {app_id}. Check if app exists and you have access."

                            return {
                                "error": error_msg,
                                "app_id": app_id,
                                "app_handle": app_handle
                            }
                        finally:
                            # Закрываем документ если он был открыт
                            if app_handle != -1:
                                try:
                                    self.engine_api.close_doc(app_handle)
                                except:
                                    pass  # Игнорируем ошибки закрытия

                            # Отключаемся от engine
                            self.engine_api.disconnect()

                    script = await asyncio.to_thread(_get_script)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(script, indent=2, ensure_ascii=False)
                        )
                    ]

                elif name == "engine_get_fields":
                    app_id = arguments["app_id"]

                    def _get_fields():
                        try:
                            self.engine_api.connect()
                            app_result = self.engine_api.open_doc(app_id, no_data=False)
                            app_handle = app_result.get("qReturn", {}).get("qHandle", -1)
                            if app_handle != -1:
                                return self.engine_api.get_fields(app_handle)
                            else:
                                raise Exception("Failed to open app")
                        except Exception as e:
                            return {"error": str(e)}
                        finally:
                            self.engine_api.disconnect()

                    fields = await asyncio.to_thread(_get_fields)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(fields, indent=2, ensure_ascii=False)
                        )
                    ]

                elif name == "engine_get_sheets":
                    app_id = arguments["app_id"]

                    def _get_sheets():
                        try:
                            self.engine_api.connect()
                            app_result = self.engine_api.open_doc(app_id)
                            app_handle = app_result.get("qReturn", {}).get("qHandle", -1)
                            if app_handle != -1:
                                return self.engine_api.get_sheets(app_handle)
                            else:
                                raise Exception("Failed to open app")
                        except Exception as e:
                            return {"error": str(e)}
                        finally:
                            self.engine_api.disconnect()

                    sheets = await asyncio.to_thread(_get_sheets)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(sheets, indent=2, ensure_ascii=False)
                        )
                    ]

                # New Analytics Tools - Этап 1
                elif name == "engine_get_table_data":
                    app_id = arguments["app_id"]
                    table_name = arguments.get("table_name")
                    max_rows = arguments.get("max_rows", 1000)

                    def _get_table_data():
                        try:
                            self.engine_api.connect()
                            app_result = self.engine_api.open_doc(app_id, no_data=False)
                            app_handle = app_result.get("qReturn", {}).get("qHandle", -1)
                            if app_handle != -1:
                                return self.engine_api.get_table_data(app_handle, table_name, max_rows)
                            else:
                                raise Exception("Failed to open app")
                        except Exception as e:
                            return {"error": str(e)}
                        finally:
                            self.engine_api.disconnect()

                    result = await asyncio.to_thread(_get_table_data)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(result, indent=2, ensure_ascii=False)
                        )
                    ]

                elif name == "engine_get_field_values":
                    app_id = arguments["app_id"]
                    field_name = arguments["field_name"]
                    max_values = arguments.get("max_values", 100)
                    include_frequency = arguments.get("include_frequency", True)

                    def _get_field_values():
                        try:
                            self.engine_api.connect()
                            app_result = self.engine_api.open_doc(app_id, no_data=False)
                            app_handle = app_result.get("qReturn", {}).get("qHandle", -1)
                            if app_handle != -1:
                                return self.engine_api.get_field_values(app_handle, field_name, max_values, include_frequency)
                            else:
                                raise Exception("Failed to open app")
                        except Exception as e:
                            return {"error": str(e)}
                        finally:
                            self.engine_api.disconnect()

                    result = await asyncio.to_thread(_get_field_values)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(result, indent=2, ensure_ascii=False)
                        )
                    ]

                elif name == "engine_get_field_statistics":
                    app_id = arguments["app_id"]
                    field_name = arguments["field_name"]

                    def _get_field_statistics():
                        app_handle = -1
                        debug_info = []
                        try:
                            debug_info.append(f"Starting field statistics for app_id={app_id}, field_name={field_name}")

                            self.engine_api.connect()
                            debug_info.append("Connected to engine")

                            app_result = self.engine_api.open_doc_safe(app_id, no_data=False)
                            debug_info.append(f"App open result: {app_result}")

                            app_handle = app_result.get("qReturn", {}).get("qHandle", -1)
                            debug_info.append(f"App handle: {app_handle}")

                            if app_handle != -1:
                                result = self.engine_api.get_field_statistics(app_handle, field_name)
                                debug_info.append("Field statistics method completed")

                                # Add debug info to result if it doesn't already have it
                                if isinstance(result, dict) and "debug_log" not in result:
                                    result["server_debug"] = debug_info

                                return result
                            else:
                                raise Exception(f"Failed to open app: {app_result}")

                        except Exception as e:
                            import traceback
                            debug_info.append(f"Exception in server handler: {e}")
                            debug_info.append(f"Traceback: {traceback.format_exc()}")
                            return {
                                "error": str(e),
                                "server_debug": debug_info,
                                "traceback": traceback.format_exc()
                            }
                        finally:
                            debug_info.append("Disconnecting from engine")
                            self.engine_api.disconnect()

                    result = await asyncio.to_thread(_get_field_statistics)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(result, indent=2, ensure_ascii=False)
                        )
                    ]

                elif name == "engine_get_data_model":
                    app_id = arguments["app_id"]

                    def _get_data_model():
                        try:
                            self.engine_api.connect()
                            app_result = self.engine_api.open_doc(app_id, no_data=False)
                            app_handle = app_result.get("qReturn", {}).get("qHandle", -1)
                            if app_handle != -1:
                                return self.engine_api.get_data_model(app_handle)
                            else:
                                raise Exception("Failed to open app")
                        except Exception as e:
                            return {"error": str(e)}
                        finally:
                            self.engine_api.disconnect()

                    result = await asyncio.to_thread(_get_data_model)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(result, indent=2, ensure_ascii=False)
                        )
                    ]



                elif name == "engine_create_hypercube":
                    app_id = arguments["app_id"]
                    dimensions = arguments["dimensions"]
                    measures = arguments["measures"]
                    max_rows = arguments.get("max_rows", 1000)

                    def _create_hypercube():
                        try:
                            self.engine_api.connect()
                            app_result = self.engine_api.open_doc(app_id, no_data=False)
                            app_handle = app_result.get("qReturn", {}).get("qHandle", -1)
                            if app_handle != -1:
                                return self.engine_api.create_hypercube(app_handle, dimensions, measures, max_rows)
                            else:
                                raise Exception("Failed to open app")
                        except Exception as e:
                            return {"error": str(e)}
                        finally:
                            self.engine_api.disconnect()

                    result = await asyncio.to_thread(_create_hypercube)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(result, indent=2, ensure_ascii=False)
                        )
                    ]

                elif name == "engine_create_data_export":
                    app_id = arguments["app_id"]
                    table_name = arguments.get("table_name")
                    fields = arguments.get("fields")
                    format_type = arguments.get("format_type", "json")
                    max_rows = arguments.get("max_rows", 10000)
                    filters = arguments.get("filters")

                    def _create_data_export():
                        try:
                            self.engine_api.connect()
                            app_result = self.engine_api.open_doc(app_id, no_data=False)
                            app_handle = app_result.get("qReturn", {}).get("qHandle", -1)
                            if app_handle != -1:
                                return self.engine_api.create_data_export(
                                    app_handle, table_name, fields, format_type, max_rows, filters
                                )
                            else:
                                raise Exception("Failed to open app")
                        except Exception as e:
                            return {"error": str(e)}
                        finally:
                            self.engine_api.disconnect()

                    result = await asyncio.to_thread(_create_data_export)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(result, indent=2, ensure_ascii=False)
                        )
                    ]


                # Advanced Repository API handlers
                elif name == "get_app_reload_chain":
                    app_id = arguments["app_id"]

                    def _get_reload_chain():
                        # Get reload tasks for app
                        tasks = self.repository_api.get_reload_tasks_for_app(app_id)

                        # Get execution history for each task
                        chain_info = {
                            "app_id": app_id,
                            "reload_tasks": [],
                            "execution_history": []
                        }

                        for task in tasks:
                            task_id = task.get("id")
                            if task_id:
                                executions = self.repository_api.get_task_executions(task_id, 10)
                                chain_info["reload_tasks"].append(task)
                                chain_info["execution_history"].extend(executions)

                        return chain_info

                    chain = await asyncio.to_thread(_get_reload_chain)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(chain, indent=2, ensure_ascii=False)
                        )
                    ]

                elif name == "get_app_objects_detailed":
                    app_id = arguments["app_id"]
                    object_type = arguments.get("object_type")
                    objects = await asyncio.to_thread(self.repository_api.get_app_objects, app_id, object_type)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(objects, indent=2, ensure_ascii=False)
                        )
                    ]

                # Advanced Engine API handlers
                elif name == "engine_get_data_model":
                    app_id = arguments["app_id"]

                    def _get_data_model():
                        self.engine_api.connect(app_id)
                        try:
                            app_result = self.engine_api.open_doc(app_id)
                            app_handle = app_result.get("qReturn", {}).get("qHandle", -1)
                            if app_handle != -1:
                                return self.engine_api.get_data_model(app_handle)
                            else:
                                raise Exception("Failed to open app")
                        finally:
                            self.engine_api.disconnect()

                    model = await asyncio.to_thread(_get_data_model)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(model, indent=2, ensure_ascii=False)
                        )
                    ]

                elif name == "engine_get_field_info":
                    app_id = arguments["app_id"]
                    field_name = arguments["field_name"]

                    def _get_field_info():
                        self.engine_api.connect(app_id)
                        try:
                            app_result = self.engine_api.open_doc(app_id)
                            app_handle = app_result.get("qReturn", {}).get("qHandle", -1)
                            if app_handle != -1:
                                field_desc = self.engine_api.get_field_description(app_handle, field_name)
                                field_values = self.engine_api.get_field_values(app_handle, field_name, 50)
                                return {
                                    "field_description": field_desc,
                                    "sample_values": field_values
                                }
                            else:
                                raise Exception("Failed to open app")
                        finally:
                            self.engine_api.disconnect()

                    field_info = await asyncio.to_thread(_get_field_info)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(field_info, indent=2, ensure_ascii=False)
                        )
                    ]

                elif name == "engine_extract_data":
                    app_id = arguments["app_id"]
                    dimensions = arguments["dimensions"]
                    measures = arguments["measures"]
                    max_rows = arguments.get("max_rows", 1000)

                    def _extract_data():
                        self.engine_api.connect(app_id)
                        try:
                            app_result = self.engine_api.open_doc(app_id)
                            app_handle = app_result.get("qReturn", {}).get("qHandle", -1)
                            if app_handle != -1:
                                # Create hypercube
                                cube_result = self.engine_api.create_hypercube(app_handle, dimensions, measures, max_rows)
                                cube_handle = cube_result.get("qReturn", {}).get("qHandle", -1)

                                if cube_handle != -1:
                                    # Get data from hypercube
                                    data = self.engine_api.get_hypercube_data(cube_handle, 0, max_rows)
                                    return {
                                        "dimensions": dimensions,
                                        "measures": measures,
                                        "data": data
                                    }
                                else:
                                    raise Exception("Failed to create hypercube")
                            else:
                                raise Exception("Failed to open app")
                        finally:
                            self.engine_api.disconnect()

                    data = await asyncio.to_thread(_extract_data)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(data, indent=2, ensure_ascii=False)
                        )
                    ]

                elif name == "engine_get_visualization_data":
                    app_id = arguments["app_id"]
                    object_id = arguments["object_id"]

                    def _get_viz_data():
                        self.engine_api.connect(app_id)
                        try:
                            app_result = self.engine_api.open_doc(app_id)
                            app_handle = app_result.get("qReturn", {}).get("qHandle", -1)
                            if app_handle != -1:
                                return self.engine_api.get_object_data(app_handle, object_id)
                            else:
                                raise Exception("Failed to open app")
                        finally:
                            self.engine_api.disconnect()

                    viz_data = await asyncio.to_thread(_get_viz_data)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(viz_data, indent=2, ensure_ascii=False)
                        )
                    ]

                elif name == "engine_search_and_analyze":
                    app_id = arguments["app_id"]
                    search_terms = arguments["search_terms"]

                    def _search_analyze():
                        self.engine_api.connect(app_id)
                        try:
                            app_result = self.engine_api.open_doc(app_id)
                            app_handle = app_result.get("qReturn", {}).get("qHandle", -1)
                            if app_handle != -1:
                                search_results = self.engine_api.search_objects(app_handle, search_terms)
                                fields = self.engine_api.get_fields(app_handle)

                                # Filter fields that match search terms
                                matching_fields = []
                                for field in fields:
                                    field_name = field.get("qName", "").lower()
                                    for term in search_terms:
                                        if term.lower() in field_name:
                                            matching_fields.append(field)
                                            break

                                return {
                                    "search_terms": search_terms,
                                    "object_matches": search_results,
                                    "field_matches": matching_fields
                                }
                            else:
                                raise Exception("Failed to open app")
                        finally:
                            self.engine_api.disconnect()

                    results = await asyncio.to_thread(_search_analyze)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(results, indent=2, ensure_ascii=False)
                        )
                    ]

                # Advanced QIX Engine API handlers
                elif name == "engine_get_master_items":
                    app_id = arguments["app_id"]

                    def _get_master_items():
                        self.engine_api.connect(app_id)
                        try:
                            app_result = self.engine_api.open_doc(app_id)
                            app_handle = app_result.get("qReturn", {}).get("qHandle", -1)
                            if app_handle != -1:
                                dimensions = self.engine_api.get_dimensions(app_handle)
                                measures = self.engine_api.get_measures(app_handle)
                                variables = self.engine_api.get_variables(app_handle)

                                return {
                                    "master_dimensions": dimensions,
                                    "master_measures": measures,
                                    "variables": variables
                                }
                            else:
                                raise Exception("Failed to open app")
                        finally:
                            self.engine_api.disconnect()

                    master_items = await asyncio.to_thread(_get_master_items)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(master_items, indent=2, ensure_ascii=False)
                        )
                    ]

                elif name == "engine_calculate_expression":
                    app_id = arguments["app_id"]
                    expression = arguments["expression"]
                    dimensions = arguments.get("dimensions", [])

                    def _calculate_expression():
                        self.engine_api.connect(app_id)
                        try:
                            app_result = self.engine_api.open_doc(app_id)
                            app_handle = app_result.get("qReturn", {}).get("qHandle", -1)
                            if app_handle != -1:
                                return self.engine_api.calculate_expression(app_handle, expression, dimensions)
                            else:
                                raise Exception("Failed to open app")
                        finally:
                            self.engine_api.disconnect()

                    result = await asyncio.to_thread(_calculate_expression)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(result, indent=2, ensure_ascii=False)
                        )
                    ]

                elif name == "engine_get_associations":
                    app_id = arguments["app_id"]

                    def _get_associations():
                        self.engine_api.connect(app_id)
                        try:
                            app_result = self.engine_api.open_doc(app_id)
                            app_handle = app_result.get("qReturn", {}).get("qHandle", -1)
                            if app_handle != -1:
                                associations = self.engine_api.get_associations(app_handle)
                                data_model = self.engine_api.get_data_model(app_handle)

                                return {
                                    "associations": associations,
                                    "data_model": data_model
                                }
                            else:
                                raise Exception("Failed to open app")
                        finally:
                            self.engine_api.disconnect()

                    associations = await asyncio.to_thread(_get_associations)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(associations, indent=2, ensure_ascii=False)
                        )
                    ]

                elif name == "engine_smart_search":
                    app_id = arguments["app_id"]
                    search_terms = arguments["search_terms"]

                    def _smart_search():
                        self.engine_api.connect(app_id)
                        try:
                            app_result = self.engine_api.open_doc(app_id)
                            app_handle = app_result.get("qReturn", {}).get("qHandle", -1)
                            if app_handle != -1:
                                suggestions = self.engine_api.search_suggest(app_handle, search_terms)
                                return {
                                    "search_terms": search_terms,
                                    "suggestions": suggestions
                                }
                            else:
                                raise Exception("Failed to open app")
                        finally:
                            self.engine_api.disconnect()

                    suggestions = await asyncio.to_thread(_smart_search)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(suggestions, indent=2, ensure_ascii=False)
                        )
                    ]

                elif name == "engine_create_pivot_analysis":
                    app_id = arguments["app_id"]
                    dimensions = arguments["dimensions"]
                    measures = arguments["measures"]
                    max_rows = arguments.get("max_rows", 1000)

                    def _create_pivot():
                        self.engine_api.connect(app_id)
                        try:
                            app_result = self.engine_api.open_doc(app_id)
                            app_handle = app_result.get("qReturn", {}).get("qHandle", -1)
                            if app_handle != -1:
                                pivot_result = self.engine_api.get_pivot_table_data(app_handle, dimensions, measures, max_rows)
                                pivot_handle = pivot_result.get("qReturn", {}).get("qHandle", -1)

                                if pivot_handle != -1:
                                    layout = self.engine_api.send_request("GetLayout", handle=pivot_handle)
                                    return {
                                        "dimensions": dimensions,
                                        "measures": measures,
                                        "pivot_data": layout
                                    }
                                else:
                                    raise Exception("Failed to create pivot table")
                            else:
                                raise Exception("Failed to open app")
                        finally:
                            self.engine_api.disconnect()

                    pivot_data = await asyncio.to_thread(_create_pivot)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(pivot_data, indent=2, ensure_ascii=False)
                        )
                    ]

                # New table and visualization methods
                elif name == "engine_get_table_data":
                    app_id = arguments["app_id"]
                    table_name = arguments.get("table_name")
                    max_rows = arguments.get("max_rows", 1000)

                    def _get_table_data():
                        try:
                            self.engine_api.connect()
                            app_result = self.engine_api.open_doc(app_id, no_data=False)
                            app_handle = app_result.get("qReturn", {}).get("qHandle", -1)
                            if app_handle != -1:
                                return self.engine_api.get_table_data(app_handle, table_name, max_rows)
                            else:
                                raise Exception("Failed to open app")
                        except Exception as e:
                            return {"error": str(e)}
                        finally:
                            self.engine_api.disconnect()

                    result = await asyncio.to_thread(_get_table_data)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(result, indent=2, ensure_ascii=False)
                        )
                    ]

                elif name == "engine_get_visualization_data":
                    app_id = arguments["app_id"]
                    object_id = arguments["object_id"]

                    def _get_visualization_data():
                        try:
                            self.engine_api.connect()
                            app_result = self.engine_api.open_doc(app_id, no_data=False)
                            app_handle = app_result.get("qReturn", {}).get("qHandle", -1)
                            if app_handle != -1:
                                return self.engine_api.get_visualization_data(app_handle, object_id)
                            else:
                                raise Exception("Failed to open app")
                        except Exception as e:
                            return {"error": str(e)}
                        finally:
                            self.engine_api.disconnect()

                    result = await asyncio.to_thread(_get_visualization_data)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(result, indent=2, ensure_ascii=False)
                        )
                    ]

                elif name == "engine_create_simple_table":
                    app_id = arguments["app_id"]
                    dimensions = arguments["dimensions"]
                    measures = arguments.get("measures", [])
                    max_rows = arguments.get("max_rows", 1000)

                    def _create_simple_table():
                        try:
                            self.engine_api.connect()
                            app_result = self.engine_api.open_doc(app_id, no_data=False)
                            app_handle = app_result.get("qReturn", {}).get("qHandle", -1)
                            if app_handle != -1:
                                return self.engine_api.create_simple_table(app_handle, dimensions, measures, max_rows)
                            else:
                                raise Exception("Failed to open app")
                        except Exception as e:
                            return {"error": str(e)}
                        finally:
                            self.engine_api.disconnect()

                    result = await asyncio.to_thread(_create_simple_table)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(result, indent=2, ensure_ascii=False)
                        )
                    ]

                elif name == "engine_get_chart_data":
                    app_id = arguments["app_id"]
                    chart_type = arguments["chart_type"]
                    dimensions = arguments["dimensions"]
                    measures = arguments["measures"]
                    max_rows = arguments.get("max_rows", 1000)

                    def _get_chart_data():
                        try:
                            self.engine_api.connect()
                            app_result = self.engine_api.open_doc(app_id, no_data=False)
                            app_handle = app_result.get("qReturn", {}).get("qHandle", -1)
                            if app_handle != -1:
                                return self.engine_api.get_chart_data(app_handle, chart_type, dimensions, measures, max_rows)
                            else:
                                raise Exception("Failed to open app")
                        except Exception as e:
                            return {"error": str(e)}
                        finally:
                            self.engine_api.disconnect()

                    result = await asyncio.to_thread(_get_chart_data)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(result, indent=2, ensure_ascii=False)
                        )
                    ]

                elif name == "engine_create_hypercube":
                    app_id = arguments["app_id"]
                    dimensions = arguments["dimensions"]
                    measures = arguments["measures"]
                    max_rows = arguments.get("max_rows", 1000)

                    def _create_hypercube():
                        try:
                            self.engine_api.connect()
                            app_result = self.engine_api.open_doc(app_id, no_data=False)
                            app_handle = app_result.get("qReturn", {}).get("qHandle", -1)
                            if app_handle != -1:
                                return self.engine_api.create_hypercube(app_handle, dimensions, measures, max_rows)
                            else:
                                raise Exception("Failed to open app")
                        except Exception as e:
                            return {"error": str(e)}
                        finally:
                            self.engine_api.disconnect()

                    result = await asyncio.to_thread(_create_hypercube)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(result, indent=2, ensure_ascii=False)
                        )
                    ]

                elif name == "engine_export_visualization_to_csv":
                    app_id = arguments["app_id"]
                    object_id = arguments["object_id"]
                    file_path = arguments.get("file_path", "/tmp/export.csv")

                    def _export_visualization():
                        try:
                            self.engine_api.connect()
                            app_result = self.engine_api.open_doc(app_id, no_data=False)
                            app_handle = app_result.get("qReturn", {}).get("qHandle", -1)
                            if app_handle != -1:
                                return self.engine_api.export_visualization_to_csv(app_handle, object_id, file_path)
                            else:
                                raise Exception("Failed to open app")
                        except Exception as e:
                            return {"error": str(e)}
                        finally:
                            self.engine_api.disconnect()

                    result = await asyncio.to_thread(_export_visualization)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(result, indent=2, ensure_ascii=False)
                        )
                    ]

                else:
                    raise ValueError(f"Unknown tool: {name}")

            except Exception as e:
                return [
                    TextContent(
                        type="text",
                        text=f"Error: {str(e)}"
                    )
                ]

    async def run(self):
        """
        Start the MCP server with stdio transport.

        Initializes server capabilities and begins listening for MCP protocol messages
        over stdin/stdout for communication with MCP clients.
        """
        async with stdio_server() as (read_stream, write_stream):
            await self.server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="qlik-sense-mcp-server",
                    server_version="0.1.0",
                    capabilities=ServerCapabilities(
                        tools={}
                    ),
                ),
            )


async def async_main():
    """
    Async main entry point for the Qlik Sense MCP Server.

    Creates and starts the MCP server instance with configured
    Qlik Sense Repository and Engine API connections.
    """
    server = QlikSenseMCPServer()
    await server.run()


def main():
    """
    Synchronous entry point for CLI usage.

    This function is used as the entry point in pyproject.toml
    for the qlik-sense-mcp-server command.
    """
    # Handle command line arguments
    if len(sys.argv) > 1:
        if sys.argv[1] in ["--help", "-h"]:
            print_help()
            return
        elif sys.argv[1] in ["--version", "-v"]:
            print("qlik-sense-mcp-server 1.0.2")
            return

    asyncio.run(async_main())


def print_help():
    """Print help information."""
    help_text = """
Qlik Sense MCP Server - Model Context Protocol server for Qlik Sense Enterprise APIs

USAGE:
    qlik-sense-mcp-server [OPTIONS]
    uvx qlik-sense-mcp-server [OPTIONS]

OPTIONS:
    -h, --help     Show this help message
    -v, --version  Show version information

CONFIGURATION:
    Set these environment variables before running:

    QLIK_SERVER_URL       - Qlik Sense server URL (required)
                           Example: https://qlik.company.com

    QLIK_USER_DIRECTORY   - User directory (required)
                           Example: COMPANY

    QLIK_USER_ID          - User ID (required)
                           Example: your-username

    QLIK_CLIENT_CERT_PATH - Path to client certificate (required)
                           Example: /path/to/certs/client.pem

    QLIK_CLIENT_KEY_PATH  - Path to client key (required)
                           Example: /path/to/certs/client_key.pem

    QLIK_CA_CERT_PATH     - Path to CA certificate (required)
                           Example: /path/to/certs/root.pem

    QLIK_REPOSITORY_PORT  - Repository API port (optional, default: 4242)
    QLIK_ENGINE_PORT      - Engine API port (optional, default: 4747)
    QLIK_VERIFY_SSL       - Verify SSL certificates (optional, default: true)

EXAMPLES:
    # Using uvx with environment variables
    uvx --with-env QLIK_SERVER_URL=https://qlik.company.com \\
        --with-env QLIK_USER_DIRECTORY=COMPANY \\
        --with-env QLIK_USER_ID=username \\
        --with-env QLIK_CLIENT_CERT_PATH=/path/to/client.pem \\
        --with-env QLIK_CLIENT_KEY_PATH=/path/to/client_key.pem \\
        --with-env QLIK_CA_CERT_PATH=/path/to/root.pem \\
        qlik-sense-mcp-server

    # Using environment file
    export QLIK_SERVER_URL=https://qlik.company.com
    export QLIK_USER_DIRECTORY=COMPANY
    export QLIK_USER_ID=username
    qlik-sense-mcp-server

AVAILABLE TOOLS:
    Repository API: get_apps, get_users, get_streams, get_tasks, start_task, etc.
    Engine API: engine_get_fields, engine_create_hypercube, engine_get_data, etc.

    Total: 21 tools for Qlik Sense operations

MORE INFO:
    GitHub: https://github.com/bintocher/qlik-sense-mcp
    PyPI: https://pypi.org/project/qlik-sense-mcp-server/
"""
    print(help_text)


if __name__ == "__main__":
    main()
