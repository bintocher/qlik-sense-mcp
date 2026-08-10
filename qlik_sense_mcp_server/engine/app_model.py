"""App-level metadata: the data model, master items, variables, counts."""

from typing import Dict, List, Any



class EngineAppModelMixin:
    def _get_user_variables(self, app_handle: int) -> List[Dict[str, Any]]:
        """User-created variables only (script and UI, never system ones).

        Raises on failure instead of returning an empty list: an app with
        no variables and an app whose variable list could not be read look
        identical otherwise.
        """
        variable_list_def = {
            "qInfo": {"qId": "VariableList", "qType": "VariableList"},
            "qVariableListDef": {
                "qType": "variable",
                "qShowReserved": False,  # Exclude system variables
                "qShowConfig": False,
                "qData": {"tags": "/tags"}
            }
        }

        with self.session_object(app_handle, variable_list_def) as list_handle:
            layout_response = self.send_request("GetLayout", [], handle=list_handle)
            variables = (layout_response.get("qLayout", {})
                         .get("qVariableList", {})
                         .get("qItems", []))

            return [
                {
                    "name": variable.get("qName", ""),
                    "text_value": variable.get("qDefinition", ""),
                    "is_script_created": variable.get("qIsScriptCreated", False),
                }
                for variable in variables
                # Belt and braces: qShowReserved/qShowConfig above should
                # already have excluded these.
                if not variable.get("qIsReserved", False)
                and not variable.get("qIsConfig", False)
            ]

