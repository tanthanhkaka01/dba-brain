"""Which section of a report page a target belongs to: SQL Server, Oracle, PostgreSQL, Host only.

Every page that lists the estate - the SLA page, the server-metrics picker, the index-report picker -
used to put every target in one row, ordered by name or by status. With SQL Server instances,
Oracle and PostgreSQL databases and bare hosts mixed together, a reader looking for "the Oracle
servers" read the whole list, and a page could not say "PostgreSQL is fine, SQL Server is not" at
all. The operator asked for one section per engine (2026-10-02); this is the one rule all the pages
group by, so the same target cannot land under SQL Server on one page and Other on the next.

The engine comes from the ``db_type`` a target was registered with. A target with no database -
``db_type: "host"``, or no ``db_type`` and nothing but OS metrics - is **Host only**.
"""

from __future__ import annotations

from typing import Iterable

#: Sections in the order a page shows them: the engines first, then hosts with no database, then
#: anything this list does not know - shown, never dropped.
SECTIONS: tuple[tuple[str, str], ...] = (
    ("sqlserver", "SQL Server"),
    ("oracle", "Oracle"),
    ("postgresql", "PostgreSQL"),
    ("mysql", "MySQL"),
    ("host", "Host only"),
    ("other", "Other"),
)

_LABELS = dict(SECTIONS)
_ORDER = {key: index for index, (key, _label) in enumerate(SECTIONS)}

#: Every spelling a ``db_type`` has had in this estate's files, read to one section key.
_ALIASES = {
    "sqlserver": "sqlserver", "mssql": "sqlserver", "sql_server": "sqlserver",
    "oracle": "oracle",
    "postgresql": "postgresql", "postgres": "postgresql", "pg": "postgresql",
    "mysql": "mysql",
    "host": "host", "os": "host", "windows": "host", "linux": "host",
}


def section_of(db_type: object, *, os_only: bool = False) -> str:
    """The section key for a target registered as ``db_type``.

    ``os_only`` is the caller saying the target has no database at all; it wins over a ``db_type``,
    because a host that also names an engine it does not run is still a host.
    """
    if os_only:
        return "host"
    text = str(db_type or "").strip().lower()
    if not text:
        return "host"
    return _ALIASES.get(text, "other")


def section_label(key: str) -> str:
    """What a section is called on a page."""
    return _LABELS.get(key, _LABELS["other"])


def section_rank(key: str) -> int:
    """Sort key: the position of ``key`` in :data:`SECTIONS`."""
    return _ORDER.get(key, _ORDER["other"])


def present_sections(keys: Iterable[str]) -> list[tuple[str, str]]:
    """The ``(key, label)`` sections that at least one of ``keys`` falls in, in page order.

    A page shows a heading only for an engine it has something to show under: an empty "MySQL"
    heading on an estate with no MySQL is one more thing to read past.
    """
    seen = {key if key in _LABELS else "other" for key in keys}
    return [(key, label) for key, label in SECTIONS if key in seen]
