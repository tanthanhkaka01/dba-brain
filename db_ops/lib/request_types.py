"""Static types for every ``common.cli`` request and answer, rendered from the reference.

The reference (``shared_config_objects.json``, rules R16) already describes every request and answer
field by field, with a machine-readable kind for each. A hand-written class per command would be a
second description of the same fields, and the 2026-10-02 assessment
(``audits/20261002_audit_typed_requests_and_errors.md`` section 2) chose one description: the types
are *rendered* from the reference into ``lib/cli_types.py``, and a guard renders them again and
fails when the committed file differs - so they cannot drift from the reference any more than the
reference can drift from the parsers.

What they are for: a reader of an answer annotated with its type gets a static check of the keys it
reads. The class of defect that motivated it - 0.22.0 renamed an answer key and every script-driven
restore failed after its first step, reading ``'engine'`` - is a key read under a name the answer no
longer carries. Run-time checking of requests is :func:`db_ops.lib.shared_objects.check_record`'s,
against the same entries; these types add nothing at run time.

Pure: ``render(reference)`` and ``render_transport_stub(reference)`` take the loaded reference and
return text. The command that writes both files is master-side tooling (``control.cli
request-types --write``), like ``bump-version``: ``lib`` never starts a process or writes (R07).

``render_transport_stub`` is the half that makes the types reach the code: one overload of
``transport.common_cli.run`` per command, so ``run("restore-full", ...)`` *returns*
``RestoreStepAnswer`` without a caller annotating anything.
"""

from __future__ import annotations

import re
from typing import Any

#: The reference's field kind -> the Python type a ``TypedDict`` value has.
_PY_TYPE = {"string": "str", "boolean": "bool", "integer": "int", "number": "float",
            "object": "dict[str, Any]", "array": "list[Any]"}
_ITEM_TYPE = {"string": "str", "integer": "int", "object": "dict[str, Any]", "number": "float",
              "boolean": "bool"}

HEADER = '''"""Every ``common.cli`` request and answer as a ``TypedDict`` - GENERATED, do not edit.

Rendered from ``shared_config_objects.json`` by ``db_ops.lib.request_types.render`` and held to it by
``tests/test_cli_types_match_the_reference.py``. Change the reference, then write this file again
with ``python -m db_ops.control.cli request-types --write``.

``<Command>Request`` is a command's request (an ``input_*`` entry), ``<Command>Answer`` what it
answers under ``data`` (an ``output_*`` entry). Every key is optional unless the reference says the
field is required; a field the reference says may be null is ``... | None``.
"""

from __future__ import annotations

from typing import Any, Required, TypedDict
'''


def type_name(object_name: str) -> str:
    """``input_run_sql`` -> ``RunSqlRequest``; ``output_run_sql`` -> ``RunSqlAnswer``."""
    kind, _, rest = object_name.partition("_")
    base = "".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", rest) if part)
    return base + ("Request" if kind == "input" else "Answer")


def _value_type(field: dict[str, Any]) -> str:
    constraint = field.get("constraint") or {}
    kind = str(constraint.get("kind") or field.get("type") or "")
    text = _PY_TYPE.get(kind, "Any")
    if kind == "array" and constraint.get("item_kind") in _ITEM_TYPE:
        text = f"list[{_ITEM_TYPE[str(constraint['item_kind'])]}]"
    if constraint.get("nullable", True) and text != "Any":
        text += " | None"
    return text


def render(reference: list[dict[str, Any]]) -> str:
    """The module text for every ``input_*`` / ``output_*`` entry, in the reference's order."""
    blocks: list[str] = [HEADER]
    names: list[str] = []
    for entry in reference:
        name = str(entry.get("object") or "")
        if entry.get("kind") not in ("input", "output") or not name.startswith(("input_", "output_")):
            continue
        type_ = type_name(name)
        names.append(type_)
        fields = entry.get("fields") or []
        commands = ", ".join(str(c) for c in entry.get("commands") or [])
        # The functional form: some field names are not identifiers (``from``, ``class``), and one
        # form for all keeps the file regular.
        lines = [f"#: {entry.get('one_line') or name}" + (f" ({commands})" if commands else ""),
                 f"{type_} = TypedDict({type_!r}, {{"]
        for field in fields:
            value = _value_type(field)
            if field.get("required"):
                value = f"Required[{value}]"
            lines.append(f"    {str(field['field'])!r}: {value},")
        lines.append("}, total=False)")
        blocks.append("\n".join(lines))
    blocks.append("__all__ = [\n" + "".join(f"    {n!r},\n" for n in names) + "]")
    return "\n\n\n".join(blocks) + "\n"


STUB_HEADER = '''"""Types for ``db_ops.transport.common_cli`` - GENERATED, do not edit.

One overload per ``common.cli`` command the reference describes, so ``run("run-sql", ...)`` is typed
as returning ``RunSqlAnswer`` and a key read from it that the answer does not carry is a type error.
Rendered by ``db_ops.lib.request_types.render_transport_stub``; held by
``tests/test_cli_types_match_the_reference.py``; written by ``control.cli request-types --write``.
The module itself is ``common_cli.py`` beside this file - a stub changes no behaviour.
"""

from typing import Any, Literal, overload

from db_ops.lib import cli_types as t
from db_ops.lib.common_cli import DEFAULT_MODULE as DEFAULT_MODULE
from db_ops.lib.common_cli import CommonCliError as CommonCliError
from db_ops.lib.common_cli import build_command as build_command
from db_ops.lib.common_cli import data_or_raise as data_or_raise
from db_ops.lib.common_cli import read_answer as read_answer
from db_ops.transport.process import ProcessResult as ProcessResult
from db_ops.transport.process import execute as execute


def spawn(command: str, request: dict[str, Any], *, module: str = ...,
          timeout_seconds: int | None = ...,
          stream_stderr: bool = ...) -> tuple[ProcessResult | None, str]: ...
'''


def _command_answers(reference: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """``(command, request type, answer type)`` for every command one input and one output name."""
    inputs: dict[str, str] = {}
    outputs: dict[str, str] = {}
    for entry in reference:
        name = str(entry.get("object") or "")
        for command in entry.get("commands") or []:
            if command == "*":
                continue
            if entry.get("kind") == "input" and name.startswith("input_"):
                inputs[str(command)] = type_name(name)
            elif entry.get("kind") == "output" and name.startswith("output_"):
                outputs[str(command)] = type_name(name)
    return [(command, inputs[command], outputs[command])
            for command in sorted(set(inputs) & set(outputs))]


def render_transport_stub(reference: list[dict[str, Any]]) -> str:
    """The ``.pyi`` for ``transport/common_cli.py``: typed ``run`` and ``run_allowing_failure``."""
    keywords = "*, timeout_seconds: int | None = ..., stream_stderr: bool = ..."
    parts = [STUB_HEADER]
    for function, wrap in (("run", "t.{answer}"), ("run_allowing_failure", "tuple[bool, t.{answer}, str]")):
        for command, request, answer in _command_answers(reference):
            parts.append(f"@overload\ndef {function}(command: Literal[{command!r}], "
                         f"request: t.{request} | dict[str, Any], {keywords}) -> "
                         f"{wrap.format(answer=answer)}: ...")
        fallback = "dict[str, Any]" if function == "run" else "tuple[bool, dict[str, Any], str]"
        parts.append(f"@overload\ndef {function}(command: str, request: dict[str, Any], {keywords}) "
                     f"-> {fallback}: ...")
    return "\n\n".join(parts) + "\n"
