"""A restore workflow may not report `done` while a database it touched did not come back.

Measured 2026-09-15, on the node qualifying 0.17.0. The scheduled restore of one SQL Server
database announced every phase and finished `Restore workflow finished. status=done`. The operator
went to the target and could not open the database. `backup_restore_history` had recorded, at the
same second the chat was told the workflow had finished:

    SALESDB    FAILED  Msg 5149 - MODIFY FILE encountered operating system error 31 while
                                  attempting to expand the physical file '..._log.ldf'
               Msg 3013 - RESTORE DATABASE is terminating abnormally.

Nothing was hidden and nothing was unmeasured: `_count_restore_statuses` had already counted that
database as `failed`, and the count was already in the summary. The workflow simply asserted
`status = SUCCESS` next to it, and only an exception could change that — so the per-database
outcome was computed, published, and ignored.

`docs/releases/v0.14.0.md` records the same signature from the 0.14.0 soak on 2026-09-09: two
restores `FAILED` in the history table while the workflow said `done`.

The rule these tests hold: **the verdict is computed from the per-database outcomes, never asserted
beside them.**
"""

from __future__ import annotations

import pytest

from db_ops.backup_restore.cli import (
    _count_restore_statuses,
    _failed_restore_databases,
)


def _source(**per_database: str) -> dict[str, object]:
    return {"source_id": "SRC", "target_id": "TGT", "per_database_restore_status": dict(per_database)}


def test_a_failed_database_is_named_not_only_counted() -> None:
    """"1 database failed" sends its reader back to the store, which is where this hid for two
    releases. The message has to carry the name."""
    outputs = [_source(SALESDB="FAILED", APPDB="SUCCESS")]

    assert _failed_restore_databases(outputs) == ["SALESDB"]
    assert _count_restore_statuses(outputs)["failed"] == 1


def test_every_database_succeeding_is_the_only_clean_run() -> None:
    outputs = [_source(A="SUCCESS", B="SUCCESS")]

    assert _failed_restore_databases(outputs) == []
    assert _count_restore_statuses(outputs)["failed"] == 0


def test_a_skipped_database_has_not_failed() -> None:
    """A database the run had no work for was not restored, which is not the same as a restore
    that ran and did not work. Counting it as a failure would fail every partial run."""
    outputs = [_source(A="SUCCESS", B="SKIPPED")]

    assert _failed_restore_databases(outputs) == []
    assert _count_restore_statuses(outputs)["skipped"] == 1


def test_a_dry_run_is_not_a_failure() -> None:
    outputs = [_source(A="DRY_RUN", B="DRY_RUN")]

    assert _failed_restore_databases(outputs) == []


def test_a_status_nobody_recognises_counts_against_the_run() -> None:
    """The engines do not agree on a word for failure, and a status this code has never seen is not
    evidence that a database is usable. Unknown is read as "not proven", which is the only reading
    that cannot report a broken restore as a good one."""
    outputs = [_source(A="SUCCESS", B="PARTIALLY_RESTORED", C="terminating abnormally")]

    assert _failed_restore_databases(outputs) == ["B", "C"]
    assert _count_restore_statuses(outputs)["failed"] == 2


def test_several_sources_are_gathered_into_one_answer() -> None:
    """One workflow run restores from several sources; a failure in the second must not be lost
    behind a first that worked."""
    outputs = [_source(A="SUCCESS"), _source(B="FAILED", C="FAILED")]

    assert sorted(_failed_restore_databases(outputs)) == ["B", "C"]


def test_a_source_with_no_per_database_detail_still_answers() -> None:
    """Not every engine reports per database - Oracle restores an instance. The source's own status
    is then the whole answer, and it is read the same way."""
    assert _failed_restore_databases([{"source_id": "ORA", "status": "FAILED"}]) == ["ORA"]
    assert _failed_restore_databases([{"source_id": "ORA", "status": "SUCCESS"}]) == []


@pytest.mark.parametrize("junk", [None, "not a dict", 42, []])
def test_a_malformed_source_does_not_crash_the_verdict(junk: object) -> None:
    """The verdict runs at the end of a long job. It may not be the thing that raises."""
    assert _failed_restore_databases([junk]) == []  # type: ignore[list-item]
