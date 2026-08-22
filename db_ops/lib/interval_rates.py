"""Reading the structured fields back out of a collector's message text.

A metric row carries one derived number in ``metric_value``; everything else the collector measured
survives only as ``key=value`` pairs inside ``message``. The report re-reads them from there, so
the parser is shared rather than copied — a page that split those fields differently from the
collector that wrote them would disagree with the alert about the same sample.

This module used to own more: differencing two stored samples of a cumulative counter into a rate,
built for ``PERFORMANCE_IO_LATENCY`` because ``sys.dm_io_virtual_file_stats`` reports totals since
the engine started. That was withdrawn on 2026-08-11 along with the rest of that metric's bespoke
path — per-metric machinery in the collector is the thing this codebase does not do, and one metric
did not justify a policy module, a config file and a store lookup of its own. Metrics grade
themselves in their SQL or their command, like every other metric. The arithmetic and its two
honesty rules (a counter that went backwards is a restart, not negative work; a pair too far apart
describes an average, not a moment) are in git history at 2.75.04, to be lifted from there if
interval grading ever returns as a capability *any* cumulative metric can declare.
"""

from __future__ import annotations

import re
from typing import Any


#: The ``key=value`` pairs a collector message is built from. Anchored on a comma or whitespace so
#: a value containing ``=`` (a Windows path, an error text) cannot be mistaken for the next key.
_FIELD_RE = re.compile(r"(?:^|[,\s])([A-Za-z_][A-Za-z0-9_]*)=([^,]*)")


def message_fields(message: Any) -> dict[str, str]:
    """The structured fields a collector carried in its message text, keys lower-cased.

    Shared with the report so the page and the alert read the same sample the same way.
    """
    return {
        key.lower(): value.strip()
        for key, value in _FIELD_RE.findall(str(message or ""))
    }
