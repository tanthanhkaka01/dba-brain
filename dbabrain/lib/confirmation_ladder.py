"""How hard an operation is to confirm - one reading of an ``emergency_operations.json`` document.

Two sides read a ladder since 0.24.0, and they must read it the same way. ``common.confirm`` prices
an operation from the rules its request carries, else from the ladder the package ships; the app
that calls it - the bot, ``sql_tasks`` - reads this node's own ``data/emergency_operations.json``
and puts the operation's rules in the request, because ``common.cli`` reads no configuration
(rules R09). This is the one function both use, so an operator's ladder means the same thing
whichever side read it.
"""

from __future__ import annotations

from typing import Any

__all__ = ["STRICTEST", "operation_rules", "rules_from_request"]

#: What an operation nobody priced costs: two confirmations and the target typed back. A command
#: added to the CLI but forgotten in the ladder must become harder to run, never easier.
STRICTEST: dict[str, Any] = {"level": 100, "confirmations": 2, "challenge": "target_id", "effects": []}


def operation_rules(document: Any, operation: str) -> dict[str, Any]:
    """``{"level", "confirmations", "challenge", "effects"}`` for ``operation``; strictest when the
    document does not price it, or is not a ladder at all."""
    if not isinstance(document, dict):
        return dict(STRICTEST)
    entry = (document.get("operations") or {}).get(operation)
    if not isinstance(entry, dict):
        return dict(STRICTEST)
    level = entry.get("level", 100)
    rules = (document.get("levels") or {}).get(str(level))
    if not isinstance(rules, dict):
        return dict(STRICTEST)
    return {
        "level": int(level),
        "confirmations": int(rules.get("confirmations", 2)),
        "challenge": str(rules.get("challenge") or ""),
        "effects": [str(item) for item in (entry.get("effects") or [])],
    }


def rules_from_request(raw: Any) -> dict[str, Any] | None:
    """The rules a request states, checked - or ``None`` when it states none.

    A block that is there but malformed is refused rather than read as "none": falling back to the
    shipped ladder would price the operation from a table its caller did not send.
    """
    if raw in (None, {}):
        return None
    if not isinstance(raw, dict):
        raise ValueError('"rules" must be an object: {"confirmations", "challenge", "effects"}.')
    try:
        confirmations = int(raw.get("confirmations", 2))
    except (TypeError, ValueError) as exc:
        raise ValueError(f'"rules.confirmations" must be a number; got {raw.get("confirmations")!r}.') from exc
    if confirmations < 0:
        raise ValueError(f'"rules.confirmations" cannot be negative; got {confirmations}.')
    return {
        "level": int(raw.get("level", 100)),
        "confirmations": confirmations,
        "challenge": str(raw.get("challenge") or ""),
        "effects": [str(item) for item in (raw.get("effects") or [])],
    }
