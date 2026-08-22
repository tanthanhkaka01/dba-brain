"""A restore must copy the chain it restores, not the whole backup history.

The transfer used to send the entire source backup directory, and ``prune_target_dir`` then
deleted whatever was past the target's retention. Because the source keeps 14 days and the target
8, the files in that gap were copied and deleted again on every run — measured on the CLOUD lab
as ``copied=1090 skipped=14495 pruned=1089`` on three consecutive runs, about 310 MB crossing an
internet link each time to be thrown away. The comment on the prune call assumed "whatever is old
here is old at the source too", which is only true when the two retentions agree.

PostgreSQL's layout names the chain (`<stamp>_FULL`, `<stamp>_INCR`), so the copy can be narrowed
to exactly what ``pg_combinebackup`` will read. Oracle's layout does not: an RMAN directory is flat
and its chain is a property of the catalog, not of the file names. Oracle is narrowed all the same,
by **asking RMAN** — `RESTORE DATABASE PREVIEW` for the datafile pieces, then the catalog for
everything recorded from that level 0 onward. The rule these tests pin is the distinction, not the
absence of narrowing: deriving an RMAN chain from `FREE_L0_<date>_...` file names is still
forbidden, because it is a second and weaker copy of logic RMAN already owns, and getting it wrong
does not fail loudly — DUPLICATE restores to whatever point the pieces present allow.

It matters at this size: the lab's directory reached 90 GB, of which the chain was ~5 GB, the rest
seven days of 45 MB controlfile autobackups written every 15 minutes.

The directory listing is the real one from the lab on 2026-08-04.
"""

from __future__ import annotations

from db_ops.backup_restore import restore_script


LAB_LISTING = """\
/b/base/20260725T122854Z_FULL
/b/base/20260725T122857Z_INCR
/b/base/20260725T180522Z_INCR
/b/base/20260726T180136Z_FULL
/b/base/20260727T132956Z_FULL
/b/base/20260728T011007Z_FULL
/b/base/20260728T011010Z_INCR
/b/base/20260728T211213Z_INCR
/b/base/20260729T180315Z_INCR
/b/base/20260730T180209Z_INCR
/b/base/20260731T180522Z_INCR
/b/base/20260801T180602Z_INCR
/b/base/20260801T224721Z_INCR
/b/base/20260802T185220Z_FULL
/b/base/20260802T225227Z_FULL
/b/base/20260803T185512Z_INCR
/b/base/20260804T081733Z_INCR
"""


class _FakeStdout:
    def __init__(self, text):
        self._text = text.encode("utf-8")

    def read(self):
        return self._text


class _FakeClient:
    """Just enough paramiko to answer the one `ls` the chain selection makes."""

    def __init__(self, listing):
        self.listing = listing
        self.commands = []

    def exec_command(self, command):
        self.commands.append(command)
        return None, _FakeStdout(self.listing), None


class _Job:
    def __init__(self, db_type):
        self.db_type = db_type
        self.source_backup_host_dir = "/b"


def test_only_the_newest_full_and_the_incrementals_after_it_are_copied():
    include = restore_script._postgresql_chain_include(_Job("postgresql"), _FakeClient(LAB_LISTING))
    assert include == (
        "base/20260802T225227Z_FULL",
        "base/20260803T185512Z_INCR",
        "base/20260804T081733Z_INCR",
        "wal/",
    )


def test_the_older_chains_the_restore_will_never_read_are_left_behind():
    """14 of the lab's 17 backup directories belong to superseded chains. Those are the files
    that were being copied and pruned on every run."""
    include = restore_script._postgresql_chain_include(_Job("postgresql"), _FakeClient(LAB_LISTING))
    copied = [prefix for prefix in include if prefix.startswith("base/")]
    assert len(copied) == 3
    assert "base/20260802T185220Z_FULL" not in include  # the previous full, same day
    assert "base/20260801T224721Z_INCR" not in include  # chained to a full that is gone


def test_the_wal_directory_always_travels_whole():
    """Which segments recovery needs is decided by PostgreSQL at replay time, not here — so the
    narrowing must never reach into wal/."""
    include = restore_script._postgresql_chain_include(_Job("postgresql"), _FakeClient(LAB_LISTING))
    assert "wal/" in include


def test_a_source_with_no_full_backup_copies_everything_rather_than_guessing():
    """A narrowed copy that guessed wrong fails the restore; an un-narrowed one only costs
    bandwidth. When the listing cannot be trusted, spend the bandwidth."""
    only_incrementals = "/b/base/20260803T185512Z_INCR\n"
    assert restore_script._postgresql_chain_include(
        _Job("postgresql"), _FakeClient(only_incrementals)) == ()
    assert restore_script._postgresql_chain_include(_Job("postgresql"), _FakeClient("")) == ()


def test_sqlserver_still_copies_the_whole_directory():
    assert restore_script._transfer_include(_Job("sqlserver"), _FakeClient(LAB_LISTING)) == ()


