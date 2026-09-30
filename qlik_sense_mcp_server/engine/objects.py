"""What an object on a sheet shows, read from its properties alone.

`GetLayout` is never needed to describe an object and is expensive to ask
for: it computes the data, and for some types it computes a lot of it (a
map layer twice, a date picker every date of its field, a container child
the whole master visualization, megabytes and seconds per object). The
properties carry everything a description needs, and some of it exists
nowhere else: measure expressions, `qLibraryId`, calculation conditions,
alternate states.

Properties are read by a whitelist of paths per type rather than by
stripping noise: 80-95 percent of their bytes are formatting and remnants
of a type the object used to be (`qLayoutExclude`, `qUndoExclude`,
`qMiniChartDef`, `extensionMeta`). An extension without a rule of its own
goes through the universal reader, which walks the tree for cubes, list
objects and expressions and drops what is colour, style or a constant.

Every object comes back as a dict with the same keys - type, title,
dimensions, measures, conditions, alternative columns, variables, controls,
children - and a one-line `summary` for a model to read. Keys starting with
an underscore (`_cube`) are for the server and never leave it.
"""

import html
import json
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

# One expression in a summary line. The structured fields carry it whole;
# a line is for reading, and a page-long calculated dimension drowns it.
EXPRESSION_DISPLAY_CHARS = 300
# Text objects are context for a sheet, not content to reproduce.
TEXT_DISPLAY_CHARS = 300
# Columns or expressions listed in one line before "... N more".
MAX_LISTED = 10
# Containers are opened to this depth. Deeper nesting exists in theory; in
# practice a sheet does not go past tabs holding a filter pane.
MAX_CONTAINER_DEPTH = 3

# Types that are never shown to a user as an object on a sheet: stored
# cubes left by other clients, ODAG records, colour maps, business models,
# markers of other tools, and a decorative line.
SERVICE_TYPES = frozenset({
    "adhoc-query", "adhoc-export", "bulk-export", "dynamicappview",
    "odagapplink", "ColorMap", "_gitoqlikMetaData", "BusinessModel",
    "VizlibLineObject", "sheet", "story", "slide", "slideitem",
    "appprops", "LoadModel", "measure", "dimension", "bookmark",
})

# What a model is told about types that carry no data by design.
PLACEHOLDER_TYPES = {
    "master-items-viewer": "shows the list of master items",
    "sn-video-player": "a video",
}

CONTAINER_TYPES = frozenset({"container", "sn-tabbed-container", "sn-layout-container"})

# Values an extension ships as defaults. Shown back, they read as content.
_DEFAULT_TEXT = re.compile(
    r"^(button \d+|totals|click here|tooltip text goes here|this is my vizlib tooltip"
    r"|[xy] axis title|type some html.*|https?://|<type in .*>)$",
    re.IGNORECASE)

# Conditions that are always true, and flags that are switches, not conditions.
_ALWAYS_TRUE = frozenset({"", "1", "=1", "-1", "=-1", "true", "=true", "true()", "=true()"})

_VARIABLE_REFERENCE = re.compile(r"\$\(\s*([^\W\d][\w.]*)", re.UNICODE)
_COLOUR = re.compile(r"^=?\s*('\s*#|#|rgb\s*\(|argb\s*\(|hsl\s*\(|colormix|color\s*\(|"
                     r"(red|green|blue|black|white|gray|grey|yellow|lightgray|darkgray)\s*\(\s*\d*\s*\))",
                     re.IGNORECASE)
_URL = re.compile(r"^=?\s*'?\s*(https?://|data:image|file://)", re.IGNORECASE)
_STYLE_WORDS = ("color", "colour", "style", "font", "background", "border", "icon",
                "symbol", "legend", "geometry", "css", "theme", "palette")
_COORDINATE = re.compile(
    r"^=?\s*(avg|only|min|max|sum)?\s*\(?\s*\[?(lat|lng|lon|long|latitude|longitude|x|y)\]?\s*\)?\s*$",
    re.IGNORECASE)

_EXPRESSION_KEYWORDS = frozenset("""
    and or not xor like precedes follows
    if then else elseif end
    total distinct all aggr
    null true false
    as in when
    intervalmatch resident inline autogenerate
    e pi
""".split())


# -- text helpers ------------------------------------------------------------

def expression_text(value: Any) -> str:
    """The text behind a property that may be a literal, a number or an
    expression object (`qStringExpression`, `qValueExpression`, `qCond`)."""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return f"{value:g}"
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("qStringExpression", "qValueExpression"):
            inner = value.get(key)
            if isinstance(inner, dict) and isinstance(inner.get("qExpr"), str):
                return inner["qExpr"]
            if isinstance(inner, str):
                return inner
        for key in ("qExpr", "qv"):
            if isinstance(value.get(key), str):
                return value[key]
        if "qCond" in value:
            return expression_text(value["qCond"])
    return ""


def strip_comments(text: str) -> str:
    """Remove `//` line comments and `/* */` blocks outside quotes and brackets.

    Authors leave old versions of a formula commented out, sometimes on the
    first line; read as text, the dead formula looks like the live one.
    """
    out: List[str] = []
    i, n, closing = 0, len(text), None
    while i < n:
        ch = text[i]
        if closing:
            out.append(ch)
            if ch == closing:
                closing = None
            i += 1
        elif ch in "'\"":
            closing = ch
            out.append(ch)
            i += 1
        elif ch == "[":
            closing = "]"
            out.append(ch)
            i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            out.append(" ")
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def cut(text: str, limit: Optional[int]) -> str:
    """Shorten for display, saying how much was left out."""
    if limit and len(text) > limit:
        return f"{text[:limit]}... (+{len(text) - limit} chars)"
    return text


def clean_expression(value: Any, limit: Optional[int] = None) -> str:
    """An expression as a reader should see it: no comments, one line.

    Only for display. What goes to Engine is the expression as written.
    """
    text = " ".join(strip_comments(expression_text(value)).split())
    text = re.sub(r"^=\s+", "=", text)
    return "" if text == "=" else cut(text, limit)


def measure_text(value: Any) -> str:
    """A measure for reading: a leading "=" changes nothing in a measure."""
    text = clean_expression(value)
    return text[1:].strip() if text.startswith("=") else text


def _same(label: str, *names: Any) -> bool:
    """Whether a label only repeats what it labels."""
    def bare(text: Any) -> str:
        text = str(text or "").strip().lstrip("=").strip()
        return text[1:-1] if text.startswith("[") and text.endswith("]") else text
    return any(bare(label) == bare(name) for name in names if name)


def condition_text(value: Any) -> Optional[str]:
    """A show or calculation condition, or None when it is always true."""
    text = clean_expression(value)
    return None if text.replace(" ", "").lower() in _ALWAYS_TRUE else text


def literal_text(value: Any, from_expression_object: bool = False) -> str:
    """A title or label: plain text, a quoted constant unwrapped, or an
    expression kept as `=...` - it is not evaluated, since that is a
    calculation, and the expression still says what it shows."""
    text = clean_expression(value)
    if not text:
        return ""
    match = re.fullmatch(r"=?\s*'([^']*)'", text)
    if match:
        return match.group(1).strip()
    if from_expression_object and not text.startswith("="):
        return f"={text}"
    return text


def title_text(value: Any) -> str:
    """A title stored as a string or as `qStringExpression`; the latter is
    an expression even without the leading "="."""
    is_expression = isinstance(value, dict) and bool(expression_text(value))
    text = literal_text(value, from_expression_object=is_expression)
    return "" if is_default(text) else text


def is_default(text: str) -> bool:
    return not text or bool(_DEFAULT_TEXT.match(text.strip()))


def variables_in(*texts: Any) -> List[str]:
    """Variables a text expands with `$(name)`, in order of appearance."""
    found: List[str] = []
    for text in texts:
        for name in _VARIABLE_REFERENCE.findall(expression_text(text)):
            if name not in found:
                found.append(name)
    return found


def fields_in_expression(expression: str) -> List[str]:
    """Field names an expression refers to.

    Bracketed names are unambiguous. Bare ones are not, so the rule is
    structural: an identifier followed by `(` is a function call, and
    everything else that is not a keyword is taken as a field. A lexical
    guess, not a parser: an unusual function name would be reported as a
    field, which is the tolerable direction of error for "which fields
    does this object touch".
    """
    if not expression:
        return []
    fields = set(re.findall(r"\[([^\]]+)\]", expression))
    text = strip_comments(expression)
    # Dollar expansion is resolved before evaluation; the name inside is a
    # variable, not a field.
    text = re.sub(r"\$\([^)]*\)", " ", text)
    # Both quote styles are values inside a set modifier: single quotes a
    # literal, double quotes a search - `Country={"New Zealand"}` would
    # otherwise name the fields "New" and "Zealand".
    text = re.sub(r"'[^']*'", " ", text)
    text = re.sub(r'"[^"]*"', " ", text)
    text = re.sub(r"\[[^\]]*\]", " ", text)
    # `Num#(...)` and its siblings are calls too.
    text = re.sub(r"#\s*\(", "(", text)
    # Field names are routinely non-Latin, so the identifier pattern is
    # Unicode-aware rather than A-Za-z.
    for match in re.finditer(r"(?<![\w.])(\w[\w.]*)\s*(\()?", text, re.UNICODE):
        name, is_call = match.group(1), match.group(2)
        if is_call or name.lower() in _EXPRESSION_KEYWORDS:
            continue
        # An unbracketed name cannot start with a digit, so anything that
        # does is a literal - including scientific notation such as 1e3.
        if name[0].isdigit():
            continue
        fields.add(name)
    return sorted(fields)


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(html.unescape(text).split())


def markdown_text(markdown: str, measure_labels: Dict[str, str]) -> str:
    """Text of a text-image object without Qlik's markup.

    A measure is inserted into the text as `=[](<cId>)`, where cId is the
    `qDef.cId` of one of the object's own measures; it is shown as
    `{label}` so the sentence still reads.
    """
    text = re.sub(r"=\[\]\(([^)]*)\)",
                  lambda m: "{" + measure_labels.get(m.group(1), m.group(1)) + "}",
                  markdown or "")
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    # Size %[text](4), colour and alignment ^[text](inherit), colour
    # #[text](...), links @[text](url), nested freely.
    previous = None
    while previous != text:
        previous = text
        text = re.sub(r"[%^#@]\[([^\[\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\((https?://)?[^)]*\)", r"\1", text)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"\\(.)", r"\1", text)
    text = " ".join(html.unescape(text).split())
    # Markup around nothing: an empty heading used as a spacer.
    return text if re.search(r"\w", text) else ""


