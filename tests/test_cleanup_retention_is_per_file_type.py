"""``cleanup_retention`` states a window per file type (0.35.0, 1.227).

The plan of 2026-10-29 (`audits/20261029_plan_cleanup_retention_per_file_type.md`): one integer
for every file of a folder meant a LOG job's 7 days pruned the FULLs its sibling job meant to keep
14. The object form names each type in the catalog's own words, and ``default`` for the rest:

    {"FULL": 1209600, "DIFF": 1209600, "LOG": 604800, "CERT": "keep", "default": 1209600}

* the integer form is every type under one window, exactly as before - CERT included (D-R5);
* ``"keep"`` never removes by age; ``0`` stays "no age gate";
* a type the entry's engine never writes is refused - an Oracle entry naming WAL is a mistake;
* CERT not named is kept (T4): a backup encrypted with a certificate nobody kept cannot be restored;
* a dependent type kept past its base restores nothing, and the registrars refuse it (T2, D-R3);
* point-in-time reach is the shorter of base and log (T3); a window under the interval is named (T5);
* a reader that still applies one number gets the longest window, so nothing goes early.
"""

from __future__ import annotations

import pytest

from dbabrain.lib import cleanup_retention as R
from dbabrain.lib import shared_objects

DAY = 86400
SQL = {"FULL": 14 * DAY, "DIFF": 14 * DAY, "LOG": 7 * DAY, "CERT": "keep", "default": 14 * DAY}


def _per_type(value, engine="sqlserver"):
    return R.parse_per_type({R.FIELD: value}, context="entry", engine=engine)


def test_the_integer_form_is_every_type_under_one_window_as_before():
    retention = _per_type(8 * DAY)
    assert [retention.window_for(t) for t in ("FULL", "LOG", "CERT")] == [8 * DAY] * 3
    assert R.parse({R.FIELD: 8 * DAY}, context="entry") == 8 * DAY


def test_each_type_gets_its_own_window_and_keep_is_never():
    retention = _per_type(SQL)
    assert retention.window_for("FULL") == 14 * DAY and retention.window_for("LOG") == 7 * DAY
    assert retention.window_for("CERT") is None


def test_an_unnamed_cert_is_kept_but_an_unnamed_log_takes_the_default():
    retention = _per_type({"FULL": 14 * DAY, "default": 3 * DAY})
    assert retention.window_for("CERT") is None and retention.window_for("LOG") == 3 * DAY


def test_zero_stays_no_age_gate():
    assert _per_type({"default": 0}).window_for("FULL") == 0


@pytest.mark.parametrize("value, engine, said", [
    ({"WAL": DAY, "default": DAY}, "oracle", "not a file type a oracle backup writes"),
    ({"FULL": DAY}, "sqlserver", "no default"),
    ({"FULL": -1, "default": DAY}, "sqlserver", "must not be negative"),
    ({"FULL": "forever", "default": DAY}, "sqlserver", "seconds or \"keep\""),
])
def test_a_wrong_object_is_refused_and_says_why(value, engine, said):
    with pytest.raises(R.CleanupRetentionError) as raised:
        _per_type(value, engine=engine)
    assert said in str(raised.value)


def test_every_type_of_the_engine_named_needs_no_default():
    retention = _per_type({"FULL_BASE": 2 * DAY, "INCR_BASE": 2 * DAY, "MANIFEST": 2 * DAY, "WAL": DAY},
                          engine="postgresql")
    assert retention.window_for("WAL") == DAY


def test_a_log_kept_past_its_full_is_named_and_refused_by_a_registrar():
    entry = {R.FIELD: {"FULL": 7 * DAY, "LOG": 14 * DAY, "default": 7 * DAY}}
    assert R.dependents_past_their_base(R.parse_per_type(entry, context="e", engine="sqlserver"),
                                        engine="sqlserver")
    assert "LOG" in R.registrar_refusal(entry, context="e", engine="sqlserver")
    assert R.registrar_refusal({R.FIELD: SQL}, context="e", engine="sqlserver") == ""


def test_point_in_time_reach_is_the_shorter_of_base_and_log():
    assert R.point_in_time_reach(_per_type(SQL), engine="sqlserver") == 7 * DAY
    assert R.point_in_time_reach(_per_type({"RMAN_L0": 10 * DAY, "ARCHIVELOG": 20 * DAY, "default": DAY},
                                           engine="oracle"), engine="oracle") == 10 * DAY


