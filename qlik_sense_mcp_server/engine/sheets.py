"""Sheets and the objects on them, plus the fields those objects use.

How each type of object is read lives in `objects.py`; this module fetches
what the parser needs from Engine - definitions, never layouts - and
computes an object's data on request.
"""

from typing import Dict, List, Any, Optional, Tuple
import logging

from ..exceptions import QlikEngineError
from .objects import (CONTAINER_TYPES, MAX_CONTAINER_DEPTH, Library, ObjectParser,
                      fields_in_expression, fields_used, library_ids, master_references,
                      tree_references)

logger = logging.getLogger(__name__)

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


def _first_def(values: Any) -> str:
    defs = [str(v).strip() for v in values or [] if str(v or "").strip()]
    return defs[0] if defs else ""


def _cell_order(sheet: Dict[str, Any], children: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The sheet's own objects, in reading order.

    `cells` names what stands on the sheet; the tree also holds the
    children of containers and objects left over from earlier edits. A
    sheet without cells (as some extensions build them) keeps every child.
    """
    cells = [c for c in sheet.get("cells") or [] if isinstance(c, dict) and c.get("name")]
    if not cells:
        return children
    by_id = {(c.get("qProperty") or {}).get("qInfo", {}).get("qId"): c for c in children}
    cells.sort(key=lambda c: (c.get("row") or 0, c.get("col") or 0))
    return [by_id[c["name"]] for c in cells if c["name"] in by_id]


def _walk_nodes(nodes: List[Dict[str, Any]], depth: int = 0):
    for node in nodes:
        yield node
        if depth < MAX_CONTAINER_DEPTH:
            yield from _walk_nodes([c for c in node.get("qChildren") or []
                                    if isinstance(c, dict)], depth + 1)


# Keys of a computed result that belong in an object's `data`.
_DATA_KEYS = ("columns", "rows", "total_rows", "returned_rows", "grand_total", "sorted_by",
              "sort_order", "has_more", "next_offset", "truncation_warning", "warnings")

# Why an object that computes nothing has no data, and where to look instead.
_NO_DATA_NOTE = {
    "filterpane": "A filter pane computes no data; read the values of its fields with get_app_field.",
    "listbox": "A listbox computes no data; read the values of its field with get_app_field.",
    "qlik-date-picker": "A date picker computes no data; read its field with get_app_field.",
    "VizlibFilter": "A filter computes no data; read the values of its field with get_app_field.",
    "VizlibCalendar": "A calendar computes no data; read its field with get_app_field.",
    "container": "A container computes no data; read its children by their object_id.",
    "sn-tabbed-container": "A container computes no data; read its children by their object_id.",
    "sn-layout-container": "A container computes no data; read its children by their object_id.",
    "VizlibContainer": "A container computes no data; read its master visualizations by id.",
    "qlik-trellis-container": "A trellis repeats a master visualization; read that one by id.",
}



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
                elif not isinstance(data.get("field_defs"), list):
                    # A list's qData resolves a path to a scalar only: an
                    # array such as qFieldDefs comes back as "" (seen on
                    # Qlik Sense November 2025). The body is read from the
                    # dimension's own properties below.
                    entry["_unread"] = True
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
                if entry["name"] or entry.get("expression") or entry.get("definition") \
                        or entry.get("_unread"):
                    library[key].append(entry)
        unread = [e for e in library["dimensions"] if e.pop("_unread", False)]
        if unread:
            read = {e["id"]: e for e in self._library_items_by_id(
                app_handle, [e["id"] for e in unread], kinds=("dimensions",))["dimensions"]}
            for entry in unread:
                body = read.get(entry["id"]) or {}
                entry.update({k: v for k, v in body.items()
                              if k in ("definition", "fields", "expression_truncated")})
                entry.setdefault("definition", entry["name"])
        return library

    def approved_bookmarks(self, app_handle: int) -> List[Dict[str, str]]:
        """The bookmarks the app's author shipped with it, and no others.

        An approved bookmark is part of the published app, like a base
        sheet; the rest are what users saved for themselves or shared
        with each other - on one live app 698 of them, half named by a
        GUID. Only title and description are read.
        """
        definition = {
            "qInfo": {"qId": "approved-bookmarks", "qType": "BookmarkList"},
            "qBookmarkListDef": {"qType": "bookmark",
                                 "qData": {"title": "/qMetaDef/title",
                                           "description": "/qMetaDef/description"}},
        }
        with self.session_object(app_handle, definition) as handle:
            layout = (self.send_request("GetLayout", [], handle=handle) or {}).get("qLayout") or {}
        found = []
        for item in (layout.get("qBookmarkList") or {}).get("qItems") or []:
            meta, data = item.get("qMeta") or {}, item.get("qData") or {}
            if not meta.get("approved"):
                continue
            title = str(data.get("title") or meta.get("title") or "").strip()
            if not title:
                continue
            entry = {"id": (item.get("qInfo") or {}).get("qId", ""), "title": title}
            description = str(data.get("description") or meta.get("description") or "").strip()
            if description:
                entry["description"] = description
            found.append(entry)
        return found

    def published_sheets(self, app_handle: int) -> Tuple[List[Dict[str, Any]], str]:
        """The sheets the app's author made, with their objects described.

        In a published app those are the approved (base) sheets; sheets
        users made for themselves or shared are not read. An app that is
        not published has no approved sheets, and every sheet in it is its
        owner's, so all of them are read. Definitions only, as for
        `get_app_sheet_objects`: nothing is calculated.
        """
        sheets = self.get_sheets(app_handle)
        approved = [s for s in sheets if (s.get("qMeta") or {}).get("approved")]
        chosen = approved or sheets
        read = []
        for sheet in chosen:
            sheet_id = (sheet.get("qInfo") or {}).get("qId")
            if not sheet_id:
                continue
            try:
                objects = self._get_sheet_objects_detailed(app_handle, sheet_id)
            except Exception as exc:
                if isinstance(exc, TimeoutError):
                    raise
                logger.warning("Could not read sheet %s: %s", sheet_id, exc)
                continue
            read.append({"id": sheet_id,
                         "title": (sheet.get("qMeta") or {}).get("title")
                         or (sheet.get("qData") or {}).get("title") or sheet_id,
                         "objects": objects})
        return read, "approved sheets" if approved else "all sheets (none is approved)"

    def _get_sheet_objects_detailed(self, app_handle: int, sheet_id: str) -> List[Dict[str, Any]]:
        """The objects on a sheet, read from their definitions alone.

        Nothing here is calculated. `GetLayout` on a chart makes Engine
        compute it, and on a large app a sheet read that way ran past 300
        seconds - the client gave up and dropped the MCP session. The
        definitions carry everything this reply needs, and more: the
        measure expressions, `qLibraryId`, state, calculation condition and
        set expression exist only in the properties, never in the layout.

        `GetFullPropertyTree` on the sheet returns every object on it and
        the children of every container in one request. The walk starts
        from the sheet's `cells`: a container's children are in the tree
        but not in the cells, and they are reported inside their container
        rather than as objects of the sheet. Master visualizations and
        master items the objects point at are read in batches, once per
        app.
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
        top = _cell_order(tree.get("qProperty") or {}, children)

        parser = self._object_parser(app_handle, top)
        objects, seen = [], set()
        for node in top:
            try:
                described = parser.describe_node(node, 0, seen)
            except Exception as exc:
                if isinstance(exc, TimeoutError):
                    raise
                logger.warning("Could not describe an object on sheet %s: %s", sheet_id, exc)
                continue
            if described:
                described["fields_used"] = fields_used(described)
                objects.append(described)
        return objects

    def describe_app_object(self, app_handle: int, object_id: str) -> Optional[Dict[str, Any]]:
        """One object, read from its definition; None when Engine will not
        open it.

        A linked object's own properties hold only `qExtendsId`, so it is
        read with `GetEffectiveProperties` - the master's properties merged
        in, still without calculation. A container or filter pane is read
        with `GetFullPropertyTree` for its children.
        """
        opened = self.send_request("GetObject", {"qId": object_id}, handle=app_handle)
        handle = (opened.get("qReturn") or {}).get("qHandle")
        if handle is None:
            return None
        props = (self.send_request("GetProperties", [], handle=handle) or {}).get("qProp") or {}
        node: Dict[str, Any] = {"qProperty": props, "qChildren": []}
        kind = (props.get("qInfo") or {}).get("qType", "")
        if kind in CONTAINER_TYPES or kind == "filterpane":
            tree = (self.send_request("GetFullPropertyTree", [], handle=handle)
                    or {}).get("qPropEntry") or {}
            node = {"qProperty": tree.get("qProperty") or props,
                    "qChildren": tree.get("qChildren") or []}
        effective = {}
        if props.get("qExtendsId"):
            merged = (self.send_request("GetEffectiveProperties", [], handle=handle)
                      or {}).get("qProp")
            if isinstance(merged, dict):
                effective[object_id] = merged
                node["qProperty"] = {**merged, **{k: v for k, v in props.items()
                                                  if k in ("qInfo", "qExtendsId")}}
        parser = self._object_parser(app_handle, [node], effective)
        described = parser.describe_node(node)
        if described is None:
            kind = parser._kind(kind, node["qProperty"])
            described = {"object_id": object_id, "object_type": kind,
                         "summary": f"{kind}: not a visualization shown to users"}
        described["fields_used"] = fields_used(described)
        return described

    def _object_parser(self, app_handle: int, nodes: List[Dict[str, Any]],
                       effective: Optional[Dict[str, Dict[str, Any]]] = None) -> ObjectParser:
        """A parser that knows everything the given objects point at."""
        master_ids: List[str] = []
        item_ids: List[str] = []
        for node in nodes:
            more_masters, more_items = tree_references(node)
            master_ids += [m for m in more_masters if m not in master_ids]
            item_ids += [i for i in more_items if i not in item_ids]
        masters = self._master_visualizations(app_handle, master_ids)
        for props in masters.values():
            item_ids += [i for i in library_ids(props) if i not in item_ids]
        if effective is None:
            effective = self._effective_properties(
                app_handle, [n for n in _walk_nodes(nodes)
                             if (n.get("qProperty") or {}).get("qExtendsId")
                             and n["qProperty"]["qExtendsId"] not in masters])
        library = Library(self._library_with(app_handle, item_ids), masters, effective)

        titles: Dict[str, Optional[Dict[str, str]]] = {"sheets": None}

        def sheet_title(sheet_id: str) -> Optional[str]:
            if titles["sheets"] is None:
                try:
                    titles["sheets"] = {
                        (s.get("qInfo") or {}).get("qId", ""): (s.get("qMeta") or {}).get("title", "")
                        for s in self.get_sheets(app_handle)}
                except Exception as exc:
                    if isinstance(exc, TimeoutError):
                        raise
                    logger.debug("Could not read sheet titles: %s", exc)
                    titles["sheets"] = {}
            return titles["sheets"].get(sheet_id) or None

        return ObjectParser(library, sheet_title=sheet_title,
                            map_layers=lambda map_id: self._map_layers(app_handle).get(map_id, []))

    def _app_cache(self, name: str) -> Dict[str, Any]:
        """A per-app store on this client, keyed like the library cache:
        by the app, never by the handle, which Engine reuses across apps."""
        store = self.__dict__.setdefault(name, {})
        identity = getattr(self, "_cached_app_id", None) or ""
        return store.setdefault(identity, {})

    def _read_properties(self, app_handle: int, object_ids: List[str],
                         method: str = "GetProperties") -> Dict[str, Dict[str, Any]]:
        """Properties of several objects in two pipelined batches; an
        object that cannot be read is simply absent."""
        if not object_ids:
            return {}
        try:
            opened = self.send_requests_pipelined(
                [{"method": "GetObject", "params": {"qId": object_id}, "handle": app_handle}
                 for object_id in object_ids],
                raise_on_error=False)
            handles = [None if isinstance(o, Exception)
                       else ((o or {}).get("qReturn") or {}).get("qHandle")
                       for o in opened]
            positions = [i for i, handle in enumerate(handles) if handle is not None]
            replies = self.send_requests_pipelined(
                [{"method": method, "params": [], "handle": handles[i]} for i in positions],
                raise_on_error=False) if positions else []
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                raise
            logger.debug("Could not read %s of %d objects: %s", method, len(object_ids), exc)
            return {}
        found = {}
        for position, reply in zip(positions, replies):
            if isinstance(reply, dict) and isinstance(reply.get("qProp"), dict):
                found[object_ids[position]] = reply["qProp"]
        return found

    def _master_visualizations(self, app_handle: int,
                               master_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """Properties of master visualizations by id, remembered per app.

        One master often stands on several sheets; it is read once. A
        master can itself show masters (a container of master tabs), so
        what it points at is read too, one level more.
        """
        cache = self._app_cache("_master_viz_cache")
        wanted = [m for m in master_ids if m and m not in cache]
        for _round in range(2):
            if not wanted:
                break
            read = self._read_properties(app_handle, wanted)
            for master_id in wanted:
                cache[master_id] = read.get(master_id)
            wanted = [m for props in read.values() for m in master_references(props)
                      if m not in cache]
        found = {}
        pending = list(master_ids)
        while pending:
            master_id = pending.pop()
            if master_id in found or cache.get(master_id) is None:
                continue
            found[master_id] = cache[master_id]
            pending += master_references(cache[master_id])
        return found

    def _effective_properties(self, app_handle: int,
                              nodes: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """Merged properties of linked objects whose master could not be
        read on its own - still definitions, never a layout."""
        ids = [(n.get("qProperty") or {}).get("qInfo", {}).get("qId") for n in nodes]
        return self._read_properties(app_handle, [i for i in ids if i],
                                     method="GetEffectiveProperties")

    def _library_with(self, app_handle: int, item_ids: List[str]) -> Dict[str, Any]:
        """The app library, plus any item it did not list, read by id.

        The list comes from one session object per app. An id missing from
        it is asked for both as a measure and as a dimension, since the
        property it was found under does not always say which it is.
        """
        if not item_ids:
            return {}
        library = self._library_for(app_handle)
        known ={e.get("id") for e in library.get("measures", []) + library.get("dimensions", [])}
        missing = [i for i in item_ids if i not in known]
        if not missing:
            return library
        extra = self._library_items_by_id(app_handle, missing)
        return {"measures": library.get("measures", []) + extra["measures"],
                "dimensions": library.get("dimensions", []) + extra["dimensions"]}

    def _library_items_by_id(self, app_handle: int, item_ids: List[str],
                             kinds: Tuple[str, ...] = ("measures", "dimensions")
                             ) -> Dict[str, List[Dict[str, Any]]]:
        """Master items read one by one, in the shape `get_master_items` uses.

        `kinds` limits the lookup when the caller knows what the ids are.
        """
        found: Dict[str, List[Dict[str, Any]]] = {"measures": [], "dimensions": []}
        asked = [(kind, item_id) for kind in ("measures", "dimensions") if kind in kinds
                 for item_id in item_ids]
        requests = [{"method": "GetMeasure" if kind == "measures" else "GetDimension",
                     "params": {"qId": item_id}, "handle": app_handle}
                    for kind, item_id in asked]
        try:
            opened = self.send_requests_pipelined(requests, raise_on_error=False)
            handles = [None if isinstance(o, Exception)
                       else ((o or {}).get("qReturn") or {}).get("qHandle") for o in opened]
            positions = [i for i, handle in enumerate(handles) if handle is not None]
            # The definition, not the layout: a master dimension's layout
            # makes Engine count its values.
            replies = self.send_requests_pipelined(
                [{"method": "GetProperties", "params": [], "handle": handles[i]}
                 for i in positions], raise_on_error=False) if positions else []
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                raise
            logger.debug("Could not resolve master items: %s", exc)
            return found
        for position, reply in zip(positions, replies):
            body = (reply or {}).get("qProp") if isinstance(reply, dict) else None
            if not isinstance(body, dict):
                continue
            kind, item_id = asked[position]
            name = ((body.get("qMetaDef") or {}).get("title") or "").strip()
            if kind == "measures" and isinstance(body.get("qMeasure"), dict):
                measure = body["qMeasure"]
                found["measures"].append({
                    "id": item_id, "name": name or str(measure.get("qLabel") or ""),
                    "expression": str(measure.get("qDef") or "").strip()})
            elif kind == "dimensions" and isinstance(body.get("qDim"), dict):
                dim = body["qDim"]
                entry = {"id": item_id, "name": name}
                entry.update(describe_master_dimension(
                    name, dim.get("qFieldDefs"), dim.get("qFieldLabels"), dim.get("qGrouping")))
                found["dimensions"].append(entry)
        return found

    def _map_layers(self, app_handle: int) -> Dict[str, List[Dict[str, Any]]]:
        """GeoAnalytics layers by map id, read once per app.

        A layer is an object of the app, not a child of its map; the two
        are linked only by the map's numeric `mapId` and the layer's
        string `mapid`.
        """
        cache = self._app_cache("_map_layer_cache")
        if "layers" in cache:
            return cache["layers"]
        cache["layers"] = {}
        try:
            infos = (self.send_request("GetAllInfos", [], handle=app_handle) or {}).get("qInfos") or []
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                raise
            logger.debug("Could not list app objects: %s", exc)
            return cache["layers"]
        ids = [i.get("qId") for i in infos if isinstance(i, dict)
               and str(i.get("qType", "")).startswith("idevio")
               and str(i.get("qType", "")).endswith("layer")]
        for props in self._read_properties(app_handle, [i for i in ids if i]).values():
            cache["layers"].setdefault(str(props.get("mapid", "")), []).append(props)
        return cache["layers"]

    def _master_dimension_first_def(self, app_handle: int, library_id: str) -> str:
        """The first field or expression of a master dimension, whole.

        The library carries a calculated dimension cut for display; a cube
        needs all of it, so it is read from the definition itself.
        """
        read = self._library_items_by_id(app_handle, [library_id])
        for entry in read["dimensions"]:
            if entry.get("fields"):
                return entry["fields"][0]
        try:
            opened = self.send_request("GetDimension", {"qId": library_id}, handle=app_handle)
            handle = (opened.get("qReturn") or {}).get("qHandle")
            if handle is None:
                return ""
            body = (self.send_request("GetProperties", [], handle=handle) or {}).get("qProp") or {}
            return _first_def((body.get("qDim") or {}).get("qFieldDefs"))
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                raise
            return ""

    def object_data(self, app_handle: int, described: Dict[str, Any],
                    limit: int, offset: int) -> Dict[str, Any]:
        """An object's own data, computed as a flat table.

        Not the object's layout: a stacked chart ("K") or a pivot ("P")
        answers with a tree of nodes, and a map computes each layer twice.
        The object's dimensions and measures are sent to Engine as one
        straight hypercube (`qMode: "S"`) under the limits of
        `engine_create_hypercube`, with the row whose dimension is empty
        (Qlik's "-") kept, since without it the totals do not match the
        chart. A column whose show condition is false is left out, as the
        chart leaves it out.
        """
        plan = described.get("_cube") or {}
        notes: List[str] = []
        dims_plan = list(plan.get("dimensions") or [])
        measures_plan = list(plan.get("measures") or []) + list(plan.get("extra_measures") or [])
        if not dims_plan and not measures_plan:
            return {"note": _NO_DATA_NOTE.get(described.get("object_type"),
                                              "This object computes no data of its own.")}

        columns = [("dimension", i, entry, column) for i, (entry, column) in
                   enumerate(zip(dims_plan, described.get("dimensions") or []))]
        columns += [("measure", i, entry, column) for i, (entry, column) in
                    enumerate(zip(plan.get("measures") or [], described.get("measures") or []))]
        object_conditions = [c for c in described.get("conditions") or [] if c.get("on") == "object"]
        false_now = self._false_conditions(
            app_handle, [c.get("condition") for *_x, c in columns]
            + [c["condition"] for c in object_conditions])
        hidden = {p for p in false_now if p < len(columns)}
        for position in sorted(p - len(columns) for p in false_now if p >= len(columns)):
            condition = object_conditions[position]
            shows = repr(condition["message"]) if condition.get("message") else "nothing"
            notes.append(f"the object's calculation condition {condition['condition']} is "
                         f"false now: it shows {shows} instead of these numbers")
        library: Dict[str, Any] = {}
        if any(entry.get("master_id") for _k, _i, entry, _c in columns):
            library = self._library_for(app_handle)
        master_measures = {m.get("id"): m for m in library.get("measures", [])}
        master_dims = {d.get("id"): d for d in library.get("dimensions", [])}

        dimensions: List[Dict[str, Any]] = []
        measures: List[Dict[str, Any]] = []
        labels: Dict[Tuple[str, int], str] = {}
        taken: set = set()
        left_out: List[str] = []

        def unique(label: str) -> str:
            label = label or "column"
            candidate, n = label, 2
            while candidate in taken:
                candidate, n = f"{label} ({n})", n + 1
            taken.add(candidate)
            return candidate

        for position, (kind, index, entry, _column) in enumerate(columns):
            if position in hidden:
                left_out.append(str(entry.get("label")))
                continue
            if kind == "dimension":
                field = entry.get("field") or ""
                if entry.get("master_id"):
                    master = master_dims.get(entry["master_id"]) or {}
                    field = (master.get("fields") or [""])[0] \
                        or self._master_dimension_first_def(app_handle, entry["master_id"])
                    if len(master.get("fields") or []) > 1:
                        notes.append(f"master dimension {entry['master_id']} is a group; "
                                     f"its first field {field!r} is used")
                if not field.strip():
                    notes.append(f"dimension {entry.get('label')!r} could not be resolved "
                                 f"and is left out")
                    continue
                labels[(kind, index)] = unique(str(entry.get("label") or field))
                dimensions.append({"field": field.strip(), "label": labels[(kind, index)]})
            else:
                expression = entry.get("expression") or ""
                if entry.get("master_id"):
                    expression = (master_measures.get(entry["master_id"]) or {}).get(
                        "expression") or expression
                if not str(expression).strip():
                    notes.append(f"measure {entry.get('label')!r} could not be resolved "
                                 f"and is left out")
                    continue
                labels[(kind, index)] = unique(str(entry.get("label") or expression)[:80])
                measures.append({"expression": expression, "label": labels[(kind, index)]})
        if left_out:
            notes.append("left out, as the object does, because their show condition is "
                         "false now: " + ", ".join(left_out))
        for extra in plan.get("extra_measures") or []:
            measures.append({"expression": extra["expression"], "label": unique(extra["label"])})

        sort = plan.get("sort") or {}
        sort_by, sort_order = None, sort.get("order", "desc")
        for kind in ("dimension", "measure"):
            if kind in sort and (kind, sort[kind]) in labels:
                sort_by = labels[(kind, sort[kind])]

        histogram = plan.get("histogram")
        if histogram and dimensions:
            built = self._histogram_columns(app_handle, dimensions[0]["field"], histogram)
            if built.get("error"):
                return built
            dimensions, measures = built["dimensions"], built["measures"]
            notes.append(built["note"])
        if not measures and not dimensions:
            return {"note": "No column of this object could be computed.", "notes": notes}

        result = self.create_hypercube(
            app_handle, dimensions, measures, max_rows=limit, sort_by=sort_by,
            sort_order=sort_order, suppress_zero=bool(plan.get("suppress_zero")),
            exclude_null_dimensions=False, offset=offset, strict_fields=False)
        if not isinstance(result, dict) or result.get("error"):
            return result if isinstance(result, dict) else {"error": str(result)}
        data = {key: result.get(key) for key in _DATA_KEYS if result.get(key) not in (None, [], "")}
        data.setdefault("rows", result.get("rows", []))
        if notes:
            data["notes"] = notes
        return data

    def _false_conditions(self, app_handle: int, conditions: List[Optional[str]]) -> set:
        """Positions whose show condition evaluates to false right now.

        A scalar evaluation per condition, pipelined; a condition Engine
        cannot evaluate keeps its column rather than hiding it.
        """
        positions = [i for i, c in enumerate(conditions) if c]
        if not positions:
            return set()
        try:
            replies = self.send_requests_pipelined(
                [{"method": "EvaluateEx",
                  "params": {"qExpression": conditions[i][1:] if conditions[i].startswith("=")
                             else conditions[i]}, "handle": app_handle}
                 for i in positions], raise_on_error=False)
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                raise
            return set()
        hidden = set()
        for position, reply in zip(positions, replies):
            value = (reply or {}).get("qValue") if isinstance(reply, dict) else None
            if isinstance(value, dict) and value.get("qIsNumeric") and value.get("qNumber") == 0:
                hidden.add(position)
        return hidden

    def _histogram_columns(self, app_handle: int, field: str,
                           spec: Dict[str, Any]) -> Dict[str, Any]:
        """A histogram has no measure of its own: its bars are `Class()`
        of the field and a count per class."""
        bare = field.strip()
        name = bare if bare.startswith("=") or bare.startswith("[") else f"[{bare}]"
        name = name[1:] if name.startswith("=") else name
        step = spec.get("size")
        if not step:
            bounds = self.create_hypercube(
                app_handle, [], [{"expression": f"Min({name})", "label": "min"},
                                 {"expression": f"Max({name})", "label": "max"}], max_rows=1)
            row = (bounds.get("rows") or [[None, None]])[0] if isinstance(bounds, dict) else [None, None]
            low, high = (row + [None, None])[:2]
            if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
                return {"error": f"Could not read the range of {field!r} to form bins.",
                        "error_category": "engine_api_error"}
            step = (high - low) / max(1, int(spec.get("count") or 10)) or 1
        count = f"Count(distinct {name})" if spec.get("count_distinct") else f"Count({name})"
        return {"dimensions": [{"field": f"=Class({name}, {step:g})", "label": "bin"}],
                "measures": [{"expression": count, "label": "count"}],
                "note": f"bins are Class({name}, {step:g})"}


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
        """Field names a Qlik expression refers to; see `fields_in_expression`."""
        return fields_in_expression(expression)