def lexical_paragraphs(text: str) -> List[str]:
    """Paragraphs of an sn-text object, whose `text` is a JSON document
    stored as a string - it has to be decoded a second time."""
    try:
        document = json.loads(text)
    except (TypeError, ValueError):
        return [strip_html(str(text or ""))] if text else []

    def words(node: Any, out: List[str]) -> None:
        if not isinstance(node, dict):
            return
        if node.get("type") == "text" and isinstance(node.get("text"), str):
            out.append(node["text"])
        for child in node.get("children") or []:
            words(child, out)

    paragraphs = []
    root = (document or {}).get("root") if isinstance(document, dict) else None
    for block in (root or {}).get("children") or []:
        parts: List[str] = []
        words(block, parts)
        line = " ".join("".join(parts).split())
        if line:
            paragraphs.append(line)
    return paragraphs


def _name(value: Any) -> str:
    """A field, variable or bookmark name that may be stored as an
    expression: `='Region'`, `'vShow'` or a `qStringExpression`."""
    text = literal_text(value)
    text = text[1:].strip() if text.startswith("=") else text
    return text.strip("'").strip()


def _first(values: Any) -> str:
    if isinstance(values, list) and values and isinstance(values[0], str):
        return values[0]
    return ""


def _get(data: Any, path: str) -> Any:
    for key in path.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _quoted(text: str) -> str:
    return f'"{text}"'


def _listed(items: List[str], separator: str = ", ") -> str:
    shown = items[:MAX_LISTED]
    more = len(items) - len(shown)
    return separator.join(shown) + (f"{separator}... {more} more" if more > 0 else "")


def is_noise_expression(text: str) -> bool:
    """A colour, a constant, a URL, a random demo value or nothing at all."""
    bare = text.strip()
    if not bare:
        return True
    if _COLOUR.match(bare) or _URL.match(bare):
        return True
    if "rand()" in bare.lower().replace(" ", ""):
        return True
    body = bare[1:].strip() if bare.startswith("=") else bare
    if re.fullmatch(r"[\d\s.,+\-*/()]*", body):
        return True
    if re.fullmatch(r"'[^']*'", body):
        return True
    return bool(re.search(r"<\s*(div|span|p|br|img|a|b|i|table|td|tr|style)\b", body, re.I)
                and not re.search(r"\w\s*\(", re.sub(r"'[^']*'", "", body)))


# -- the master items an object may point at ------------------------------

class Library:
    """Master dimensions, measures and visualizations of one app, by id.

    Built from what `get_master_items` returns plus whatever the caller
    resolved on its own; the parser never talks to Engine.
    """

    def __init__(self, items: Optional[Dict[str, Any]] = None,
                 visualizations: Optional[Dict[str, Dict[str, Any]]] = None,
                 effective: Optional[Dict[str, Dict[str, Any]]] = None):
        items = items or {}
        self.measures = {e["id"]: e for e in items.get("measures") or []
                         if isinstance(e, dict) and e.get("id")}
        self.dimensions = {e["id"]: e for e in items.get("dimensions") or []
                           if isinstance(e, dict) and e.get("id")}
        # Master visualizations by id: their properties, as GetProperties
        # returns them.
        self.visualizations = visualizations or {}
        # Merged properties of linked objects that could not be read
        # through their master, by the linked object's own id.
        self.effective = effective or {}

    def measure(self, library_id: str) -> Optional[Dict[str, Any]]:
        return self.measures.get(library_id)

    def dimension(self, library_id: str) -> Optional[Dict[str, Any]]:
        return self.dimensions.get(library_id)

    @staticmethod
    def dimension_body(entry: Dict[str, Any]) -> str:
        """`A > B (drill-down)` out of `Name = A > B (drill-down)`."""
        definition, name = entry.get("definition") or "", entry.get("name") or ""
        prefix = f"{name} = "
        return definition[len(prefix):] if definition.startswith(prefix) else definition


def library_ids(props: Any) -> List[str]:
    """Every `qLibraryId` anywhere in a property tree."""
    found: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "qLibraryId" and isinstance(value, str) and value:
                    if value not in found:
                        found.append(value)
                elif key not in ("qLayoutExclude", "qUndoExclude", "extensionMeta"):
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(props)
    return found


