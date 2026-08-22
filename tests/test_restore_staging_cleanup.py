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
