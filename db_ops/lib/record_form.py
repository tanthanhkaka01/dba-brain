"""Turning one config record into editable fields, and back — losslessly.

The console showed a config record as raw JSON in a textarea. It is honest and it is unreadable:
a metric definition is ninety lines of braces, and finding ``repeat_interval`` in it is worse than
opening the file. This module is what lets the same record be drawn as a grid of named fields.

The whole difficulty is that a *generated form* is normally lossy. These records have no fixed
shape — a SQL target carries a nested ``time_window`` and a ``notify`` block, a metric definition
carries per-``db_type`` ``variants``, ``users.json`` carries a list of credential objects — and a
form built from a hand-written list of known fields silently drops everything it was not told
about. The first save would then delete config nobody meant to touch.

So the form is generated **from the record itself**, and the contract is one property:

    rebuild(flatten(record)) == record

for every record in ``data/``, held down by ``tests/test_record_form.py``. Nothing is dropped
because every leaf becomes a field, and nothing is corrupted because each field carries the JSON
type it came from — ``0`` stays a number, ``false`` stays a boolean, ``null`` stays null, and an
empty string stays an empty string rather than becoming one of the others.

Three shapes get special handling, and each earns it:

* **A nested object** is a section, not a field. ``time_window`` renders as a heading with its own
  rows under it, which is how an operator already thinks of it.
* **A list of scalars** (``db_types``, ``metric_codes``) is one box, one item per line. A row per
  element would make a fourteen-metric policy unreadable and adding one impossible.
* **Anything else** — a list of objects, most of all — keeps a small JSON box **for that subtree
  only**. Still lossless, still local: the reader is looking at one field's worth of JSON instead
  of the whole record.

The path is carried as JSON rather than a dotted string because a config key may contain a dot,
and inventing an escaping scheme for one is how a field silently lands in the wrong place.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field as dataclass_field
from typing import Any

#: Field name prefix in the submitted form. The name is ``f:<kind>:<json path>``.
FIELD_PREFIX = "f"

KIND_STR = "str"
KIND_INT = "int"
KIND_FLOAT = "float"
KIND_BOOL = "bool"
KIND_NULL = "null"
KIND_JSON = "json"
#: A list of scalars, rendered as one item per line. The element kind rides along as
#: ``list-int``, ``list-str``, ... — a dash and not a colon, because the field name is split on
#: colons and a kind carrying one would swallow the start of the path.
KIND_LIST_PREFIX = "list-"

_SCALAR_KINDS = {KIND_STR, KIND_INT, KIND_FLOAT, KIND_BOOL, KIND_NULL}


class RecordFormError(ValueError):
    """A submitted field cannot be read back as the value it claims to be."""


@dataclass(frozen=True)
class Field:
    """One editable leaf of a record, with everything a template needs to draw it."""

    path: tuple[str, ...]
    kind: str
    value: Any
    #: How deep the field sits, so a nested object can be indented rather than flattened away.
    depth: int = 0

    @property
    def name(self) -> str:
        """The form field name: kind and path, so the parser needs no second source."""
        return f"{FIELD_PREFIX}:{self.kind}:{json.dumps(list(self.path), ensure_ascii=False)}"

    @property
    def label(self) -> str:
        return self.path[-1] if self.path else ""

    @property
    def is_list(self) -> bool:
        return self.kind.startswith(KIND_LIST_PREFIX)

    @property
    def text(self) -> str:
        """The value as the operator will see and edit it."""
        if self.is_list:
            return "\n".join(_scalar_text(item) for item in (self.value or []))
        if self.kind == KIND_JSON:
            return json.dumps(self.value, ensure_ascii=False, indent=2)
        if self.kind == KIND_NULL:
            return ""
        return _scalar_text(self.value)


@dataclass(frozen=True)
class Section:
    """A nested object, drawn as a heading above the fields it contains."""

    path: tuple[str, ...]
    depth: int = 0
    #: Whether the object was empty. An empty block still has to survive the round trip, and it has
    #: no fields to carry it, so the section itself is what rebuilds it.
    empty: bool = False

    @property
    def label(self) -> str:
        return self.path[-1] if self.path else ""

    @property
    def name(self) -> str:
        return f"{FIELD_PREFIX}:section:{json.dumps(list(self.path), ensure_ascii=False)}"


@dataclass
class Layout:
    """A record flattened into the order it should be drawn."""

    rows: list[Field | Section] = dataclass_field(default_factory=list)

    @property
    def fields(self) -> list[Field]:
        return [row for row in self.rows if isinstance(row, Field)]


def _scalar_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def kind_of(value: Any) -> str:
    """The field kind for one value. ``bool`` is checked before ``int`` — it is a subclass."""
    if value is None:
        return KIND_NULL
    if isinstance(value, bool):
        return KIND_BOOL
    if isinstance(value, int):
        return KIND_INT
    if isinstance(value, float):
        return KIND_FLOAT
    if isinstance(value, str):
        return KIND_STR
    if isinstance(value, list):
        if value and all(_is_scalar(item) for item in value):
            return KIND_LIST_PREFIX + kind_of(_first_non_null(value))
        # An empty list, or a list of objects. Empty is typed as strings because there is nothing
        # to infer from and the rebuild only has to give back an empty list either way.
        return KIND_LIST_PREFIX + KIND_STR if not value else KIND_JSON
    return KIND_JSON


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (bool, int, float, str))


def _first_non_null(values: list[Any]) -> Any:
    for item in values:
        if item is not None:
            return item
    return ""


def flatten(payload: Any, *, path: tuple[str, ...] = (), depth: int = 0) -> Layout:
    """Walk a record into the rows a template draws, in the record's own key order.

    Key order is preserved deliberately: the store keeps records in the order their file has them
    (see :func:`db_ops.db.config_store.canonical_json`), and a form that reordered them would
    reorder the file on the first save.
    """
    layout = Layout()
    if not isinstance(payload, dict):
        layout.rows.append(Field(path=path, kind=KIND_JSON, value=payload, depth=depth))
        return layout

    for key, value in payload.items():
        here = path + (str(key),)
        if isinstance(value, dict):
            layout.rows.append(Section(path=here, depth=depth, empty=not value))
            layout.rows.extend(flatten(value, path=here, depth=depth + 1).rows)
            continue
        layout.rows.append(Field(path=here, kind=kind_of(value), value=value, depth=depth))
    return layout


def rebuild(submitted: dict[str, list[str]]) -> dict[str, Any]:
    """Reassemble a record from what the form posted. The inverse of :func:`flatten`.

    ``submitted`` is the raw ``parse_qs`` mapping — **lists**, not single values — because a
    checkbox posts its hidden default *and* its own value when ticked, and only the last one is
    the answer. Reading the first would make every checkbox permanently false.

    Fields are applied in the order they arrive, which is the order the browser walks the form,
    which is the order :func:`flatten` produced. That is what preserves the record's key order.
    """
    record: dict[str, Any] = {}
    for name, values in submitted.items():
        parts = name.split(":", 2)
        if len(parts) != 3 or parts[0] != FIELD_PREFIX:
            continue
        _, kind, raw_path = parts
        try:
            path = [str(item) for item in json.loads(raw_path)]
        except ValueError as exc:
            raise RecordFormError(f"Field '{name}' has an unreadable path.") from exc
        if not path:
            continue
        if kind == "section":
            # Only an *empty* object needs the section row; a populated one is created on the way
            # to its first leaf. Setting it unconditionally would wipe the fields that follow.
            _ensure_container(record, path)
            continue
        _assign(record, path, _coerce(kind, values[-1], field_name=name))
    return record


def _ensure_container(record: dict[str, Any], path: list[str]) -> None:
    node = record
    for key in path[:-1]:
        node = node.setdefault(key, {})
        if not isinstance(node, dict):
            return
    node.setdefault(path[-1], {})


def _assign(record: dict[str, Any], path: list[str], value: Any) -> None:
    node = record
    for key in path[:-1]:
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            node[key] = nxt
        node = nxt
    node[path[-1]] = value


def _coerce(kind: str, text: str, *, field_name: str) -> Any:
    """Read one submitted string back as the JSON value its kind promises.

    Every failure names the field and what was expected. A config form that answered "invalid
    literal for int()" would send the operator to look at a traceback instead of at the box they
    just typed in.
    """
    if kind.startswith(KIND_LIST_PREFIX):
        element_kind = kind[len(KIND_LIST_PREFIX):] or KIND_STR
        items = [line.strip() for line in str(text).replace("\r\n", "\n").split("\n")]
        return [_coerce(element_kind, item, field_name=field_name)
                for item in items if item != ""]

    if kind == KIND_JSON:
        try:
            return json.loads(text or "null")
        except ValueError as exc:
            raise RecordFormError(f"{_label(field_name)}: not valid JSON ({exc}).") from exc

    if kind == KIND_NULL:
        # The value was null. An empty box means it still is; anything typed becomes a string,
        # which is the only reading that does not require guessing what was meant.
        return None if str(text).strip() == "" else str(text)

    if kind == KIND_BOOL:
        return str(text).strip().lower() in {"1", "true", "yes", "on"}

    if kind == KIND_INT:
        raw = str(text).strip()
        if raw == "":
            return None
        try:
            return int(raw)
        except ValueError as exc:
            raise RecordFormError(
                f"{_label(field_name)}: '{raw}' is not a whole number.") from exc

    if kind == KIND_FLOAT:
        raw = str(text).strip()
        if raw == "":
            return None
        try:
            return float(raw)
        except ValueError as exc:
            raise RecordFormError(f"{_label(field_name)}: '{raw}' is not a number.") from exc

    return str(text)


def _label(field_name: str) -> str:
    """The dotted path a person would say, for an error message."""
    try:
        return ".".join(json.loads(field_name.split(":", 2)[2]))
    except (IndexError, ValueError):
        return field_name


def has_form_fields(submitted: dict[str, list[str]]) -> bool:
    """Did this submission come from the grid rather than the JSON box?"""
    return any(name.startswith(f"{FIELD_PREFIX}:") for name in submitted)
