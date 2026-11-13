"""A change between two reads of an inventory is an event (0.38.0, 1.324).

Some metrics are inventories - who is in the host's administrator groups, which keys open root,
whether each database's files are encrypted. Their rows are graded OK on purpose: a WARNING for
every member of ``sudo`` would fire on every host, on every read, for ever. What somebody has to
look at is the moment one of them **changes**: an account added to an admin group, a new root key,
password login turned on, a database's encryption turned off.

So a metric may declare which kinds of change it reports, and at what status::

    "change_alert": {"added": "ERROR", "changed": "ERROR", "removed": "WARNING"}

and the collector holds each read to the one before it (:func:`changes`). The **first read is the
baseline** - nothing to compare, nothing reported. A kind left out is not reported: the encryption
inventory states ``changed`` only, because a database created or dropped is not a security event.

Pure on purpose: rows in, changes out. The collector fetches the previous read and stores the
rows; nothing here touches a store or a clock.

Three kinds of row are never compared, by their ``metric_unit``:

* ``unreadable`` - a section of the inventory the collector could not read; its ``metric_item``
  names the section (``root_key``, ``group:Administrators``). Items are ``<section>:<name>``, and a
  section unreadable on **either** read is skipped whole: losing access to root's
  ``authorized_keys`` must never be reported as "every key was removed", nor regaining it as "every
  key is new".
* ``summary`` - what the read was made with (the account, whether it had root).
* ``change`` - a change row written by an earlier comparison; it is an event, not inventory.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from dbabrain.lib import errors

UNREADABLE_UNIT = "unreadable"
SUMMARY_UNIT = "summary"
CHANGE_UNIT = "change"
#: Units whose rows are not inventory items.
NOT_COMPARED_UNITS = frozenset({UNREADABLE_UNIT, SUMMARY_UNIT, CHANGE_UNIT})

KINDS = ("added", "removed", "changed")
STATUSES = frozenset({"LOGGING", "WARNING", "ERROR", "CRITICAL"})
CHANGE_ITEM_PREFIX = "change:"


class ChangeAlertConfigError(errors.ConfigError):
    """A ``change_alert`` block that does not say what it means."""


def parse_change_alert(raw: Any, *, context: str) -> dict[str, str]:
    """``{kind: STATUS}`` for the kinds the metric reports; ``{}`` when it declares none.

    A typo is an error, not a kind quietly ignored: ``"aded": "ERROR"`` would otherwise read as
    "this metric never reports a new administrator", which is the one thing it was added to do.
    """
    if raw in (None, {}):
        return {}
    if not isinstance(raw, Mapping):
        raise ChangeAlertConfigError(f"{context}.change_alert must be an object of kind -> status.")
    parsed: dict[str, str] = {}
    for kind, status in raw.items():
        name = str(kind).strip().lower()
        if name not in KINDS:
            raise ChangeAlertConfigError(
                f"{context}.change_alert: unknown kind {kind!r}; a kind is one of {', '.join(KINDS)}.")
        if status in (None, ""):
            continue                                   # stated as "not reported"
        level = str(status).strip().upper()
        level = "WARNING" if level == "WARN" else level
        if level not in STATUSES:
            raise ChangeAlertConfigError(
                f"{context}.change_alert.{name} must be one of {', '.join(sorted(STATUSES))}, got {status!r}.")
        parsed[name] = level
    return parsed


def _text(row: Any, key: str) -> str:
    """One field of a row, whatever holds it: a dict, a store row (indexable by name), a result object."""
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        value = getattr(row, key, None)
    return "" if value is None else str(value).strip()


def _unreadable_sections(rows: Iterable[Any]) -> set[str]:
    return {_text(row, "metric_item") for row in rows
            if _text(row, "metric_unit").lower() == UNREADABLE_UNIT and _text(row, "metric_item")}


def _items(rows: Iterable[Any]) -> dict[str, str]:
    """``item -> value`` of the rows that are inventory."""
    found: dict[str, str] = {}
    for row in rows:
        item = _text(row, "metric_item")
        if not item or _text(row, "metric_unit").lower() in NOT_COMPARED_UNITS:
            continue
        found[item] = _text(row, "metric_value")
    return found


def _in_section(item: str, sections: set[str]) -> bool:
    return any(item == section or item.startswith(section + ":") for section in sections)


def changes(previous: Iterable[Any], current: Iterable[Any]) -> list[dict[str, str]]:
    """What differs between two reads: ``[{"kind", "item", "before", "after"}]``, by item.

    ``added`` - in ``current`` only; ``removed`` - in ``previous`` only; ``changed`` - in both, with
    another ``metric_value``. A section unreadable on either read is left out (module docstring).
    Rows may be mappings or objects with the metric row's attributes.
    """
    previous, current = list(previous), list(current)
    skipped = _unreadable_sections(previous) | _unreadable_sections(current)
    before, after = _items(previous), _items(current)
    found: list[dict[str, str]] = []
    for item in sorted(set(before) | set(after)):
        if _in_section(item, skipped):
            continue
        if item not in before:
            found.append({"kind": "added", "item": item, "before": "", "after": after[item]})
        elif item not in after:
            found.append({"kind": "removed", "item": item, "before": before[item], "after": ""})
        elif before[item] != after[item]:
            found.append({"kind": "changed", "item": item, "before": before[item], "after": after[item]})
    return found


def change_message(change: Mapping[str, str], *, previous_at: str, current_at: str) -> str:
    """One line saying what changed and between which two reads."""
    kind, item = change["kind"], change["item"]
    if kind == "added":
        what = f"added: {item} ({change['after']})" if change["after"] else f"added: {item}"
    elif kind == "removed":
        what = f"removed: {item} (was {change['before']})" if change["before"] else f"removed: {item}"
    else:
        what = f"changed: {item}: {change['before'] or '-'} -> {change['after'] or '-'}"
    return f"{what} | between the read of {previous_at} and the read of {current_at}"


def change_item(change: Mapping[str, str]) -> str:
    """The ``metric_item`` of a change row: ``change:<kind>:<item>``."""
    return f"{CHANGE_ITEM_PREFIX}{change['kind']}:{change['item']}"


def is_change_row(row: Any) -> bool:
    return _text(row, "metric_unit").lower() == CHANGE_UNIT
