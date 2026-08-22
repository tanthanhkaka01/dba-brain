"""Naming and daily archiving of published report files — pure, path in / path out.

Split from the ``report_base_url`` lookup on 2026-08-15: that one reads
``data/reports_config.json`` and went to ``common.data_sources``, while everything here works on
the paths it is handed and belongs where every component can call it in-process.

Keep one dated copy per day of a report that is otherwise overwritten in place.

Two of the three published reports have deliberately **stable** file names — ``server-metrics.html``
with its per-server series JSON, and ``index-usage_<slug>.html``. Stamping every run was measured
and rejected: 18 servers of charts plus their series is ~12 MB per run, and the workflow runs every
two hours, so a stamped-per-run history costs ~4.3 GB a month of near-duplicate files. That is why
those files are rewritten instead (see :mod:`db_ops.reports.server_report`).

The cost of that decision was that ``?date=`` worked only for the fleet inventory, which *is*
stamped per run. This module buys the history back at a twelfth of the price, by writing **one
copy per calendar day**, overwritten by each later run that day:

* a date-only ``?date=2026-08-01`` can only ever address the *last* snapshot of that day — that is
  what ``parse_date_param`` means by treating a bare date as 23:59:59 — so one file per day is not
  a lossy approximation of what the URL can ask for, it is exactly it;
* ~12 MB a day instead of ~144 MB, and the directory grows by a bounded amount.

The archive name is ``YYYYMMDD_<original name>``. The webhost recognises both that and the
inventory's finer ``YYYYMMDD_HHMMSS_`` stamp, reading a date-only stamp as the end of its day so
the two orderings agree.
"""

from __future__ import annotations

import shutil
from pathlib import Path

#: Length of the ``YYYYMMDD`` prefix this module writes.
DAY_STAMP_LEN = 8


def day_stamp(stamp: str) -> str:
    """The ``YYYYMMDD`` day of a run stamp (``YYYYMMDD_HHMMSS`` or already a day)."""
    return str(stamp or "")[:DAY_STAMP_LEN]


def archive_name(stamp: str, name: str) -> str:
    """Archive file name for ``name`` on the day of ``stamp``."""
    return f"{day_stamp(stamp)}_{name}"


def archive_daily(paths: list[Path] | list[str], *, stamp: str) -> list[Path]:
    """Copy each published file to its ``YYYYMMDD_`` sibling, overwriting the day's copy.

    Copy rather than move: the stable name is what every existing link, bookmark and the
    ``report_base_url`` in ``data/reports_config.json`` point at, and it must keep serving the
    newest build. The archive is the extra, not the original.

    A file that cannot be copied is skipped rather than raised: losing one day's history of one
    server is not a reason to fail the report run that produced it.
    """
    written: list[Path] = []
    day = day_stamp(stamp)
    if not day.isdigit() or len(day) != DAY_STAMP_LEN:
        return written
    for item in paths:
        source = Path(item)
        if not source.is_file():
            continue
        target = source.with_name(archive_name(day, source.name))
        if target == source:
            continue
        try:
            shutil.copy2(source, target)
        except OSError:
            continue
        written.append(target)
    return written


#: Where the webhost publishes reports. The host serves ``<runtime>/reports`` under
#: ``/report_dba/`` (see db_ops.webhost.cli), so anything written there is reachable at this
#: prefix. Config rather than a literal: the port, mount and host are all deployment facts.
