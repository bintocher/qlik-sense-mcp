"""Qlik Sense Engine API client."""

import json
import websocket
import ssl
from typing import Dict, List, Any, Optional, Union
from .config import QlikSenseConfig


class QlikEngineAPI:
    """Client for Qlik Sense Engine API using WebSocket."""

    def __init__(self, config: QlikSenseConfig):
        self.config = config
        self.ws = None
        self.request_id = 0

    def _get_next_request_id(self) -> int:
        """Get next request ID."""
        self.request_id += 1
        return self.request_id

    def connect(self, app_id: Optional[str] = None) -> None:
        """Connect to Engine API via WebSocket."""
        # Try different WebSocket endpoints
        server_host = self.config.server_url.replace("https://", "").replace(
            "http://", ""
        )

        endpoints_to_try = [
            f"wss://{server_host}:{self.config.engine_port}/app/engineData",
            f"wss://{server_host}:{self.config.engine_port}/app",
            f"ws://{server_host}:{self.config.engine_port}/app/engineData",
            f"ws://{server_host}:{self.config.engine_port}/app",
        ]

        # Setup SSL context
        ssl_context = ssl.create_default_context()
        if not self.config.verify_ssl:
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        if self.config.client_cert_path and self.config.client_key_path:
            ssl_context.load_cert_chain(
                self.config.client_cert_path, self.config.client_key_path
            )

        if self.config.ca_cert_path:
            ssl_context.load_verify_locations(self.config.ca_cert_path)

        # Headers for authentication
        headers = [
            f"X-Qlik-User: UserDirectory={self.config.user_directory}; UserId={self.config.user_id}"
        ]

        last_error = None
        for url in endpoints_to_try:
            try:
                if url.startswith("wss://"):
                    self.ws = websocket.create_connection(
                        url, sslopt={"context": ssl_context}, header=headers, timeout=10
                    )
                else:
                    self.ws = websocket.create_connection(
                        url, header=headers, timeout=10
                    )


                self.ws.recv()
                return  # Success
            except Exception as e:
                last_error = e
                if self.ws:
                    self.ws.close()
                    self.ws = None
                continue

        raise ConnectionError(
            f"Failed to connect to Engine API. Last error: {str(last_error)}"
        )

    def disconnect(self) -> None:
        """Disconnect from Engine API."""
        if self.ws:
            self.ws.close()
            self.ws = None

    def send_request(
        self, method: str, params: List[Any] = None, handle: int = -1
    ) -> Dict[str, Any]:
        """
        Send JSON-RPC 2.0 request to Qlik Engine API and return response.

        Args:
            method: Engine API method name
            params: Method parameters list
            handle: Object handle for scoped operations (-1 for global)

        Returns:
            Response dictionary from Engine API
        """
        if not self.ws:
            raise ConnectionError("Not connected to Engine API")


        request = {
            "jsonrpc": "2.0",
            "id": self._get_next_request_id(),
            "handle": handle,
            "method": method,
            "params": params or [],
        }

        self.ws.send(json.dumps(request))


        while True:
            data = self.ws.recv()
            if "result" in data or "error" in data:
                break

        response = json.loads(data)

        if "error" in response:
            raise Exception(f"Engine API error: {response['error']}")

        return response.get("result", {})

    def get_doc_list(self) -> List[Dict[str, Any]]:
        """Get list of available documents."""
        try:
            # Connect to global engine first
            result = self.send_request("GetDocList")
            doc_list = result.get("qDocList", [])

            # Ensure we return a list even if empty
            if isinstance(doc_list, list):
                return doc_list
            else:
                return []

        except Exception as e:
            # Return empty list on error for compatibility
            return []

    def open_doc(self, app_id: str, no_data: bool = True) -> Dict[str, Any]:
        """
        Open Qlik Sense application document.

        Args:
            app_id: Application ID to open
            no_data: If True, open without loading data (faster for metadata operations)

        Returns:
            Response with document handle
        """
        try:
            if no_data:
                return self.send_request("OpenDoc", [app_id, "", "", "", True])
            else:
                return self.send_request("OpenDoc", [app_id])
        except Exception as e:
            # If app is already open, try to get existing handle
            if "already open" in str(e).lower():
                try:
                    # Try to get the already open document
                    doc_list = self.get_doc_list()
                    for doc in doc_list:
                        if doc.get("qDocId") == app_id:
                            # Return mock response with existing handle
                            return {
                                "qReturn": {
                                    "qHandle": doc.get("qHandle", -1),
                                    "qGenericId": app_id
                                }
                            }
                except:
                    pass
            raise e

    def close_doc(self, app_handle: int) -> bool:
        """Close application document."""
        try:
            result = self.send_request("CloseDoc", [], handle=app_handle)
            return result.get("qReturn", {}).get("qSuccess", False)
        except Exception:
            return False

    def get_active_doc(self) -> Dict[str, Any]:
        """Get currently active document if any."""
        try:
            result = self.send_request("GetActiveDoc")
            return result
        except Exception:
            return {}

    def open_doc_safe(self, app_id: str, no_data: bool = True) -> Dict[str, Any]:
        """
        Safely open document with better error handling for already open apps.

        Args:
            app_id: Application ID to open
            no_data: If True, open without loading data

        Returns:
            Response with document handle
        """
        try:
            # First try to open normally
            if no_data:
                return self.send_request("OpenDoc", [app_id, "", "", "", True])
            else:
                return self.send_request("OpenDoc", [app_id])

        except Exception as e:
            error_msg = str(e)

            # Handle "already open" errors specially
            if "already open" in error_msg.lower() or "app already open" in error_msg.lower():
                try:
                    # Try to get active document
                    active_doc = self.get_active_doc()
                    if active_doc and "qReturn" in active_doc:
                        return active_doc

                    # Try to find in document list
                    doc_list = self.get_doc_list()
                    for doc in doc_list:
                        if doc.get("qDocId") == app_id or doc.get("qDocName") == app_id:
                            return {
                                "qReturn": {
                                    "qHandle": doc.get("qHandle", -1),
                                    "qGenericId": app_id
                                }
                            }

                    # If still not found, re-raise original error
                    raise e

                except Exception:
                    # If all recovery attempts fail, re-raise original error
                    raise e
            else:
                # For other errors, just re-raise
                raise e

    def get_app_properties(self, app_handle: int) -> Dict[str, Any]:
        """Get app properties."""
        return self.send_request("GetAppProperties", handle=app_handle)

    def get_script(self, app_handle: int) -> str:
        """Get load script."""
        result = self.send_request("GetScript", [], handle=app_handle)
        return result.get("qScript", "")

    def set_script(self, app_handle: int, script: str) -> bool:
        """Set load script."""
        result = self.send_request("SetScript", [script], handle=app_handle)
        return result.get("qReturn", {}).get("qSuccess", False)

    def do_save(self, app_handle: int, file_name: Optional[str] = None) -> bool:
        """Save app."""
        params = {}
        if file_name:
            params["qFileName"] = file_name
        result = self.send_request("DoSave", params, handle=app_handle)
        return result.get("qReturn", {}).get("qSuccess", False)

    def get_objects(
        self, app_handle: int, object_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get app objects."""
        params = {
            "qOptions": {
                "qTypes": [object_type] if object_type else [],
                "qIncludeSessionObjects": True,
                "qData": {},
            }
        }
        result = self.send_request("GetObjects", params, handle=app_handle)
        return result.get("qList", {}).get("qItems", [])

    def get_sheets(self, app_handle: int) -> List[Dict[str, Any]]:
        """Get app sheets."""
        try:
            # Use correct method to get sheets
            params = {
                "qOptions": {
                    "qTypes": ["sheet"],
                    "qIncludeSessionObjects": False,
                    "qData": {},
                }
            }
            result = self.send_request("GetObjects", params, handle=app_handle)

            if isinstance(result, dict):
                if "qList" in result:
                    q_list = result["qList"]
                    if isinstance(q_list, dict) and "qItems" in q_list:
                        return q_list["qItems"]
                    elif isinstance(q_list, list):
                        return q_list
                    else:
                        return {"error": "qList has unexpected format", "qList": q_list}
                else:
                    return {"error": "No qList in response", "response": result}
            else:
                return {"error": "Response is not a dict", "response": result}

        except Exception as e:
            return {"error": str(e), "details": "Error in get_sheets method"}

    def get_sheet_objects(self, app_handle: int, sheet_id: str) -> List[Dict[str, Any]]:
        """Get objects on a specific sheet."""
        try:
            # First get the sheet object
            sheet_params = {"qId": sheet_id}
            sheet_result = self.send_request(
                "GetObject", sheet_params, handle=app_handle
            )

            if not sheet_result or "qReturn" not in sheet_result:
                return {"error": "Could not get sheet object", "sheet_id": sheet_id}

            sheet_handle = sheet_result["qReturn"]["qHandle"]

            # Get sheet layout to find child objects
            layout_result = self.send_request("GetLayout", {}, handle=sheet_handle)

            if not layout_result or "qLayout" not in layout_result:
                return {"error": "Could not get sheet layout", "sheet_id": sheet_id}

            # Extract child objects from layout
            layout = layout_result["qLayout"]
            child_objects = []

            # Look for cells or children in the layout
            if "qChildList" in layout:
                child_objects = layout["qChildList"]["qItems"]
            elif "cells" in layout:
                child_objects = layout["cells"]
            elif "qChildren" in layout:
                child_objects = layout["qChildren"]

            return child_objects

        except Exception as e:
            return {
                "error": str(e),
                "details": f"Error getting objects for sheet {sheet_id}",
            }

    def get_sheets_with_objects(self, app_id: str) -> Dict[str, Any]:
        """Get sheets and their objects for an app."""
        try:
            self.connect()

            # Open the app
            app_result = self.open_doc(app_id, no_data=False)
            if "qReturn" not in app_result or "qHandle" not in app_result["qReturn"]:
                return {"error": "Failed to open app", "response": app_result}

            app_handle = app_result["qReturn"]["qHandle"]

            # Get sheets
            sheets = self.get_sheets(app_handle)

            if isinstance(sheets, dict) and "error" in sheets:
                return sheets

            # Get objects for each sheet
            detailed_sheets = []
            for sheet in sheets:
                if isinstance(sheet, dict) and "qInfo" in sheet:
                    sheet_id = sheet["qInfo"]["qId"]
                    sheet_objects = self.get_sheet_objects(app_handle, sheet_id)

                    sheet_info = {
                        "sheet_info": sheet,
                        "objects": (
                            sheet_objects
                            if not isinstance(sheet_objects, dict)
                            or "error" not in sheet_objects
                            else []
                        ),
                        "objects_error": (
                            sheet_objects.get("error")
                            if isinstance(sheet_objects, dict)
                            and "error" in sheet_objects
                            else None
                        ),
                    }
                    detailed_sheets.append(sheet_info)

            return {"sheets": detailed_sheets, "total_sheets": len(detailed_sheets)}

        except Exception as e:
            return {
                "error": str(e),
                "details": "Error in get_sheets_with_objects method",
            }

    def get_fields(self, app_handle: int) -> List[Dict[str, Any]]:
        """Get app fields using GetTablesAndKeys method."""
        try:
            # Use correct GetTablesAndKeys method as in qsea.py
            result = self.send_request(
                "GetTablesAndKeys",
                [
                    {"qcx": 1000, "qcy": 1000},  # Max dimensions
                    {"qcx": 0, "qcy": 0},  # Min dimensions
                    30,  # Max tables
                    True,  # Include system tables
                    False,  # Include hidden fields
                ],
                handle=app_handle,
            )


            fields_info = []

            if "qtr" in result:
                for table in result["qtr"]:
                    table_name = table.get("qName", "Unknown")

                    if "qFields" in table:
                        for field in table["qFields"]:
                            field_info = {
                                "field_name": field.get("qName", ""),
                                "table_name": table_name,
                                "data_type": field.get("qType", ""),
                                "is_key": field.get("qIsKey", False),
                                "is_system": field.get("qIsSystem", False),
                                "is_hidden": field.get("qIsHidden", False),
                                "is_semantic": field.get("qIsSemantic", False),
                                "distinct_values": field.get(
                                    "qnTotalDistinctValues", 0
                                ),
                                "present_distinct_values": field.get(
                                    "qnPresentDistinctValues", 0
                                ),
                                "rows_count": field.get("qnRows", 0),
                                "subset_ratio": field.get("qSubsetRatio", 0),
                                "key_type": field.get("qKeyType", ""),
                                "tags": field.get("qTags", []),
                            }
                            fields_info.append(field_info)

            return {
                "fields": fields_info,
                "tables_count": len(result.get("qtr", [])),
                "total_fields": len(fields_info),
            }

        except Exception as e:
            return {"error": str(e), "details": "Error in get_fields method"}

    def get_tables(self, app_handle: int) -> List[Dict[str, Any]]:
        """Get app tables."""
        result = self.send_request("GetTablesList", handle=app_handle)
        return result.get("qtr", [])

    def create_session_object(
        self, app_handle: int, obj_def: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Create session object."""
        return self.send_request(
            "CreateSessionObject", {"qProp": obj_def}, handle=app_handle
        )

    def get_object(self, app_handle: int, object_id: str) -> Dict[str, Any]:
        """Get object by ID."""
        return self.send_request("GetObject", {"qId": object_id}, handle=app_handle)

    def evaluate_expression(self, app_handle: int, expression: str) -> Any:
        """Evaluate expression."""
        result = self.send_request(
            "Evaluate", {"qExpression": expression}, handle=app_handle
        )
        return result.get("qReturn", {})

    def select_in_field(
        self, app_handle: int, field_name: str, values: List[str], toggle: bool = False
    ) -> bool:
        """Select values in field."""
        params = {"qFieldName": field_name, "qValues": values, "qToggleMode": toggle}
        result = self.send_request("SelectInField", params, handle=app_handle)
        return result.get("qReturn", False)

    def clear_selections(self, app_handle: int, locked_also: bool = False) -> bool:
        """Clear all selections."""
        params = {"qLockedAlso": locked_also}
        result = self.send_request("ClearAll", params, handle=app_handle)
        return result.get("qReturn", False)

    def get_current_selections(self, app_handle: int) -> List[Dict[str, Any]]:
        """Get current selections."""
        result = self.send_request("GetCurrentSelections", handle=app_handle)
        return result.get("qSelections", [])

    def get_data_model(self, app_handle: int) -> Dict[str, Any]:
        """Get complete data model with tables and associations."""
        try:
            # Use GetAllInfos to get basic structure information
            all_infos = self.send_request("GetAllInfos", [], handle=app_handle)

            # Analyze the objects to understand data structure
            sheets = []
            visualizations = []
            measures = []
            dimensions = []

            for info in all_infos.get("qInfos", []):
                obj_type = info.get("qType", "")
                obj_id = info.get("qId", "")

                if obj_type == "sheet":
                    sheets.append({"id": obj_id, "type": obj_type})
                elif obj_type in [
                    "table",
                    "barchart",
                    "linechart",
                    "piechart",
                    "combochart",
                    "kpi",
                    "listbox",
                ]:
                    visualizations.append({"id": obj_id, "type": obj_type})
                elif obj_type == "measure":
                    measures.append({"id": obj_id, "type": obj_type})
                elif obj_type == "dimension":
                    dimensions.append({"id": obj_id, "type": obj_type})

            return {
                "app_structure": {
                    "total_objects": len(all_infos.get("qInfos", [])),
                    "sheets": sheets,
                    "visualizations": visualizations,
                    "measures": measures,
                    "dimensions": dimensions,
                },
                "raw_info": all_infos,
            }
        except Exception as e:
            return {"error": str(e)}

    def get_field_description(self, app_handle: int, field_name: str) -> Dict[str, Any]:
        """Get detailed field information including values."""
        # Use correct structure as in pyqlikengine
        params = [{"qFieldName": field_name, "qStateName": "$"}]
        result = self.send_request("GetField", params, handle=app_handle)
        return result

    def create_hypercube(
        self,
        app_handle: int,
        dimensions: List[str],
        measures: List[str],
        max_rows: int = 1000,
    ) -> Dict[str, Any]:
        """Create hypercube for data extraction with proper structure."""
        try:
            # Create correct hypercube structure
            hypercube_def = {
                "qDimensions": [
                    {
                        "qDef": {
                            "qFieldDefs": [dim],
                            "qSortCriterias": [
                                {
                                    "qSortByState": 0,
                                    "qSortByFrequency": 0,
                                    "qSortByNumeric": 1,
                                    "qSortByAscii": 1,
                                    "qSortByLoadOrder": 0,
                                    "qSortByExpression": 0,
                                    "qExpression": {"qv": ""},
                                }
                            ],
                        },
                        "qNullSuppression": False,
                        "qIncludeElemValue": True,
                    }
                    for dim in dimensions
                ],
                "qMeasures": [
                    {
                        "qDef": {"qDef": measure, "qLabel": f"Measure_{i}"},
                        "qSortBy": {"qSortByNumeric": -1, "qSortByLoadOrder": 0},
                    }
                    for i, measure in enumerate(measures)
                ],
                "qInitialDataFetch": [
                    {
                        "qTop": 0,
                        "qLeft": 0,
                        "qHeight": max_rows,
                        "qWidth": len(dimensions) + len(measures),
                    }
                ],
                "qSuppressZero": False,
                "qSuppressMissing": False,
                "qMode": "S",
                "qInterColumnSortOrder": list(range(len(dimensions) + len(measures))),
            }

            obj_def = {
                "qInfo": {
                    "qId": f"hypercube-{len(dimensions)}d-{len(measures)}m",
                    "qType": "HyperCube",
                },
                "qHyperCubeDef": hypercube_def,
            }

            result = self.send_request(
                "CreateSessionObject", [obj_def], handle=app_handle
            )

            if "qReturn" not in result or "qHandle" not in result["qReturn"]:
                return {"error": "Failed to create hypercube", "response": result}

            cube_handle = result["qReturn"]["qHandle"]

            # Получаем layout с данными
            layout = self.send_request("GetLayout", [], handle=cube_handle)

            if "qLayout" not in layout or "qHyperCube" not in layout["qLayout"]:
                return {"error": "No hypercube in layout", "layout": layout}

            hypercube = layout["qLayout"]["qHyperCube"]

            return {
                "hypercube_handle": cube_handle,
                "hypercube_data": hypercube,
                "dimensions": dimensions,
                "measures": measures,
                "total_rows": hypercube.get("qSize", {}).get("qcy", 0),
                "total_columns": hypercube.get("qSize", {}).get("qcx", 0),
            }

        except Exception as e:
            return {"error": str(e), "details": "Error in create_hypercube method"}

    def get_hypercube_data(
        self,
        hypercube_handle: int,
        page_top: int = 0,
        page_height: int = 1000,
        page_left: int = 0,
        page_width: int = 50,
    ) -> Dict[str, Any]:
        """Get data from existing hypercube with pagination."""
        try:
            # Use correct GetHyperCubeData method
            params = [
                {
                    "qPath": "/qHyperCubeDef",
                    "qPages": [
                        {
                            "qTop": page_top,
                            "qLeft": page_left,
                            "qHeight": page_height,
                            "qWidth": page_width,
                        }
                    ],
                }
            ]

            result = self.send_request(
                "GetHyperCubeData", params, handle=hypercube_handle
            )
            return result

        except Exception as e:
            return {"error": str(e), "details": "Error in get_hypercube_data method"}

    def get_table_data(
        self, app_handle: int, table_name: str = None, max_rows: int = 1000
    ) -> Dict[str, Any]:
        """Get data from a specific table by creating hypercube with all table fields."""
        try:
            if not table_name:
                # Get list of available tables
                fields_result = self.get_fields(app_handle)
                if "error" in fields_result:
                    return fields_result

                tables = {}
                for field in fields_result.get("fields", []):
                    table = field.get("table_name", "Unknown")
                    if table not in tables:
                        tables[table] = []
                    tables[table].append(field["field_name"])

                return {
                    "message": "Please specify table_name parameter",
                    "available_tables": tables,
                    "note": "Use one of the available table names to get data",
                }

            # Get fields for specified table
            fields_result = self.get_fields(app_handle)
            if "error" in fields_result:
                return fields_result

            table_fields = []
            for field in fields_result.get("fields", []):
                if field.get("table_name") == table_name:
                    table_fields.append(field["field_name"])

            if not table_fields:
                return {"error": f"Table '{table_name}' not found or has no fields"}

            # Limit number of fields to avoid too wide tables
            max_fields = 20
            if len(table_fields) > max_fields:
                table_fields = table_fields[:max_fields]
                truncated = True
            else:
                truncated = False

            # Create hypercube with all table fields as dimensions
            hypercube_def = {
                "qDimensions": [
                    {
                        "qDef": {
                            "qFieldDefs": [field],
                            "qSortCriterias": [
                                {
                                    "qSortByState": 0,
                                    "qSortByFrequency": 0,
                                    "qSortByNumeric": 1,
                                    "qSortByAscii": 1,
                                    "qSortByLoadOrder": 1,
                                    "qSortByExpression": 0,
                                    "qExpression": {"qv": ""},
                                }
                            ],
                        },
                        "qNullSuppression": False,
                        "qIncludeElemValue": True,
                    }
                    for field in table_fields
                ],
                "qMeasures": [],
                "qInitialDataFetch": [
                    {
                        "qTop": 0,
                        "qLeft": 0,
                        "qHeight": max_rows,
                        "qWidth": len(table_fields),
                    }
                ],
                "qSuppressZero": False,
                "qSuppressMissing": False,
                "qMode": "S",
            }

            obj_def = {
                "qInfo": {"qId": f"table-data-{table_name}", "qType": "HyperCube"},
                "qHyperCubeDef": hypercube_def,
            }

            # Создаем session object
            result = self.send_request(
                "CreateSessionObject", [obj_def], handle=app_handle
            )

            if "qReturn" not in result or "qHandle" not in result["qReturn"]:
                return {
                    "error": "Failed to create hypercube for table data",
                    "response": result,
                }

            cube_handle = result["qReturn"]["qHandle"]

            # Получаем layout с данными
            layout = self.send_request("GetLayout", [], handle=cube_handle)

            if "qLayout" not in layout or "qHyperCube" not in layout["qLayout"]:
                try:
                    self.send_request(
                        "DestroySessionObject",
                        [f"table-data-{table_name}"],
                        handle=app_handle,
                    )
                except:
                    pass
                return {"error": "No hypercube in layout", "layout": layout}

            hypercube = layout["qLayout"]["qHyperCube"]

            # Process data into convenient format
            table_data = []
            headers = table_fields

            for page in hypercube.get("qDataPages", []):
                for row in page.get("qMatrix", []):
                    row_data = {}
                    for i, cell in enumerate(row):
                        if i < len(headers):
                            row_data[headers[i]] = {
                                "text": cell.get("qText", ""),
                                "numeric": (
                                    cell.get("qNum", None)
                                    if cell.get("qNum") != "NaN"
                                    else None
                                ),
                                "is_numeric": cell.get("qIsNumeric", False),
                                "state": cell.get("qState", "O"),
                            }
                    table_data.append(row_data)

            result_data = {
                "table_name": table_name,
                "headers": headers,
                "data": table_data,
                "total_rows": hypercube.get("qSize", {}).get("qcy", 0),
                "returned_rows": len(table_data),
                "total_columns": len(headers),
                "truncated_fields": truncated,
                "dimension_info": hypercube.get("qDimensionInfo", []),
            }

            # Очищаем созданный объект
            try:
                self.send_request(
                    "DestroySessionObject",
                    [f"table-data-{table_name}"],
                    handle=app_handle,
                )
            except Exception as cleanup_error:
                result_data["cleanup_warning"] = str(cleanup_error)

            return result_data

        except Exception as e:
            return {"error": str(e), "details": "Error in get_table_data method"}

    def get_field_values(
        self,
        app_handle: int,
        field_name: str,
        max_values: int = 100,
        include_frequency: bool = True,
    ) -> Dict[str, Any]:
        """Get field values with frequency information using ListObject."""
        try:
            # Use correct structure
            list_def = {
                "qInfo": {"qId": f"field-values-{field_name}", "qType": "ListObject"},
                "qListObjectDef": {
                    "qStateName": "$",
                    "qLibraryId": "",
                    "qDef": {
                        "qFieldDefs": [field_name],
                        "qFieldLabels": [],
                        "qSortCriterias": [
                            {
                                "qSortByState": 0,
                                "qSortByFrequency": 1 if include_frequency else 0,
                                "qSortByNumeric": 1,
                                "qSortByAscii": 1,
                                "qSortByLoadOrder": 0,
                                "qSortByExpression": 0,
                                "qExpression": {"qv": ""},
                            }
                        ],
                    },
                    "qInitialDataFetch": [
                        {"qTop": 0, "qLeft": 0, "qHeight": max_values, "qWidth": 1}
                    ],
                },
            }

            # Create session object - use correct parameter format
            result = self.send_request(
                "CreateSessionObject", [list_def], handle=app_handle
            )

            if "qReturn" not in result or "qHandle" not in result["qReturn"]:
                return {"error": "Failed to create session object", "response": result}

            list_handle = result["qReturn"]["qHandle"]

            # Получаем layout с данными
            layout = self.send_request("GetLayout", [], handle=list_handle)

            # Correct path to qListObject - it's in qLayout
            if "qLayout" not in layout or "qListObject" not in layout["qLayout"]:
                # Clean up object before returning error
                try:
                    self.send_request(
                        "DestroySessionObject",
                        [f"field-values-{field_name}"],
                        handle=app_handle,
                    )
                except:
                    pass
                return {"error": "No list object in layout", "layout": layout}

            list_object = layout["qLayout"]["qListObject"]
            values_data = []

            # Process data
            for page in list_object.get("qDataPages", []):
                for row in page.get("qMatrix", []):
                    if row and len(row) > 0:
                        cell = row[0]
                        value_info = {
                            "value": cell.get("qText", ""),
                            "state": cell.get(
                                "qState", "O"
                            ),  # O=Optional, S=Selected, A=Alternative, X=Excluded
                            "numeric_value": cell.get("qNum", None),
                            "is_numeric": cell.get("qIsNumeric", False),
                        }

                        # Add frequency if available
                        if "qFrequency" in cell:
                            value_info["frequency"] = cell.get("qFrequency", 0)

                        values_data.append(value_info)

            # Get general field information
            field_info = {
                "field_name": field_name,
                "values": values_data,
                "total_values": list_object.get("qSize", {}).get("qcy", 0),
                "returned_count": len(values_data),
                "dimension_info": list_object.get("qDimensionInfo", {}),
                "debug_info": {
                    "list_handle": list_handle,
                    "data_pages_count": len(list_object.get("qDataPages", [])),
                    "raw_size": list_object.get("qSize", {}),
                },
            }

            # Очищаем созданный объект
            try:
                self.send_request(
                    "DestroySessionObject",
                    [f"field-values-{field_name}"],
                    handle=app_handle,
                )
            except Exception as cleanup_error:
                field_info["cleanup_warning"] = str(cleanup_error)

            return field_info

        except Exception as e:
            return {"error": str(e), "details": "Error in get_field_values method"}

    def get_field_statistics(self, app_handle: int, field_name: str) -> Dict[str, Any]:
        """Get comprehensive statistics for a field."""
        debug_log = []
        debug_log.append(f"get_field_statistics called with app_handle={app_handle}, field_name={field_name}")
        try:
            # Create expressions for statistics
            stats_expressions = [
                f"Count(DISTINCT [{field_name}])",  # Unique values
                f"Count([{field_name}])",  # Total count
                f"Count({{$<[{field_name}]={{'*'}}>}})",  # Non-null count
                f"Min([{field_name}])",  # Minimum value
                f"Max([{field_name}])",  # Maximum value
                f"Avg([{field_name}])",  # Average value
                f"Sum([{field_name}])",  # Sum (if numeric)
                f"Median([{field_name}])",  # Median
                f"Mode([{field_name}])",  # Mode (most frequent)
                f"Stdev([{field_name}])",  # Standard deviation
            ]
            debug_log.append(f"Created {len(stats_expressions)} expressions: {stats_expressions}")

            # Create hypercube for statistics calculation
            hypercube_def = {
                "qDimensions": [],
                "qMeasures": [
                    {"qDef": {"qDef": expr, "qLabel": f"Stat_{i}"}}
                    for i, expr in enumerate(stats_expressions)
                ],
                "qInitialDataFetch": [
                    {
                        "qTop": 0,
                        "qLeft": 0,
                        "qHeight": 1,
                        "qWidth": len(stats_expressions),
                    }
                ],
                "qSuppressZero": False,
                "qSuppressMissing": False,
            }

            obj_def = {
                "qInfo": {"qId": f"field-stats-{field_name}", "qType": "HyperCube"},
                "qHyperCubeDef": hypercube_def,
            }

            # Create session object
            debug_log.append(f"Creating session object with obj_def: {obj_def}")
            result = self.send_request(
                "CreateSessionObject", [obj_def], handle=app_handle
            )
            debug_log.append(f"CreateSessionObject result: {result}")

            if "qReturn" not in result or "qHandle" not in result["qReturn"]:
                debug_log.append(f"Failed to create session object, returning error")
                return {
                    "error": "Failed to create statistics hypercube",
                    "response": result,
                    "debug_log": debug_log
                }

            cube_handle = result["qReturn"]["qHandle"]

            # Get layout with data
            layout = self.send_request("GetLayout", [], handle=cube_handle)

            if "qLayout" not in layout or "qHyperCube" not in layout["qLayout"]:
                try:
                    self.send_request(
                        "DestroySessionObject",
                        [f"field-stats-{field_name}"],
                        handle=app_handle,
                    )
                except:
                    pass
                return {"error": "No hypercube in statistics layout", "layout": layout, "debug_log": debug_log}

            hypercube = layout["qLayout"]["qHyperCube"]

            # Extract statistics values
            stats_labels = [
                "unique_values",
                "total_count",
                "non_null_count",
                "min_value",
                "max_value",
                "avg_value",
                "sum_value",
                "median_value",
                "mode_value",
                "std_deviation",
            ]

            statistics = {"field_name": field_name}

            for page in hypercube.get("qDataPages", []):
                for row in page.get("qMatrix", []):
                    for i, cell in enumerate(row):
                        if i < len(stats_labels):
                            stat_name = stats_labels[i]
                            statistics[stat_name] = {
                                "text": cell.get("qText", ""),
                                "numeric": (
                                    cell.get("qNum", None)
                                    if cell.get("qNum") != "NaN"
                                    else None
                                ),
                                "is_numeric": cell.get("qIsNumeric", False),
                            }

                                    # Calculate additional derived statistics
            debug_log.append(f"Statistics before calculation: {statistics}")
            if "total_count" in statistics and "non_null_count" in statistics:
                # Handle None values safely
                total_dict = statistics["total_count"]
                non_null_dict = statistics["non_null_count"]
                debug_log.append(f"total_dict: {total_dict}")
                debug_log.append(f"non_null_dict: {non_null_dict}")

                total = total_dict.get("numeric", 0) if total_dict.get("numeric") is not None else 0
                non_null = non_null_dict.get("numeric", 0) if non_null_dict.get("numeric") is not None else 0
                debug_log.append(f"total: {total} (type: {type(total)})")
                debug_log.append(f"non_null: {non_null} (type: {type(non_null)})")

                if total > 0:
                    debug_log.append(f"Calculating percentages...")
                    debug_log.append(f"Calculation: ({total} - {non_null}) / {total} * 100")
                    statistics["null_percentage"] = round(
                        (total - non_null) / total * 100, 2
                    )
                    statistics["completeness_percentage"] = round(
                        non_null / total * 100, 2
                    )
                    debug_log.append(f"Percentages calculated successfully")

            # Cleanup
            try:
                self.send_request(
                    "DestroySessionObject",
                    [f"field-stats-{field_name}"],
                    handle=app_handle,
                )
            except Exception as cleanup_error:
                statistics["cleanup_warning"] = str(cleanup_error)

            statistics["debug_log"] = debug_log
            return statistics

        except Exception as e:
            import traceback
            debug_log.append(f"Exception in get_field_statistics: {e}")
            debug_log.append(f"Traceback: {traceback.format_exc()}")
            return {
                "error": str(e),
                "details": "Error in get_field_statistics method",
                "traceback": traceback.format_exc(),
                "debug_log": debug_log
            }

    def get_object_data(self, app_handle: int, object_id: str) -> Dict[str, Any]:
        """Get data from existing visualization object."""
        obj_result = self.send_request(
            "GetObject", {"qId": object_id}, handle=app_handle
        )
        obj_handle = obj_result.get("qReturn", {}).get("qHandle", -1)

        if obj_handle != -1:
            layout = self.send_request("GetLayout", handle=obj_handle)
            return layout
        return {}

    def export_data_to_csv(
        self, app_handle: int, object_id: str, file_path: str = "/tmp/export.csv"
    ) -> Dict[str, Any]:
        """Export object data to CSV."""
        params = {
            "qObjectId": object_id,
            "qPath": file_path,
            "qExportState": "A",  # All data
        }
        result = self.send_request("ExportData", params, handle=app_handle)
        return result

    def search_objects(
        self, app_handle: int, search_terms: List[str], object_types: List[str] = None
    ) -> List[Dict[str, Any]]:
        """Search for objects by terms."""
        params = {
            "qOptions": {"qSearchFields": ["*"], "qContext": "LockedFieldsOnly"},
            "qTerms": search_terms,
            "qPage": {"qOffset": 0, "qCount": 100, "qMaxNbrFieldMatches": 5},
        }

        if object_types:
            params["qOptions"]["qTypes"] = object_types

        result = self.send_request("SearchObjects", params, handle=app_handle)
        return result.get("qResult", {}).get("qSearchTerms", [])

    def get_field_and_variable_list(self, app_handle: int) -> Dict[str, Any]:
        """Get comprehensive list of fields and variables."""
        result = self.send_request("GetFieldAndVariableList", {}, handle=app_handle)
        return result

    def get_measures(self, app_handle: int) -> List[Dict[str, Any]]:
        """Get master measures."""
        result = self.send_request("GetMeasureList", handle=app_handle)
        return result.get("qMeasureList", {}).get("qItems", [])

    def get_dimensions(self, app_handle: int) -> List[Dict[str, Any]]:
        """Get master dimensions."""
        result = self.send_request("GetDimensionList", handle=app_handle)
        return result.get("qDimensionList", {}).get("qItems", [])

    def get_variables(self, app_handle: int) -> List[Dict[str, Any]]:
        """Get variables."""
        result = self.send_request("GetVariableList", handle=app_handle)
        return result.get("qVariableList", {}).get("qItems", [])

    def create_list_object(
        self, app_handle: int, field_name: str, sort_by_frequency: bool = True
    ) -> Dict[str, Any]:
        """Create optimized list object for field analysis."""
        list_def = {
            "qInfo": {"qType": "ListObject"},
            "qListObjectDef": {
                "qDef": {
                    "qFieldDefs": [field_name],
                    "qSortCriterias": [
                        {
                            "qSortByFrequency": 1 if sort_by_frequency else 0,
                            "qSortByNumeric": 1,
                            "qSortByAscii": 1,
                        }
                    ],
                },
                "qInitialDataFetch": [
                    {"qTop": 0, "qLeft": 0, "qHeight": 100, "qWidth": 1}
                ],
            },
        }

        result = self.send_request(
            "CreateSessionObject", {"qProp": list_def}, handle=app_handle
        )
        return result

    def get_pivot_table_data(
        self,
        app_handle: int,
        dimensions: List[str],
        measures: List[str],
        max_rows: int = 1000,
    ) -> Dict[str, Any]:
        """Create pivot table for complex data analysis."""
        pivot_def = {
            "qInfo": {"qType": "PivotTable"},
            "qHyperCubeDef": {
                "qDimensions": [
                    {"qDef": {"qFieldDefs": [dim]}, "qNullSuppression": True}
                    for dim in dimensions
                ],
                "qMeasures": [
                    {"qDef": {"qDef": measure}, "qSortBy": {"qSortByNumeric": -1}}
                    for measure in measures
                ],
                "qInitialDataFetch": [
                    {
                        "qTop": 0,
                        "qLeft": 0,
                        "qHeight": max_rows,
                        "qWidth": len(dimensions) + len(measures),
                    }
                ],
                "qSuppressZero": True,
                "qSuppressMissing": True,
            },
        }

        result = self.send_request(
            "CreateSessionObject", {"qProp": pivot_def}, handle=app_handle
        )
        return result

    def calculate_expression(
        self, app_handle: int, expression: str, dimensions: List[str] = None
    ) -> Dict[str, Any]:
        """Calculate expression with optional grouping by dimensions."""
        if dimensions:
            # Create hypercube for grouped calculation
            hypercube_def = {
                "qDimensions": [{"qDef": {"qFieldDefs": [dim]}} for dim in dimensions],
                "qMeasures": [{"qDef": {"qDef": expression}}],
                "qInitialDataFetch": [
                    {
                        "qTop": 0,
                        "qLeft": 0,
                        "qHeight": 1000,
                        "qWidth": len(dimensions) + 1,
                    }
                ],
            }

            obj_def = {
                "qInfo": {"qType": "calculation"},
                "qHyperCubeDef": hypercube_def,
            }

            result = self.send_request(
                "CreateSessionObject", {"qProp": obj_def}, handle=app_handle
            )
            return result
        else:
            # Simple expression evaluation
            return self.evaluate_expression(app_handle, expression)

    def get_bookmarks(self, app_handle: int) -> List[Dict[str, Any]]:
        """Get bookmarks (saved selections)."""
        result = self.send_request("GetBookmarkList", handle=app_handle)
        return result.get("qBookmarkList", {}).get("qItems", [])

    def apply_bookmark(self, app_handle: int, bookmark_id: str) -> bool:
        """Apply bookmark selections."""
        result = self.send_request(
            "ApplyBookmark", {"qBookmarkId": bookmark_id}, handle=app_handle
        )
        return result.get("qReturn", False)

    def get_locale_info(self, app_handle: int) -> Dict[str, Any]:
        """Get locale information for proper number/date formatting."""
        result = self.send_request("GetLocaleInfo", handle=app_handle)
        return result

    def search_suggest(
        self, app_handle: int, search_terms: List[str], object_types: List[str] = None
    ) -> List[Dict[str, Any]]:
        """Get search suggestions for better field/value discovery."""
        params = {
            "qSuggestions": {
                "qSuggestionTypes": (
                    ["Field", "Value", "Object"] if not object_types else object_types
                )
            },
            "qTerms": search_terms,
        }

        result = self.send_request("SearchSuggest", params, handle=app_handle)
        return result.get("qResult", {}).get("qSuggestions", [])

    def create_data_export(
        self,
        app_handle: int,
        table_name: str = None,
        fields: List[str] = None,
        format_type: str = "json",
        max_rows: int = 10000,
        filters: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """Create data export in various formats (JSON, CSV-like structure)."""
        try:
            # If no specific fields provided, get all fields from table
            if not fields:
                if table_name:
                    fields_result = self.get_fields(app_handle)
                    if "error" in fields_result:
                        return fields_result

                    table_fields = []
                    for field in fields_result.get("fields", []):
                        if field.get("table_name") == table_name:
                            table_fields.append(field["field_name"])

                    if not table_fields:
                        return {"error": f"No fields found for table '{table_name}'"}

                    fields = table_fields[:50]  # Limit to 50 fields max
                else:
                    return {
                        "error": "Either table_name or fields list must be provided"
                    }

            # Create hypercube for data extraction
            hypercube_def = {
                "qDimensions": [
                    {
                        "qDef": {
                            "qFieldDefs": [field],
                            "qSortCriterias": [
                                {
                                    "qSortByState": 0,
                                    "qSortByFrequency": 0,
                                    "qSortByNumeric": 1,
                                    "qSortByAscii": 1,
                                    "qSortByLoadOrder": 1,
                                    "qSortByExpression": 0,
                                    "qExpression": {"qv": ""},
                                }
                            ],
                        },
                        "qNullSuppression": False,
                        "qIncludeElemValue": True,
                    }
                    for field in fields
                ],
                "qMeasures": [],
                "qInitialDataFetch": [
                    {"qTop": 0, "qLeft": 0, "qHeight": max_rows, "qWidth": len(fields)}
                ],
                "qSuppressZero": False,
                "qSuppressMissing": False,
                "qMode": "S",
            }

            # Apply filters if provided
            if filters:
                # Add selection expressions as calculated dimensions
                for field_name, filter_values in filters.items():
                    if isinstance(filter_values, list):
                        values_str = ", ".join([f"'{v}'" for v in filter_values])
                        filter_expr = f"If(Match([{field_name}], {values_str}), [{field_name}], Null())"
                    else:
                        filter_expr = f"If([{field_name}] = '{filter_values}', [{field_name}], Null())"

                    # Replace the original field with filtered version
                    for dim in hypercube_def["qDimensions"]:
                        if dim["qDef"]["qFieldDefs"][0] == field_name:
                            dim["qDef"]["qFieldDefs"] = [filter_expr]
                            break

            obj_def = {
                "qInfo": {
                    "qId": f"data-export-{table_name or 'custom'}",
                    "qType": "HyperCube",
                },
                "qHyperCubeDef": hypercube_def,
            }

            # Create session object
            result = self.send_request(
                "CreateSessionObject", [obj_def], handle=app_handle
            )

            if "qReturn" not in result or "qHandle" not in result["qReturn"]:
                return {
                    "error": "Failed to create export hypercube",
                    "response": result,
                }

            cube_handle = result["qReturn"]["qHandle"]

            # Get layout with data
            layout = self.send_request("GetLayout", [], handle=cube_handle)

            if "qLayout" not in layout or "qHyperCube" not in layout["qLayout"]:
                try:
                    self.send_request(
                        "DestroySessionObject",
                        [f"data-export-{table_name or 'custom'}"],
                        handle=app_handle,
                    )
                except:
                    pass
                return {"error": "No hypercube in export layout", "layout": layout}

            hypercube = layout["qLayout"]["qHyperCube"]

            # Process data based on format
            export_data = []
            headers = fields

            for page in hypercube.get("qDataPages", []):
                for row in page.get("qMatrix", []):
                    if format_type.lower() == "json":
                        row_data = {}
                        for i, cell in enumerate(row):
                            if i < len(headers):
                                row_data[headers[i]] = {
                                    "text": cell.get("qText", ""),
                                    "numeric": (
                                        cell.get("qNum", None)
                                        if cell.get("qNum") != "NaN"
                                        else None
                                    ),
                                    "is_numeric": cell.get("qIsNumeric", False),
                                }
                        export_data.append(row_data)

                    elif format_type.lower() == "csv":
                        # CSV-like structure (list of values)
                        row_values = []
                        for cell in row:
                            row_values.append(cell.get("qText", ""))
                        export_data.append(row_values)

                    elif format_type.lower() == "simple":
                        # Simple key-value structure
                        row_data = {}
                        for i, cell in enumerate(row):
                            if i < len(headers):
                                row_data[headers[i]] = cell.get("qText", "")
                        export_data.append(row_data)

            result_data = {
                "export_format": format_type,
                "table_name": table_name,
                "fields": headers,
                "data": export_data,
                "metadata": {
                    "total_rows": hypercube.get("qSize", {}).get("qcy", 0),
                    "exported_rows": len(export_data),
                    "total_columns": len(headers),
                    "filters_applied": filters is not None,
                    "export_timestamp": None,  # Could be added with datetime.now() if needed
                    "dimension_info": hypercube.get("qDimensionInfo", []),
                },
            }

            # Add CSV headers if CSV format
            if format_type.lower() == "csv":
                result_data["csv_headers"] = headers

            # Cleanup
            try:
                self.send_request(
                    "DestroySessionObject",
                    [f"data-export-{table_name or 'custom'}"],
                    handle=app_handle,
                )
            except Exception as cleanup_error:
                result_data["cleanup_warning"] = str(cleanup_error)

            return result_data

        except Exception as e:
            return {"error": str(e), "details": "Error in create_data_export method"}

    def get_visualization_data(self, app_handle: int, object_id: str) -> Dict[str, Any]:
        """Get data from existing visualization object (chart, table, etc.)."""
        try:
            # Получаем объект по ID
            obj_result = self.send_request("GetObject", [object_id], handle=app_handle)

            if "qReturn" not in obj_result or "qHandle" not in obj_result["qReturn"]:
                return {
                    "error": f"Failed to get object with ID: {object_id}",
                    "response": obj_result,
                }

            obj_handle = obj_result["qReturn"]["qHandle"]

            # Получаем layout объекта
            layout = self.send_request("GetLayout", [], handle=obj_handle)

            if "qLayout" not in layout:
                return {"error": "No layout found for object", "layout": layout}

            obj_layout = layout["qLayout"]
            obj_info = obj_layout.get("qInfo", {})
            obj_type = obj_info.get("qType", "unknown")

            result = {
                "object_id": object_id,
                "object_type": obj_type,
                "object_title": obj_layout.get("qMeta", {}).get("title", ""),
                "data": None,
                "structure": None,
            }

            # Обрабатываем разные типы объектов
            if "qHyperCube" in obj_layout:
                # Объект с hypercube (большинство графиков и таблиц)
                hypercube = obj_layout["qHyperCube"]

                # Извлекаем данные
                table_data = []
                dimensions = []
                measures = []

                # Получаем информацию о dimensions
                for dim_info in hypercube.get("qDimensionInfo", []):
                    dimensions.append(
                        {
                            "title": dim_info.get("qFallbackTitle", ""),
                            "field": (
                                dim_info.get("qGroupFieldDefs", [""])[0]
                                if dim_info.get("qGroupFieldDefs")
                                else ""
                            ),
                            "cardinal": dim_info.get("qCardinal", 0),
                        }
                    )

                # Получаем информацию о measures
                for measure_info in hypercube.get("qMeasureInfo", []):
                    measures.append(
                        {
                            "title": measure_info.get("qFallbackTitle", ""),
                            "expression": measure_info.get("qDef", ""),
                            "format": measure_info.get("qNumFormat", {}),
                        }
                    )

                # Извлекаем данные из страниц
                for page in hypercube.get("qDataPages", []):
                    for row in page.get("qMatrix", []):
                        row_data = {}

                        # Dimensions
                        for i, cell in enumerate(row[: len(dimensions)]):
                            if i < len(dimensions):
                                row_data[f"dim_{i}_{dimensions[i]['title']}"] = {
                                    "text": cell.get("qText", ""),
                                    "numeric": (
                                        cell.get("qNum", None)
                                        if cell.get("qNum") != "NaN"
                                        else None
                                    ),
                                    "state": cell.get("qState", "O"),
                                }

                        # Measures
                        for i, cell in enumerate(row[len(dimensions) :]):
                            if i < len(measures):
                                row_data[f"measure_{i}_{measures[i]['title']}"] = {
                                    "text": cell.get("qText", ""),
                                    "numeric": (
                                        cell.get("qNum", None)
                                        if cell.get("qNum") != "NaN"
                                        else None
                                    ),
                                }

                        table_data.append(row_data)

                result["data"] = table_data
                result["structure"] = {
                    "dimensions": dimensions,
                    "measures": measures,
                    "total_rows": hypercube.get("qSize", {}).get("qcy", 0),
                    "total_columns": hypercube.get("qSize", {}).get("qcx", 0),
                    "returned_rows": len(table_data),
                }

            elif "qListObject" in obj_layout:
                # ListBox объект
                list_object = obj_layout["qListObject"]

                values_data = []
                for page in list_object.get("qDataPages", []):
                    for row in page.get("qMatrix", []):
                        if row and len(row) > 0:
                            cell = row[0]
                            values_data.append(
                                {
                                    "value": cell.get("qText", ""),
                                    "state": cell.get("qState", "O"),
                                    "frequency": cell.get("qFrequency", 0),
                                }
                            )

                result["data"] = values_data
                result["structure"] = {
                    "field_name": list_object.get("qDimensionInfo", {}).get(
                        "qFallbackTitle", ""
                    ),
                    "total_values": list_object.get("qSize", {}).get("qcy", 0),
                    "returned_values": len(values_data),
                }

            elif "qPivotTable" in obj_layout:
                # Pivot Table объект
                pivot_table = obj_layout["qPivotTable"]
                result["data"] = pivot_table.get("qDataPages", [])
                result["structure"] = {
                    "type": "pivot_table",
                    "size": pivot_table.get("qSize", {}),
                }

            else:
                # Неизвестный тип объекта - возвращаем raw layout
                result["data"] = obj_layout
                result["structure"] = {"type": "unknown", "raw_layout": True}

            return result

        except Exception as e:
            return {
                "error": str(e),
                "details": "Error in get_visualization_data method",
            }

    def get_detailed_app_metadata(self, app_id: str) -> Dict[str, Any]:
        """Get detailed app metadata similar to /api/v1/apps/{app_id}/data/metadata endpoint."""
        try:
            self.connect()

            # Open the app
            app_result = self.open_doc(app_id, no_data=False)
            if "qReturn" not in app_result or "qHandle" not in app_result["qReturn"]:
                return {"error": "Failed to open app", "response": app_result}

            app_handle = app_result["qReturn"]["qHandle"]

            # Get app layout and properties using correct methods
            try:
                layout = self.send_request("GetAppLayout", [], handle=app_handle)
            except:
                layout = {}

            try:
                properties = self.send_request(
                    "GetAppProperties", [], handle=app_handle
                )
            except:
                properties = {}

            # Get fields information
            fields_result = self.get_fields(app_handle)

            # Get tables information using GetTablesAndKeys
            tables_result = self.send_request(
                "GetTablesAndKeys",
                [
                    {"qcx": 1000, "qcy": 1000},  # Max dimensions
                    {"qcx": 0, "qcy": 0},  # Min dimensions
                    30,  # Max tables
                    True,  # Include system tables
                    False,  # Include hidden fields
                ],
                handle=app_handle,
            )

            # Process fields data
            fields_metadata = []
            if "fields" in fields_result:
                for field in fields_result["fields"]:
                    field_meta = {
                        "name": field.get("field_name", ""),
                        "src_tables": [field.get("table_name", "")],
                        "is_system": field.get("is_system", False),
                        "is_hidden": field.get("is_hidden", False),
                        "is_semantic": field.get("is_semantic", False),
                        "distinct_only": False,
                        "cardinal": field.get("distinct_values", 0),
                        "total_count": field.get("rows_count", 0),
                        "is_locked": False,
                        "always_one_selected": False,
                        "is_numeric": "numeric" in field.get("tags", []),
                        "comment": "",
                        "tags": field.get("tags", []),
                        "byte_size": 0,  # Not available via Engine API
                        "hash": "",  # Not available via Engine API
                    }
                    fields_metadata.append(field_meta)

            # Process tables data
            tables_metadata = []
            if "qtr" in tables_result:
                for table in tables_result["qtr"]:
                    table_meta = {
                        "name": table.get("qName", ""),
                        "is_system": table.get("qIsSystem", False),
                        "is_semantic": table.get("qIsSemantic", False),
                        "is_loose": table.get("qIsLoose", False),
                        "no_of_rows": table.get("qNoOfRows", 0),
                        "no_of_fields": len(table.get("qFields", [])),
                        "no_of_key_fields": len(
                            [
                                f
                                for f in table.get("qFields", [])
                                if f.get("qIsKey", False)
                            ]
                        ),
                        "comment": table.get("qComment", ""),
                        "byte_size": 0,  # Not available via Engine API
                    }
                    tables_metadata.append(table_meta)

            # Get reload metadata if available
            reload_meta = {
                "cpu_time_spent_ms": 0,  # Not available via Engine API
                "hardware": {"logical_cores": 0, "total_memory": 0},
                "peak_memory_bytes": 0,
                "fullReloadPeakMemoryBytes": 0,
                "partialReloadPeakMemoryBytes": 0,
            }

            # Calculate static byte size approximation
            static_byte_size = sum(
                table.get("byte_size", 0) for table in tables_metadata
            )

            # Build response similar to the expected format
            metadata = {
                "reload_meta": reload_meta,
                "static_byte_size": static_byte_size,
                "fields": fields_metadata,
                "tables": tables_metadata,
                "has_section_access": False,  # Would need to check script for this
                "tables_profiling_data": [],
                "is_direct_query_mode": False,
                "usage": "ANALYTICS",
                "source": "engine_api",
                "app_layout": layout,
                "app_properties": properties,
            }

            return metadata

        except Exception as e:
            return {"error": str(e), "details": "Error in get_detailed_app_metadata"}
        finally:
            self.disconnect()
