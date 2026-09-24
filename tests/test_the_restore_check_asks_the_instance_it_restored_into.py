"""The verification must ask the SQL Server the restore wrote to, not a different one.

Found on 2026-09-19 by pointing the check at a real drill rather than at a fixture.

``ACME_SALES_TO_MSSQL_LAB_DOCKER`` restores into ``localhost,1453`` — the
container ``MSSQL_192_0_2_115_1453``. ``_verify_request`` built its target with ``"port": 1433``,
unconditionally, so the check connected to ``192.0.2.115:1433``: the container ``MSSQL_LAB``,
thirteen unrelated databases, none of them the drill's. Measured side by side that afternoon::

    port 1433   SALES_Stg  ABSENT      PAYROLL_Stg  ABSENT      ORDERS_Stg  ABSENT     -> 3 failed
    port 1453   SALES_Stg  ONLINE      PAYROLL_Stg  RESTORING   ORDERS_Stg  ONLINE     -> 1 failed

The right-hand line is the point twice over. ``PAYROLL_Stg`` was **stuck in RESTORING** — the single
state this whole verification exists to catch — and the check could not see it; and the two healthy
databases were being called broken. A reader given "SALES_Stg not present on the instance" goes to
look for a restore that never ran, which is the opposite of where the fault is.

So the port is read from the entry. A *named* instance has no port to give and is skipped with a
reason instead of guessed at, because guessing 1433 is how this happened.
"""

from __future__ import annotations

import pytest

from db_ops.backup_restore.cli import _verify_request
from db_ops.backup_restore.config import DatabaseRestoreMapping
from db_ops.lib import sql_instance


def Mapping(name):  # noqa: N802 - reads as the class it stands for, and now IS that class
    """The real mapping class. A hand-written stand-in here carried the attribute names the code
    asked for rather than the ones the parser produces, and hid that verification never ran."""
    return DatabaseRestoreMapping(source_database=name, target_database=name)


class Entry:
    """The estate's own drill, as `_verify_request` sees it."""

    vm_credential_target = "192.0.2.115"
    restore_sql_username = "sa"
    restore_sql_password_env = "MSSQL_LAB_1453_SA"
    restore_sql_instance_on_vm = "localhost,1453"
    databases = (Mapping("SALES_Stg"), Mapping("PAYROLL_Stg"), Mapping("ORDERS_Stg"))


SECRETS = {"MSSQL_LAB_1453_SA": "…"}


# --------------------------------------------------------------------------- #
# The request
# --------------------------------------------------------------------------- #
def test_the_port_comes_from_the_entry_and_not_from_a_default():
    request = _verify_request(Entry(), SECRETS)

    assert request["target"]["port"] == 1453, "1433 is a different container on this host"


def test_the_host_stays_this_node_s_route_and_is_not_taken_from_the_instance_string():
    """`sql_instance` reads `localhost` on most entries — localhost *on the target*, which is not
    where this process runs. Taking the host from there would check the wrong machine entirely."""
    request = _verify_request(Entry(), SECRETS)

    assert request["target"]["host"] == "192.0.2.115"


def test_an_entry_with_no_port_in_its_instance_string_still_gets_the_default():
    class Plain(Entry):
        restore_sql_instance_on_vm = "localhost"

    assert _verify_request(Plain(), SECRETS)["target"]["port"] == 1433


def test_a_named_instance_is_skipped_rather_than_asked_on_a_guessed_port():
    """A named instance is resolved by the SQL Server Browser at connect time. Substituting 1433
    would ask a different server and report its answer as this drill's verdict — which is the whole
    failure above, with different numbers."""
    class Named(Entry):
        restore_sql_instance_on_vm = "SQLHOST\\SQLEXPRESS"

    assert _verify_request(Named(), SECRETS) is None


