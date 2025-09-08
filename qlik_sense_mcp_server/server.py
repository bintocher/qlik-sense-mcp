"""Main MCP Server for Qlik Sense APIs."""

import asyncio
import json
import sys
from typing import Any, Dict, List, Optional
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import ServerCapabilities, Tool
from mcp.types import CallToolResult, TextContent

from .config import QlikSenseConfig
from .repository_api import QlikRepositoryAPI
from .engine_api import QlikEngineAPI

import logging
import os
from dotenv import load_dotenv

# Initialize logging configuration early
load_dotenv()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Configure root logger to stderr (stdout reserved for MCP protocol)
_logging_level = getattr(logging, LOG_LEVEL, logging.INFO)
if not logging.getLogger().handlers:
    handler = logging.StreamHandler(stream=sys.stderr)
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logging.getLogger().addHandler(handler)
logging.getLogger().setLevel(_logging_level)
logger = logging.getLogger(__name__)


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
                logging.getLogger(__name__).warning("Failed to initialize APIs: %s", e)

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
            including applications, analytics tools, and data export.
            """
            tools_list = [
                Tool(
                    name="get_apps",
                    description="Get list of Qlik Sense applications with essential fields and filters (name, stream, published) and pagination.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "limit": {
                                "type": "integer",
                                "description": "Maximum number of apps to return (default: 25, max: 50)",
                                "default": 25
                            },
                            "offset": {
                                "type": "integer",
                                "description": "Number of apps to skip for pagination (default: 0)",
                                "default": 0
                            },
                            "name": {
                                "type": "string",
                                "description": "Wildcard case-insensitive search in application name"
                            },
                            "stream": {
                                "type": "string",
                                "description": "Wildcard case-insensitive search in stream name"
                            },
                            "published": {
                                "type": ["boolean", "integer", "string"],
                                "description": "Filter by published status (true/false or 1/0). Default: true",
                                "default": True
                            }
                        }
                    }
                ),
                Tool(
                    name="get_app_details",
                    description="Get compact application info with filters by guid or name (case-insensitive). Returns metainfo, tables/fields list, master items, sheets and objects with used fields.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "app_id": {"type": "string", "description": "Application GUID (preferred if known)"},
                            "name": {"type": "string", "description": "Case-insensitive fuzzy search by app name"}
                        },
                        "oneOf": [
                            {"required": ["app_id"]},
                            {"required": ["name"]}
                        ]
                    }
                ),

                Tool(name="engine_get_script", description="Get load script from app", inputSchema={"type": "object", "properties": {"app_id": {"type": "string", "description": "Application ID"}}, "required": ["app_id"]}),
                Tool(name="engine_get_field_statistics", description="Get comprehensive statistics for a field", inputSchema={"type": "object", "properties": {"app_id": {"type": "string", "description": "Application ID"}, "field_name": {"type": "string", "description": "Field name"}}, "required": ["app_id", "field_name"]}),
                Tool(name="engine_create_hypercube", description="Create hypercube for data analysis", inputSchema={"type": "object", "properties": {"app_id": {"type": "string", "description": "Application ID"}, "dimensions": {"type": "array", "items": {"type": "string"}, "description": "List of dimension fields"}, "measures": {"type": "array", "items": {"type": "string"}, "description": "List of measure expressions"}, "max_rows": {"type": "integer", "description": "Maximum rows to return", "default": 1000}}, "required": ["app_id", "dimensions", "measures"]})
                ,
                Tool(
                    name="get_app_field",
                    description="Return values of a single field from app with pagination and wildcard search (supports * and %).",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "app_id": {"type": "string", "description": "Application GUID"},
                            "field_name": {"type": "string", "description": "Field name"},
                            "limit": {"type": "integer", "description": "Max values to return (default: 10, max: 100)", "default": 10},
                            "offset": {"type": "integer", "description": "Offset for pagination (default: 0)", "default": 0},
                            "search_string": {"type": "string", "description": "Wildcard text search mask (* and % supported), case-insensitive by default"},
                            "search_number": {"type": "string", "description": "Wildcard numeric search mask (* and % supported)"},
                            "case_sensitive": {"type": "boolean", "description": "Case sensitive matching for search_string", "default": False}
                        },
                        "required": ["app_id", "field_name"],
                    }
                ),
                Tool(
                    name="get_app_variables",
                    description="Return variables split by source (script/ui) with pagination and wildcard search.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "app_id": {"type": "string", "description": "Application GUID"},
                            "limit": {"type": "integer", "description": "Max variables to return (default: 10, max: 100)", "default": 10},
                            "offset": {"type": "integer", "description": "Offset for pagination (default: 0)", "default": 0},
                            "created_in_script": {"type": ["boolean", "integer", "string"], "description": "Return only variables created in script (true/false). If omitted, return both"},
                            "search_string": {"type": "string", "description": "Wildcard search by variable name or text value (* and % supported), case-insensitive by default"},
                            "search_number": {"type": "string", "description": "Wildcard search among numeric variable values (* and % supported)"},
                            "case_sensitive": {"type": "boolean", "description": "Case sensitive matching for search_string", "default": False}
                        },
                        "required": ["app_id"],
                    }
                ),
                Tool(
                    name="get_app_sheets",
                    description="Get list of sheets from application with title and description.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "app_id": {"type": "string", "description": "Application GUID"}
                        },
                        "required": ["app_id"]
                    }
                ),
                Tool(
                    name="get_app_sheet_objects",
                    description="Get list of objects from specific sheet with object ID, type and description.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "app_id": {"type": "string", "description": "Application GUID"},
                            "sheet_id": {"type": "string", "description": "Sheet GUID"}
                        },
                        "required": ["app_id", "sheet_id"]
                    }
                )
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
                    limit = arguments.get("limit", 25)
                    offset = arguments.get("offset", 0)
                    name_filter = arguments.get("name")
                    stream_filter = arguments.get("stream")
                    published_arg = arguments.get("published", True)

                    if limit is None or limit < 1:
                        limit = 25
                    if limit > 50:
                        limit = 50

                    def _to_bool(value: Any, default: bool = True) -> bool:
                        if isinstance(value, bool):
                            return value
                        if isinstance(value, int):
                            return value != 0
                        if isinstance(value, str):
                            v = value.strip().lower()
                            if v in ("true", "1", "yes", "y"): return True
                            if v in ("false", "0", "no", "n"): return False
                        return default

                    published_bool = _to_bool(published_arg, True)

                    apps_payload = await asyncio.to_thread(
                        self.repository_api.get_comprehensive_apps,
                        limit,
                        offset,
                        name_filter,
                        stream_filter,
                        published_bool,
                    )
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(apps_payload, indent=2, ensure_ascii=False)
                        )
                    ]

                elif name == "get_app_details":
                    req_app_id = arguments.get("app_id")
                    req_name = arguments.get("name")

                    def _resolve_app() -> Dict[str, Any]:
                        try:
                            if req_app_id:
                                app_meta = self.repository_api.get_app_by_id(req_app_id)
                                if isinstance(app_meta, dict) and app_meta.get("id"):
                                    return {
                                        "app_id": app_meta.get("id"),
                                        "name": app_meta.get("name", ""),
                                        "description": app_meta.get("description") or "",
                                        "stream": (app_meta.get("stream", {}) or {}).get("name", "") if app_meta.get("published") else "",
                                        "modified_dttm": app_meta.get("modifiedDate", "") or "",
                                        "reload_dttm": app_meta.get("lastReloadTime", "") or ""
                                    }
                                return {"error": "App not found by provided app_id"}
                            if req_name:
                                apps_payload = self.repository_api.get_comprehensive_apps(limit=50, offset=0, name=req_name, stream=None, published=None)
                                apps = apps_payload.get("apps", []) if isinstance(apps_payload, dict) else []
                                if not apps:
                                    return {"error": "No apps found by name"}
                                lowered = req_name.lower()
                                exact = [a for a in apps if a.get("name", "").lower() == lowered]
                                selected = exact[0] if exact else apps[0]
                                selected["app_id"] = selected.pop("guid", "")
                                return selected
                            return {"error": "Either app_id or name must be provided"}
                        except Exception as e:
                            return {"error": str(e)}

                    resolved = await asyncio.to_thread(_resolve_app)
                    if "error" in resolved:
                        return [TextContent(type="text", text=json.dumps(resolved, indent=2, ensure_ascii=False))]

                    app_id = resolved.get("app_id") or resolved.get("guid")

                    def _build_compact_details():
                        app_handle = -1
                        try:
                            self.engine_api.connect()
                            app_result = self.engine_api.open_doc_safe(app_id, no_data=False)
                            app_handle = app_result.get("qReturn", {}).get("qHandle", -1)
                            if app_handle == -1:
                                return {"error": "Failed to open app"}

                            meta = self.engine_api._get_app_metadata_fast(app_handle) or {}
                            fields_info = self.engine_api.get_fields(app_handle) or {}

                            tables_map = {}
                            for f in fields_info.get("fields", []):
                                tname = f.get("table_name", "")
                                fname = f.get("field_name", "")
                                if not tname or not fname:
                                    continue
                                tables_map.setdefault(tname, []).append(fname)

                            total_tables = len(tables_map)
                            total_fields = sum(len(v) for v in tables_map.values())

                            mi = self.engine_api._get_user_master_items(app_handle) or {}
                            master_dimensions = [d.get("name", "") for d in mi.get("dimensions", []) if d.get("name")]
                            master_measures = [m.get("name", "") for m in mi.get("measures", []) if m.get("name")]

                            sheets_payload = self.engine_api.get_sheets(app_handle) or []
                            sheets_list = []
                            for s in sheets_payload:
                                try:
                                    sid = s.get("qInfo", {}).get("qId", "")
                                    sname = s.get("qMeta", {}).get("title", "")
                                    if not sid:
                                        continue
                                    objs = self.engine_api._get_sheet_objects_detailed(app_handle, sid) or []
                                    obj_list = []
                                    for o in objs:
                                        oid = o.get("object_id", "")
                                        otype = o.get("object_type", "")
                                        otitle = o.get("object_title", "")
                                        fields_used = o.get("fields_used", [])
                                        if not oid:
                                            continue
                                        obj_list.append({
                                            oid: {
                                                "object_type": otype,
                                                "object_desription": otitle,
                                                "fields": fields_used
                                            }
                                        })
                                    sheets_list.append({
                                        sid: {
                                            "sheet_name": sname,
                                            "objects": obj_list
                                        }
                                    })
                                except Exception:
                                    continue

                            metainfo = {
                                "app_id": app_id,
                                "name": resolved.get("name") or meta.get("title", ""),
                                "description": resolved.get("description") or meta.get("description", ""),
                                "stream": resolved.get("stream", ""),
                                "modified_dttm": resolved.get("modified_dttm") or meta.get("modified_date", ""),
                                "reload_dttm": resolved.get("reload_dttm") or meta.get("last_reload_time", ""),
                                "size_bytes": meta.get("size", 0)
                            }

                            return {
                                "metainfo": metainfo,
                                "tables_data": tables_map,
                                "total_tables": total_tables,
                                "total_fields": total_fields,
                                "master_dimensions": master_dimensions,
                                "master_measures": master_measures,
                                "sheets": sheets_list
                            }
                        except Exception as e:
                            return {"error": str(e)}
                        finally:
                            try:
                                if app_handle != -1:
                                    self.engine_api.close_doc(app_handle)
                            finally:
                                self.engine_api.disconnect()

                    compact = await asyncio.to_thread(_build_compact_details)
                    return [
                        TextContent(type="text", text=json.dumps(compact, indent=2, ensure_ascii=False))
                    ]

                elif name == "engine_get_script":
                    app_id = arguments["app_id"]

                    def _get_script():
                        app_handle = -1
                        try:
                            self.engine_api.connect()
                            app_result = self.engine_api.open_doc_safe(app_id, no_data=True)
                            if "qReturn" not in app_result:
                                raise Exception(f"Failed to open app: invalid response {app_result}")
                            app_handle = app_result["qReturn"].get("qHandle", -1)
                            if app_handle == -1:
                                raise Exception(f"Failed to get app handle: {app_result}")
                            script = self.engine_api.get_script(app_handle)
                            return {
                                "qScript": script,
                                "app_id": app_id,
                                "app_handle": app_handle,
                                "script_length": len(script) if script else 0
                            }
                        except Exception as e:
                            error_msg = str(e)
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
                            if app_handle != -1:
                                try:
                                    self.engine_api.close_doc(app_handle)
                                except:
                                    pass
                            self.engine_api.disconnect()

                    script = await asyncio.to_thread(_get_script)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(script, indent=2, ensure_ascii=False)
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

                elif name == "get_app_reload_chain":
                    app_id = arguments["app_id"]

                    def _get_reload_chain():
                        tasks = self.repository_api.get_reload_tasks_for_app(app_id)
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
                elif name == "get_app_field":
                    app_id = arguments["app_id"]
                    field_name = arguments["field_name"]
                    limit = arguments.get("limit", 10)
                    offset = arguments.get("offset", 0)
                    search_string = arguments.get("search_string")
                    search_number = arguments.get("search_number")
                    case_sensitive = arguments.get("case_sensitive", False)

                    if limit is None or limit < 1:
                        limit = 10
                    if limit > 100:
                        limit = 100
                    if offset is None or offset < 0:
                        offset = 0

                    def _wildcard_to_regex(pattern: str, case_sensitive_flag: bool) -> Any:
                        import re
                        escaped = re.escape(pattern).replace("\\*", ".*").replace("%", ".*")
                        regex = f"^{escaped}$"
                        return re.compile(regex, 0 if case_sensitive_flag else re.IGNORECASE)

                    def _get_values():
                        try:
                            self.engine_api.connect()
                            app_result = self.engine_api.open_doc_safe(app_id, no_data=False)
                            app_handle = app_result.get("qReturn", {}).get("qHandle", -1)
                            if app_handle == -1:
                                return {"error": "Failed to open app"}

                            fetch_size = max(limit + offset, 500)
                            if fetch_size > 5000:
                                fetch_size = 5000
                            field_data = self.engine_api.get_field_values(app_handle, field_name, fetch_size, include_frequency=False)
                            values = [v.get("value", "") for v in field_data.get("values", [])]

                            if search_string:
                                rx = _wildcard_to_regex(search_string, case_sensitive)
                                values = [val for val in values if isinstance(val, str) and rx.match(val)]

                            if search_number:
                                rxn = _wildcard_to_regex(search_number, case_sensitive)
                                filtered = []
                                for idx, vobj in enumerate(field_data.get("values", [])):
                                    cell_text = vobj.get("value", "")
                                    qnum = vobj.get("numeric_value", None)
                                    if qnum is not None:
                                        if rxn.match(str(qnum)) or rxn.match(str(cell_text)):
                                            filtered.append(cell_text)
                                values = filtered

                            sliced = values[offset:offset + limit]
                            return {"field_values": sliced}
                        except Exception as e:
                            return {"error": str(e)}
                        finally:
                            self.engine_api.disconnect()

                    result = await asyncio.to_thread(_get_values)
                    return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

                elif name == "get_app_variables":
                    app_id = arguments["app_id"]
                    limit = arguments.get("limit", 10)
                    offset = arguments.get("offset", 0)
                    created_in_script_arg = arguments.get("created_in_script", None)
                    search_string = arguments.get("search_string")
                    search_number = arguments.get("search_number")
                    case_sensitive = arguments.get("case_sensitive", False)

                    if limit is None or limit < 1:
                        limit = 10
                    if limit > 100:
                        limit = 100
                    if offset is None or offset < 0:
                        offset = 0

                    def _to_bool(value: Any, default: Optional[bool] = None) -> Optional[bool]:
                        if value is None:
                            return default
                        if isinstance(value, bool):
                            return value
                        if isinstance(value, int):
                            return value != 0
                        if isinstance(value, str):
                            v = value.strip().lower()
                            if v in ("true", "1", "yes", "y"): return True
                            if v in ("false", "0", "no", "n"): return False
                        return default

                    created_in_script = _to_bool(created_in_script_arg, None)

                    def _wildcard_to_regex(pattern: str, case_sensitive_flag: bool):
                        import re
                        escaped = re.escape(pattern).replace("\\*", ".*").replace("%", ".*")
                        regex = f"^{escaped}$"
                        return re.compile(regex, 0 if case_sensitive_flag else re.IGNORECASE)

                    def _get_variables():
                        try:
                            self.engine_api.connect()
                            app_result = self.engine_api.open_doc_safe(app_id, no_data=False)
                            app_handle = app_result.get("qReturn", {}).get("qHandle", -1)
                            if app_handle == -1:
                                return {"error": "Failed to open app"}

                            # Use the alternative method for getting variables
                            var_list = self.engine_api._get_user_variables(app_handle) or []
                            prepared = []
                            for v in var_list:
                                name = v.get("name", "")
                                text_val = v.get("text_value", "")
                                num_val = v.get("numeric_value")
                                is_script = v.get("is_script_created", False)
                                prepared.append({
                                    "name": name,
                                    "text_value": text_val if text_val is not None else "",
                                    "numeric_value": num_val,
                                    "is_script": is_script
                                })

                            if created_in_script is True:
                                prepared = [x for x in prepared if x["is_script"]]
                            elif created_in_script is False:
                                prepared = [x for x in prepared if not x["is_script"]]

                            if search_string:
                                rx = _wildcard_to_regex(search_string, case_sensitive)
                                prepared = [x for x in prepared if rx.match(x["name"]) or rx.match(x.get("text_value", ""))]

                            if search_number:
                                rxn = _wildcard_to_regex(search_number, case_sensitive)
                                prepared = [x for x in prepared if x.get("numeric_value") is not None and rxn.match(str(x.get("numeric_value")))]

                            from_script = [x for x in prepared if x["is_script"]]
                            from_ui = [x for x in prepared if not x["is_script"]]

                            def _slice_and_map(items):
                                sliced = items[offset:offset + limit]
                                result_map = {}
                                for it in sliced:
                                    val = it.get("text_value")
                                    if val is None or val == "":
                                        if it.get("numeric_value") is not None:
                                            val = str(it["numeric_value"])
                                        else:
                                            val = ""
                                    result_map[it["name"]] = val
                                return result_map

                            res_script = _slice_and_map(from_script)
                            res_ui = _slice_and_map(from_ui)

                            return {
                                "variables_from_script": res_script if res_script else "",
                                "variables_from_ui": res_ui if res_ui else ""
                            }
                        except Exception as e:
                            return {"error": str(e)}
                        finally:
                            self.engine_api.disconnect()

                    result = await asyncio.to_thread(_get_variables)
                    return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

                elif name == "get_app_sheets":
                    app_id = arguments["app_id"]

                    def _get_app_sheets():
                        try:
                            self.engine_api.connect()
                            app_result = self.engine_api.open_doc_safe(app_id, no_data=True)
                            app_handle = app_result.get("qReturn", {}).get("qHandle", -1)
                            if app_handle == -1:
                                return {"error": "Failed to open app"}

                            sheets = self.engine_api.get_sheets(app_handle)
                            sheets_list = []
                            for sheet in sheets:
                                sheet_info = sheet.get("qMeta", {})
                                sheet_data = sheet.get("qData", {})
                                sheets_list.append({
                                    "sheet_id": sheet.get("qInfo", {}).get("qId", ""),
                                    "title": sheet_info.get("title", ""),
                                    "description": sheet_info.get("description", "")
                                })

                            return {
                                "app_id": app_id,
                                "total_sheets": len(sheets_list),
                                "sheets": sheets_list
                            }
                        except Exception as e:
                            return {"error": str(e)}
                        finally:
                            self.engine_api.disconnect()

                    result = await asyncio.to_thread(_get_app_sheets)
                    return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

                elif name == "get_app_sheet_objects":
                    app_id = arguments["app_id"]
                    sheet_id = arguments["sheet_id"]

                    def _get_sheet_objects():
                        try:
                            self.engine_api.connect()
                            app_result = self.engine_api.open_doc_safe(app_id, no_data=True)
                            app_handle = app_result.get("qReturn", {}).get("qHandle", -1)
                            if app_handle == -1:
                                return {"error": "Failed to open app"}

                            # Get detailed objects from the sheet
                            objects = self.engine_api._get_sheet_objects_detailed(app_handle, sheet_id) or []

                            # Format objects according to requirements: id объекта, тип объекта, описание объекта
                            formatted_objects = []
                            for obj in objects:
                                if isinstance(obj, dict):
                                    obj_info = {
                                        "object_id": obj.get("object_id", ""),
                                        "object_type": obj.get("object_type", ""),
                                        "object_description": obj.get("object_title", "")
                                    }
                                    formatted_objects.append(obj_info)

                            return {
                                "app_id": app_id,
                                "sheet_id": sheet_id,
                                "total_objects": len(formatted_objects),
                                "objects": formatted_objects
                            }

                        except Exception as e:
                            return {"error": str(e), "details": f"Error getting objects for sheet {sheet_id} in app {app_id}"}
                        finally:
                            self.engine_api.disconnect()

                    result = await asyncio.to_thread(_get_sheet_objects)
                    return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

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
                                cube_result = self.engine_api.create_hypercube(app_handle, dimensions, measures, max_rows)
                                cube_handle = cube_result.get("qReturn", {}).get("qHandle", -1)
                                if cube_handle != -1:
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

                elif name == "get_app_object":
                    app_id = arguments["app_id"]
                    object_id = arguments["object_id"]

                    def _get_app_object():
                        try:
                            self.engine_api.connect()
                            app_result = self.engine_api.open_doc(app_id, no_data=False)
                            app_handle = app_result.get("qReturn", {}).get("qHandle", -1)

                            if app_handle == -1:
                                return {"error": "Failed to open app"}

                            # Get object
                            obj_result = self.engine_api.send_request("GetObject", {"qId": object_id}, handle=app_handle)
                            if "qReturn" not in obj_result:
                                return {"error": f"Object {object_id} not found"}

                            obj_handle = obj_result["qReturn"]["qHandle"]

                            # Get layout
                            layout_result = self.engine_api.send_request("GetLayout", [], handle=obj_handle)
                            if "qLayout" not in layout_result:
                                return {"error": "Failed to get object layout"}

                            layout = layout_result["qLayout"]

                            # Extract object properties
                            obj_info = layout.get("qInfo", {})
                            obj_meta = layout.get("qMeta", {})

                            object_type = obj_info.get("qType", "unknown")
                            object_title = layout.get("title", "")
                            object_description = layout.get("subtitle", "")

                            # Extract dimensions and measures
                            dimensions = []
                            measures = []

                            if "qHyperCube" in layout:
                                hypercube = layout["qHyperCube"]

                                # Dimensions
                                for dim_info in hypercube.get("qDimensionInfo", []):
                                    dimensions.append({
                                        "title": dim_info.get("qFallbackTitle", ""),
                                        "field": (dim_info.get("qGroupFieldDefs", [""])[0] if dim_info.get("qGroupFieldDefs") else ""),
                                        "cardinal": dim_info.get("qCardinal", 0),
                                    })

                                # Measures
                                for measure_info in hypercube.get("qMeasureInfo", []):
                                    # Try to get expression from different places
                                    expression = ""
                                    if measure_info.get("qDef"):
                                        expression = measure_info.get("qDef")
                                    elif measure_info.get("qMeasure", {}).get("qDef"):
                                        expression = measure_info.get("qMeasure", {}).get("qDef")

                                    measures.append({
                                        "title": measure_info.get("qFallbackTitle", ""),
                                        "expression": expression,
                                    })

                            # Extract table data for tables and charts
                            table_data = None
                            if object_type in ["sn-table", "sn-bar-chart", "sn-line-chart", "sn-pie-chart", "sn-combo-chart", "sn-scatter-plot", "sn-kpi"]:
                                table_data = _extract_table_data_from_layout(layout)

                            result = {
                                "object_id": object_id,
                                "object_type": object_type,
                                "title": object_title,
                                "description": object_description,
                                "dimensions": dimensions,
                                "measures": measures,
                                "table_data": table_data,
                            }

                            return result

                        except Exception as e:
                            return {"error": str(e), "app_id": app_id, "object_id": object_id}
                        finally:
                            self.engine_api.disconnect()

                    def _extract_table_data_from_layout(layout):
                        """Extract table data from layout."""
                        if "qHyperCube" not in layout:
                            return None

                        hypercube = layout["qHyperCube"]

                        if not hypercube.get("qDataPages"):
                            return None

                        # Form headers
                        headers = []
                        for dim_info in hypercube.get("qDimensionInfo", []):
                            headers.append(dim_info.get("qFallbackTitle", ""))
                        for measure_info in hypercube.get("qMeasureInfo", []):
                            headers.append(measure_info.get("qFallbackTitle", ""))

                        # Extract data
                        table_rows = []
                        for page in hypercube.get("qDataPages", []):
                            for row in page.get("qMatrix", []):
                                row_data = {}
                                for i, cell in enumerate(row):
                                    if i < len(headers):
                                        header = headers[i]
                                        cell_text = cell.get("qText", "")
                                        cell_num = cell.get("qNum", None)

                                        row_data[f"{header}_text"] = cell_text
                                        if cell_num is not None and str(cell_num) != "NaN":
                                            row_data[f"{header}_num"] = cell_num

                                table_rows.append(row_data)

                        return table_rows

                    result = await asyncio.to_thread(_get_app_object)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(result, indent=2, ensure_ascii=False)
                        )
                    ]

                else:
                    return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}, indent=2, ensure_ascii=False))]

            except Exception as e:
                return [TextContent(type="text", text=json.dumps({"error": str(e)}, indent=2, ensure_ascii=False))]

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
                    server_version="1.3.0",
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
            print("qlik-sense-mcp-server 1.3.0")
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
    uvx --with-env QLIK_SERVER_URL=https://qlik.company.com \
        --with-env QLIK_USER_DIRECTORY=COMPANY \
        --with-env QLIK_USER_ID=username \
        --with-env QLIK_CLIENT_CERT_PATH=/path/to/client.pem \
        --with-env QLIK_CLIENT_KEY_PATH=/path/to/client_key.pem \
        --with-env QLIK_CA_CERT_PATH=/path/to/root.pem \
        qlik-sense-mcp-server

    # Using environment file
    export QLIK_SERVER_URL=https://qlik.company.com
    export QLIK_USER_DIRECTORY=COMPANY
    export QLIK_USER_ID=username
    qlik-sense-mcp-server

AVAILABLE TOOLS:
    Repository API: get_apps (comprehensive), get_app_details, get_app_sheet_objects
    Engine API: engine_get_script, engine_create_hypercube, engine_get_field_statistics

    Total: 10 tools for Qlik Sense analytics operations

MORE INFO:
    GitHub: https://github.com/bintocher/qlik-sense-mcp
    PyPI: https://pypi.org/project/qlik-sense-mcp-server/
"""
    print(help_text)


if __name__ == "__main__":
    main()