def test_a_window_under_the_jobs_interval_is_named():
    assert R.shorter_than_its_interval(60, 300) and not R.shorter_than_its_interval(0, 300)
    assert not R.shorter_than_its_interval(600, 300)


def test_a_one_number_reader_gets_the_longest_window_so_nothing_goes_early():
    assert R.parse({R.FIELD: SQL}, context="job") == 14 * DAY
    assert R.parse({R.FIELD: {"FULL": "keep", "default": DAY}}, context="job") == R.KEEP_AS_SECONDS


def test_the_reference_takes_the_object_and_judges_it_by_the_parser():
    constraint = {"kind": "integer", "min": 0, "also_accepts": ["object"],
                  "object_checked_by": "dbabrain.lib.cleanup_retention.problems"}
    assert shared_objects.check_value(constraint, SQL, where="w", field=R.FIELD) == []
    bad = shared_objects.check_value(constraint, {"FULL": DAY}, where="w", field=R.FIELD)
    assert len(bad) == 1 and "no default" in bad[0]["detail"]


# ------------------------------------------------------------------------------- the registrars
def _with_retention(request, value, *, on_job):
    if on_job:
        request["jobs"][0]["cleanup_retention"] = value
    else:
        request["cleanup_retention"] = value
    return request


def test_backup_add_writes_a_consistent_object_and_refuses_a_log_past_its_full(tmp_path):
    from dbabrain.backup_restore import registration
    from tests.test_registering_a_backup_a_restore_and_an_os_login import KEY, _backup

    registration.add_backup(_with_retention(_backup(), SQL, on_job=True), data_dir=tmp_path, key=KEY)
    with pytest.raises(registration.RegistrationError, match="LOG"):
        registration.add_backup(_with_retention(_backup(backup_id="ACME_APPDB_LOG"),
                                                {"FULL": 7 * DAY, "LOG": 14 * DAY, "default": 7 * DAY},
                                                on_job=True), data_dir=tmp_path, key=KEY)


def test_restore_add_refuses_a_type_its_engine_never_writes(tmp_path):
    from dbabrain.backup_restore import registration
    from tests.test_registering_a_backup_a_restore_and_an_os_login import KEY, _restore

    registration.add_restore(_with_retention(_restore(), {"FULL": DAY, "LOG": DAY, "default": DAY},
                                             on_job=False), data_dir=tmp_path, key=KEY)
    with pytest.raises(registration.RegistrationError, match="WAL"):
        registration.add_restore(_with_retention(_restore(restore_id="SECOND"), {"WAL": DAY, "default": DAY},
                                                 on_job=False), data_dir=tmp_path, key=KEY)


@pytest.mark.parametrize("engine, value", [
    ("postgresql", {"FULL_BASE": 14 * DAY, "INCR_BASE": 2 * DAY, "default": 14 * DAY}),
    ("oracle", {"RMAN_L0": 14 * DAY, "RMAN_L1": 2 * DAY, "default": 14 * DAY}),
])
def test_an_incremental_that_needs_its_whole_chain_cannot_go_before_it(engine, value):
    """Oracle's level 1 and PostgreSQL's incremental are differential: each needs every link back.
    A shorter window deletes mid-chain and leaves a set that looks present and restores nothing."""
    assert "goes with its chain" in R.registrar_refusal({R.FIELD: value}, context="e", engine=engine)


def test_a_sqlserver_diff_may_have_its_own_shorter_window():
    """A SQL Server DIFF is cumulative: it needs only its FULL."""
    value = {"FULL": 14 * DAY, "DIFF": 3 * DAY, "LOG": 3 * DAY, "default": 14 * DAY}
    assert R.registrar_refusal({R.FIELD: value}, context="e", engine="sqlserver") == ""


@pytest.mark.parametrize("value, ended, said", [
    (7 * DAY, "2026-10-01T00:00:00Z", "past its LOG window"),
    (7 * DAY, "2026-10-29T00:00:00Z", ""),
    ({"LOG": "keep", "default": DAY}, "2026-01-01T00:00:00Z", ""),
    (0, "2026-10-29T00:00:00Z", "no age gate"),
])
def test_a_gone_file_is_its_retentions_only_past_its_own_window(value, ended, said):
    """1.233: the scrub reads a gone file past its type's window as the retention's, not an attack."""
    why = R.removed_by_retention(_per_type(value), "LOG", ended, now_utc="2026-10-30T00:00:00Z")
    assert (said in why) if said else why == ""
