"""A SQL Server RESTORE is written in one place, and it is the text the nightly restore always ran.

Until 0.24.0 two modules wrote it: the nightly SMB restore (`backup_restore restore-latest`) its own
batch, run through `run-sqlcmd`, and `common restore-full/-diff/-log` another, for the drills - a
third lived behind `restore-database`. The operator's rule (R43): one job, one command. The nightly's
shapes moved into `common/restorestep/sqlserver.py`, and the app asks for its steps there.

The nightly restores a production estate every night, so moving its text is only safe if the text
does not move. `tests/fixtures/nightly_restore_sql.json` is what its own builders emitted on
0.23.0, captured before they went; the composer must give it back byte for byte. The one place it
may differ is a quote inside a name or a path, which the old batch escaped one level too few and
so could not have run at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from db_ops.common.restorestep import RestoreStepError
from db_ops.common.restorestep import sqlserver as mssql

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "nightly_restore_sql.json")
                    .read_text(encoding="utf-8"))["cases"]


def _composed(inputs: dict, kind: str) -> str:
    files = {"data": inputs["data"], "log": inputs["log"]}
    requests = {
        "full_norecovery": ("full", {"move_files": files}, inputs["full"]),
        "full_recovery": ("full", {"move_files": files, "with_recovery": True}, inputs["full"]),
        "diff": ("diff", {}, inputs["diff"]),
        "log": ("log", {}, inputs["logfile"]),
        # As the app sends it: the moment in UTC with its offset, read into the server's clock.
        "log_stopat": ("log", {"with_recovery": True, "stopat": inputs["stopat"] + "+00:00"},
                       inputs["logfile"]),
    }
    level, fields, path = requests[kind]
    # The nightly overwrote its own last restore, which is what overwrite_existing states; without it
    # a full gets the ONLINE guard first (G2.10, tests/test_a_restore_never_overwrites_an_online_database.py).
    return mssql.build_statements(level, {"database_name": inputs["database"], "overwrite_existing": True,
                                          **fields}, [path])[0]


@pytest.mark.parametrize("case", sorted(GOLDEN))
@pytest.mark.parametrize("kind", ["full_norecovery", "full_recovery", "diff", "log", "log_stopat"])
def test_the_composer_writes_what_the_nightly_restore_ran(case, kind):
    assert _composed(GOLDEN[case]["inputs"], kind) == GOLDEN[case][kind]


def test_a_quote_in_a_name_or_path_is_doubled_once_per_literal_it_sits_in():
    """Inside the EXEC string and inside @restoreSql a value is two literals deep. The nightly doubled
    its quote once, so an `'` ended the outer literal and the batch could not parse."""
    text = mssql.build_statements("full", {
        "database_name": "Odd]Name's", "move_files": {"data": r"D:\O'dd.mdf", "log": r"D:\O'dd.ldf"},
    }, [r"E:\in\full's.bak"])[0]

    assert "IF DB_ID(N'Odd]Name''s') IS NOT NULL" in text          # one literal deep
    assert "ALTER DATABASE [Odd]]Name's] SET SINGLE_USER" in text   # an identifier, no literal
    assert r"EXEC('RESTORE FILELISTONLY FROM DISK = N''E:\in\full''''s.bak''');" in text
    assert "RESTORE DATABASE [Odd]]Name''s]" in text               # inside @restoreSql
    assert r"TO N''D:\O''''dd.mdf''," in text


def test_the_two_ways_of_placing_the_files_are_one_or_the_other():
    with pytest.raises(RestoreStepError, match="not both"):
        mssql.build_statements("full", {"database_name": "D", "move": {"a": "/a"},
                                        "move_files": {"data": "/d", "log": "/l"}}, ["/b.bak"])
    with pytest.raises(RestoreStepError, match="both \"data\" and \"log\""):
        mssql.build_statements("full", {"database_name": "D", "move_files": {"data": "/d"}}, ["/b.bak"])
    with pytest.raises(RestoreStepError, match="full restore only"):
        mssql.build_statements("log", {"database_name": "D", "move_files": {"data": "/d", "log": "/l"}},
                               ["/b.trn"])


def test_through_sqlcmd_a_step_runs_one_file_and_answers_as_run_sqlcmd_does(monkeypatch):
    """The caller reads the answer - a Msg 4305 log is skipped, a lost connection is inspected - so
    the step answers what sqlcmd said and does not decide for it."""
    from db_ops.common import sqlcmd_run

    seen = {}

    def run(request):
        seen.update(request)
        return {"via": "ssh", "exit_code": 1, "stdout": "Msg 4305 ...", "stderr": "",
                "timed_out": False, "duration_ms": 12}

    monkeypatch.setattr(sqlcmd_run, "run_sqlcmd", run)
    block = {"instance": "localhost,1433", "via": "ssh", "host": {"host": "192.0.2.10"},
             "sql": "ignored - the step writes its own"}

    answer = mssql.apply("log", {"database_name": "D", "sqlcmd": block}, ["/i/l.trn"])

    assert seen["sql"] == answer["statements"][0] and "RESTORE LOG [D]" in seen["sql"]
    assert seen["instance"] == "localhost,1433" and seen["host"] == {"host": "192.0.2.10"}
    assert (answer["exit_code"], answer["stdout"], answer["ran"], answer["applied"]) == (
        1, "Msg 4305 ...", ["/i/l.trn"], [])
    with pytest.raises(RestoreStepError, match="one file per call"):
        mssql.apply("log", {"database_name": "D", "sqlcmd": block}, ["/i/1.trn", "/i/2.trn"])
