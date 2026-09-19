"""The field-level reference for the shared configuration objects, as data.

``time_window``, ``notify``, ``cmd_access`` and the rest are parsed once in this layer and appear
inside many ``data/*.json`` records. What each of their fields *means* — required or not, what
values it takes, and what its number is measured against — lived only in docstrings and in
``docs/configuration.md``, so the answer to "what is ``retry_interval``?" was unreachable from the
console, from the bot, and from anything that is not a person reading Python.

So the reference is a file: ``data/shared_config_objects.json``, with the same content shipped
inside this package as the seed ``init`` writes. This module is the only reader, and
``db_ops.common.cli describe-object`` is the only caller — the CLI exists so an app never imports
it, the same rule every shared operation follows.

**It describes; it does not decide.** Nothing in the scheduler, the parsers or the validators reads
this file, and editing it changes nothing an app does. That makes it exactly the kind of file
``data/README.md`` warns about — config nothing acts on — which is why
``tests/test_shared_config_objects_reference.py`` checks it against the code it describes: a field
added to :class:`db_ops.lib.time_window.TimeWindow` and not to this file fails the suite. A
reference that cannot drift is worth having; one that can is a lie with a schema.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from db_ops.lib.json_io import load_json_file
from db_ops.lib.paths import DEFAULT_DATA_DIR, reference_file

#: The file, by the name it has in every one of its three homes.
FILENAME = "shared_config_objects.json"

class SharedObjectError(ValueError):
    """The reference is missing, unreadable, or does not name the object asked for."""


def reference_path(data_dir: str | Path | None = None) -> Path:
    """Where the reference is read from — :func:`db_ops.lib.paths.reference_file` for this file."""
    return reference_file(FILENAME, data_dir)


def load(data_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """Every described object, in the file's own order."""
    path = reference_path(data_dir)
    try:
        payload = load_json_file(path)
    except Exception as exc:  # noqa: BLE001 - the reason matters more than the class
        raise SharedObjectError(f"{path}: {exc}") from exc
    objects = payload.get("shared_config_objects")
    if not isinstance(objects, list) or not objects:
        raise SharedObjectError(f"{path} describes no shared_config_objects.")
    return [item for item in objects if isinstance(item, dict)]


def names(data_dir: str | Path | None = None) -> list[str]:
    """The objects this build can describe, for an error message and for a listing."""
    return [str(item.get("object") or "") for item in load(data_dir)]


def describe(name: str, *, data_dir: str | Path | None = None,
             field: str = "") -> dict[str, Any]:
    """One object's entry, or — with ``field`` — one field's.

    Names are matched case-insensitively and a ``time-window`` spelling is accepted for
    ``time_window``: the person asking is typing into a chat, not writing a config file.
    """
    wanted = str(name or "").strip().lower().replace("-", "_")
    if not wanted:
        raise SharedObjectError(
            f"name is required. This build describes: {', '.join(names(data_dir))}.")
    for item in load(data_dir):
        if str(item.get("object") or "").strip().lower() == wanted:
            if not field:
                return item
            return _field(item, field)
    raise SharedObjectError(
        f"no shared config object named '{name}'. This build describes: "
        f"{', '.join(names(data_dir))}.")


def _field(item: dict[str, Any], field: str) -> dict[str, Any]:
    wanted = str(field).strip().lower()
    for entry in item.get("fields") or []:
        if str(entry.get("field") or "").strip().lower() == wanted:
            return {"object": item.get("object"), **entry}
    available = ", ".join(str(entry.get("field")) for entry in item.get("fields") or [])
    raise SharedObjectError(
        f"{item.get('object')} has no field '{field}'. It has: {available}.")


def field_names(name: str, *, data_dir: str | Path | None = None) -> list[str]:
    """The fields one object's entry lists — what the guard test compares against the code."""
    return [str(entry.get("field") or "")
            for entry in describe(name, data_dir=data_dir).get("fields") or []]

# --------------------------------------------------------------------------- #
# Holding the estate to the reference
# --------------------------------------------------------------------------- #
#: What one violation is called. Three kinds, because they are three different mistakes and an
#: operator fixes them differently:
#:
#: * ``missing`` — a required field is not there. The 2026-09-11 finding: six of fourteen restore
#:   entries carried no ``cleanup_retention`` and each ran on a default nobody had chosen.
#: * ``value`` — the field is there and its value is outside what the reference says it accepts.
#: * ``unknown`` — a field the reference does not describe. Usually a typo (``to_hours``), and a
#:   typo in a schedule is silent: the parser ignores what it does not recognise, so the schedule
#:   runs on the default and looks configured.
#:
#: A ``deprecated`` finding is the fourth and is not a violation: the parser still reads the older
#: spelling, so it is reported and counted separately rather than failing anything.
VIOLATION_KINDS = ("missing", "value", "unknown")

