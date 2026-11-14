"""A PostgreSQL or Oracle restore replaces its target only when the entry says so, or it holds nothing (0.39.0, 1.362).

The operator, 2026-11-13 (translated): *overwriting a DB must be set explicitly, off by default* - and,
asked of these two engines: *what does not overwriting mean for Oracle, PostgreSQL?*

Until 0.39.0 nothing: neither restore step read ``overwrite_existing``. A base backup became the
target's whole data directory and an RMAN duplicate rebuilt the target's whole database, whatever was
there - and ``upgrade-config`` had just written ``false`` into every such entry of this estate, a
word the steps did not hear.

Now it means: without ``overwrite_existing: true`` the step goes on only when the target holds **no
user data** - a data directory that is absent, empty, or a cluster with no database and no relation
of its own (read from the directory, so a stopped or crash-looping target answers too); an Oracle
database that is open with no segment owned by a schema of its own. Anything else stops the restore
**before the target is touched**, and what cannot be read is refused, never assumed empty.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from dbabrain.backup_restore import restore_by_id
from dbabrain.common.restorestep import RestoreStepError, target_holds as T

DONE = "DBABRAIN_HOLDS_DONE"


# --------------------------------------------------------------------------- #
# What a data directory holds, read from its files
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text, read, empty, said", [
    (f"STATE=absent\n{DONE}\n", True, True, "is absent"),
    (f"STATE=empty\n{DONE}\n", True, True, "is empty"),
    # initdb's own: template1 (1), template0 (4), postgres (5) - no relation of the cluster's own in them.
    (f"STATE=cluster\nREL=1:0\nREL=4:0\nREL=5:0\n{DONE}\n", True, True, "no database and no relation of its own"),
    (f"STATE=cluster\nREL=1:0\nREL=5:0\nDB=16385\nDB=16402\n{DONE}\n", True, False, "2 database(s) of its own (OID 16385, 16402)"),
    (f"STATE=cluster\nREL=1:0\nREL=5:3\n{DONE}\n", True, False, "3 relation(s) of its own in the databases initdb made"),
    (f"STATE=cluster\nREL=5:0\nTS=16500\n{DONE}\n", True, False, "1 tablespace(s)"),
    (f"STATE=not_a_cluster\n{DONE}\n", True, False, "files that are not a PostgreSQL cluster"),
    (f"STATE=unreadable\n{DONE}\n", False, False, "cannot be read by the user the restore runs as"),
])
def test_a_data_directory_is_read_as_what_it_holds(text, read, empty, said):
    holds = T.read_postgresql(text, "/pgdata")

    assert (holds.read, holds.empty) == (read, empty)
    assert said in holds.what


@pytest.mark.parametrize("text", ["", "STATE=cluster\nDB=16385\n", "docker: Error response from daemon",
                                  f"STATE=cluster\nREL=5:x\n{DONE}\n"])
def test_a_listing_that_did_not_finish_is_not_an_empty_directory(text):
    holds = T.read_postgresql(text, "/pgdata")

    assert holds.read is False and holds.empty is False
    assert "could not be read" in holds.what


def test_the_listing_asks_the_directory_about_objects_made_after_initdb():
    command = T.postgresql_command("/var/lib/postgresql/18/main")

    assert "d=/var/lib/postgresql/18/main;" in command
    assert f"-ge {T.FIRST_NORMAL_OBJECT_ID}" in command and T.FIRST_NORMAL_OBJECT_ID == 16384
    assert command.endswith(f"echo {DONE}"), "the last line says the listing ran to its end"
    # Read only: nothing in it writes, removes or starts anything.
    assert not any(word in command for word in ("rm ", "mv ", "> ", "touch", "pg_ctl", "docker"))


@pytest.mark.parametrize("text, read, empty, said", [
    (f"OPEN_MODE=READ WRITE\nUSER_SEGMENTS=0\n{DONE}\n", True, True, "no schema of its own owns a segment"),
    (f"OPEN_MODE=READ WRITE\nUSER_SEGMENTS=12\nUSER_SCHEMA=APP\nUSER_SCHEMA=HR\n{DONE}\n", True, False,
     "12 segment(s) owned by its own schema(s) (APP, HR)"),
    ("ERROR:\nORA-01034: ORACLE not available\n", False, False, "ORA-01034"),
    ("OPEN_MODE=MOUNTED\nERROR at line 1:\nORA-01219: database or pluggable database not open\n", False, False,
     "not open"),
    ("", False, False, "could not be read"),
])
def test_an_oracle_database_is_read_as_what_it_holds(text, read, empty, said):
    holds = T.read_oracle(text, "FREE")

    assert (holds.read, holds.empty) == (read, empty)
    assert said in holds.what


def test_the_refusal_says_what_was_found_and_what_to_state():
    held = T.refusal("postgresql", T.read_postgresql(f"STATE=cluster\nDB=16385\n{DONE}", "/pgdata"))
    unread = T.refusal("oracle", T.read_oracle("ORA-01034: ORACLE not available", "FREE"))

    assert held.startswith("the cluster in /pgdata holds 1 database(s) of its own")
    assert "a base backup becomes the target's whole data directory" in held
    assert "Nothing was restored" in held and "overwrite_existing: true" in held
    assert "what cannot be read is not replaced" in unread and "a duplicate rebuilds the target's whole database" in unread


# --------------------------------------------------------------------------- #
# PostgreSQL: asked before the target is stopped
# --------------------------------------------------------------------------- #
@pytest.fixture
def postgres(monkeypatch):
    """The container path of the PostgreSQL restore step, every command recorded; the data directory
    answers with ``held``."""
    import dbabrain.common.restorestep.postgresql as pg

    calls: list[str] = []
    state = {"held": f"STATE=cluster\nREL=1:0\nREL=5:0\nDB=16385\n{DONE}\n"}
    mounts = [{"Source": "/var/lib/docker/volumes/v/_data", "Destination": "/var/lib/postgresql"}]

    def fake_run(host, command, **kw):
        calls.append(command)
        if DONE in command:
            return {"exit_code": 0, "stdout": state["held"], "stderr": ""}
        if "inspect -f" in command:
            return {"exit_code": 0, "stdout": "postgres:18\n", "stderr": ""}
        if "pg_is_in_recovery" in command:
            return {"exit_code": 0, "stdout": "f\n", "stderr": ""}
        return {"exit_code": 0, "stdout": json.dumps([{"Mounts": mounts}]), "stderr": ""}

    monkeypatch.setattr(pg, "run", fake_run)

    def run(held=None, runtime="docker", **extra):
        if held is not None:
            state["held"] = held
        from dbabrain.common.restorestep import restore_step
        host = {"runtime": runtime, "host": "h", "sudo": True, **({"container": "c"} if runtime == "docker" else {})}
        return restore_step("full", {
            "db_type": "postgresql", "data_dir": "/var/lib/postgresql/18/docker",
            "staging_dir": "/var/lib/postgresql/dbabrain_staging",
            "backup_path": "/in/base/a_FULL", "host": host, **extra})
    run.calls = calls
    return run


def test_a_cluster_with_a_database_of_its_own_stops_the_restore_before_the_target_is_stopped(postgres):
    with pytest.raises(RestoreStepError, match="holds 1 database\\(s\\) of its own .* overwrite_existing is not true"):
        postgres()

    assert not [c for c in postgres.calls if c.endswith(" stop c")], "a refused restore leaves the target running"
    assert not [c for c in postgres.calls if "mv -t" in c or "rm -rf" in c], "and moves or removes nothing"


def test_it_is_read_through_the_targets_own_volumes_so_a_stopped_target_answers(postgres):
    with pytest.raises(RestoreStepError):
        postgres()

    asked = next(c for c in postgres.calls if DONE in c)
    assert "run --rm -u postgres --volumes-from c " in asked and " exec " not in asked


@pytest.mark.parametrize("held", [f"STATE=empty\n{DONE}\n", f"STATE=cluster\nREL=1:0\nREL=4:0\nREL=5:0\n{DONE}\n"])
def test_a_target_that_holds_nothing_is_restored_onto_without_the_word(postgres, held):
    answer = postgres(held=held)

    assert [c for c in postgres.calls if c.endswith(" stop c")] and [c for c in postgres.calls if "mv -t" in c]
    assert answer["target_held"].startswith("/var/lib/postgresql/18/docker is ")


def test_what_cannot_be_read_is_not_replaced(postgres):
    with pytest.raises(RestoreStepError, match="could not be read .* what cannot be read is not replaced"):
        postgres(held="docker: Error response from daemon: No such container: c")

    assert not [c for c in postgres.calls if c.endswith(" stop c")]


def test_the_entrys_word_replaces_it_and_the_answer_says_what_was_there(postgres):
    answer = postgres(overwrite_existing=True)

    assert [c for c in postgres.calls if "mv -t" in c]
    assert "holds 1 database(s) of its own" in answer["target_held"]
    assert answer["target_held"].endswith("replaced (overwrite_existing is true)")


@pytest.mark.parametrize("word", [1, "true", None, False])
def test_only_true_is_the_word(postgres, word):
    with pytest.raises(RestoreStepError, match="overwrite_existing is not true"):
        postgres(overwrite_existing=word)


def test_a_host_with_no_container_is_asked_the_same_way(postgres):
    with pytest.raises(RestoreStepError, match="holds 1 database"):
        postgres(runtime="linux")

    assert len(postgres.calls) == 1 and DONE in postgres.calls[0], "asked, and nothing else ran"


def test_a_dry_run_asks_nothing_and_changes_nothing(postgres):
    answer = postgres(dry_run=True)

    assert answer["dry_run"] is True and postgres.calls == []


# --------------------------------------------------------------------------- #
# Oracle: asked before anything is staged or the instance is touched
# --------------------------------------------------------------------------- #
@pytest.fixture
def oracle(monkeypatch):
    import dbabrain.common.restorestep.oracle as ora
    import dbabrain.common.restorestep.postgresql as pg

    calls: list[str] = []
    state = {"held": f"OPEN_MODE=READ WRITE\nUSER_SEGMENTS=7\nUSER_SCHEMA=APP\n{DONE}\n"}

    def fake_run(host, command, **kw):
        calls.append(command)
        if "sqlplus" in command and "rman" not in command and len([c for c in calls if "sqlplus" in c]) == 1:
            return {"exit_code": 0, "stdout": state["held"], "stderr": ""}
        return {"exit_code": 0, "stdout": "Finished Duplicate Db", "stderr": ""}

    monkeypatch.setattr(pg, "run", fake_run)
    monkeypatch.setattr(ora, "run", fake_run)
    monkeypatch.setattr(pg, "_container_mounts", lambda *a, **k: [])

    def run(held=None, **extra):
        if held is not None:
            state["held"] = held
        return ora.apply("full", {"db_type": "oracle", "mode": "duplicate", "oracle_sid": "FREE",
                                  "backup_location": "/in/ora",
                                  "host": {"runtime": "docker", "host": "h", "container": "c"}, **extra}, [])
    run.calls = calls
    return run


def test_a_database_with_user_segments_stops_the_duplicate_before_anything_is_touched(oracle):
    with pytest.raises(RestoreStepError, match="FREE holds 7 segment\\(s\\) owned by its own schema\\(s\\) \\(APP\\)"):
        oracle()

    assert len(oracle.calls) == 1, "asked once: nothing staged into the container, no SHUTDOWN ABORT, no RMAN"


def test_an_instance_that_is_not_open_cannot_say_and_is_not_replaced(oracle):
    with pytest.raises(RestoreStepError, match="what cannot be read is not replaced"):
        oracle(held="ERROR:\nORA-01034: ORACLE not available\n")

    assert not [c for c in oracle.calls if "rman" in c]


def test_an_open_database_with_no_user_data_is_duplicated_onto(oracle):
    answer = oracle(held=f"OPEN_MODE=READ WRITE\nUSER_SEGMENTS=0\n{DONE}\n")

    assert [c for c in oracle.calls if "rman auxiliary" in c]
    assert answer["target_held"] == "FREE is open and no schema of its own owns a segment"


def test_the_entrys_word_replaces_an_oracle_database(oracle):
    answer = oracle(overwrite_existing=True)

    assert [c for c in oracle.calls if "rman auxiliary" in c]
    assert answer["target_held"].endswith("replaced (overwrite_existing is true)")


def test_the_question_is_asked_of_the_instance_the_restore_names(oracle):
    with pytest.raises(RestoreStepError):
        oracle()

    assert oracle.calls[0].startswith("export ORACLE_SID=FREE && ")


# --------------------------------------------------------------------------- #
# The entry's word reaches both steps - and a lockdown takes it away
# --------------------------------------------------------------------------- #
def _pg_plan(monkeypatch, overwrite):
    monkeypatch.setattr(restore_by_id, "_list_backup_files", lambda request: {
        "files": [{"path": "/in/base/a_FULL"}] if "full" in request["kinds"] else [],
        "newest_finished_at": "2026-09-24T10:00:00Z"})
    job = SimpleNamespace(restore_id="R", server_id="SRC", target_server_id="TGT", target_backup_dir="/in",
                          target_visible_dir="", backup_dir="/o", env={}, overwrite_existing=overwrite)
    return restore_by_id._plan_postgresql(job, {}, point_in_time="", data_dir=None,
                                          host={"runtime": "docker", "host": "h", "container": "c"})


def _ora_plan(overwrite):
    job = SimpleNamespace(restore_id="R", server_id="SRC", target_server_id="TGT", target_backup_dir="/in",
                          target_visible_dir="", backup_dir="/o", env={}, env_secrets={},
                          overwrite_existing=overwrite)
    return restore_by_id._plan_oracle(job, {}, point_in_time="", data_dir=None,
                                      host={"runtime": "docker", "host": "h", "container": "c"})


@pytest.mark.parametrize("overwrite", [True, False])
def test_the_plan_hands_each_engine_the_entrys_word(monkeypatch, overwrite):
    assert _pg_plan(monkeypatch, overwrite)[0]["request"]["overwrite_existing"] is overwrite
    assert _ora_plan(overwrite)[0]["request"]["overwrite_existing"] is overwrite


def test_a_lockdown_takes_the_word_away(monkeypatch):
    from dbabrain.db import lockdown_store

    monkeypatch.setattr(lockdown_store, "lockdown_on", lambda app_config=None: True)

    assert _pg_plan(monkeypatch, True)[0]["request"]["overwrite_existing"] is False
    assert _ora_plan(True)[0]["request"]["overwrite_existing"] is False


def test_the_requests_are_ones_the_steps_reference_takes(monkeypatch):
    from dbabrain.lib import request_check

    for step in (_pg_plan(monkeypatch, False)[0], _ora_plan(False)[0]):
        assert request_check.refusal(request_check.check(step["op"], step["request"])) == ""
