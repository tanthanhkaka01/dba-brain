"""A verification that never reached the database must not label it healthy.

Measured on 2026-09-19 against the runtime store on 192.0.2.115, three ways — ``psql`` missing
from the container, a role that does not exist, and a ``sudo`` that wanted a terminal. All three
came back::

    ok=False   state=ACCEPTING   detail=psql: not found

The verdict was right and the word beside it was the opposite of the truth. ``state`` was
``"IN RECOVERY" if in_recovery else "ACCEPTING"``, and ``in_recovery`` is false whenever the output
does not begin with ``t`` — including when there is no output at all, because nothing ran. The flag
is what the workflow counts; **the word is what a person reads**, in the summary and in the Telegram
message, and it said the cluster was accepting connections.

The other two engines were checked at the same time and are honest already: SQL Server raises when
it cannot connect, so no row is invented; Oracle falls back to ``UNKNOWN`` when no open mode was
matched. Only PostgreSQL had a default that read as good news.
"""

from __future__ import annotations

import pytest

from db_ops.common import verifyrestore


def _run(monkeypatch, *, stdout: str, exit_code: int) -> dict:
    monkeypatch.setattr(verifyrestore, "run",
                        lambda host, command, timeout: {"stdout": stdout, "exit_code": exit_code})
    return verifyrestore.verify({"db_type": "postgresql", "host": {}, "database": "db_ops"})


def test_a_cluster_that_answered_is_accepting(monkeypatch):
    """The live store, as measured: not in recovery, four databases."""
    answer = _run(monkeypatch, stdout="f\n4\n", exit_code=0)

    row = answer["databases"][0]
    assert row["ok"] is True
    assert row["state"] == "ACCEPTING"


def test_a_cluster_still_in_recovery_is_not_usable_and_says_which(monkeypatch):
    answer = _run(monkeypatch, stdout="t\n4\n", exit_code=0)

    row = answer["databases"][0]
    assert row["ok"] is False
    assert row["state"] == "IN RECOVERY"


def test_a_command_that_could_not_run_is_not_called_accepting(monkeypatch):
    """`psql: not found` — the check never reached a cluster, so it has nothing to report about
    one. This said ACCEPTING."""
    answer = _run(monkeypatch, stdout="sh: 1: psql: not found", exit_code=127)

    row = answer["databases"][0]
    assert row["ok"] is False
    assert row["state"] == "NO ANSWER"
    assert "psql" in row["detail"]


def test_a_login_the_cluster_refuses_is_not_called_accepting(monkeypatch):
    answer = _run(monkeypatch, stdout='psql: error: FATAL: role "nobody_here" does not exist',
                  exit_code=2)

    row = answer["databases"][0]
    assert row["ok"] is False
    assert row["state"] == "NO ANSWER"
    assert "nobody_here" in row["detail"]


def test_silence_is_reported_as_silence_rather_than_as_an_empty_detail(monkeypatch):
    """The `sudo` case produced no output at all, and an empty detail tells the reader nothing about
    where to look."""
    answer = _run(monkeypatch, stdout="", exit_code=1)

    row = answer["databases"][0]
    assert row["state"] == "NO ANSWER"
    assert row["detail"] == "exit code 1, no output"


@pytest.mark.parametrize("stdout", ["t", "true", "T\n4"])
def test_recovery_is_read_only_from_a_command_that_worked(monkeypatch, stdout):
    """A failed command whose output happens to start with `t` must not be read as a state either."""
    assert _run(monkeypatch, stdout=stdout, exit_code=1)["databases"][0]["state"] == "NO ANSWER"
    assert _run(monkeypatch, stdout=stdout, exit_code=0)["databases"][0]["state"] == "IN RECOVERY"


# --------------------------------------------------------------------------- #
# Oracle, and the container that is not there
# --------------------------------------------------------------------------- #
def _oracle(monkeypatch, *, stdout: str, exit_code: int, stderr: str = "", **over):
    monkeypatch.setattr(verifyrestore, "run",
                        lambda host, command, timeout: {"stdout": stdout, "exit_code": exit_code,
                                                        "stderr": stderr})
    return verifyrestore.verify({"db_type": "oracle", "host": {}, **over})


def test_an_open_oracle_database_answers(monkeypatch):
    row = _oracle(monkeypatch, stdout="READ WRITE 1", exit_code=0)["databases"][0]

    assert row["ok"] is True
    assert row["state"] == "READ WRITE"


def test_a_mounted_oracle_database_is_the_trap_and_is_caught(monkeypatch):
    """RMAN finished, the instance is up, and the database is not open."""
    row = _oracle(monkeypatch, stdout="MOUNTED", exit_code=0)["databases"][0]

    assert row["ok"] is False
    assert row["state"] == "MOUNTED"


def test_an_sid_that_is_not_there_is_reported_with_oracle_s_own_error(monkeypatch):
    """Measured against the lab container: ORA-01034, and the row named the SID that was asked."""
    row = _oracle(monkeypatch, stdout="ERROR at line 1: ORA-01034: ORACLE not available",
                  exit_code=1, oracle_sid="NOSUCHSID")["databases"][0]

    assert row["ok"] is False
    assert row["database"] == "NOSUCHSID"
    assert "ORA-01034" in row["detail"]


def test_a_container_that_cannot_be_reached_says_why(monkeypatch):
    """The case that had nothing to show. `docker exec` writes "No such container" to stderr, and
    Oracle's command - unlike the PostgreSQL one - does not redirect it, so the detail came back
    empty: a correct verdict with nothing in it for whoever has to fix the thing."""
    row = _oracle(monkeypatch, stdout="", exit_code=125,
                  stderr="Error response from daemon: No such container: oracle_lab"
                  )["databases"][0]

    assert row["ok"] is False
    assert row["state"] == "UNKNOWN"
    assert "No such container" in row["detail"]


def test_silence_with_no_stderr_either_still_names_the_exit_code(monkeypatch):
    row = _oracle(monkeypatch, stdout="", exit_code=127)["databases"][0]

    assert row["detail"] == "exit code 127, no output"


def test_postgres_reports_its_stderr_too(monkeypatch):
    monkeypatch.setattr(verifyrestore, "run",
                        lambda host, command, timeout: {
                            "stdout": "", "exit_code": 125,
                            "stderr": "Error response from daemon: No such container: pg_store"})
    row = verifyrestore.verify({"db_type": "postgresql", "host": {}})["databases"][0]

    assert row["state"] == "NO ANSWER"
    assert "No such container" in row["detail"]
