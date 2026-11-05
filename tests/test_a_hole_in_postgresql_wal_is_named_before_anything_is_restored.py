"""A PostgreSQL restore's WAL is held to its names before anything is restored (0.35.0, 1.250).

Gap G10 (2026-10-30), *chain completeness*. A recovery to latest replays segment after segment and
stops at the first one missing - and then opens, as if that were the end. The drill reports done,
and what it restored is an older state than the backups hold. The listing now reads each base
backup's start segment (``backup_label``) and, asked, the names under ``wal/``; the plan refuses:

* when the start segment is not archived;
* to latest, when a segment between the start and the newest is missing - named;

and does not judge another timeline after the start, a segment size other than 16 MB, or (for a
point-in-time restore) a hole after the start, whose place against the target a name cannot tell.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dbabrain.lib import integrity as I

START = "000000010000000000000002"


def seg(log, n, tli=1):
    return f"{tli:08X}{log:08X}{n:08X}"


def test_a_continuous_wal_holds_across_a_log_boundary():
    segments = [seg(0, 2), seg(0, 3)] + [seg(0, n) for n in range(4, 0x100)] + [seg(1, 0), seg(1, 1)]
    assert I.postgresql_wal_break(START, segments) == ""


def test_a_missing_start_segment_is_named():
    assert "starts from, 000000010000000000000002, is not archived" in I.postgresql_wal_break(START, [seg(0, 3)])


def test_a_hole_is_named_with_the_segment_that_is_missing():
    broken = I.postgresql_wal_break(START, [seg(0, 2), seg(0, 3), seg(0, 5)])
    assert "WAL segment 000000010000000000000004 is missing" in broken
    assert "before 000000010000000000000005" in broken


def test_a_point_in_time_restore_is_held_to_the_start_only():
    assert I.postgresql_wal_break(START, [seg(0, 2), seg(0, 5)], to_latest=False) == ""
    assert "not archived" in I.postgresql_wal_break(START, [seg(0, 5)], to_latest=False)


@pytest.mark.parametrize("segments", [
    [seg(0, 2), seg(0, 3, tli=2)],          # a promotion: its history decides
    [seg(0, 2), seg(0, 0x1FF)],             # not 16 MB segments
    [],                                     # nothing listed
])
def test_what_a_name_cannot_tell_is_not_judged(segments):
    assert I.postgresql_wal_break(START, segments) == ""


def test_older_segments_before_the_start_do_not_matter():
    assert I.postgresql_wal_break(START, [seg(0, 0), seg(0, 2), seg(0, 3)]) == ""


def test_the_plan_refuses_a_hole_before_any_step(monkeypatch):
    from dbabrain.backup_restore import restore_by_id

    def listed(request):
        kind = request["kinds"][0]
        if kind == "full":
            return {"files": [{"path": "/b/base/20261020T010000Z_FULL", "start_wal": seg(0, 2)}],
                    "newest_finished_at": "2026-10-20 01:10:00"}
        if kind == "diff":
            return {"files": [{"path": "/b/base/20261021T010000Z_INCR", "start_wal": seg(0, 5)}],
                    "newest_finished_at": "2026-10-21 01:10:00"}
        assert request.get("wal_segments") is True
        return {"files": [{"path": "/b/wal", "wal_segments": [seg(0, 5), seg(0, 6), seg(0, 8)]}],
                "newest_finished_at": None}

    monkeypatch.setattr(restore_by_id, "_list_backup_files", listed)
    monkeypatch.setattr(restore_by_id, "_visible_dir", lambda job: "/b")
    job = SimpleNamespace(restore_id="PG_DRILL", env={}, env_secrets={})
    with pytest.raises(restore_by_id.RestoreByIdError,
                       match="the WAL chain is broken - WAL segment 000000010000000000000007 is missing"):
        restore_by_id._plan_postgresql(job, {}, point_in_time="", host={"host": "192.0.2.40"}, data_dir=None)
