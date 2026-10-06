"""A ``common.cli`` request, checked against the reference before the command reads it.

The reference describes every request field - its kind, its range, whether it is required - and
until 0.26.0 nothing held a request to it: each command checked what it remembered to, in words of
its own, and a field the command did not look at was simply not read. The 0.22.0 rename of
``engine`` broke every scripted restore that way - the old key arrived, nobody read it, and the
restore ran on its default - and a wrong type surfaced three calls deep as a ``KeyError`` or a
``TypeError`` instead of as the caller's mistake (``audits/20261002_audit_typed_requests_and_errors.md``
section 1).

So the reader checks first, with the checker the config files already use
(:func:`db_ops.lib.shared_objects.check_record`), and - once :data:`REFUSING` is on - refuses what
the reference says is wrong (until then it measures it, see that constant):

* a ``value`` finding - the wrong type, out of range, not one of a closed list;
* a ``missing`` finding - a required field absent, under its own name and every legacy spelling.

An **unknown** key is *measured, not refused*: a caller may still send a key the reference has not
caught up with, and refusing it would turn a description gap into an outage. With
``DB_OPS_REQUEST_CHECK_LOG`` naming a file, every such finding is appended to it as one JSON line -
the suite runs with it once, and a refusal follows when what it collects is empty. There is nowhere
else to put it: the answer's envelope is seven keys (rules R15), and a line on stderr reads to a
caller as a failure.

A deprecated spelling and an unlisted value of an open list are the reference's own "still read"
cases and are never refused.

The reference read is the **packaged** one (``db_ops/common/catalogue``), never a node's ``data/``
copy: a ``common.cli`` command works from its request alone (rules R09), and the packaged copy is
part of the code, identical on every node.
"""

from __future__ import annotations

from typing import Any

from db_ops.lib import shared_objects
from db_ops.lib.paths import PACKAGED_CATALOGUE

#: The findings that refuse a request - once :data:`REFUSING` is on. Everything else is measured.
REFUSED_KINDS = frozenset({"value", "missing"})

#: **On since 0.27.0** (the operator, 2026-10-05). It was off until what the apps really send had
#: been measured clean: the suite cannot certify it - most app tests fake the transport - and the
#: first node to run it refused every SQL task within a minute, because the reference described
#: `run-sql`'s `capture` as a boolean while the runner sends "all" (2026-10-03). The 0.26.0 soak
#: node then measured 24 hours of real requests with no finding at all. A reference still wrong
#: somewhere is now a refusal with the field named, and the 0.27.0 soak is where it shows.
REFUSING = True

#: The reference, read once per process: a command reads one request, and the file is 1 MB.
_REFERENCE: list[dict[str, Any]] | None = None


def reference() -> list[dict[str, Any]]:
    """The packaged reference - the copy that ships with the code (rules R09)."""
    global _REFERENCE
    if _REFERENCE is None:
        _REFERENCE = shared_objects.load(PACKAGED_CATALOGUE)
    return _REFERENCE


def input_entry(command: str, entries: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """The ``input`` entry describing ``command``'s request, or ``None`` for a command without one.

    Every command has exactly one (``tests/test_every_json_the_tool_reads_or_writes_is_described.py``),
    so ``None`` means a name this build does not route - the dispatcher answers that itself.
    """
    for entry in entries if entries is not None else reference():
        if entry.get("kind") == "input" and command in (entry.get("commands") or []):
            return entry
    return None


def check(command: str, request: Any, *,
          entries: list[dict[str, Any]] | None = None) -> list[dict[str, str]]:
    """Every finding for ``request`` against ``command``'s input entry; ``[]`` when it has none."""
    entries = entries if entries is not None else reference()
    entry = input_entry(command, entries)
    if entry is None:
        return []
    # in_config=False: a request is not a config file. A password belongs in `connection` - it is
    # how the login is stated (R09) - where the same field in data/*.json is refused.
    return shared_objects.check_record(str(entry["object"]), _given(request, entry), where="request",
                                       in_config=False, reference=entries)


def _given(request: Any, entry: dict[str, Any]) -> Any:
    """The request without the optional fields it leaves blank.

    An empty string is how code says "not given" - ``"database_name": target.db_name or ""`` - and
    every command reads it so. The reference's "must not be empty" is written for config files,
    where a blank key is a hand-edit gone wrong; held against a request it would refuse calls that
    work. A *required* field left blank is kept, and refused: there it is the mistake.
    """
    if not isinstance(request, dict):
        return request
    required = {str(f.get("field")) for f in entry.get("fields") or [] if f.get("required")}
    return {key: value for key, value in request.items()
            if key in required or not (isinstance(value, str) and not value.strip())}


def refusal(findings: list[dict[str, str]]) -> str:
    """The refusal sentence for the findings that refuse, or ``""`` when none does."""
    refused = [f for f in findings if f.get("kind") in REFUSED_KINDS]
    if not refused:
        return ""
    return "The request does not match its reference: " + "; ".join(
        f"{f['where']}.{f['field']}: {f['detail']}" for f in refused)


def measured(findings: list[dict[str, str]]) -> list[dict[str, str]]:
    """The findings that are measured and not refused - unknown keys, deprecated spellings."""
    return [f for f in findings if f.get("kind") not in REFUSED_KINDS]