#: Reported and counted, never failed. ``deprecated`` is an older spelling the parser still reads;
#: ``unlisted`` is a value outside an ``open_enum``, which the estate is allowed to extend — a
#: Telegram level it defined in ``telegram_groups.json``, a shell this list has not heard of. They
#: are two different sentences to the reader and were one until the first run over this estate
#: reported 110 routing levels as deprecated.
DEPRECATED_KIND = "deprecated"
UNLISTED_KIND = "unlisted"
NOTICE_KINDS = (DEPRECATED_KIND, UNLISTED_KIND)

_TRUE_TEXT = {"1", "true", "yes", "y", "on"}
_FALSE_TEXT = {"0", "false", "no", "n", "off"}


def _finding(kind: str, where: str, field: str, detail: str) -> dict[str, str]:
    return {"kind": kind, "where": where, "field": field, "detail": detail}


def _as_int(value: Any) -> int | None:
    """``value`` as an int, or ``None`` when it is not one.

    A bool is refused even though Python says ``True == 1``: ``"repeat_interval": true`` is a
    mistake, and a checker that accepts it as 1 turns a schedule into a one-second loop silently.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    try:
        return int(text)
    except ValueError:
        return None


def check_value(constraint: dict[str, Any], value: Any, *, where: str, field: str,
                record: dict[str, Any] | None = None,
                in_config: bool = True) -> list[dict[str, str]]:
    """Every way ``value`` fails one field's ``constraint``, as findings.

    Pure, and it takes the constraint rather than looking it up, so a caller can check a value it
    has not read from a file — a console form, a Telegram argument, a proposed edit.
    """
    findings: list[dict[str, str]] = []
    kind = str(constraint.get("kind") or "")

    if constraint.get("forbidden_in_config") and in_config and value not in (None, ""):
        findings.append(_finding("value", where, field,
                                 f"must never appear in a config file: "
                                 f"{constraint.get('forbidden_reason') or 'it holds a secret'}"))
        return findings

    if value is None:
        if not constraint.get("nullable", True):
            findings.append(_finding("value", where, field, "must not be null"))
        return findings

    if kind == "integer":
        number = _as_int(value)
        if number is None:
            findings.append(_finding("value", where, field,
                                     f"must be a whole number; got {value!r}"))
            return findings
        low, high = constraint.get("min"), constraint.get("max")
        if low is not None and number < int(low):
            findings.append(_finding("value", where, field, f"must be >= {low}; got {number}"))
        if high is not None and number > int(high):
            findings.append(_finding("value", where, field, f"must be <= {high}; got {number}"))
        return findings

    if kind == "boolean":
        if isinstance(value, bool):
            return findings
        text = str(value).strip().lower()
        if text in _TRUE_TEXT or text in _FALSE_TEXT:
            return findings
        findings.append(_finding("value", where, field, f"must be true or false; got {value!r}"))
        return findings

    if kind == "string":
        if isinstance(value, (dict, list)):
            findings.append(_finding("value", where, field, f"must be text; got {type(value).__name__}"))
            return findings
        text = str(value)
        # Checked before the empty test, not after: `bridge_url` is required only when
        # `method` is api, and the case that matters is exactly the one where it is present and
        # blank. Returning early on empty made this rule unreachable for its own reason to exist.
        required_when = constraint.get("required_when") or {}
        if required_when and record is not None and not text.strip():
            other = str(record.get(str(required_when.get("field"))) or "").strip().lower()
            if other == str(required_when.get("equals") or "").lower():
                findings.append(_finding("missing", where, field,
                                         f"required when {required_when['field']} is "
                                         f"{required_when['equals']}"))
                return findings
        if not text.strip():
            if not constraint.get("allow_empty", False):
                findings.append(_finding("value", where, field, "must not be empty"))
            return findings
        enum = constraint.get("enum")
        if enum:
            candidates = [str(item) for item in enum]
            compared = text.strip().lower() if constraint.get("case_insensitive") else text.strip()
            allowed = ([item.lower() for item in candidates]
                       if constraint.get("case_insensitive") else candidates)
            if compared not in allowed:
                detail = f"must be one of {', '.join(candidates)}; got {text!r}"
                if constraint.get("open_enum"):
                    detail += (f" (reported, not refused: "
                               f"{constraint.get('open_enum_reason') or 'the estate may add its own'})")
                    findings.append(_finding(UNLISTED_KIND, where, field, detail))
                else:
                    findings.append(_finding("value", where, field, detail))
        return findings

    if kind == "object":
        if isinstance(value, bool) and "boolean" in (constraint.get("also_accepts") or []):
            return findings
        if not isinstance(value, dict):
            findings.append(_finding("value", where, field,
                                     f"must be an object; got {type(value).__name__}"))
            return findings
        return check_record(str(constraint.get("object")), value,
                            where=f"{where}.{field}", in_config=in_config)

    return findings


def check_record(object_name: str, record: Any, *, where: str,
                 data_dir: str | Path | None = None,
                 in_config: bool = True) -> list[dict[str, str]]:
    """Check one occurrence of one shared object: required fields, values, unknown fields."""
    entry = describe(object_name, data_dir=data_dir)
    if not isinstance(record, dict):
        return [_finding("value", where, object_name,
                         f"must be an object; got {type(record).__name__}")]

    specs = {str(item.get("field")): item for item in entry.get("fields") or []}
    legacy = {str(name): str(target) for name, target in (entry.get("legacy_fields") or {}).items()}
    findings: list[dict[str, str]] = []

    for name, spec in specs.items():
        constraint = spec.get("constraint") or {}
        if name in record:
            findings.extend(check_value(constraint, record[name], where=where, field=name,
                                        record=record, in_config=in_config))
        elif spec.get("required"):
            # A legacy spelling satisfies the requirement — the parser reads it, so refusing here
            # would report a file that loads correctly as broken.
            satisfied = [alias for alias in constraint.get("satisfied_by") or [] if alias in record]
            if not satisfied:
                findings.append(_finding("missing", where, name,
                                         f"required: {spec.get('purpose') or ''}".strip()))

    for name in record:
        if name in specs:
            continue
        if name in legacy:
            findings.append(_finding(DEPRECATED_KIND, where, name,
                                     f"deprecated spelling of {legacy[name]}; still read, never written"))
        elif str(name).startswith("_"):
            continue  # `_note`, `_note_timeout`: the estate's own comments, and a known idiom here
        else:
            findings.append(_finding("unknown", where, name,
                                     f"not a {object_name} field, so nothing reads it. "
                                     f"{object_name} has: {', '.join(specs)}"))
    return findings


def walk_records(payload: Any, path: str) -> list[tuple[str, Any]]:
    """Every record at a dotted path, where ``[]`` means "each item of this list".

    Public because :mod:`db_ops.lib.config_references` walks the same declared paths to follow a
    pointer from one config file into another, and two walkers would disagree about what
    ``backup_restore.backups[].jobs[]`` means the first time one of them was extended.
    """
    found: list[tuple[str, Any]] = [("", payload)]
    for step in [part for part in path.split(".") if part]:
        name, is_list = (step[:-2], True) if step.endswith("[]") else (step, False)
        nxt: list[tuple[str, Any]] = []
        for label, node in found:
            if not isinstance(node, dict) or name not in node:
                continue
            value = node[name]
            if is_list:
                if isinstance(value, list):
                    nxt.extend((f"{label}.{name}[{index}]".lstrip("."), item)
                               for index, item in enumerate(value))
            else:
                nxt.append((f"{label}.{name}".lstrip("."), value))
        found = nxt
    return found


def check_data_dir(data_dir: str | Path | None = None) -> dict[str, Any]:
    """Every use of every shared object in ``data_dir``, checked against the reference.

    Walks the reference's own ``used_in`` list, so a new place an object is used is declared in the
    file rather than discovered by a scan that cannot tell a record from a note.
    """
    root = Path(data_dir or DEFAULT_DATA_DIR)
    findings: list[dict[str, str]] = []
    notices: list[dict[str, str]] = []
    checked_records = 0
    checked_objects = 0
    files_missing: list[str] = []

    for entry in load(root):
        object_name = str(entry.get("object"))
        is_field = str(entry.get("kind")) == "field"
        for site in entry.get("used_in") or []:
            file_name = str(site.get("file") or "")
            if not file_name:
                continue  # reached through another object; checked there
            path = str(site.get("path") or "")
            # The example stands in for the real file, because the export ships tests/ and a public
            # checkout has only `*.example.json`: without this the check would pass there by
            # walking nothing, which is the one result a guard must never produce.
            payload_path = root / file_name
            if not payload_path.is_file():
                payload_path = root / file_name.replace(".json", ".example.json")
            if not payload_path.is_file():
                files_missing.append(file_name)
                continue
            try:
                payload = load_json_file(payload_path)
            except Exception as exc:  # noqa: BLE001 - a broken file is a finding, not a crash
                findings.append(_finding("value", file_name, "", f"cannot be read: {exc}"))
                continue
            for label, record in walk_records(payload, path):
                if not isinstance(record, dict):
                    continue
                checked_records += 1
                where = f"{file_name}:{label}"
                if is_field:
                    # A shared FIELD lives on the record itself, so the record IS the occurrence.
                    checked_objects += 1
                    for item in check_record(object_name, record, where=where, data_dir=root):
                        # Only this field's own names matter here; the record's other keys belong
                        # to its own file's schema, not to the shared field.
                        if item["field"] in {str(f.get("field")) for f in entry["fields"]}:
                            (notices if item["kind"] in NOTICE_KINDS else findings).append(item)
                    continue
                if object_name not in record:
                    continue
                checked_objects += 1
                for item in check_record(object_name, record[object_name],
                                         where=f"{where}.{object_name}", data_dir=root):
                    (notices if item["kind"] in NOTICE_KINDS else findings).append(item)

    return {
        "data_dir": str(root),
        "records_walked": checked_records,
        "objects_checked": checked_objects,
        "violations": findings,
        "notices": notices,
        "deprecated": [item for item in notices if item["kind"] == DEPRECATED_KIND],
        "unlisted": [item for item in notices if item["kind"] == UNLISTED_KIND],
        "files_missing": sorted(set(files_missing)),
        "ok": not findings,
    }