# --------------------------------------------------------------------------- #
# Oracle: narrowed by the catalog, never by the file names
# --------------------------------------------------------------------------- #
RMAN_PREVIEW = """\
List of Backup Sets
BS Key  Type LV Size
  Piece Name: /opt/oracle/backup/dbops/FREE_L0_20260805_6q9auau9_4314_1_1.bkp
  Piece Name: /opt/oracle/backup/dbops/FREE_L0_20260805_6rmbuavm_4315_1_1.bkp
  Piece Name: /opt/oracle/backup/dbops/FREE_L0_20260805_6sfcub0f_4316_1_1.bkp
List of Archived Log Copies for database with db_unique_name FREE
  Name: /opt/oracle/oradata/fra/FREE/archivelog/2026_08_05/o1_mf_1_6041_o75yt05g_.arc
"""

# What the catalog answers for "every piece from that level 0 onward": the level 0 itself, the
# archivelog backups that roll it forward, a controlfile autobackup - and one handle that lives
# outside the directory being transferred.
CATALOG_HANDLES = """\
/opt/oracle/backup/dbops/FREE_L0_20260805_6q9auau9_4314_1_1.bkp
/opt/oracle/backup/dbops/FREE_L0_20260805_6rmbuavm_4315_1_1.bkp
/opt/oracle/backup/dbops/FREE_L0_20260805_6sfcub0f_4316_1_1.bkp
/opt/oracle/backup/dbops/arch_FREE_20260805_6mamtaaa_4310_1_1.bkp
/opt/oracle/backup/dbops/autobackup_c-1506701350-20260805-1c
/opt/oracle/fra/autobackup_c-1506701350-20260805-1d
"""


class _OracleClient:
    """Answers the RMAN preview and the catalog query, in that order."""

    def __init__(self, preview=RMAN_PREVIEW, handles=CATALOG_HANDLES):
        self.preview, self.handles = preview, handles
        self.commands = []

    def exec_command(self, command, timeout=None):
        self.commands.append(command)
        payload = self.preview if "rman target" in command else self.handles
        return None, _FakeStdout(payload), None


class _OracleSource:
    container_name = "ora_dg_lab-primary"


class _OracleJob:
    db_type = "oracle"
    backup_dir = "/opt/oracle/backup/dbops"
    source_backup_host_dir = "/opt/db_ops/containers/ora_dg_lab/backup/dbops"


def test_oracle_copies_only_the_pieces_the_catalog_names():
    include = restore_script._transfer_include(
        _OracleJob(), _OracleClient(), source=_OracleSource())

    assert include == (
        "FREE_L0_20260805_6q9auau9_4314_1_1.bkp",
        "FREE_L0_20260805_6rmbuavm_4315_1_1.bkp",
        "FREE_L0_20260805_6sfcub0f_4316_1_1.bkp",
        "arch_FREE_20260805_6mamtaaa_4310_1_1.bkp",
        "autobackup_c-1506701350-20260805-1c",
    )


def test_a_piece_outside_the_transferred_directory_is_not_included():
    """The catalog knows handles anywhere on the source - an FRA copy has no counterpart here."""
    include = restore_script._transfer_include(
        _OracleJob(), _OracleClient(), source=_OracleSource())

    assert not any(name.endswith("-1d") for name in include)


def test_oracle_asks_rman_and_never_reads_the_file_names():
    """The guard on the whole design: the chain must come from RMAN, not from a directory listing."""
    client = _OracleClient()
    restore_script._transfer_include(_OracleJob(), client, source=_OracleSource())

    assert any("rman target" in c for c in client.commands)
    assert any("v$backup_piece" in c for c in client.commands)
    assert not any(c.lstrip().startswith("ls ") for c in client.commands)


def test_a_preview_that_names_no_pieces_copies_everything():
    """Spend the bandwidth rather than restore to an older point than the operator believes."""
    assert restore_script._transfer_include(
        _OracleJob(), _OracleClient(preview="RMAN-06026: some targets not found\n"),
        source=_OracleSource()) == ()


def test_a_catalog_answer_with_nothing_under_the_backup_dir_copies_everything():
    assert restore_script._transfer_include(
        _OracleJob(), _OracleClient(handles="/somewhere/else/piece.bkp\n"),
        source=_OracleSource()) == ()


def test_oracle_copies_everything_when_the_source_container_is_unknown():
    """Without a container there is nothing to ask, and guessing is what this must never do."""
    assert restore_script._transfer_include(_OracleJob(), _OracleClient()) == ()


def test_postgresql_is_selected_by_db_type_whichever_spelling_config_uses():
    for spelling in ("postgresql", "postgres", "PostgreSQL"):
        include = restore_script._transfer_include(_Job(spelling), _FakeClient(LAB_LISTING))
        assert include and include[-1] == "wal/"
