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

# A calculated dimension can run to pages; the line that names it only has
# to say what it is.
MASTER_DIMENSION_EXPRESSION_CHARS = 150


def describe_master_dimension(name: str, field_defs: Any, field_labels: Any,
                              grouping: Any) -> Dict[str, Any]:
    """A master dimension as one line: `name = what it is built from`.

    Qlik stores the fields in `qFieldDefs` and how they combine in
    `qGrouping`: "N" a single field or expression, "H" a drill-down whose
    levels are read top to bottom, "C" a cyclic group the user switches
    between. A definition starting with "=" is an expression, not a field.
    A level label that differs from its field is kept as `[label]=field`,
    since the label is the word people use for it.
    """
    defs = [str(d).strip() for d in (field_defs or []) if str(d or "").strip()]
    labels = list(field_labels or [])
    levels = []
    for index, field in enumerate(defs):
        label = str(labels[index]).strip() if index < len(labels) and labels[index] else ""
        levels.append(f"[{label}]={field}" if label and label != field and len(defs) > 1
                      else field)

    result: Dict[str, Any] = {}
    kind = str(grouping or "N").upper()[:1]
    if kind == "H" and len(defs) > 1:
        body, suffix = " > ".join(levels), " (drill-down)"
    elif kind == "C" and len(defs) > 1:
        body, suffix = " | ".join(levels), " (cyclic)"
    elif defs and defs[0].startswith("="):
        body, suffix = " ".join(defs[0].split()), " (calculated)"
        if len(body) > MASTER_DIMENSION_EXPRESSION_CHARS:
            body = body[:MASTER_DIMENSION_EXPRESSION_CHARS] + "..."
            result["expression_truncated"] = True
    else:
        body, suffix = ", ".join(levels), ""
    result["definition"] = f"{name} = {body}{suffix}" if body else name
    if defs and not defs[0].startswith("="):
        result["fields"] = defs
    return result


def _plain_text(value: Any) -> str:
    """A title as stored: literal text, or the expression that computes it.

    The expression is not evaluated - that would be a calculation - so a
    computed title comes back as `=...`, which still says what it shows.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        expr = (value.get("qStringExpression") or {}).get("qExpr")
        if isinstance(expr, str) and expr.strip():
            text = expr.strip()
            return text if text.startswith("=") else f"={text}"
    return ""


def _list_object_dimension(prop: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The one field a listbox (or date picker, or filter) is built on."""
    list_def = prop.get("qListObjectDef")
    if not isinstance(list_def, dict):
        return None
    inner = list_def.get("qDef") or {}
    fields = [f for f in (inner.get("qFieldDefs") or []) if f]
    library_id = list_def.get("qLibraryId") or inner.get("qLibraryId")
    if not fields and not library_id:
        return None
    labels = inner.get("qFieldLabels") or []
    return {"fields": fields, "label": (labels[0] or "") if labels else "",
            "library_id": library_id or None}


def _calculation_context(prop: Dict[str, Any]) -> Dict[str, Any]:
    """What makes an object's numbers differ from the main selection.

    An alternate state, a condition that hides the calculation, a set
    expression applied to the whole cube: each changes what a figure means,
    and none of them is visible in the measure expressions.
    """
    found: Dict[str, Any] = {}
    state = prop.get("qStateName")
    if isinstance(state, str) and state not in ("", "$"):
        found["state"] = state
    cube = prop.get("qHyperCubeDef") or {}
    if isinstance(cube, dict):
        # qCalcCondition is the current form, qCalcCond the older one.
        condition, text = cube.get("qCalcCondition"), None
        if isinstance(condition, dict):
            text = (condition.get("qCond") or {}).get("qv")
        if not text and isinstance(cube.get("qCalcCond"), dict):
            text = cube["qCalcCond"].get("qv")
        if isinstance(text, str) and text.strip() not in ("", "1", "=1"):
            found["calc_condition"] = text.strip()
        context_set = cube.get("qContextSetExpression")
        if isinstance(context_set, str) and context_set.strip():
            found["context_set"] = context_set.strip()
    return found


