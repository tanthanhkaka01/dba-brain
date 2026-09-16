"""The restore staging folder is cleaned by two conditions, and the second one only ever spares.

Age has always decided it: ``copy_recent_hours`` says which staged copies are old enough to
consider. What is new is the second condition — **obsolete**, meaning a newer full exists — because
age alone will happily delete the newest full when the window is short, and the next restore then
starts from nothing.

The two are an AND, and the order matters for reading the code: age narrows, obsolete narrows
further. Nothing the age gate rejected can be deleted by the obsolete rule.

Two mistakes are recorded here as tests because both were made while writing this and both were
silent — the cleanup kept reporting success while doing the wrong thing:

* judging the chain against the *age-selected subset*, when the newest full is exactly what the
  age gate filters out, so a lone aged file was always its own anchor and nothing was ever deleted;
* comparing timestamps without a tie-break, on an SMB share whose mtime resolution is two seconds.
"""

from __future__ import annotations

from db_ops.backup_restore.delete_backup import obsolete_only


def _f(name, stamp, size=100):
    return (f"/stage/{name}", float(stamp), size)


def test_the_newest_full_is_never_obsolete():
    """It is what the next restore starts from."""
    candidates = [_f("a_FULL.bak", 100), _f("b_FULL.bak", 200)]

    assert obsolete_only(candidates) == {"/stage/a_FULL.bak"}


def test_a_log_older_than_the_newest_full_is_obsolete():
    """Nothing restores from it any more: the chain now begins at the newer full."""
    candidates = [_f("old.trn", 100), _f("b_FULL.bak", 200)]

    assert obsolete_only(candidates) == {"/stage/old.trn"}


def test_a_log_at_the_same_instant_as_the_newest_full_is_kept():
    """It may belong to the chain that starts there, and a staging folder cannot tell. Only a
    strictly older log is spared."""
    candidates = [_f("same.trn", 200), _f("b_FULL.bak", 200)]

    assert obsolete_only(candidates) == set()


def test_tied_fulls_do_not_all_survive():
    """`vm_import_unc` is an SMB share, where mtime resolution can be two seconds and copies land
    in the same tick routinely. Compared on the timestamp alone, tied fulls are each "not older
    than the newest" and every one of them is kept — the cleanup stops deleting anything and
    reports success while the share fills up. Exactly one is the anchor; the rest are obsolete."""
    candidates = [_f("a_FULL.bak", 200), _f("b_FULL.bak", 200), _f("c_FULL.bak", 200)]

    obsolete = obsolete_only(candidates)

    assert len(obsolete) == 2, "one anchor kept, the others obsolete"
    assert "/stage/c_FULL.bak" not in obsolete, "the tie-break is stable, not arbitrary per run"


def test_the_tie_break_is_deterministic():
    """Two runs over the same directory must reach the same verdict; a set that changes between
    runs would delete a different file each night."""
    candidates = [_f("a_FULL.bak", 200), _f("b_FULL.bak", 200)]

    assert obsolete_only(candidates) == obsolete_only(list(reversed(candidates)))


def test_logs_with_no_full_at_all_are_left_to_the_age_gate():
    """There is no chain here to protect, and holding them forever would defeat the cleanup — the
    staging folder would grow without limit on a set that was never restorable anyway."""
    candidates = [_f("x.trn", 100), _f("y.trn", 200)]

    assert obsolete_only(candidates) == {"/stage/x.trn", "/stage/y.trn"}


def test_a_single_file_is_its_own_anchor_and_is_kept():
    """The lone-file case that used to make the whole cleanup a no-op when it was judged against
    the age-selected subset rather than the whole directory."""
    assert obsolete_only([_f("only_FULL.bak", 100)]) == set()


# ------------------------------------------------------------------ one directory, many chains
#
# A staging directory holds every database copied from one source, laid out as
# `<source>/<database>/<FULL|LOG>/<file>`. A chain is per database: one database's full anchors
# that database's logs and nothing else. Judged with a single anchor for the whole tree, the
# newest full anywhere decided every database's fate.
#
# Measured 2026-09-14 against a real staging tree holding four databases. Sessions's 5 MB full,
# taken 89 seconds after Orders's, made Orders's 30 GB full obsolete - while the logs that
# restore from it were correctly kept as still_needed. A chain with no anchor is the single
# outcome this function exists to prevent, and it was reported as a clean success.


def _db(database, kind, name, stamp, size=100):
    return (f"/stage/SRC/{database}/{kind}/{name}", float(stamp), size)


def test_one_databases_full_does_not_retire_anothers():
    candidates = [
        _db("Orders", "FULL", "sales_FULL.bak", 1000, 32_000_000_000),
        _db("Orders", "LOG", "sales_LOG.trn", 1100),
        _db("Sessions", "FULL", "app_FULL.bak", 1089, 5_000_000),
    ]

    assert obsolete_only(candidates) == set(), (
        "every file here belongs to a live chain: two newest fulls and a log after one of them")


def test_each_chain_retires_on_its_own_newest_full():
    candidates = [
        _db("Orders", "FULL", "sales_old_FULL.bak", 1000),
        _db("Orders", "FULL", "sales_new_FULL.bak", 3000),
        _db("Orders", "LOG", "sales_mid_LOG.trn", 2000),
        _db("Sessions", "FULL", "app_FULL.bak", 1500),
        _db("Sessions", "LOG", "app_LOG.trn", 2500),
    ]

    assert obsolete_only(candidates) == {
        "/stage/SRC/Orders/FULL/sales_old_FULL.bak",
        "/stage/SRC/Orders/LOG/sales_mid_LOG.trn",
    }, "Sessions's log is after Sessions's own full, so it is still needed"


def test_fulls_and_logs_staged_under_different_roots_are_still_one_chain():
    """`vm_import_linux_path` and `vm_import_linux_log_path` may differ, so the chain is keyed on
    the database folder's NAME rather than on its path."""
    candidates = [
        ("/import/SRC/Orders/FULL/sales_FULL.bak", 2000.0, 100),
        ("/logs/SRC/Orders/LOG/sales_before_LOG.trn", 1000.0, 100),
        ("/logs/SRC/Orders/LOG/sales_after_LOG.trn", 3000.0, 100),
    ]

    assert obsolete_only(candidates) == {"/logs/SRC/Orders/LOG/sales_before_LOG.trn"}


def test_a_chain_with_no_full_of_its_own_still_answers_to_the_newest_full_staged():
    """Otherwise a database whose full has already been cleaned keeps its logs for ever."""
    candidates = [
        _db("Orders", "FULL", "sales_FULL.bak", 3000),
        _db("VRS_Prod", "LOG", "vrs_LOG.trn", 1000),
    ]

    assert obsolete_only(candidates) == {"/stage/SRC/VRS_Prod/LOG/vrs_LOG.trn"}
