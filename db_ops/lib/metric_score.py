"""How a set of metric rows scores for one status — the fleet ordering rule.

The sum of ``importance`` over the rows at a given status, and the number the metrics CLI and the
reports app both use to decide which target is worth showing first. It was written out in both,
identically: two files, one ordering rule, and no way to change how the fleet is ranked without
remembering that the other one exists.

Importance is summed rather than counted on purpose: ten OK-but-noisy rows must not outrank one
critical row on a database nobody has backed up.
"""

from __future__ import annotations


def target_score(rows: list[object], status: str) -> int:
    status = status.upper()
    return sum(int(row["importance"] or 0) for row in rows
               if str(row["status"] or "").upper() == status)