def test_the_databases_asked_about_are_the_target_names():
    request = _verify_request(Entry(), SECRETS)

    assert request["database_names"] == ["SALES_Stg", "PAYROLL_Stg", "ORDERS_Stg"]


# --------------------------------------------------------------------------- #
# Reading the address
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("value", "port"), [
    ("localhost,1453", 1453),
    ("localhost", 1433),
    ("192.0.2.115,1433", 1433),
    ("tcp:host,1500", 1500),
    ("HOST\\INST,1600", 1600),
    ("", 1433),
])
def test_an_instance_string_gives_up_its_port(value, port):
    assert sql_instance.parse(value).port == port


def test_a_named_instance_has_no_port_to_give():
    address = sql_instance.parse("HOST\\SQLEXPRESS")

    assert address.port is None
    assert not address.has_port
    assert address.named_instance == "SQLEXPRESS"


# --------------------------------------------------------------------------- #
# The same question for the other two engines
# --------------------------------------------------------------------------- #
def test_oracle_is_told_which_instance_to_look_at(monkeypatch):
    """`sqlplus / as sysdba` takes whatever ORACLE_SID the shell carries. One container with one
    instance is right by accident; a host with two is not, and the check would answer for an
    instance nobody restored while labelling the row with the one they did. The restore step has
    always been given the SID — this one had not."""
    from db_ops.common import verifyrestore

    seen = {}

    def fake_run(host, command, timeout):
        seen["command"] = command
        return {"stdout": "READ WRITE 1", "exit_code": 0}

    monkeypatch.setattr(verifyrestore, "run", fake_run)
    answer = verifyrestore.verify({"db_type": "oracle", "host": {}, "oracle_sid": "LTR"})

    assert "ORACLE_SID=LTR sqlplus" in seen["command"]
    assert answer["databases"][0]["database_name"] == "LTR", "the row names what was actually asked"


def test_oracle_without_a_sid_behaves_exactly_as_before(monkeypatch):
    """A caller that cannot name one must not be given a guess."""
    from db_ops.common import verifyrestore

    seen = {}
    monkeypatch.setattr(verifyrestore, "run",
                        lambda host, command, timeout: (seen.update(command=command)
                                                        or {"stdout": "READ WRITE 1",
                                                            "exit_code": 0}))
    verifyrestore.verify({"db_type": "oracle", "host": {}})

    assert "ORACLE_SID=" not in seen["command"]


def test_postgres_is_told_which_cluster_when_the_job_names_one(monkeypatch):
    """psql with no -p takes the default cluster: right inside a container, wrong on a host with
    two."""
    from db_ops.common import verifyrestore

    seen = {}
    monkeypatch.setattr(verifyrestore, "run",
                        lambda host, command, timeout: (seen.update(command=command)
                                                        or {"stdout": "f 4", "exit_code": 0}))
    verifyrestore.verify({"db_type": "postgresql", "host": {}, "port": 5433})

    assert "-p 5433" in seen["command"]


def test_postgres_without_a_port_is_unchanged(monkeypatch):
    from db_ops.common import verifyrestore

    seen = {}
    monkeypatch.setattr(verifyrestore, "run",
                        lambda host, command, timeout: (seen.update(command=command)
                                                        or {"stdout": "f 4", "exit_code": 0}))
    verifyrestore.verify({"db_type": "postgresql", "host": {}})

    assert "-p " not in seen["command"]


def test_the_restore_plan_hands_the_check_what_it_hands_the_restore():
    """The wiring, not just the ability: a parameter the planner never passes is a parameter that
    does not exist. This is how the SQL Server port came to be 1433 for two releases."""
    import inspect

    from db_ops.backup_restore import restore_by_id

    oracle = inspect.getsource(restore_by_id._plan_oracle)
    postgres = inspect.getsource(restore_by_id._plan_postgresql)

    assert '"oracle_sid": request["oracle_sid"]' in oracle
    assert '"port": job.env["PGPORT"]' in postgres