def _all_expressions(objects: Dict[int, Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    """Every object and nested child, indexed, for one library resolution."""
    flat: Dict[int, Dict[str, Any]] = {}

    def walk(obj: Dict[str, Any]) -> None:
        flat[len(flat)] = obj
        for child in obj.get("children") or []:
            walk(child)

    for obj in objects.values():
        walk(obj)
    return flat


def _fill_fields_used(reader: Any, obj: Dict[str, Any]) -> List[str]:
    """Set `fields_used` on an object and its children; a container's
    fields include those of everything inside it."""
    fields = set()
    for measure in obj.get("measures") or []:
        fields.update(reader._extract_fields_from_expression(measure.get("expression", "")))
    for dimension in obj.get("dimensions") or []:
        for field_def in dimension.get("fields") or []:
            text = str(field_def or "").strip()
            if text.startswith("="):
                fields.update(reader._extract_fields_from_expression(text))
            elif text:
                # Without "=" a qFieldDefs entry is a field name, spaces and
                # all; read as an expression, `Sport (detail)` became the
                # fields "Sport" and "detail".
                fields.add(text[1:-1] if text.startswith("[") and text.endswith("]") else text)
    for child in obj.get("children") or []:
        fields.update(_fill_fields_used(reader, child))
    obj["fields_used"] = sorted(fields)
    return obj["fields_used"]


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

        A dimension has no `qDef` expression; its body is `qFieldDefs`,
        `qFieldLabels` and `qGrouping`. Reading `/qDim/qDef` returned an
        empty expression for every dimension, and the name people see
        ("Trader") often differs from the field it stands for
        (`TraderName`), so the model could not connect the two. Each
        dimension therefore carries a `definition` line: `name = field`.
        """
        wanted = [
            ("measures", "MeasureList", "qMeasureListDef", "qMeasureList",
             {"expression": "/qMeasure/qDef"}),
            ("dimensions", "DimensionList", "qDimensionListDef",
             "qDimensionList", {"field_defs": "/qDim/qFieldDefs",
                                "field_labels": "/qDim/qFieldLabels",
                                "grouping": "/qDim/qGrouping"}),
        ]
        library: Dict[str, Any] = {"measures": [], "dimensions": []}
        for key, kind, definition_key, layout_key, body_paths in wanted:
            object_id = f"library-{key}"
            definition = {
                "qInfo": {"qId": object_id, "qType": kind},
                definition_key: {
                    "qType": key[:-1],
                    "qData": {"title": "/qMetaDef/title",
                              "description": "/qMetaDef/description",
                              "tags": "/qMetaDef/tags",
                              **body_paths},
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
                    "id": (item.get("qInfo") or {}).get("qId", ""),
                }
                if key == "measures":
                    entry["expression"] = str(data.get("expression") or "").strip()
                else:
                    entry.update(describe_master_dimension(
                        entry["name"], data.get("field_defs"),
                        data.get("field_labels"), data.get("grouping")))
                description = (data.get("description")
                               or meta.get("description") or "").strip()
                if description:
                    entry["description"] = description
                tags = [str(tag) for tag in (data.get("tags")
                                             or meta.get("tags") or []) if tag]
                if tags:
                    entry["tags"] = tags
                if entry["name"] or entry.get("expression") or entry.get("definition"):
                    library[key].append(entry)
        return library

    def _get_sheet_objects_detailed(self, app_handle: int, sheet_id: str) -> List[Dict[str, Any]]:
        """The objects on a sheet, read from their definitions alone.

        Nothing here is calculated. `GetLayout` on a chart makes Engine
        compute it, and on a large app a sheet read that way ran past 300
        seconds - the client gave up and dropped the MCP session. The
        definitions carry everything this reply needs, and more: the
        measure expressions, `qLibraryId`, state, calculation condition and
        set expression exist only in the properties, never in the layout.

        `GetFullPropertyTree` on the sheet returns every object on it and
        the children of every container in one request. Objects linked to a
        master visualization hold only `qExtendsId`; their definitions come
        from the master's properties, read in one more batch.
        """
        # A failure to reach the sheet itself is reported; a failure on one
        # of its objects is not, because the other objects are still worth
        # returning.
        sheet_result = self.send_request("GetObject", {"qId": sheet_id}, handle=app_handle)
        sheet_handle = (sheet_result.get("qReturn") or {}).get("qHandle")
        if sheet_handle is None:
            raise QlikEngineError(
                f"No sheet {sheet_id!r} in this app: {sheet_result}")

        tree = (self.send_request("GetFullPropertyTree", [], handle=sheet_handle)
                or {}).get("qPropEntry") or {}
        children = [c for c in tree.get("qChildren") or [] if isinstance(c, dict)]
        if not children:
            logger.info("Sheet %s has no child objects", sheet_id)
            return []

        masters = self._master_visualizations(app_handle, children)
        objects = []
        for node in children:
            try:
                described = self._describe_object(node, masters, depth=0, seen=set())
            except Exception as exc:
                logger.warning("Could not describe an object on sheet %s: %s", sheet_id, exc)
                continue
            if described:
                objects.append(described)

        by_index = {i: obj for i, obj in enumerate(objects)}
        self._resolve_library_items(app_handle, _all_expressions(by_index))
        for obj in objects:
            _fill_fields_used(self, obj)
        return objects

    # Container children are read to this depth. Deeper nesting exists in
    # theory; in practice a sheet does not go past a container of tabs
    # holding a filter pane.
    _MAX_CONTAINER_DEPTH = 3

    def _master_visualizations(self, app_handle: int,
                               nodes: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """Properties of the master visualizations the given objects link to.

        Keyed by master id. Two pipelined batches whatever the number of
        linked objects; a master that cannot be read is simply absent, and
        its linked object is reported with what it has.
        """
        wanted: List[str] = []

        def collect(node: Dict[str, Any], depth: int) -> None:
            prop = node.get("qProperty") or {}
            extends = prop.get("qExtendsId")
            if extends and extends not in wanted:
                wanted.append(extends)
            if depth < self._MAX_CONTAINER_DEPTH:
                for child in node.get("qChildren") or []:
                    if isinstance(child, dict):
                        collect(child, depth + 1)

        for node in nodes:
            collect(node, 0)
        if not wanted:
            return {}
        try:
            opened = self.send_requests_pipelined(
                [{"method": "GetObject", "params": {"qId": master_id}, "handle": app_handle}
                 for master_id in wanted],
                raise_on_error=False)
            handles = [None if isinstance(o, Exception)
                       else ((o or {}).get("qReturn") or {}).get("qHandle")
                       for o in opened]
            positions = [i for i, handle in enumerate(handles) if handle is not None]
            properties = self.send_requests_pipelined(
                [{"method": "GetProperties", "params": [], "handle": handles[i]}
                 for i in positions],
                raise_on_error=False) if positions else []
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                raise
            logger.debug("Could not read master visualizations: %s", exc)
            return {}
        masters = {}
        for position, outcome in zip(positions, properties):
            if isinstance(outcome, dict) and isinstance(outcome.get("qProp"), dict):
                masters[wanted[position]] = outcome["qProp"]
        return masters

    def _describe_object(self, node: Dict[str, Any], masters: Dict[str, Dict[str, Any]],
                         depth: int, seen: set) -> Optional[Dict[str, Any]]:
        """One object from its property tree node: what it shows and how."""
        prop = node.get("qProperty") or {}
        info = prop.get("qInfo") or {}
        object_id = info.get("qId", "")
        if not object_id or object_id in seen:
            return None
        seen.add(object_id)

        master_id = prop.get("qExtendsId")
        definition = prop
        if master_id and master_id in masters:
            # The linked object's own properties are an empty shell; what
            # it shows is the master's. Its own title, when set, still wins.
            definition = {**masters[master_id],
                          **{k: v for k, v in prop.items() if v not in (None, "", {}, [])}}
        object_type = (info.get("qType") or definition.get("visualization")
                       or (definition.get("qInfo") or {}).get("qType") or "")
        expressions = self._object_expressions({"qProp": definition})
        list_dimension = _list_object_dimension(definition)
        if list_dimension:
            expressions["dimensions"].append(list_dimension)

        described: Dict[str, Any] = {
            "object_id": object_id,
            "object_type": object_type,
            "object_title": _plain_text(definition.get("title"))
                            or _plain_text((definition.get("qMetaDef") or {}).get("title")),
            "measures": expressions["measures"],
            "dimensions": expressions["dimensions"],
        }
        subtitle = _plain_text(definition.get("subtitle"))
        if subtitle:
            described["object_subtitle"] = subtitle
        if master_id:
            described["master_object"] = master_id
        described.update(_calculation_context(definition))

        nested = []
        if depth < self._MAX_CONTAINER_DEPTH:
            for child in node.get("qChildren") or []:
                if not isinstance(child, dict):
                    continue
                child_described = self._describe_object(child, masters, depth + 1, seen)
                if not child_described:
                    continue
                if (child_described["object_type"] == "listbox"
                        and not child_described["measures"]):
                    # A filter pane is its listboxes: their fields are what
                    # it filters by, so they belong to the pane itself.
                    described["dimensions"].extend(child_described["dimensions"])
                else:
                    nested.append(child_described)
        if nested:
            described["children"] = nested
        return described

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

        # The definition, not the layout: a master dimension's layout makes
        # Engine count its values, and the definition is all that is used.
        layout_indices = [i for i, h in enumerate(resolved_handles) if h is not None]
        try:
            layouts = self.send_requests_pipelined(
                [{"method": "GetProperties", "params": [], "handle": resolved_handles[i]}
                 for i in layout_indices],
                raise_on_error=False,
            ) if layout_indices else []
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                raise
            logger.debug("Could not read master item definitions: %s", exc)
            return

        measure_expressions: Dict[str, str] = {}
        dimension_fields: Dict[str, List[str]] = {}
        for position, layout in zip(layout_indices, layouts):
            if isinstance(layout, Exception):
                continue
            body = (layout or {}).get("qProp") or {}
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

