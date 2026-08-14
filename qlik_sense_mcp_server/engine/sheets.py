"""Sheets and the objects on them, plus the fields those objects use."""

from typing import Dict, List, Any, Optional
import logging

from ..exceptions import QlikEngineError

logger = logging.getLogger(__name__)

# Words that appear where a field name could, but never name a field:
# operators, set-analysis syntax, constants, and the aggregation modifiers
# that are written without parentheses.
_EXPRESSION_KEYWORDS = frozenset("""
    and or not xor like precedes follows
    if then else elseif end
    total distinct all aggr
    null true false
    as in when
    intervalmatch resident inline autogenerate
    e pi
""".split())


class EngineSheetsMixin:
    def get_script(self, app_handle: int) -> str:
        """Get load script."""
        result = self.send_request("GetScript", [], handle=app_handle)
        return result.get("qScript", "")

    def get_sheets(self, app_handle: int) -> List[Dict[str, Any]]:
        """Get app sheets.

        Raises rather than returning an empty list on failure: "this app
        has no sheets" and "the Engine call failed" are different answers,
        and the caller cannot act on them the same way.
        """
        sheet_list_def = {
            "qInfo": {"qId": "SheetList", "qType": "SheetList"},
            "qAppObjectListDef": {
                "qType": "sheet",
                "qData": {
                    "title": "/qMetaDef/title",
                    "description": "/qMetaDef/description",
                    "thumbnail": "/thumbnail",
                    "cells": "/cells",
                    "rank": "/rank",
                    "columns": "/columns",
                    "rows": "/rows"
                }
            }
        }

        with self.session_object(app_handle, sheet_list_def) as list_handle:
            layout_result = self.send_request("GetLayout", [], handle=list_handle)
            object_list = (layout_result.get("qLayout") or {}).get("qAppObjectList")
            if object_list is None:
                raise QlikEngineError(
                    f"No sheet list in layout for app handle {app_handle}: "
                    f"{layout_result}")
            sheets = object_list.get("qItems", [])
            logger.info("Found %d sheets", len(sheets))
            return sheets

    def _library_for(self, app_handle: int,
                     app_id: Optional[str] = None) -> Dict[str, Any]:
        """The library of the app this handle currently points at.

        Keyed by the app, never by the handle: Engine hands out handles per
        session and reuses the numbers, so after switching documents the
        same number means a different app - and a measure named from the
        library would be computed from another app's expression.
        """
        identity = app_id or getattr(self, "_cached_app_id", None)
        if not identity:
            return self.get_master_items(app_handle)
        cache = getattr(self, "_library_cache", None)
        if cache is None:
            cache = {}
            self._library_cache = cache
        if identity not in cache:
            cache[identity] = self.get_master_items(app_handle)
        return cache[identity]

    def get_master_items(self, app_handle: int) -> Dict[str, Any]:
        """The library of measures and dimensions the app itself defines.

        This is the vocabulary the author of the app wrote down: what
        "revenue" means here, which field it is built from, what the
        business calls it. Without it a caller assembles `Sum([Amount])`
        by guesswork and may pick a neighbouring field or the wrong
        aggregation - the answer looks reasonable and is not the number
        anyone on this dashboard would recognise.

        Both lists come back in one round trip, and an app with no library
        answers with two empty lists rather than an error.
        """
        wanted = [
            ("measures", "MeasureList", "qMeasureListDef", "qMeasureList",
             "qMeasure"),
            ("dimensions", "DimensionList", "qDimensionListDef",
             "qDimensionList", "qDim"),
        ]
        library: Dict[str, Any] = {"measures": [], "dimensions": []}
        for key, kind, definition_key, layout_key, body_key in wanted:
            object_id = f"library-{key}"
            definition = {
                "qInfo": {"qId": object_id, "qType": kind},
                definition_key: {
                    "qType": key[:-1],
                    "qData": {"title": "/qMetaDef/title",
                              "description": "/qMetaDef/description",
                              "tags": "/qMetaDef/tags",
                              "expression": f"/{body_key}/qDef"},
                },
            }
            try:
                with self.session_object(app_handle, definition) as handle:
                    layout = (self.send_request("GetLayout", [], handle=handle)
                              or {}).get("qLayout") or {}
            except Exception as exc:
                logger.debug("Could not read %s: %s", key, exc)
                continue
            for item in (layout.get(layout_key) or {}).get("qItems") or []:
                data = item.get("qData") or {}
                meta = item.get("qMeta") or {}
                entry = {
                    "name": (data.get("title") or meta.get("title")
                             or "").strip(),
                    "expression": str(data.get("expression") or "").strip(),
                    "id": (item.get("qInfo") or {}).get("qId", ""),
                }
                description = (data.get("description")
                               or meta.get("description") or "").strip()
                if description:
                    entry["description"] = description
                tags = [str(tag) for tag in (data.get("tags")
                                             or meta.get("tags") or []) if tag]
                if tags:
                    entry["tags"] = tags
                if entry["name"] or entry["expression"]:
                    library[key].append(entry)
        return library

    def _get_sheet_objects_detailed(self, app_handle: int, sheet_id: str) -> List[Dict[str, Any]]:
        """Get detailed information about objects on a sheet.

        Fetches all child objects' handles and layouts in two pipelined
        batches (see `send_requests_pipelined`) instead of one
        GetObject+GetLayout round-trip per object — a sheet with N objects
        used to cost 2N sequential round-trips, now costs 2 regardless of N.
        Per-object failures are still isolated: one bad object is skipped,
        the rest of the sheet is unaffected, same as before.
        """
        # A failure to reach the sheet itself is reported; a failure on one
        # of its objects is not, because the other objects are still worth
        # returning. Those two cases are what the try/except split below is.
        sheet_result = self.send_request("GetObject", {"qId": sheet_id}, handle=app_handle)
        sheet_handle = (sheet_result.get("qReturn") or {}).get("qHandle")
        if sheet_handle is None:
            raise QlikEngineError(
                f"No sheet {sheet_id!r} in this app: {sheet_result}")

        sheet_layout = self.send_request("GetLayout", [], handle=sheet_handle)
        child_list = (sheet_layout.get("qLayout") or {}).get("qChildList")
        if child_list is None:
            logger.info("Sheet %s has no child objects", sheet_id)
            return []

        try:
            child_objects = child_list.get("qItems", [])
            child_meta = [
                (co.get("qInfo", {}).get("qId", ""), co.get("qInfo", {}).get("qType", ""), co)
                for co in child_objects
            ]
            child_meta = [(obj_id, obj_type, co) for obj_id, obj_type, co in child_meta if obj_id]
            if not child_meta:
                return []

            # Wave 1: resolve every child object's handle in one pipelined
            # round-trip instead of N sequential GetObject calls.
            get_object_outcomes = self.send_requests_pipelined(
                [{"method": "GetObject", "params": {"qId": obj_id}, "handle": app_handle}
                 for obj_id, _obj_type, _co in child_meta],
                raise_on_error=False,
            )

            obj_handles: List[Optional[int]] = []
            for outcome in get_object_outcomes:
                if (isinstance(outcome, Exception)
                        or "qReturn" not in outcome or "qHandle" not in outcome["qReturn"]):
                    obj_handles.append(None)
                else:
                    obj_handles.append(outcome["qReturn"]["qHandle"])

            # Wave 2: fetch every resolved object's layout in one more
            # pipelined round-trip instead of N sequential GetLayout calls.
            layout_indices = [i for i, h in enumerate(obj_handles) if h is not None]
            layout_outcomes = self.send_requests_pipelined(
                [{"method": "GetLayout", "params": [], "handle": obj_handles[i]}
                 for i in layout_indices],
                raise_on_error=False,
            ) if layout_indices else []
            layout_by_index = dict(zip(layout_indices, layout_outcomes))

            # Wave 3: the layout does NOT carry measure expressions. Verified
            # against a live sheet: `qMeasureInfo` has
            # qFallbackTitle, formatting and statistics, but no `qDef` —
            # reading the expression from the layout found nothing on every
            # real object. The definition lives in the properties, under
            # qHyperCubeDef.qMeasures[].qDef.qDef.
            property_outcomes = self.send_requests_pipelined(
                [{"method": "GetProperties", "params": [], "handle": obj_handles[i]}
                 for i in layout_indices],
                raise_on_error=False,
            ) if layout_indices else []
            properties_by_index = dict(zip(layout_indices, property_outcomes))

            expressions_by_index = {
                i: self._object_expressions(properties_by_index.get(i))
                for i in layout_indices
            }
            # A filter pane holds no fields itself — it is a container whose
            # listbox children each carry one. Reading only the top level
            # reported every filter pane on every sheet as using no fields,
            # which is the opposite of what a filter pane is for.
            nested_fields = self._nested_object_fields(app_handle, layout_by_index)
            # Wave 4: master items carry a library id instead of a definition,
            # so resolve those in one more batch.
            self._resolve_library_items(app_handle, expressions_by_index)

            detailed_objects = []
            for i, (obj_id, obj_type, child_obj) in enumerate(child_meta):
                obj_layout = layout_by_index.get(i)
                if obj_layout is None:
                    continue  # GetObject failed for this child — same as the old `continue`
                if isinstance(obj_layout, Exception):
                    logger.warning(f"Error processing object {obj_id}: {obj_layout}")
                    continue
                if "qLayout" not in obj_layout:
                    continue
                try:
                    expressions = expressions_by_index.get(i) or {"measures": [], "dimensions": []}
                    fields_used = set(self._extract_fields_from_object(obj_layout["qLayout"]))
                    fields_used.update(nested_fields.get(i, []))
                    for measure in expressions["measures"]:
                        fields_used.update(
                            self._extract_fields_from_expression(measure.get("expression", "")))
                    for dimension in expressions["dimensions"]:
                        for field_def in dimension.get("fields", []):
                            name = self._extract_field_name_from_expression(field_def)
                            if name:
                                fields_used.add(name)
                            else:
                                fields_used.update(
                                    self._extract_fields_from_expression(field_def))
                    fields_used = sorted(fields_used)
                    detailed_obj = {
                        "object_id": obj_id,
                        "object_type": obj_type,
                        "object_title": obj_layout["qLayout"].get("title", ""),
                        "object_subtitle": obj_layout["qLayout"].get("subtitle", ""),
                        "fields_used": fields_used,
                        "measures": expressions["measures"],
                        "dimensions": expressions["dimensions"],
                        "basic_info": child_obj,
                        "detailed_layout": obj_layout["qLayout"]
                    }
                    detailed_objects.append(detailed_obj)
                    logger.info(f"Processed object {obj_id} ({obj_type}) with {len(fields_used)} fields")
                except Exception as obj_error:
                    logger.warning(f"Error processing object {obj_id}: {obj_error}")
                    continue

            return detailed_objects

        except Exception as e:
            logger.error("_get_sheet_objects_detailed error: %s", e)
            raise

    def _nested_object_fields(self, app_handle: int,
                              layout_by_index: Dict[int, Any]) -> Dict[int, List[str]]:
        """Fields used by the children of container objects.

        One level deep: that covers the filter pane, which is the container
        that matters and the only one commonly nested on a sheet. A
        container inside a container would need recursion, and buying that
        with two more round-trips per level is not worth it until something
        actually needs it.
        """
        wanted = []  # (parent_index, child_id)
        for index, layout in layout_by_index.items():
            if isinstance(layout, Exception) or not isinstance(layout, dict):
                continue
            children = ((layout.get("qLayout") or {}).get("qChildList") or {}).get("qItems") or []
            for child in children:
                child_id = ((child or {}).get("qInfo") or {}).get("qId")
                if child_id:
                    wanted.append((index, child_id))
        if not wanted:
            return {}

        try:
            handle_outcomes = self.send_requests_pipelined(
                [{"method": "GetObject", "params": {"qId": child_id}, "handle": app_handle}
                 for _parent, child_id in wanted],
                raise_on_error=False,
            )
        except Exception as exc:
            logger.debug("Could not resolve nested objects: %s", exc)
            return {}

        handles = []
        for outcome in handle_outcomes:
            if isinstance(outcome, Exception):
                handles.append(None)
                continue
            handles.append(((outcome or {}).get("qReturn") or {}).get("qHandle"))

        positions = [i for i, handle in enumerate(handles) if handle is not None]
        try:
            layouts = self.send_requests_pipelined(
                [{"method": "GetLayout", "params": [], "handle": handles[i]}
                 for i in positions],
                raise_on_error=False,
            ) if positions else []
        except Exception as exc:
            logger.debug("Could not read nested object layouts: %s", exc)
            return {}

        by_parent: Dict[int, List[str]] = {}
        for position, layout in zip(positions, layouts):
            if isinstance(layout, Exception) or not isinstance(layout, dict):
                continue
            parent_index = wanted[position][0]
            fields = self._extract_fields_from_object(layout.get("qLayout") or {})
            if fields:
                by_parent.setdefault(parent_index, []).extend(fields)
        return by_parent

    @staticmethod
    def _object_expressions(properties: Any) -> Dict[str, List[Dict[str, Any]]]:
        """Measure and dimension definitions from an object's properties.

        Returns them in the shape a reader wants — expression plus label —
        rather than Qlik's doubly-nested `qDef.qDef`. A master item has no
        expression here, only `qLibraryId`; those are filled in afterwards
        by `_resolve_library_items`.
        """
        empty: Dict[str, List[Dict[str, Any]]] = {"measures": [], "dimensions": []}
        if isinstance(properties, Exception) or not isinstance(properties, dict):
            return empty
        cube = ((properties.get("qProp") or {}).get("qHyperCubeDef")) or {}
        if not isinstance(cube, dict):
            return empty

        measures = []
        for measure in cube.get("qMeasures", []) or []:
            if not isinstance(measure, dict):
                continue
            inner = measure.get("qDef") or {}
            measures.append({
                "expression": (inner.get("qDef") or "") if isinstance(inner, dict) else "",
                "label": (inner.get("qLabel") or "") if isinstance(inner, dict) else "",
                "library_id": measure.get("qLibraryId")
                              or (inner.get("qLibraryId") if isinstance(inner, dict) else None),
            })

        dimensions = []
        for dimension in cube.get("qDimensions", []) or []:
            if not isinstance(dimension, dict):
                continue
            inner = dimension.get("qDef") or {}
            dimensions.append({
                "fields": (inner.get("qFieldDefs") or []) if isinstance(inner, dict) else [],
                "label": ((inner.get("qFieldLabels") or [""])[0]
                          if isinstance(inner, dict) and inner.get("qFieldLabels") else ""),
                "library_id": dimension.get("qLibraryId")
                              or (inner.get("qLibraryId") if isinstance(inner, dict) else None),
            })

        return {"measures": measures, "dimensions": dimensions}

    def _resolve_library_items(self, app_handle: int,
                               expressions_by_index: Dict[int, Dict[str, Any]]) -> None:
        """Fill in the expressions of master measures and dimensions.

        A chart built from the library stores only `qLibraryId`, so without
        this step exactly the charts a modeller took the trouble to
        standardise are the ones reporting no fields.
        """
        wanted_measures, wanted_dimensions = [], []
        for entry in expressions_by_index.values():
            for measure in entry["measures"]:
                if measure.get("library_id") and not measure.get("expression"):
                    wanted_measures.append(measure["library_id"])
            for dimension in entry["dimensions"]:
                if dimension.get("library_id") and not dimension.get("fields"):
                    wanted_dimensions.append(dimension["library_id"])

        wanted_measures = list(dict.fromkeys(wanted_measures))
        wanted_dimensions = list(dict.fromkeys(wanted_dimensions))
        if not wanted_measures and not wanted_dimensions:
            return

        try:
            handles = self.send_requests_pipelined(
                [{"method": "GetMeasure", "params": {"qId": lib_id}, "handle": app_handle}
                 for lib_id in wanted_measures]
                + [{"method": "GetDimension", "params": {"qId": lib_id}, "handle": app_handle}
                   for lib_id in wanted_dimensions],
                raise_on_error=False,
            )
        except Exception as exc:
            logger.debug("Could not resolve master items: %s", exc)
            return

        resolved_handles = []
        for outcome in handles:
            if isinstance(outcome, Exception):
                resolved_handles.append(None)
                continue
            resolved_handles.append(((outcome or {}).get("qReturn") or {}).get("qHandle"))

        layout_indices = [i for i, h in enumerate(resolved_handles) if h is not None]
        try:
            layouts = self.send_requests_pipelined(
                [{"method": "GetLayout", "params": [], "handle": resolved_handles[i]}
                 for i in layout_indices],
                raise_on_error=False,
            ) if layout_indices else []
        except Exception as exc:
            logger.debug("Could not read master item layouts: %s", exc)
            return

        measure_expressions: Dict[str, str] = {}
        dimension_fields: Dict[str, List[str]] = {}
        for position, layout in zip(layout_indices, layouts):
            if isinstance(layout, Exception):
                continue
            body = (layout or {}).get("qLayout") or {}
            if position < len(wanted_measures):
                lib_id = wanted_measures[position]
                measure_expressions[lib_id] = ((body.get("qMeasure") or {}).get("qDef") or "")
            else:
                lib_id = wanted_dimensions[position - len(wanted_measures)]
                dimension_fields[lib_id] = ((body.get("qDim") or {}).get("qFieldDefs") or [])

        for entry in expressions_by_index.values():
            for measure in entry["measures"]:
                lib_id = measure.get("library_id")
                if lib_id and not measure.get("expression"):
                    measure["expression"] = measure_expressions.get(lib_id, "")
            for dimension in entry["dimensions"]:
                lib_id = dimension.get("library_id")
                if lib_id and not dimension.get("fields"):
                    dimension["fields"] = dimension_fields.get(lib_id, [])

    def _extract_fields_from_object(self, obj_layout: Dict[str, Any]) -> List[str]:
        """Extract field names used in an object layout.

        A hypercube carries a *list* of qDimensionInfo, a list object exactly
        one — Engine's own shapes, not a quirk of some charts. Iterating the
        list object's as a list walked the dict's keys and raised
        `'str' object has no attribute 'get'`, which the catch-all below
        turned into a warning and an empty field list. Filter panes are the
        objects built on list objects, so every filter on every sheet came
        back as "uses no fields".
        """
        fields = set()

        def add_dimension(dim_info: Any) -> None:
            if not isinstance(dim_info, dict):
                return
            for field_def in dim_info.get("qGroupFieldDefs", []):
                field_name = self._extract_field_name_from_expression(field_def)
                if field_name:
                    fields.add(field_name)
                else:
                    # A calculated dimension, e.g. "=Year(OrderDate)": the
                    # simple form finds nothing, but the fields inside it are
                    # exactly what the caller asked about.
                    fields.update(self._extract_fields_from_expression(field_def))

        try:
            hypercube = obj_layout.get("qHyperCube")
            if isinstance(hypercube, dict):
                for dim_info in hypercube.get("qDimensionInfo", []):
                    add_dimension(dim_info)
                for measure_info in hypercube.get("qMeasureInfo", []):
                    if isinstance(measure_info, dict):
                        fields.update(
                            self._extract_fields_from_expression(measure_info.get("qDef", ""))
                        )

            list_obj = obj_layout.get("qListObject")
            if isinstance(list_obj, dict):
                dim_info = list_obj.get("qDimensionInfo")
                # Tolerate a list too: some object types nest several.
                for entry in (dim_info if isinstance(dim_info, list) else [dim_info]):
                    add_dimension(entry)

        except Exception as e:
            logger.warning(f"Error extracting fields from object: {e}")

        return list(fields)

    def _extract_field_name_from_expression(self, expression: str) -> Optional[str]:
        """Extract field name from a simple field expression."""
        if not expression:
            return None
        expression = expression.strip()
        if expression.startswith('[') and expression.endswith(']') and expression.count('[') == 1:
            return expression[1:-1]
        if ' ' not in expression and '(' not in expression and not any(op in expression for op in ['=', '+', '-', '*', '/']):
            return expression
        return None

    def _extract_fields_from_expression(self, expression: str) -> List[str]:
        """Extract field names from a Qlik expression.

        Bracketed names are unambiguous. Bare ones are not, so the rule is
        structural: an identifier followed by `(` is a function call, and
        everything else that is not a keyword is taken as a field. Matching
        only `[...]` — as this did — meant `Sum(Sales)` reported no fields at
        all, and most real measures are written without brackets.

        This is a lexical guess, not a parser: an unusual function name would
        be reported as a field. That is the tolerable direction of error for
        "which fields does this object touch".
        """
        import re

        if not expression:
            return []

        fields = set(re.findall(r"\[([^\]]+)\]", expression))

        text = expression
        # Dollar expansion is resolved by Qlik before evaluation; the name
        # inside is a variable, not a field.
        text = re.sub(r"\$\([^)]*\)", " ", text)
        # Both quote styles are values inside a set modifier: single quotes
        # are a literal, double quotes are a *search* —
        # `Country={"New Zealand"}` matched "New" and "Zealand" as fields
        # until this line existed.
        text = re.sub(r"'[^']*'", " ", text)
        text = re.sub(r'"[^"]*"', " ", text)
        # Bracketed names, already collected above.
        text = re.sub(r"\[[^\]]*\]", " ", text)

        # Field names are routinely non-Latin — `Год` and `Месяц` are the
        # dimensions of a chart on the stand this was measured against — so
        # the identifier pattern has to be Unicode-aware rather than A-Za-z.
        for match in re.finditer(r"(?<![\w.])(\w[\w.]*)\s*(\()?", text, re.UNICODE):
            name, is_call = match.group(1), match.group(2)
            if is_call or name.lower() in _EXPRESSION_KEYWORDS:
                continue
            # A name cannot start with a digit in an unbracketed reference,
            # so anything that does is a literal — including the scientific
            # notation `1e3`, which was being reported as a field.
            if name[0].isdigit():
                continue
            fields.add(name)

        return sorted(fields)

