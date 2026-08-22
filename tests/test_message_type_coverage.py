"""Every literal a producer hands the queue must resolve to a known message_type.

This exists because the gap kept being found in production, one row at a time: a producer
passes `status="validation_error"` or `level="logging"`, the mapper has no entry for it, the
column silently stores NULL, and the send layer quietly falls back to guessing from the header.
Nothing fails, nothing logs — the only symptom is a message that goes out without its symbol.

So the call sites are read statically and every literal is resolved. Adding a producer with a
new status word fails here, at the point the word is introduced, rather than after an operator
notices an untagged alert days later.
"""

import ast
import pathlib

import pytest

from db_ops.db.telegram_queue import message_type_for
from db_ops.telegram.severity import normalize_message_type

QUEUE_FUNCTIONS = {"queue_telegram_message", "queue_command_reply"}
HINT_KEYWORDS = {"status", "phase", "level", "message_type"}


def _string_literals(node):
    """The str constants a keyword value can evaluate to, ignoring anything dynamic.

    A conditional (`"failed" if action_error else "success"`) contributes both branches: both
    are literals a producer really can send, and both must map.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return _string_literals(node.body) + _string_literals(node.orelse)
    return []


def _call_sites():
    for path in sorted(pathlib.Path("db_ops").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name not in QUEUE_FUNCTIONS:
                continue
            for keyword in node.keywords:
                if keyword.arg in HINT_KEYWORDS:
                    for literal in _string_literals(keyword.value):
                        yield f"{path}:{node.lineno}", keyword.arg, literal


def test_the_scan_finds_the_producers_at_all():
    """A guard that silently matches nothing proves nothing."""
    sites = list(_call_sites())
    assert len(sites) >= 5, f"expected several literal hints, found {sites}"


def test_every_literal_hint_resolves_to_a_message_type():
    unmapped = []
    for where, keyword, literal in _call_sites():
        if keyword == "message_type":
            resolved = normalize_message_type(literal)
        else:
            resolved = normalize_message_type(message_type_for(**{keyword: literal}))
        if not resolved:
            unmapped.append(f"{where}  {keyword}={literal!r}")
    assert not unmapped, (
        "these producer values store NULL and fall back to guessing from the header; "
        "add them to db_ops.db.telegram_queue._PHASE_TYPES/_LEVEL_TYPES:\n  "
        + "\n  ".join(unmapped)
    )


@pytest.mark.parametrize("literal", ["validation_error", "TIMEOUT", "logging", "PASSED", "AT_RISK"])
def test_the_values_that_reached_production_as_null(literal):
    """Regression list: each of these was found as a NULL row on the live store."""
    assert normalize_message_type(message_type_for(status=literal)) or \
           normalize_message_type(message_type_for(level=literal))