def master_references(props: Any) -> List[str]:
    """Master visualizations an object shows: the one it is linked to, a
    trellis' `vizId`, a Vizlib container's tabs and KPI layers."""
    if not isinstance(props, dict):
        return []
    found: List[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value and value not in found:
            found.append(value)

    add(props.get("qExtendsId"))
    add(_get(props, "prop.vizId"))
    for tab in props.get("containerObjects") or []:
        if isinstance(tab, dict):
            add(tab.get("masterItemId"))
            for cell in _get(tab, "gridView.masterItems") or []:
                if isinstance(cell, dict) and cell.get("cellId"):
                    add(cell.get("masterItemId"))
    return found


def tree_references(node: Dict[str, Any], depth: int = 0) -> Tuple[List[str], List[str]]:
    """Master visualizations and library items used anywhere in a property
    tree node and its children, for one batched read."""
    props = node.get("qProperty") or {}
    masters, items = master_references(props), library_ids(props)
    if depth < MAX_CONTAINER_DEPTH:
        for child in node.get("qChildren") or []:
            if isinstance(child, dict):
                more_masters, more_items = tree_references(child, depth + 1)
                masters += [m for m in more_masters if m not in masters]
                items += [i for i in more_items if i not in items]
    return masters, items


# -- the parser ---------------------------------------------------------------

class ObjectParser:
    """Turns object properties into descriptions, one rule per type.

    `sheet_title` names a sheet by id for buttons that navigate; `map_layers`
    returns the properties of the GeoAnalytics layers of a map id, which are
    separate objects of the app rather than children of the map.
    """

    def __init__(self, library: Optional[Library] = None,
                 sheet_title: Optional[Callable[[str], Optional[str]]] = None,
                 map_layers: Optional[Callable[[str], List[Dict[str, Any]]]] = None):
        self.library = library or Library()
        self.sheet_title = sheet_title or (lambda _id: None)
        self.map_layers = map_layers or (lambda _id: [])

    # -- entry points --

    def describe_node(self, node: Dict[str, Any], depth: int = 0,
                      seen: Optional[set] = None) -> Optional[Dict[str, Any]]:
        """One node of `GetFullPropertyTree`: an object and its children."""
        children = [c for c in node.get("qChildren") or [] if isinstance(c, dict)]
        return self.describe(node.get("qProperty") or {}, children, depth, seen)

    def describe(self, props: Dict[str, Any], children: Iterable[Dict[str, Any]] = (),
                 depth: int = 0, seen: Optional[set] = None) -> Optional[Dict[str, Any]]:
        """One object from its properties; None for what is not shown to users."""
        seen = set() if seen is None else seen
        info = props.get("qInfo") or {}
        object_id, object_type = info.get("qId") or "", info.get("qType") or ""
        if object_id:
            if object_id in seen:
                return None
            seen.add(object_id)

        master_id = props.get("qExtendsId") or None
        definition, master = props, None
        if master_id:
            master = self.library.visualizations.get(master_id)
            merged = self.library.effective.get(object_id)
            if merged is not None:
                definition = merged
            elif master is not None:
                # The linked object's own properties are an empty shell;
                # what it shows is the master's. Its own title still wins.
                definition = {**master, **{k: v for k, v in props.items()
                                           if v not in (None, "", {}, [])}}

        kind = self._kind(object_type, definition)
        if kind in SERVICE_TYPES:
            return None

        summary: Dict[str, Any] = {"object_id": object_id, "object_type": kind}
        title = title_text(definition.get("title")) or title_text(
            (definition.get("qMetaDef") or {}).get("title"))
        if title:
            summary["title"] = title
        for key in ("subtitle", "footnote"):
            text = title_text(definition.get(key))
            if text:
                summary[key] = text
        if master_id:
            summary["master_object"] = {"id": master_id}
            master_title = title_text(((master or {}).get("qMetaDef") or {}).get("title"))
            if master_title:
                summary["master_object"]["title"] = master_title

        if master_id and master is None and definition is props:
            summary["summary"] = (f"{kind} linked to master visualization {master_id}, "
                                  f"whose definition could not be read")
            return summary

        handler = _HANDLERS.get(kind)
        if handler is None and kind in PLACEHOLDER_TYPES:
            summary["summary"] = f"{kind}: {PLACEHOLDER_TYPES[kind]}"
            return summary
        parts: List[str] = []
        if handler is None:
            keep = self._universal(summary, definition, parts)
        else:
            keep = handler(self, summary, definition, parts, list(children), depth, seen)
        if keep is False:
            return None
        self._common(summary, definition, parts)
        summary["summary"] = self._line(summary, parts)
        return summary

    @staticmethod
    def _kind(object_type: str, props: Dict[str, Any]) -> str:
        """The type that decides how to read the object.

        A master visualization is `masterobject` and a container child
        `containerChild`; the chart they hold is in `visualization`. An
        auto-chart names the type Qlik chose in `qUndoExclude.generated`.
        """
        visualization = props.get("visualization")
        if object_type in ("masterobject", "containerChild", "") and isinstance(visualization, str):
            return visualization or object_type
        if object_type == "auto-chart":
            return "auto-chart"
        return object_type or (visualization if isinstance(visualization, str) else "")

    # -- shared pieces --

    def _common(self, summary: Dict[str, Any], props: Dict[str, Any],
                parts: List[str]) -> None:
        """State, whole-cube set and variables: they change what every
        figure of the object means and appear in none of its expressions."""
        state = props.get("qStateName") or _get(props, "qHyperCubeDef.qStateName") \
            or _get(props, "props.alternateState")
        if isinstance(state, str) and state not in ("", "$"):
            summary["state"] = state
            parts.append(f"state {state}")
        context_set = _get(props, "qHyperCubeDef.qContextSetExpression")
        if isinstance(context_set, str) and context_set.strip():
            summary["context_set"] = clean_expression(context_set)
            parts.append(f"set {cut(summary['context_set'], EXPRESSION_DISPLAY_CHARS)}")
        texts: List[Any] = []
        for column in summary.get("dimensions", []) + summary.get("measures", []) \
                + summary.get("alt_measures", []) + summary.get("alt_dimensions", []):
            texts += [column.get("expression"), column.get("label_expression"),
                      column.get("condition"), column.get("target"), column.get("field")]
        for condition in summary.get("conditions", []):
            texts.append(condition.get("condition"))
        for control in summary.get("controls", []):
            texts += [control.get("value"), control.get("condition")]
        texts += [summary.get("title"), summary.get("subtitle"), summary.get("text"),
                  summary.get("text_expression")]
        texts += [e.get("expression") for e in summary.get("expressions", [])]
        texts += [layer.get("colour_expression") for layer in summary.get("layers", [])]
        # A tab picked by a condition is picked by the variables in it.
        texts += [child.get("shown_if") for child in summary.get("children", [])]
        names = variables_in(*[t for t in texts if t])
        # A variable an object sets is one it works with, too.
        for name in summary.get("variables", []) + [
                c["variable"] for c in summary.get("controls", []) if c.get("variable")]:
            if name not in names:
                names.append(name)
        if names:
            summary["variables"] = names
            parts.append("variables " + ", ".join(names))

    def _line(self, summary: Dict[str, Any], parts: List[str]) -> str:
        head = summary["object_type"]
        if summary.get("title"):
            head += f" {_quoted(cut(summary['title'], EXPRESSION_DISPLAY_CHARS))}"
        master = summary.get("master_object")
        if master:
            parts = parts + [f"from master {_quoted(master['title']) + ' ' if master.get('title') else ''}"
                             f"{master['id']}"]
        if summary.get("subtitle"):
            parts = parts + [f"subtitle {_quoted(cut(summary['subtitle'], EXPRESSION_DISPLAY_CHARS))}"]
        body = "; ".join(p for p in parts if p)
        return f"{head}: {body}" if body else head

    def dimension(self, dim: Dict[str, Any], role: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """A dimension: field, calculated expression or master item."""
        if not isinstance(dim, dict):
            return None
        inner = dim.get("qDef") if isinstance(dim.get("qDef"), dict) else {}
        library_id = dim.get("qLibraryId") or inner.get("qLibraryId")
        defs = [d for d in inner.get("qFieldDefs") or [] if isinstance(d, str) and d.strip()]
        entry: Dict[str, Any] = {}
        raw = ""
        if library_id:
            entry["master_id"] = library_id
            master = self.library.dimension(library_id)
            if master:
                entry["master_name"] = master.get("name", "")
                entry["definition"] = Library.dimension_body(master)
                if master.get("fields"):
                    entry["fields"] = list(master["fields"])
        elif defs:
            raw = defs[0].strip()
            if raw.startswith("="):
                entry["expression"] = clean_expression(raw)
            else:
                entry["field"] = raw[1:-1] if raw.startswith("[") and raw.endswith("]") else raw
            if len(defs) > 1:
                entry["drill_down"] = [d.strip() for d in defs]
        else:
            return None
        label = literal_text(_first(inner.get("qFieldLabels")))
        label_expression = expression_text(inner.get("qLabelExpression"))
        if label.startswith("=") and not label_expression.strip():
            label_expression, label = label, ""
        if label_expression.strip():
            unwrapped = literal_text(label_expression)
            if not unwrapped.startswith("="):
                label = unwrapped
            elif not _same(unwrapped, raw, entry.get("field")):
                entry["label_expression"] = measure_text(label_expression)
        if label and not _same(label, entry.get("field"), entry.get("master_name"), raw):
            entry["label"] = label
        condition = (condition_text(dim.get("qCalcCondition"))
                     or condition_text(inner.get("qShowHide"))
                     or condition_text(inner.get("vzlCalcCond")))
        if condition:
            entry["condition"] = condition
        others = dim.get("qOtherTotalSpec") or {}
        mode = others.get("qOtherMode") if isinstance(others, dict) else None
        if mode and mode != "OTHER_OFF":
            # Only a count is a top N a reader recognises; the limit modes
            # ship with a zero limit that restricts nothing.
            counted = expression_text(others.get("qOtherCounted")) if mode == "OTHER_COUNTED" else ""
            if counted.strip() not in ("", "0"):
                entry["top_n"] = counted.strip()
                if others.get("qSuppressOther"):
                    entry["others_hidden"] = True
        if role:
            entry["role"] = role
        entry["_cube"] = {"field": raw, "master_id": library_id,
                          "label": entry.get("label") or entry.get("master_name")
                          or entry.get("field") or clean_expression(raw, 60)}
        return entry

    def measure(self, measure: Dict[str, Any], role: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """A measure: expression or master item, with its label."""
        if not isinstance(measure, dict):
            return None
        inner = measure.get("qDef") if isinstance(measure.get("qDef"), dict) else {}
        library_id = measure.get("qLibraryId") or inner.get("qLibraryId")
        raw = inner.get("qDef") if isinstance(inner.get("qDef"), str) else ""
        entry: Dict[str, Any] = {}
        if library_id:
            entry["master_id"] = library_id
            master = self.library.measure(library_id)
            if master:
                entry["master_name"] = master.get("name", "")
                entry["expression"] = measure_text(master.get("expression", ""))
        elif measure_text(raw):
            entry["expression"] = measure_text(raw)
        else:
            return None
        label = literal_text(inner.get("qLabel") or "")
        label_expression = expression_text(inner.get("qLabelExpression"))
        if label_expression.strip():
            unwrapped = literal_text(label_expression)
            if unwrapped.startswith("="):
                entry["label_expression"] = measure_text(label_expression)
            elif unwrapped:
                label = unwrapped
        if label and not _same(label, entry.get("expression"), entry.get("master_name")):
            entry["label"] = label
        condition = (condition_text(measure.get("qCalcCondition"))
                     or condition_text(measure.get("qCalcCond"))
                     or condition_text(inner.get("qShowHide"))
                     or condition_text(inner.get("vzlCalcCond")))
        if condition:
            entry["condition"] = condition
        number_format = inner.get("qNumFormat")
        if isinstance(number_format, dict) and number_format.get("qType") not in (None, "U") \
                and number_format.get("qFmt"):
            entry["format"] = number_format["qFmt"]
        if role:
            entry["role"] = role
        entry["_cube"] = {"expression": raw, "master_id": library_id,
                          "label": entry.get("label") or entry.get("master_name")
                          or cut(entry.get("expression", ""), 60)}
        if isinstance(inner.get("cId"), str):
            entry["_cube"]["cid"] = inner["cId"]
        return entry

    def cube(self, summary: Dict[str, Any], cube: Any,
             dimension_roles: Optional[List[Optional[str]]] = None,
             measure_roles: Optional[List[Optional[str]]] = None,
             alternatives: bool = True) -> None:
        """Dimensions, measures, conditions and alternatives of a hypercube
        definition, added to the summary. Also plans the object's own data:
        the same columns, computed flat."""
        if not isinstance(cube, dict):
            return
        dimension_roles = dimension_roles or []
        measure_roles = measure_roles or []
        raw_dims = [d for d in cube.get("qDimensions") or [] if isinstance(d, dict)]
        raw_measures = [m for m in cube.get("qMeasures") or [] if isinstance(m, dict)]
        dims, measures = summary.setdefault("dimensions", []), summary.setdefault("measures", [])
        dim_index, measure_index = {}, {}
        for i, dim in enumerate(raw_dims):
            entry = self.dimension(dim, dimension_roles[i] if i < len(dimension_roles) else None)
            if entry:
                dim_index[i] = len(dims)
                dims.append(entry)
        for i, measure in enumerate(raw_measures):
            entry = self.measure(measure, measure_roles[i] if i < len(measure_roles) else None)
            if entry:
                measure_index[i] = len(measures)
                measures.append(entry)

        condition = condition_text(_get(cube, "qCalcCondition.qCond")) \
            or condition_text(cube.get("qCalcCond"))
        if condition and summary["object_type"] != "VizlibFilter":
            found = {"on": "object", "condition": condition}
            message = literal_text(_get(cube, "qCalcCondition.qMsg"))
            if message:
                found["message"] = message
            summary.setdefault("conditions", []).append(found)

        if alternatives:
            hidden = _get(cube, "qLayoutExclude.qHyperCubeDef") or {}
            alt_dims = [self.dimension(d) for d in hidden.get("qDimensions") or []]
            alt_measures = [self.measure(m) for m in hidden.get("qMeasures") or []]
            if any(alt_dims):
                summary["alt_dimensions"] = [d for d in alt_dims if d]
            if any(alt_measures):
                summary["alt_measures"] = [m for m in alt_measures if m]

        plan = summary.setdefault("_cube", {"dimensions": [], "measures": []})
        plan["dimensions"] += [e["_cube"] for e in dims[len(plan["dimensions"]):]]
        plan["measures"] += [e["_cube"] for e in measures[len(plan["measures"]):]]
        if cube.get("qSuppressZero"):
            plan["suppress_zero"] = True
        order = cube.get("qInterColumnSortOrder")
        if isinstance(order, list) and order and isinstance(order[0], int) and order[0] >= 0 \
                and "sort" not in plan:
            first = order[0]
            if first < len(raw_dims) and first in dim_index:
                criteria = (_get(raw_dims[first], "qDef.qSortCriterias") or [{}])[0] or {}
                descending = -1 in (criteria.get("qSortByNumeric"), criteria.get("qSortByAscii"))
                plan["sort"] = {"dimension": dim_index[first],
                                "order": "desc" if descending else "asc"}
            elif first - len(raw_dims) in measure_index:
                position = first - len(raw_dims)
                numeric = (raw_measures[position].get("qSortBy") or {}).get("qSortByNumeric")
                plan["sort"] = {"measure": measure_index[position],
                                "order": "asc" if numeric == 1 else "desc"}

    # -- rendering columns --

    @staticmethod
    def render_dimension(entry: Dict[str, Any]) -> str:
        if entry.get("master_id"):
            text = f"master {entry['master_id']}"
            if entry.get("master_name"):
                text += f" {_quoted(entry['master_name'])}"
            if entry.get("definition") and entry["definition"] != entry.get("master_name"):
                text += f" = {cut(entry['definition'], EXPRESSION_DISPLAY_CHARS)}"
            if entry.get("label"):
                text = f"[{entry['label']}]={text}"
        elif entry.get("expression"):
            body = cut(entry["expression"], EXPRESSION_DISPLAY_CHARS)
            text = f"[{entry['label']}]={body}" if entry.get("label") else body
        else:
            field = entry.get("field", "")
            if entry.get("drill_down"):
                field = " > ".join(entry["drill_down"]) + " (drill-down)"
            text = f"[{entry['label']}]={field}" if entry.get("label") else field
        if entry.get("label_expression"):
            text += f" label={cut(entry['label_expression'], EXPRESSION_DISPLAY_CHARS)}"
        if entry.get("top_n"):
            text += f" (top {entry['top_n']}" + (", others hidden)" if entry.get("others_hidden")
                                                   else ', rest "Others")')
        if entry.get("condition"):
            text += f" [if {cut(entry['condition'], EXPRESSION_DISPLAY_CHARS)}]"
        return text

    @staticmethod
    def render_measure(entry: Dict[str, Any]) -> str:
        if entry.get("label_expression"):
            label = f"label={cut(entry['label_expression'], EXPRESSION_DISPLAY_CHARS)}"
        elif entry.get("label"):
            label = _quoted(entry["label"])
        else:
            label = ""
        if entry.get("master_id"):
            body = f"master {entry['master_id']}"
            if entry.get("master_name"):
                body += f" {_quoted(entry['master_name'])}"
            if entry.get("expression"):
                body += f" = {cut(entry['expression'], EXPRESSION_DISPLAY_CHARS)}"
        else:
            body = cut(entry.get("expression", ""), EXPRESSION_DISPLAY_CHARS)
        text = f"{label} = {body}" if label else body
        if entry.get("role"):
            text = f"{entry['role']} {text}"
        if entry.get("target"):
            text += f", target = {cut(entry['target'], EXPRESSION_DISPLAY_CHARS)}"
        if entry.get("format"):
            text += f" [format {entry['format']}]"
        if entry.get("condition"):
            text += f" [if {cut(entry['condition'], EXPRESSION_DISPLAY_CHARS)}]"
        if entry.get("subtotal"):
            text += f", then subtotal {_quoted(entry['subtotal'])}"
        return text

    def render_columns(self, summary: Dict[str, Any], parts: List[str],
                       dimension_word: str = "dims", path: bool = False) -> None:
        """Dimensions grouped by role, then measures, then what is hidden."""
        dims = summary.get("dimensions") or []
        if path and dims:
            parts.append("path " + " -> ".join(self.render_dimension(d) for d in dims))
        else:
            groups: List[Tuple[Optional[str], List[str]]] = []
            for entry in dims:
                role = entry.get("role")
                if groups and groups[-1][0] == role:
                    groups[-1][1].append(self.render_dimension(entry))
                else:
                    groups.append((role, [self.render_dimension(entry)]))
            for role, items in groups:
                parts.append(f"{role or dimension_word} {_listed(items)}")
        measures = summary.get("measures") or []
        if measures:
            rendered = [self.render_measure(m) for m in measures]
            if any(m.get("role") for m in measures):
                parts.append(_listed(rendered))
            else:
                parts.append(("measure " if len(rendered) == 1 else "measures ") + _listed(rendered))
        for key, word in (("alt_dimensions", "alt dims"), ("alt_measures", "alt measures")):
            if summary.get(key):
                render = self.render_dimension if key == "alt_dimensions" else self.render_measure
                parts.append(f"{word} " + _listed([render(e) for e in summary[key]]))
        for condition in summary.get("conditions") or []:
            text = f"calculated if {cut(condition['condition'], EXPRESSION_DISPLAY_CHARS)}"
            if condition.get("message"):
                text += f" (else {_quoted(cut(condition['message'], EXPRESSION_DISPLAY_CHARS))})"
            parts.append(text)

    # -- per-type rules --

    def _chart(self, summary, props, parts, children, depth, seen,
               dimension_roles=None, measure_roles=None, path=False):
        kind = summary["object_type"]
        cube = props.get("qHyperCubeDef")
        if kind == "boxplot":
            cube = _get(props, "boxplotDef.qHyperCubeDef")
        dimension_roles = dimension_roles or _DIMENSION_ROLES.get(kind)
        measure_roles = measure_roles or _MEASURE_ROLES.get(kind)
        if kind in ("pivot-table", "sn-pivot-table", "VizlibPivotTable") and isinstance(cube, dict):
            left = cube.get("qNoOfLeftDims")
            count = len(cube.get("qDimensions") or [])
            if isinstance(left, int) and 0 <= left <= count:
                dimension_roles = ["rows"] * left + ["columns"] * (count - left)
            else:
                dimension_roles = ["rows"] * count
        self.cube(summary, cube, dimension_roles, measure_roles)
        rule = _TYPE_DETAILS.get(kind)
        if rule:
            rule(self, summary, props, cube if isinstance(cube, dict) else {})
        self.render_columns(summary, parts, path=path or kind in _PATH_TYPES)
        options = summary.get("options") or {}
        for key in _OPTION_ORDER:
            if key in options:
                parts.append(_render_option(key, options[key]))
        sort = summary.get("_cube", {}).get("sort") or {}
        # Ascending by the first column is what every table does unless told
        # otherwise; only a sort someone chose says something.
        if kind in _TABLE_TYPES and sort and (sort.get("dimension") != 0 or sort["order"] == "desc"):
            column = (summary["dimensions"][sort["dimension"]] if "dimension" in sort
                      else summary["measures"][sort["measure"]])
            name = (column.get("label") or column.get("master_name") or column.get("field")
                    or cut(column.get("expression", ""), 60))
            parts.append(f"sorted by {name}" + (" desc" if sort["order"] == "desc" else ""))
        if not summary.get("dimensions") and not summary.get("measures") \
                and kind in _DROP_WHEN_EMPTY:
            return False
        return True

    def _histogram(self, summary, props, parts, children, depth, seen):
        self.cube(summary, props.get("qHyperCubeDef"))
        dims = summary.get("dimensions") or []
        bins = props.get("bins") or {}
        spec: Dict[str, Any] = {"count_distinct": bool(bins.get("countDistinct"))}
        if bins.get("auto", True):
            spec["auto"] = True
            words = "automatic bins"
        elif bins.get("binMode") == "size" or (bins.get("binSize") and bins.get("binMode") != "maxCount"):
            spec["size"] = bins.get("binSize")
            words = f"bins of {bins.get('binSize')}"
        else:
            spec["count"] = bins.get("binCount") or 10
            words = f"{spec['count']} bins"
        summary["options"] = {"bins": spec}
        field = dims[0].get("field") or dims[0].get("expression") or dims[0].get("master_name", "") \
            if dims else ""
        parts.append(f"field {field}, {words}, "
                     + ("count of distinct values" if spec["count_distinct"] else "count of records"))
        plan = summary.get("_cube") or {}
        plan["histogram"] = spec
        return True

    def _map(self, summary, props, parts, children, depth, seen):
        """Native map: the analysis lives in each layer, not in `qMeasures`."""
        layers = []
        for layer in props.get("gaLayers") or props.get("layers") or []:
            if not isinstance(layer, dict):
                continue
            entry: Dict[str, Any] = {"type": layer.get("type", "")}
            cube = layer.get("qHyperCubeDef") or {}
            dim = self.dimension((cube.get("qDimensions") or [None])[0] or {})
            if dim:
                entry["dimension"] = dim
            location = [clean_expression(_get(layer, key)) for key in
                        ("locationOrLatitude.key", "longitude.key")]
            location = [x for x in location if x]
            if location:
                entry["location"] = location
            measures = []
            size = _get(layer, "size.expression")
            if isinstance(size, dict) and clean_expression(size.get("key")) \
                    and not is_noise_expression(clean_expression(size.get("key"))):
                measures.append({"role": "size", "label": literal_text(layer.get("size", {}).get("label", ""))
                                 or literal_text(size.get("label", "")),
                                 "expression": measure_text(size.get("key")),
                                 "_cube": {"expression": size.get("key"), "label": "size"}})
            colour = layer.get("color") or {}
            if colour.get("mode") == "byMeasure" and clean_expression(_get(colour, "byMeasureDef.key")):
                key = _get(colour, "byMeasureDef.key")
                measures.append({"role": "colour", "label": literal_text(_get(colour, "byMeasureDef.label")),
                                 "expression": measure_text(key),
                                 "_cube": {"expression": key, "label": "colour"}})
            elif colour.get("mode") == "byExpression" and clean_expression(colour.get("colorExpression")):
                entry["colour_expression"] = clean_expression(colour["colorExpression"])
            for measure in measures:
                if not measure["label"]:
                    del measure["label"]
            if measures:
                entry["measures"] = measures
            if layer.get("hideCalcCond") and condition_text(layer.get("calcCond")):
                entry["condition"] = condition_text(layer.get("calcCond"))
            layers.append(entry)
        summary["layers"] = [{k: v for k, v in layer.items()} for layer in layers]
        texts = []
        for layer in layers:
            words = [layer["type"] or "layer"]
            if layer.get("dimension"):
                words.append("of " + self.render_dimension(layer["dimension"]))
            if layer.get("location"):
                words.append("at " + "/".join(layer["location"]))
            for measure in layer.get("measures") or []:
                words.append(self.render_measure(measure))
            if layer.get("colour_expression"):
                words.append(f"colour = {cut(layer['colour_expression'], EXPRESSION_DISPLAY_CHARS)}")
            texts.append(" ".join(words))
        if texts:
            parts.append("layers: " + _listed(texts, "; "))
        # The object's own data is the first layer that has something to count.
        for layer in layers:
            if layer.get("dimension") and layer.get("measures"):
                summary["_cube"] = {"dimensions": [layer["dimension"]["_cube"]],
                                    "measures": [m["_cube"] for m in layer["measures"]]}
                summary["dimensions"] = [layer["dimension"]]
                summary["measures"] = layer["measures"]
                break
        for layer in summary["layers"]:
            if "dimension" in layer:
                layer["dimension"] = _public(layer["dimension"])
            if "measures" in layer:
                layer["measures"] = [_public(m) for m in layer["measures"]]
        return True

    def _idevio_map(self, summary, props, parts, children, depth, seen):
        map_id = props.get("mapId")
        layers = []
        for layer_props in self.map_layers(str(map_id)) if map_id not in (None, "") else []:
            described = self.describe(layer_props, (), depth + 1, seen)
            if described:
                layers.append(described)
        if layers:
            # Each layer is described in `children`; the map names them.
            summary["children"] = layers
            parts.append("layers: " + _listed(
                [c["object_type"] + (f" {_quoted(c['title'])}" if c.get("title") else "")
                 for c in layers], "; "))
        elif map_id not in (None, ""):
            parts.append(f"map {map_id}, no layers found")
        return True

    def _idevio_layer(self, summary, props, parts, children, depth, seen):
        kind = summary["object_type"]
        if kind == "ideviogeodatalayer":
            source = _get(props, "geodata.sourceformat") or ""
            parts.append(f"external {source.lower() or 'geodata'} layer, no app data")
            summary["options"] = {"map": props.get("mapid")}
            return True
        role = {"ideviobubblelayer": "points", "idevioarealayer": "areas",
                "ideviolinelayer": "lines"}.get(kind, "objects")
        self.cube(summary, props.get("qHyperCubeDef"), [role], alternatives=False)
        # Coordinates, geometry and demo colouring are how a layer draws,
        # not what it measures. A bare field without aggregation is a
        # location or a shape, not a figure.
        kept = [m for m in summary.get("measures") or []
                if not _COORDINATE.match(m.get("expression", ""))
                and "geometry" not in m.get("expression", "").lower()
                and not re.fullmatch(r"\[?[^\s()\[\]]+\]?", m.get("expression", ""))
                and not is_noise_expression(m.get("expression", ""))]
        summary["measures"] = kept
        plan = summary.get("_cube") or {}
        plan["measures"] = [m["_cube"] for m in kept]
        dims = [d for d in summary.get("dimensions") or []
                if not is_noise_expression(d.get("expression", "") or d.get("field", ""))
                or d.get("master_id")]
        summary["dimensions"] = dims
        plan["dimensions"] = [d["_cube"] for d in dims]
        self.render_columns(summary, parts)
        location = props.get("location") or {}
        if isinstance(location, dict):
            source = location.get("source")
            if location.get("keyAttribute"):
                parts.append(f"located by {source or 'key'} {location['keyAttribute']}")
            elif source:
                parts.append(f"located by {source}")
        tooltip = clean_expression(_get(props, "infoBubble.expression"))
        if tooltip:
            fields = fields_in_expression(tooltip)
            summary["tooltip_fields"] = fields
            if fields:
                parts.append("tooltip " + _listed(fields))
        if props.get("mapid") not in (None, ""):
            summary["options"] = {"map": props.get("mapid")}
        return True

    def _kpi_layers(self, summary, props, parts, children, depth, seen):
        """VizlibKPI: the measures are text layers, outside the cube."""
        measures, labels = summary.setdefault("measures", []), []
        for layer in props.get("layers") or []:
            if not isinstance(layer, dict):
                continue
            content = layer.get("textLayerContentText")
            condition = condition_text(layer.get("showHideLayer"))
            master_item = _get(layer, "masterItemLayer.masterItemId")
            if isinstance(content, dict) and clean_expression(content):
                raw = expression_text(content)
                entry: Dict[str, Any] = {"expression": measure_text(raw)}
                if isinstance(layer.get("textLayerContentTextFormat"), str) \
                        and layer["textLayerContentTextFormat"]:
                    entry["format"] = layer["textLayerContentTextFormat"]
                if condition:
                    entry["condition"] = condition
                entry["_cube"] = {"expression": raw.strip(),
                                  "label": layer.get("layername") or f"layer {len(measures) + 1}"}
                measures.append(entry)
            elif isinstance(content, str) and content.strip() and not is_default(content.strip()):
                labels.append(strip_html(content))
            if isinstance(master_item, str) and master_item:
                entry = self.measure({"qLibraryId": master_item})
                if entry and entry.get("master_name"):
                    measures.append(entry)
                else:
                    parts.append(f"layer shows master item {master_item}")
        if labels and not summary.get("title"):
            summary["title"] = labels[0]
            labels = labels[1:]
        if len(measures) == 1 and labels:
            measures[0]["label"] = labels[0]
        summary["_cube"] = {"dimensions": [], "measures": [m["_cube"] for m in measures]}
        self.render_columns(summary, parts)
        if labels:
            summary["labels"] = labels
            parts.append("text " + _listed([_quoted(label) for label in labels]))
        return True

    def _text_image(self, summary, props, parts, children, depth, seen):
        self.cube(summary, props.get("qHyperCubeDef"), alternatives=False)
        by_cid = {}
        for entry in summary.get("measures") or []:
            cid = entry["_cube"].get("cid")
            if cid:
                by_cid[cid] = entry.get("label") or entry.get("master_name") \
                    or entry.get("master_id") \
                    or cut(entry.get("expression", ""), 60)
        text = markdown_text(props.get("markdown") or "", by_cid)
        if text:
            summary["text"] = text
            parts.append(_quoted(cut(text, TEXT_DISPLAY_CHARS)))
        elif props.get("images") or _get(props, "background.url"):
            parts.append("image")
        if summary.get("measures"):
            self.render_columns(summary, parts)
        else:
            summary.pop("_cube", None)
        return bool(text or summary.get("measures") or parts)

    def _sn_text(self, summary, props, parts, children, depth, seen):
        paragraphs = lexical_paragraphs(props.get("text") or "")
        self.cube(summary, props.get("qHyperCubeDef"), alternatives=False)
        if paragraphs:
            summary["text"] = "\n".join(paragraphs)
            parts.append(_quoted(cut(" / ".join(paragraphs), TEXT_DISPLAY_CHARS)))
        if summary.get("measures"):
            # Where each measure sits in the text is not reconstructed; the
            # text and the measures are given separately.
            self.render_columns(summary, parts)
        else:
            summary.pop("_cube", None)
        return bool(paragraphs or summary.get("measures"))

    def _advanced_text(self, summary, props, parts, children, depth, seen):
        value = props.get("text")
        raw = expression_text(value)
        if isinstance(value, dict) or raw.strip().startswith("="):
            text = clean_expression(raw)
            summary["text_expression"] = text
            parts.append(f"text {cut(text, TEXT_DISPLAY_CHARS)}")
            summary["_cube"] = {"dimensions": [], "measures": [], "extra_measures": [
                {"expression": raw.strip(), "label": "text"}]}
        else:
            text = strip_html(raw)
            if text:
                summary["text"] = text
                parts.append(_quoted(cut(text, TEXT_DISPLAY_CHARS)))
        condition = condition_text(props.get("showHideCondition"))
        if condition:
            summary.setdefault("conditions", []).append({"on": "object", "condition": condition})
            parts.append(f"shown if {cut(condition, EXPRESSION_DISPLAY_CHARS)}")
        self._vizlib_actions(summary, props.get("action"), parts)
        return bool(text or summary.get("controls"))

    def _html_measure(self, summary, props, parts, children, depth, seen):
        self.cube(summary, props.get("qHyperCubeDef"), alternatives=False)
        measures = summary.get("measures") or []
        if measures and re.fullmatch(r"=?\s*'[^']*'", measures[0].get("expression", ""), re.S):
            summary["text"] = strip_html(literal_text(measures[0]["expression"]))
            summary["measures"] = []
            summary.pop("_cube", None)
            parts.append(f"static HTML {_quoted(cut(summary['text'], TEXT_DISPLAY_CHARS))}")
            return True
        for entry in measures:
            entry["expression"] = strip_html(entry.get("expression", ""))
        self.render_columns(summary, parts)
        return bool(measures)

    def _multi_kpi(self, summary, props, parts, children, depth, seen):
        self.cube(summary, props.get("qHyperCubeDef"))
        measures = summary.get("measures") or []
        # A style carrier: CSS in the options and constants as measures.
        if _get(props, "options.styles") and all(
                not fields_in_expression(m.get("expression", "")) and not m.get("master_id")
                for m in measures):
            return False
        self.render_columns(summary, parts)
        return True

    def _listbox(self, summary, props, parts, children, depth, seen):
        entry = self._list_object(props.get("qListObjectDef"))
        if not entry:
            return False
        summary["dimensions"] = [entry]
        parts.append(self.render_dimension(entry))
        return True

    def _list_object(self, list_def: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(list_def, dict):
            return None
        entry = self.dimension({"qDef": list_def.get("qDef") or {},
                                "qLibraryId": list_def.get("qLibraryId")})
        if entry:
            entry.pop("_cube", None)
            state = list_def.get("qStateName")
            if isinstance(state, str) and state not in ("", "$"):
                entry["state"] = state
        return entry

    def _filterpane(self, summary, props, parts, children, depth, seen):
        """A filter pane is its listboxes: their fields are what it filters by."""
        dims = []
        for node in children:
            child = node.get("qProperty") or {}
            child_id = (child.get("qInfo") or {}).get("qId")
            if child_id:
                seen.add(child_id)
            source = child
            if child.get("qExtendsId") in self.library.visualizations:
                source = {**self.library.visualizations[child["qExtendsId"]], **child}
            entry = self._list_object(source.get("qListObjectDef"))
            if entry:
                title = title_text(source.get("title"))
                if title and title not in (entry.get("field"), entry.get("master_name"),
                                           entry.get("label")):
                    entry["label"] = title
                dims.append(entry)
        summary["dimensions"] = dims
        if dims:
            parts.append("fields " + _listed([self.render_dimension(d) for d in dims]))
        return True

    def _date_picker(self, summary, props, parts, children, depth, seen):
        entry = self._list_object(props.get("qListObjectDef"))
        if not entry:
            return False
        summary["dimensions"] = [entry]
        options = props.get("props") or {}
        single = bool(options.get("isSingleDate") or props.get("isSingleDate"))
        words = [f"on {self.render_dimension(entry)}", "single date" if single else "date range"]
        default = [clean_expression(options.get(k)) for k in ("startDate", "endDate")]
        if any(default):
            words.append(f"default {default[0]} .. {default[1]}")
        bounds = [clean_expression(options.get(k)) for k in ("minDate", "maxDate")]
        if any(bounds):
            words.append(f"within {bounds[0]} .. {bounds[1]}")
        summary["controls"] = [{"action": "select dates", "field": entry.get("field")
                                or entry.get("expression") or entry.get("master_name", "")}]
        parts.append(", ".join(words))
        return True

    def _vizlib_filter(self, summary, props, parts, children, depth, seen):
        self.cube(summary, props.get("qHyperCubeDef"), alternatives=False)
        dims = summary.get("dimensions") or []
        summary.pop("_cube", None)
        summary["measures"] = []
        if not dims:
            return False
        words = [f"on {self.render_dimension(dims[0])}"]
        label = literal_text(props.get("dropdownLabel"))
        if not label and _get(props, "caption.show"):
            label = literal_text(_get(props, "caption.title"))
        if label and not is_default(label):
            words.append(_quoted(label))
        if props.get("alwaysSelectedEnabled"):
            words.append("always one value selected")
        default = clean_expression(props.get("defSelectedValues"))
        if default:
            words.append(f"default {default}")
        if _get(props, "qDef.measure.mode") and clean_expression(_get(props, "qDef.measure.value")):
            words.append(f"with measure {clean_expression(_get(props, 'qDef.measure.value'))}")
        condition = condition_text(props.get("calcCondition"))
        if condition and condition.replace(" ", "") not in ("0", "=0"):
            summary.setdefault("conditions", []).append({"on": "object", "condition": condition})
            words.append(f"shown if {condition}")
        summary["controls"] = [{"action": "select", "field": dims[0].get("field")
                                or dims[0].get("expression") or dims[0].get("master_name", "")}]
        parts.append(", ".join(words))
        return True

    def _calendar(self, summary, props, parts, children, depth, seen):
        self.cube(summary, props.get("qHyperCubeDef"), alternatives=False)
        dims = summary.get("dimensions") or []
        summary.pop("_cube", None)
        summary["measures"] = []
        if not dims:
            return False
        mode = _get(props, "calendar.mode") or ("single" if props.get("singleDatePicker") else "range")
        words = [f"on {self.render_dimension(dims[0])}",
                 "single date" if mode == "single" else "date range"]
        measure = _get(props, "qHyperCubeDef.qDimensions.0.qDef.measure") or {}
        if measure.get("mode") and clean_expression(measure.get("value")):
            words.append(f"with measure {clean_expression(measure['value'])}")
        bounds = [clean_expression(props.get(k)) for k in ("minDate", "maxDate")]
        if any(bounds):
            words.append(f"within {bounds[0]} .. {bounds[1]}")
        ranges = []
        for item in props.get("daterange") or []:
            if isinstance(item, dict):
                name = item.get("customRangeName") if item.get("useCustomDateRangeName") else None
                name = name or item.get("name")
                if name:
                    ranges.append(str(name))
        if ranges:
            words.append("quick ranges " + _listed(ranges))
        summary["controls"] = [{"action": "select dates", "field": dims[0].get("field")
                                or dims[0].get("expression") or dims[0].get("master_name", "")}]
        parts.append(", ".join(words))
        self._vizlib_actions(summary, props.get("actions"), parts)
        return True

    def _animator(self, summary, props, parts, children, depth, seen):
        self.cube(summary, props.get("qHyperCubeDef"), alternatives=False)
        summary.pop("_cube", None)
        dims = summary.get("dimensions") or []
        if not dims:
            return False
        interval = _get(props, "animator.intervalTime")
        words = [f"steps through {self.render_dimension(dims[0])}"]
        if interval:
            words.append(f"every {interval} ms")
        if _get(props, "animator.loop"):
            words.append("in a loop")
        summary["controls"] = [{"action": "animate selections",
                                "field": dims[0].get("field") or dims[0].get("expression", "")}]
        parts.append(" ".join(words))
        return True

    def _variable_input(self, summary, props, parts, children, depth, seen):
        name = props.get("variableName")
        if not isinstance(name, str) or not name:
            return False
        control: Dict[str, Any] = {"action": "set variable", "variable": name}
        alternatives = []
        for item in props.get("alternatives") or []:
            if isinstance(item, dict) and item.get("value") not in (None, ""):
                value, label = str(item.get("value")), str(item.get("label") or "")
                alternatives.append(f"{value}={label}" if label and label != value else value)
        words = [f"variable {name}"]
        if alternatives:
            control["alternatives"] = alternatives
            words.append(", ".join(alternatives))
        elif clean_expression(props.get("dynamicvalues")):
            control["alternatives_expression"] = clean_expression(props.get("dynamicvalues"))
            words.append(f"values from {control['alternatives_expression']}")
        elif props.get("render") in ("l", "s"):
            words.append(f"slider {props.get('min')}..{props.get('max')} step {props.get('step')}")
        summary["controls"] = [control]
        summary["variables"] = [name]
        parts.append(": ".join(words) if len(words) > 1 else words[0])
        return True

    def _navigation_target(self, action: str, sheet_value: Any) -> str:
        if action == "goToSheetById" or (action and action.startswith("goToSheet")):
            target = literal_text(sheet_value)
            if target:
                name = self.sheet_title(target)
                return f"go to sheet {_quoted(name)}" if name else f"go to sheet {target}"
            return "go to a sheet"
        return {"firstSheet": "go to the first sheet", "lastSheet": "go to the last sheet",
                "nextSheet": "go to the next sheet", "prevSheet": "go to the previous sheet",
                "openWebsite": "open a website", "goToStory": "go to a story",
                "openChainedApp": "open another app", "odagLink": "open an ODAG app"}.get(
            action, action)

    def _action_button(self, summary, props, parts, children, depth, seen):
        label = title_text(_get(props, "style.label"))
        if label and not summary.get("title"):
            summary["title"] = label
        controls = []
        for action in props.get("actions") or []:
            if not isinstance(action, dict) or not action.get("actionType"):
                continue
            controls.append(_button_action(action["actionType"], action))
        navigation = props.get("navigation") or {}
        action = navigation.get("action")
        if action and action != "none":
            controls.append({"action": "navigate",
                             "target": self._navigation_target(action, navigation.get("sheet"))})
        if props.get("useEnabledCondition") and condition_text(props.get("enabledCondition")):
            summary.setdefault("conditions", []).append(
                {"on": "enabled", "condition": condition_text(props.get("enabledCondition"))})
        if not controls:
            return False
        summary["controls"] = controls
        parts.append(_listed([_render_control(c) for c in controls]))
        return True

    def _navigation_button(self, summary, props, parts, children, depth, seen):
        options = props.get("props") or {}
        label = title_text(options.get("buttonLabel"))
        if label and not summary.get("title"):
            summary["title"] = label
        controls = []
        for action in options.get("actionItems") or []:
            if not isinstance(action, dict) or not action.get("actionType"):
                continue
            field = action.get("selectedField")
            if field == "by-expr" or not field:
                field = action.get("field")
            controls.append(_button_action(action["actionType"], {
                "field": field, "variable": action.get("variable"),
                "value": action.get("value"), "bookmark": action.get("selectedBookmark")}))
        action = options.get("navigationAction")
        if action and action != "none":
            controls.append({"action": "navigate", "target": self._navigation_target(
                action, options.get("selectedSheet") or options.get("sheetId"))})
        if not controls:
            return False
        summary["controls"] = controls
        parts.append(_listed([_render_control(c) for c in controls]))
        return True

    def _vizlib_actions(self, summary, actions: Any, parts: List[str]) -> None:
        """Vizlib actions that change data: selections, variables, bookmarks.
        Navigation and exports are not what a figure depends on."""
        controls = []
        for action in actions or []:
            if not isinstance(action, dict):
                continue
            name = str(action.get("name") or "")
            control: Dict[str, Any] = {"action": name}
            if name in ("Select values", "Select Values", "Select field value", "Toggle value"):
                field = _name(_get(action, "field.name") or _get(action, "field.fieldName"))
                if not field:
                    continue
                control["field"] = field
                value = clean_expression(_get(action, "field.value"))
                if value:
                    control["value"] = value
            elif name == "Set Variable":
                variable = _name(_get(action, "variable.name"))
                if not variable:
                    continue
                control["variable"] = variable
                value = clean_expression(_get(action, "variable.value"))
                if value:
                    control["value"] = value
            elif name == "Apply Bookmark":
                bookmark = _name(_get(action, "bookmark.title") or _get(action, "bookmark.id"))
                if not bookmark:
                    continue
                control["bookmark"] = bookmark
            else:
                continue
            controls.append(control)
        if controls:
            summary.setdefault("controls", []).extend(controls)
            parts.append("actions " + _listed([_render_control(c) for c in controls]))

    def _vizlib_button(self, summary, props, parts, children, depth, seen):
        buttons = []
        for button in props.get("buttons") or []:
            if not isinstance(button, dict):
                continue
            label = title_text(button.get("label"))
            condition = condition_text(button.get("showCondition"))
            before = len(summary.get("controls") or [])
            local: List[str] = []
            self._vizlib_actions(summary, button.get("actions"), local)
            has_actions = len(summary.get("controls") or []) > before
            if not has_actions and not condition:
                continue
            text = _quoted(label) if label else "button"
            if local:
                text += " " + local[0]
            if condition:
                text += f" [if {cut(condition, EXPRESSION_DISPLAY_CHARS)}]"
            buttons.append(text)
        if not buttons:
            return False
        parts.append("buttons " + _listed(buttons, "; "))
        return True

    def _sheet_menu(self, summary, props, parts, children, depth, seen):
        """A sheet menu is navigation; only data-changing actions matter."""
        found: List[Dict[str, Any]] = []

        def walk(node: Any, level: int) -> None:
            if level > 8:
                return
            if isinstance(node, dict):
                if isinstance(node.get("actions"), list):
                    found.extend(a for a in node["actions"] if isinstance(a, dict))
                if isinstance(node.get("action"), list):
                    found.extend(a for a in node["action"] if isinstance(a, dict))
                for key, value in node.items():
                    if key not in ("actions", "action", "extensionMeta", "qInfo", "qMetaDef"):
                        walk(value, level + 1)
            elif isinstance(node, list):
                for item in node:
                    walk(item, level + 1)

        walk(props, 0)
        title = title_text(_get(props, "topBar.title.customText"))
        if title and not summary.get("title"):
            summary["title"] = title
        parts.append("sheet menu")
        self._vizlib_actions(summary, found, parts)
        return True

    def _container(self, summary, props, parts, children, depth, seen):
        """Tabs are the real children; labels and conditions come from the
        container's list, matched by `containerChildId`, `childRefId` or,
        for a master tab, `qExtendsId`. A tab with no child is dropped,
        a child missing from the list keeps its own title."""
        kind = summary["object_type"]
        listed = props.get("objects") if kind != "container" else None
        if not listed:
            listed = props.get("children") or []
        tabs: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        for position, tab in enumerate(listed):
            if not isinstance(tab, dict):
                continue
            key = tab.get("childRefId") or tab.get("refId")
            if not key:
                continue
            bounds = tab.get("bounds") or {}
            tabs[key] = {"label": title_text(tab.get("label")),
                         "condition": condition_text(tab.get("condition")),
                         "rank": ((bounds.get("y") or 0, bounds.get("x") or 0)
                                  if kind == "sn-layout-container" else (position, 0))}
            order.append(key)
        default = props.get("defaultTab") or props.get("defaultTabId")

        if depth >= MAX_CONTAINER_DEPTH:
            parts.append(f"{len(children)} objects inside, not expanded")
            return True
        described = []
        for node in children:
            child_props = node.get("qProperty") or {}
            keys = [child_props.get(k) for k in ("containerChildId", "childRefId", "qExtendsId")]
            tab = next((tabs[k] for k in keys if k in tabs), None)
            child = self.describe_node(node, depth + 1, seen)
            if not child:
                continue
            if tab:
                # On a layout container the label is what the property
                # panel calls the object ("Text & image"), not a name.
                if tab["label"] and kind != "sn-layout-container":
                    child["tab"] = tab["label"]
                if tab["condition"]:
                    child["shown_if"] = tab["condition"]
            if default and default in (child["object_id"],) + tuple(k for k in keys if k):
                child["default_tab"] = True
            rank = tab["rank"] if tab else (len(order) + len(described), 0)
            described.append((rank, child))
        described.sort(key=lambda pair: pair[0])
        summary["children"] = [child for _rank, child in described]
        texts = []
        for child in summary["children"]:
            text = child["object_type"]
            if child.get("title"):
                text += f" {_quoted(cut(child['title'], 80))}"
            if child.get("tab") and child.get("tab") != child.get("title"):
                text = f"{_quoted(child['tab'])} -> {text}"
            if child.get("shown_if"):
                text += f" [if {cut(child['shown_if'], EXPRESSION_DISPLAY_CHARS)}]"
            texts.append(text)
        noun = "objects" if kind == "sn-layout-container" else "tabs"
        parts.append(f"{len(texts)} {noun}: " + _listed(texts, "; ") if texts else f"no {noun}")
        return True

    def _master_children(self, ids_and_labels: List[Tuple[str, str, Optional[str]]],
                         depth: int, seen: set) -> List[Dict[str, Any]]:
        """Master visualizations shown inside an object, described by
        their own rules; the object only points at them."""
        children, listed = [], set()
        for master_id, label, condition in ids_and_labels:
            # A grid tab repeats its first cell as the tab's own master.
            if master_id in listed:
                continue
            listed.add(master_id)
            props = self.library.visualizations.get(master_id)
            if depth >= MAX_CONTAINER_DEPTH or props is None:
                child = {"object_id": master_id, "object_type": "masterobject",
                         "summary": f"master visualization {master_id}"
                                    + ("" if props is None else ", not expanded")}
            else:
                child = self.describe(props, (), depth + 1, seen)
                if not child:
                    continue
                child["master_object"] = {"id": master_id}
                if child.get("title"):
                    child["master_object"]["title"] = child["title"]
            if label:
                child["tab"] = label
            if condition:
                child["shown_if"] = condition
            children.append(child)
        return children

    def _vizlib_container(self, summary, props, parts, children, depth, seen):
        wanted = []
        for tab in props.get("containerObjects") or []:
            if not isinstance(tab, dict):
                continue
            label = title_text(tab.get("label"))
            condition = condition_text(tab.get("condition"))
            if tab.get("viewType") == "grid":
                cells = [c.get("masterItemId") for c in _get(tab, "gridView.masterItems") or []
                         if isinstance(c, dict) and c.get("cellId") and c.get("masterItemId")]
            else:
                cells = [tab.get("masterItemId")] if tab.get("masterItemId") else []
            for master_id in cells:
                wanted.append((master_id, label, condition))
        summary["children"] = self._master_children(wanted, depth, seen)
        style = _get(props, "containerSettings.containerStyle") or "tabs"
        texts = []
        for child in summary["children"]:
            text = child["object_type"] + (f" {_quoted(child['title'])}" if child.get("title") else "")
            text = f"{_quoted(child['tab'])} -> {text} {child['object_id']}" if child.get("tab") \
                else f"{text} {child['object_id']}"
            if child.get("shown_if"):
                text += f" [if {cut(child['shown_if'], EXPRESSION_DISPLAY_CHARS)}]"
            texts.append(text)
        parts.append(f"{style}: " + _listed(texts, "; ") if texts else "empty")
        return True

    def _trellis(self, summary, props, parts, children, depth, seen):
        self.cube(summary, props.get("qHyperCubeDef"), alternatives=False)
        summary.pop("_cube", None)
        viz_id = _get(props, "prop.vizId")
        words = []
        if isinstance(viz_id, str) and viz_id:
            summary["children"] = self._master_children([(viz_id, "", None)], depth, seen)
            child = summary["children"][0] if summary["children"] else {}
            words.append(f"repeats master {viz_id}"
                         + (f" {_quoted(child['title'])}" if child.get("title") else ""))
        dims = summary.get("dimensions") or []
        if dims:
            words.append("by " + self.render_dimension(dims[0]))
        charts = _get(props, "prop.maxCharts")
        if charts:
            words.append(f"up to {charts} charts")
        parts.append(" ".join(words))
        child = (summary.get("children") or [{}])[0]
        if child.get("object_type") not in (None, "masterobject"):
            parts.append(f"chart: {child['summary']}")
        return True

    def _auto_chart(self, summary, props, parts, children, depth, seen):
        generated = _get(props, "qUndoExclude.generated") or {}
        chosen = generated.get("visualization") if isinstance(generated, dict) else None
        if chosen:
            summary["options"] = {"shown_as": chosen}
        parts.append(f"shown as {chosen}" if chosen else "")
        state = generated.get("qStateName") if isinstance(generated, dict) else None
        if isinstance(state, str) and state not in ("", "$"):
            summary["state"] = state
        return self._chart(summary, props, parts, children, depth, seen)

    def _universal(self, summary: Dict[str, Any], props: Dict[str, Any],
                   parts: List[str]) -> bool:
        """An extension without a rule: cubes, list objects and expressions
        found anywhere in its properties, minus colour, style and constants."""
        cubes: List[Tuple[str, Dict[str, Any]]] = []
        lists: List[Dict[str, Any]] = []
        expressions: List[Dict[str, str]] = []
        references: List[str] = []

        def styled(path: List[str]) -> bool:
            if "color~measure" in path:
                return False
            return any(any(word in segment.lower() for word in _STYLE_WORDS)
                       for segment in path if segment != "color~measure")

        def add(path: List[str], value: Any) -> None:
            # A URL is checked before comments are cut: `//` starts both.
            if _URL.match(expression_text(value).strip()):
                return
            text = clean_expression(value)
            if not text or is_noise_expression(text) or styled(path):
                return
            expressions.append({"path": ".".join(path), "expression": text})

        def walk(node: Any, path: List[str]) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in _UNIVERSAL_DROP:
                        continue
                    here = path + [key]
                    if key == "color" and isinstance(value, dict) \
                            and value.get("mode") in ("byExpression", "byMeasure"):
                        here = path + ["color~measure"]
                    if key == "qHyperCubeDef" and isinstance(value, dict):
                        cubes.append((".".join(here), value))
                    elif key == "qListObjectDef" and isinstance(value, dict):
                        lists.append(value)
                    elif key in ("qStringExpression", "qValueExpression") and isinstance(value, dict):
                        add(path, value)
                    elif key == "qLibraryId" and isinstance(value, str) and value:
                        references.append(value)
                    elif isinstance(value, str):
                        if key == "qExpr" or key.endswith("Expression") or key.endswith("expression") \
                                or (value.strip().startswith("=") and len(value.strip()) > 2):
                            add(here, value)
                    elif key not in ("title", "subtitle", "footnote"):
                        walk(value, here)
            elif isinstance(node, list):
                for index, item in enumerate(node):
                    walk(item, path[:-1] + [f"{path[-1]}[{index}]"] if path else [f"[{index}]"])

        walk(props, [])
        for _path, cube in cubes:
            self.cube(summary, cube, alternatives=False)
        for list_def in lists:
            entry = self._list_object(list_def)
            if entry:
                summary.setdefault("dimensions", []).append(entry)
        known = {measure_text(c.get("expression")) for c in
                 (summary.get("measures") or []) + (summary.get("dimensions") or [])}
        unique, texts = [], set()
        for item in expressions:
            # A copy of a measure of the same cube (a tooltip, a colour by
            # the same formula) says nothing new.
            key = measure_text(item["expression"])
            if key in known or key in texts:
                continue
            texts.add(key)
            unique.append(item)
        if unique:
            summary["expressions"] = unique
        for library_id in references:
            used = {c.get("master_id") for c in summary.get("dimensions", []) + summary.get("measures", [])}
            if library_id not in used:
                summary.setdefault("master_items", []).append(library_id)
        self.render_columns(summary, parts)
        if unique:
            parts.append("expressions " + _listed(
                [f"{e['path']} = {cut(e['expression'], EXPRESSION_DISPLAY_CHARS)}" for e in unique]))
        if summary.get("master_items"):
            parts.append("master items " + ", ".join(summary["master_items"]))
        return True


_UNIVERSAL_DROP = frozenset({
    "extensionMeta", "qLayoutExclude", "qInfo", "qMetaDef", "components", "qInitialDataFetch",
    "qSortCriterias", "qNumFormat", "qOtherTotalSpec", "qMiniChartDef", "qTrendLines",
    "qExpansionState", "qUndoExclude", "qChildListDef", "qAttributeExpressions",
    "qAttributeDimensions", "visualization", "version", "qStateName",
})

# Roles fixed by position, where the expression does not say what a column is for.
_DIMENSION_ROLES: Dict[str, List[Optional[str]]] = {
    "scatterplot": ["points"],
    "distributionplot": ["points", "groups"],
    "boxplot": ["points", "groups"],
    "VizlibHeatmap": ["rows", "columns"],
    "sn-grid-chart": ["x", "y"],
    "sn-org-chart": ["node", "parent"],
    "qlik-funnel-chart-ext": ["stages"],
    "linechart": [None, "lines by"],
    "VizlibLineChart": [None, "lines by"],
    "mekkochart": [None, "segments by"],
}
_MEASURE_ROLES: Dict[str, List[Optional[str]]] = {
    "scatterplot": ["x", "y", "size"],
    "qlik-bullet-chart": ["value", "target", "range"],
}
_PATH_TYPES = frozenset({"qlik-sankey-chart-ext", "VizlibSankeyChart"})
_TABLE_TYPES = frozenset({"table", "sn-table", "VizlibTable", "pivot-table", "sn-pivot-table",
                          "VizlibPivotTable"})
# Chart types that mean nothing without columns: an unconfigured insight.
_DROP_WHEN_EMPTY = frozenset({"sn-nlg-chart"})
_OPTION_ORDER = ("grouping", "stacked_area", "reference_lines", "scale", "segments",
                 "box", "box_elements", "max_records", "funnel", "labels_as", "trend_lines")


def _render_option(key: str, value: Any) -> str:
    if key == "grouping":
        return f"bars {value}"
    if key == "stacked_area":
        return "stacked area"
    if key == "reference_lines":
        return "reference lines " + _listed(value)
    if key == "scale":
        return f"scale {value[0]} .. {value[1]}"
    if key == "segments":
        return "segments at " + ", ".join(value)
    if key == "box":
        return f"boxes by {value}"
    if key == "box_elements":
        return "elements " + _listed(value)
    if key == "max_records":
        return f"max records {value}"
    if key == "funnel":
        return f"funnel {value}"
    if key == "labels_as":
        return f"labels as {value}"
    if key == "trend_lines":
        return "trend lines " + ", ".join(value)
    return f"{key} {value}"


# Which keys each button action uses. The rest keep whatever the author
# typed before switching the action, and read as if the action used them.
_ACTION_KEYS = {
    "clearAll": (), "lockAll": (), "unlockAll": (), "back": (), "forward": (),
    "clearAllButThis": ("field",), "clearField": ("field",), "selectAll": ("field",),
    "selectAlternatives": ("field",), "selectExcluded": ("field",),
    "selectPossible": ("field",), "lockField": ("field",), "unlockField": ("field",),
    "selectValues": ("field", "value"), "selectMatchingValues": ("field", "value"),
    "toggleSelect": ("field", "value"), "selectField": ("field", "value"),
    "setVariable": ("variable", "value"), "applyBookmark": ("bookmark",),
    "moveBookmark": ("bookmark",),
}


def _button_action(action_type: str, action: Dict[str, Any]) -> Dict[str, Any]:
    control: Dict[str, Any] = {"action": action_type}
    for key in _ACTION_KEYS.get(action_type, ("field", "variable", "bookmark", "value")):
        text = clean_expression(action.get(key)) if key == "value" else _name(action.get(key))
        if text:
            control[key] = text
    return control


def _render_control(control: Dict[str, Any]) -> str:
    action = control.get("action", "")
    if action == "navigate":
        return control.get("target", "navigate")
    words = [action]
    if control.get("field"):
        words.append(control["field"])
    if control.get("variable"):
        words.append(control["variable"])
    if control.get("bookmark"):
        words.append(f"bookmark {control['bookmark']}")
    text = " ".join(words)
    if control.get("value"):
        text += f" to {cut(control['value'], EXPRESSION_DISPLAY_CHARS)}"
    return text


def _public(value: Any) -> Any:
    """Drop the server's own keys (leading underscore) at every level."""
    if isinstance(value, dict):
        return {k: _public(v) for k, v in value.items() if not k.startswith("_")}
    if isinstance(value, list):
        return [_public(v) for v in value]
    return value


public = _public


# -- details of single types ---------------------------------------------------

def _bar_details(parser: ObjectParser, summary, props, cube) -> None:
    options = summary.setdefault("options", {})
    if len(summary.get("dimensions") or []) > 1:
        grouping = _get(props, "barGrouping.grouping")
        if grouping:
            options["grouping"] = grouping
    lines = []
    for line in _get(props, "refLine.refLines") or props.get("yRefLines") or []:
        if not isinstance(line, dict):
            continue
        value = clean_expression(_get(line, "refLineExpr.value") or line.get("value"))
        label = literal_text(_get(line, "refLineExpr.label") or line.get("label"))
        if value:
            lines.append(f"{label} = {value}" if label and not _same(label, value) else value)
    if lines:
        options["reference_lines"] = lines
    if not options:
        summary.pop("options")


def _line_details(parser: ObjectParser, summary, props, cube) -> None:
    _bar_details(parser, summary, props, cube)
    if props.get("stackedArea"):
        summary.setdefault("options", {})["stacked_area"] = True


def _combo_details(parser: ObjectParser, summary, props, cube) -> None:
    raw = [m for m in cube.get("qMeasures") or [] if isinstance(m, dict)]
    for entry, measure in zip(summary.get("measures") or [], raw):
        inner = measure.get("qDef") or {}
        series = inner.get("series") or {}
        custom = inner.get("custom") or {}
        kind = custom.get("markerType") or series.get("type")
        if not kind:
            continue
        role = {"symbol": "marker"}.get(kind, kind)
        if series.get("axis") == 1 or custom.get("axisPosition") == "secondary":
            role += " (axis 2)"
        entry["role"] = role
    max_records = expression_text(props.get("maxRecords"))
    if max_records and max_records.strip().startswith("="):
        summary.setdefault("options", {})["max_records"] = clean_expression(max_records)
    _bar_details(parser, summary, props, cube)


def _gauge_details(parser: ObjectParser, summary, props, cube) -> None:
    axis = props.get("measureAxis") or {}
    low, high = clean_expression(axis.get("min")), clean_expression(axis.get("max"))
    options = summary.setdefault("options", {})
    if low or high:
        options["scale"] = [low or "0", high or "?"]
    if props.get("useSegments"):
        limits = [clean_expression(limit.get("value")) for limit in
                  _get(props, "segmentInfo.limits") or [] if isinstance(limit, dict)]
        if any(limits):
            options["segments"] = [x for x in limits if x]
    if not options:
        summary.pop("options")


def _bullet_details(parser: ObjectParser, summary, props, cube) -> None:
    raw = [m for m in cube.get("qMeasures") or [] if isinstance(m, dict)]
    plan = summary.get("_cube") or {}
    for entry, measure in zip(summary.get("measures") or [], raw):
        target = (measure.get("qDef") or {}).get("target")
        text = clean_expression(target)
        if text:
            entry["target"] = text
            plan.setdefault("extra_measures", []).append(
                {"expression": expression_text(target),
                 "label": f"{entry['_cube']['label']} target"})


def _waterfall_details(parser: ObjectParser, summary, props, cube) -> None:
    raw = [m for m in cube.get("qMeasures") or [] if isinstance(m, dict)]
    vizlib = summary["object_type"] == "VizlibWaterfall"
    for entry, measure in zip(summary.get("measures") or [], raw):
        inner = measure.get("qDef") or {}
        value_type = inner.get("valueType")
        if value_type:
            entry["role"] = {"NORMAL": "step" if vizlib else "+", "INVERSE": "-",
                             "SUBTOTAL": "start" if vizlib else "subtotal"}.get(value_type, value_type)
        subtotal = inner.get("subtotal") or {}
        if isinstance(subtotal, dict) and subtotal.get("enable"):
            entry["subtotal"] = literal_text(subtotal.get("label")) or "subtotal"


def _scatter_details(parser: ObjectParser, summary, props, cube) -> None:
    lines = []
    for measure in cube.get("qMeasures") or []:
        for trend in (measure or {}).get("qTrendLines") or []:
            if isinstance(trend, dict) and trend.get("qType"):
                lines.append(str(trend["qType"]).lower())
    if lines:
        summary.setdefault("options", {})["trend_lines"] = lines


def _boxplot_details(parser: ObjectParser, summary, props, cube) -> None:
    calculations = _get(props, "boxplotDef.calculations") or {}
    options = summary.setdefault("options", {})
    if calculations.get("auto", True):
        mode = calculations.get("mode") or "tukey"
        parameter = (calculations.get("parameters") or {}).get(mode)
        options["box"] = f"{mode} {parameter}" if parameter is not None else mode
    else:
        elements = []
        for name in ("firstWhisker", "boxStart", "boxMiddle", "boxEnd", "lastWhisker"):
            element = _get(props, f"boxplotDef.elements.{name}") or {}
            text = clean_expression(element.get("expression"))
            if text:
                elements.append(f"{title_text(element.get('name')) or name} = {text}")
        if elements:
            options["box_elements"] = elements


def _funnel_details(parser: ObjectParser, summary, props, cube) -> None:
    options = summary.setdefault("options", {})
    if _get(props, "funnel.mode"):
        options["funnel"] = str(_get(props, "funnel.mode")).lower()
    if _get(props, "dataPoint.labelMode"):
        options["labels_as"] = _get(props, "dataPoint.labelMode")
    if not options:
        summary.pop("options")


def _vizlib_pivot_details(parser: ObjectParser, summary, props, cube) -> None:
    raw = [d for d in cube.get("qDimensions") or [] if isinstance(d, dict)]
    for entry, dim in zip(summary.get("dimensions") or [], raw):
        if (dim.get("qDef") or {}).get("topDim"):
            entry["role"] = "columns"


_TYPE_DETAILS: Dict[str, Callable[..., None]] = {
    "barchart": _bar_details,
    "VizlibBarChart": _bar_details,
    "linechart": _line_details,
    "combochart": _combo_details,
    "VizlibComboChart": _combo_details,
    "gauge": _gauge_details,
    "bulletchart": _bullet_details,
    "waterfallchart": _waterfall_details,
    "VizlibWaterfall": _waterfall_details,
    "scatterplot": _scatter_details,
    "boxplot": _boxplot_details,
    "qlik-funnel-chart-ext": _funnel_details,
    "VizlibPivotTable": _vizlib_pivot_details,
}

_CHART_TYPES = (
    "barchart", "linechart", "piechart", "treemap", "mekkochart", "table", "sn-table", "kpi",
    "sn-nlg-chart", "pivot-table", "sn-pivot-table", "combochart", "scatterplot",
    "bulletchart", "gauge", "waterfallchart", "distributionplot", "boxplot",
    "VizlibBarChart", "VizlibLineChart", "VizlibPieChart", "VizlibTable", "VizlibPivotTable",
    "VizlibHeatmap", "VizlibSankeyChart", "VizlibComboChart", "VizlibWaterfall",
    "qlik-funnel-chart-ext", "qlik-sankey-chart-ext", "qlik-bullet-chart", "sn-grid-chart",
    "sn-org-chart",
)

_HANDLERS: Dict[str, Callable[..., Any]] = {kind: ObjectParser._chart for kind in _CHART_TYPES}
_HANDLERS.update({
    "auto-chart": ObjectParser._auto_chart,
    "histogram": ObjectParser._histogram,
    "map": ObjectParser._map,
    "ideviomap": ObjectParser._idevio_map,
    "ideviobubblelayer": ObjectParser._idevio_layer,
    "idevioarealayer": ObjectParser._idevio_layer,
    "ideviolinelayer": ObjectParser._idevio_layer,
    "ideviogeodatalayer": ObjectParser._idevio_layer,
    "text-image": ObjectParser._text_image,
    "sn-text": ObjectParser._sn_text,
    "VizlibAdvancedTextObject": ObjectParser._advanced_text,
    "ShowHTMLfromMeasure": ObjectParser._html_measure,
    "qlik-multi-kpi": ObjectParser._multi_kpi,
    "VizlibKPI": ObjectParser._kpi_layers,
    "listbox": ObjectParser._listbox,
    "filterpane": ObjectParser._filterpane,
    "qlik-date-picker": ObjectParser._date_picker,
    "VizlibFilter": ObjectParser._vizlib_filter,
    "VizlibCalendar": ObjectParser._calendar,
    "qlik-animator": ObjectParser._animator,
    "idevioanimator": ObjectParser._animator,
    "qlik-variable-input": ObjectParser._variable_input,
    "action-button": ObjectParser._action_button,
    "qlik-button-for-navigation": ObjectParser._navigation_button,
    "VizlibButton": ObjectParser._vizlib_button,
    "VizlibSheetMenu": ObjectParser._sheet_menu,
    "VizlibSheetMenuPlus": ObjectParser._sheet_menu,
    "container": ObjectParser._container,
    "sn-tabbed-container": ObjectParser._container,
    "sn-layout-container": ObjectParser._container,
    "VizlibContainer": ObjectParser._vizlib_container,
    "qlik-trellis-container": ObjectParser._trellis,
})


def fields_used(summary: Dict[str, Any]) -> List[str]:
    """Fields an object and everything inside it refers to."""
    fields = set()
    columns = (summary.get("dimensions") or []) + (summary.get("measures") or []) \
        + (summary.get("alt_dimensions") or []) + (summary.get("alt_measures") or [])
    for layer in summary.get("layers") or []:
        columns += ([layer["dimension"]] if layer.get("dimension") else []) + (layer.get("measures") or [])
    for column in columns:
        if column.get("field"):
            fields.add(column["field"])
        for field in column.get("fields") or column.get("drill_down") or []:
            text = str(field or "").strip()
            if text.startswith("="):
                fields.update(fields_in_expression(text))
            elif text:
                fields.add(text[1:-1] if text.startswith("[") and text.endswith("]") else text)
        for key in ("expression", "target"):
            if column.get(key):
                fields.update(fields_in_expression(column[key]))
    for control in summary.get("controls") or []:
        if control.get("field") and control.get("action") != "navigate":
            field = control["field"]
            if field.startswith("="):
                fields.update(fields_in_expression(field))
            else:
                fields.add(field)
    for item in summary.get("expressions") or []:
        fields.update(fields_in_expression(item["expression"]))
    fields.update(summary.get("tooltip_fields") or [])
    if summary.get("text_expression"):
        fields.update(fields_in_expression(summary["text_expression"]))
    for child in summary.get("children") or []:
        fields.update(fields_used(child))
    return sorted(fields)
