"""The share-driven SQL Server restore names a broken chain before it restores anything (1.250).

``restore-by-id`` has held its chain to the LSNs since 0.35.0. The share workflow reaches the target
only through ``sqlcmd``, so it could not: a missing ``.trn`` surfaced at ``RESTORE LOG``, after the
FULL and the DIFF were applied, leaving the database in RESTORING. It now reads every chain file's
``RESTORE HEADERONLY`` in one batch first. A header it cannot read is *not judged* - the restore
then runs as it did - never a refusal made on a guess.

The marker lines below have the shape the batch printed on a SQL Server 2025 target on 2026-11-06
(59 header columns; the batch found the count itself).
"""

from __future__ import annotations

import pytest

from dbabrain.backup_restore import restore_chain_check as cc

FULL, DIFF = "/b/APP_FULL_1.bak", "/b/APP_DIFF_1.bak"
LOG1, LOG2, LOG3 = "/b/APP_LOG_1.trn", "/b/APP_LOG_2.trn", "/b/APP_LOG_3.trn"


def _line(index, kind, first, last, checkpoint="0", base="", position="1", database="APP"):
    return f"{cc.MARKER}{index}|{kind}|{position}|{database}|{first}|{last}|{checkpoint}|{base}"


def _chain_stdout(*, log2_first="300"):
    return "\n".join([
        _line(0, "1", "100", "120", checkpoint="110"),
        _line(1, "5", "200", "220", checkpoint="210", base="110"),
        _line(2, "2", "150", "300"),
        _line(3, "2", log2_first, "400"),
        _line(4, "2", "400", "500"),
    ])


def test_a_chain_whose_lsns_join_holds():
    assert cc.chain_break(FULL, DIFF, [LOG1, LOG2, LOG3], _chain_stdout()) == ("", "")


def test_a_missing_log_is_named_before_anything_is_restored():
    broken, unjudged = cc.chain_break(FULL, DIFF, [LOG1, LOG2, LOG3], _chain_stdout(log2_first="350"))
    assert unjudged == ""
    assert "APP_LOG_2.trn" in broken and "350" in broken and "300" in broken


def test_a_diff_of_another_full_is_named():
    stdout = "\n".join([_line(0, "1", "100", "120", checkpoint="110"),
                        _line(1, "5", "200", "220", checkpoint="210", base="999")])
    broken, _ = cc.chain_break(FULL, DIFF, [], stdout)
    assert "APP_DIFF_1.bak" in broken and "another FULL" in broken


def test_a_header_that_could_not_be_read_is_not_judged():
    stdout = _chain_stdout() + f"\n{cc.ERROR_MARKER}3|3013|RESTORE HEADERONLY is terminating abnormally."
    broken, unjudged = cc.chain_break(FULL, DIFF, [LOG1, LOG2, LOG3], stdout)
    assert broken == "" and "APP_LOG_2.trn" in unjudged and "3013" in unjudged


def test_no_marker_at_all_is_not_judged():
    broken, unjudged = cc.chain_break(FULL, None, [LOG1], "Msg 15151 something else")
    assert broken == "" and "no header was printed" in unjudged


def test_columns_read_out_of_place_are_not_trusted():
    # A log that reads as a database backup: the positions are not where they were read.
    stdout = "\n".join([_line(0, "1", "100", "120"), _line(1, "1", "999", "1000")])
    broken, unjudged = cc.chain_break(FULL, None, [LOG1], stdout)
    assert broken == "" and "not trusted" in unjudged


def test_a_file_with_several_sets_is_read_at_its_first():
    stdout = "\n".join([_line(0, "1", "100", "120", position="2"), _line(0, "1", "50", "60", position="1")])
    heads, _ = cc.parse_headers(stdout, [FULL])
    assert heads[0]["first_lsn"] == 50


def test_the_batch_escapes_a_quote_in_a_path_and_reads_each_file():
    sql = cc.build_header_sql(["/b/O'Brien_FULL.bak", "/b/x.trn"])
    assert "N'/b/O''Brien_FULL.bak'" in sql and "(1, N'/b/x.trn')" in sql
    # Every position read is a column the table has by the time it fits (BASE_COLUMNS and up).
    assert max(cc.POSITIONS.values()) < 52 and "c48" in sql


def test_the_share_restore_refuses_a_broken_chain_and_runs_no_restore(tmp_path, monkeypatch):
    from conftest import patch_restore
    from tests.test_a_restore_measures_the_database_it_builds import _entry

    from dbabrain.backup_restore import restore_database as restore_module
    from dbabrain.lib import errors
    from dbabrain.lib.config import DbaBrainConfig

    config = _entry(tmp_path, databases=(), source_database_name="APP", restore_database_name="APP_DR")
    backup = config.vm_import_unc / "APP" / "FULL" / "latest.bak"
    backup.parent.mkdir(parents=True)
    backup.write_text("backup", encoding="utf-8")
    steps: list = []
    patch_restore(monkeypatch, "run_sqlcmd_query_command", lambda cmd, **_k: steps.append(cmd))
    patch_restore(monkeypatch, "find_restore_log_backups", lambda *_a, **_k: [backup.parent / "l1.trn", backup.parent / "l2.trn"])
    patch_restore(monkeypatch, "find_restore_diff_backup", lambda *_a, **_k: None)
    patch_restore(monkeypatch, "_missing_backup_paths", lambda *_a, **_k: set())
    patch_restore(monkeypatch, "_read_chain_headers", lambda *_a, **_k: "\n".join([
        _line(0, "1", "100", "120"), _line(1, "2", "110", "200"), _line(2, "2", "250", "300")]))

    with pytest.raises(errors.Refused, match="l2.trn"):
        restore_module.run_restore_database(
            config=config, backup_file=backup, ensure_certificate=False, ensure_credential=False,
            dbabrain_config=DbaBrainConfig(log_dir=tmp_path / "logs", runtime_dir=tmp_path / "runtime",
                                           sqlite_path=tmp_path / "runtime" / "dbabrain.sqlite"))
    assert steps == []
