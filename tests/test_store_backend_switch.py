"""Pointing a node at its own store must be a command, not a sentence in a procedure.

`store_config.json` travels inside a config bundle. So `import-data` faithfully points a machine
that has never run at the store the bundle came from — in practice the shared production one — and
every procedure that stands up a node ended with "now edit store_config.json and change backend to
sqlite". That is a hand-edit on the one path where forgetting it writes an unproven build's rows
into the record every other node shares, and on 2026-09-09 it was the only hand-edit left in the
whole estate-move procedure.

What these hold down is what makes the command safe to use rather than merely present: the section
not being switched to survives, and a section that cannot stand on its own is refused *here*
rather than at connect time, inside whichever app command touches the store first.
"""

from __future__ import annotations

import pytest

from db_ops.db import declaration


def full() -> dict:
    return {
        "notes": "the long explanation this file carries",
        "backend": "postgresql",
        "sqlite": {"path": "runtime/db_ops.sqlite"},
        "postgresql": {"host": "192.0.2.115", "port": 5433, "database": "db_ops",
                       "schema": "db_ops", "username": "postgres", "password_ref": "PG_REF"},
    }


def test_switching_to_sqlite_selects_it() -> None:
    assert declaration.switch_backend(full(), "sqlite")["backend"] == "sqlite"


def test_switching_keeps_the_way_back() -> None:
    """The usual reason to switch is to prove a node on its own file *before* pointing it at the
    shared store. A switch that erased the postgresql block would make the return trip a retyping
    exercise — and retyping a connection is how a node ends up on the wrong one."""
    switched = declaration.switch_backend(full(), "sqlite")

    assert switched["postgresql"] == full()["postgresql"]
    assert switched["notes"] == full()["notes"], "the file's own documentation survives too"


def test_a_node_can_be_pointed_at_another_schema_on_the_same_server() -> None:
    """Its own schema is how a node shares a server without sharing a store — which is what R8
    ("the soak node writes to its own store, never the shared one") needs on an estate whose only
    PostgreSQL server is the shared one."""
    moved = declaration.switch_backend(full(), "postgresql", postgres={"schema": "dba_brain"})

    assert moved["postgresql"]["schema"] == "dba_brain"
    assert moved["postgresql"]["database"] == "db_ops", "only what was named moves"


def test_the_connection_string_is_rebuilt_when_the_target_moves() -> None:
    """The trap, and the reason this is not a dict update.

    `connection_string` is authoritative when non-empty — `config.py` returns it verbatim and never
    looks at the fields beside it. So setting `schema` on a declaration that carries one would
    change the human-readable breakdown and *nothing about where the node writes*: `store-info`
    would report the new schema and the node would go on writing to the old one. Two nodes
    silently sharing a store while both believe they are separate is the worst outcome this file
    can produce.
    """
    raw = full()
    raw["postgresql"]["connection_string"] = (
        "postgresql://postgres:{password}@192.0.2.115:5433/db_ops"
        "?sslmode=prefer&options=-csearch_path%3Ddb_ops")

    moved = declaration.switch_backend(raw, "postgresql", postgres={"schema": "dba_brain"})

    assert "search_path%3Ddba_brain" in moved["postgresql"]["connection_string"]
    assert "search_path%3Ddb_ops" not in moved["postgresql"]["connection_string"]


def test_a_declaration_with_no_connection_string_does_not_grow_one() -> None:
    """The sibling fields are authoritative when it is blank, so building one here would move the
    file from one shape to the other as a side effect of changing a schema."""
    moved = declaration.switch_backend(full(), "postgresql", postgres={"schema": "dba_brain"})

    assert "connection_string" not in moved["postgresql"]


def test_every_declared_target_field_can_actually_be_set() -> None:
    """POSTGRES_TARGET_FIELDS is what the CLI builds its flags from, so an entry it cannot apply
    is a flag that is accepted and ignored — the failure `instance-add`'s --key-base64 had."""
    overrides = {"host": "192.0.2.200", "port": 5555, "database": "other", "schema": "elsewhere",
                 "username": "someone", "password_ref": "OTHER_REF"}
    assert set(overrides) == set(declaration.POSTGRES_TARGET_FIELDS)

    moved = declaration.switch_backend(full(), "postgresql", postgres=overrides)

    for name, value in overrides.items():
        assert moved["postgresql"][name] == value


def test_re_pointing_at_what_is_already_there_changes_nothing() -> None:
    raw = full()

    assert declaration.switch_backend(raw, "postgresql", postgres={"schema": "db_ops"}) == raw


def test_moving_to_a_target_this_build_cannot_read_is_refused() -> None:
    """Blanking the host through the same door it can be set through must fail here, not at
    connect time inside whichever app command touches the store first."""
    with pytest.raises(declaration.StoreDeclarationError, match="host"):
        declaration.switch_backend(full(), "postgresql", postgres={"host": ""})


def test_switching_back_to_postgresql_works_from_sqlite() -> None:
    once = declaration.switch_backend(full(), "sqlite")
    twice = declaration.switch_backend(once, "postgresql")

    assert twice["backend"] == "postgresql"
    assert twice["sqlite"]["path"] == "runtime/db_ops.sqlite"


def test_postgres_is_accepted_as_a_spelling_of_postgresql() -> None:
    assert declaration.switch_backend(full(), "postgres")["backend"] == "postgresql"


def test_a_sqlite_path_may_be_given_and_otherwise_is_kept() -> None:
    assert declaration.switch_backend(full(), "sqlite")["sqlite"]["path"] == "runtime/db_ops.sqlite"
    assert declaration.switch_backend(
        full(), "sqlite", sqlite_path="runtime/soak.sqlite")["sqlite"]["path"] == "runtime/soak.sqlite"


# --------------------------------------------------------------------------- #
# Refusals — each one prevents a failure that would otherwise surface at connect time
# --------------------------------------------------------------------------- #
def test_switching_to_a_postgres_section_with_no_host_is_refused() -> None:
    raw = {"backend": "sqlite", "sqlite": {"path": "x.sqlite"}, "postgresql": {"database": "db"}}

    with pytest.raises(declaration.StoreDeclarationError, match="cannot be read"):
        declaration.switch_backend(raw, "postgresql")


def test_switching_to_a_sqlite_section_with_no_path_is_refused() -> None:
    raw = {"backend": "postgresql", "postgresql": {"host": "h", "database": "d"}}

    with pytest.raises(declaration.StoreDeclarationError, match="names no path"):
        declaration.switch_backend(raw, "sqlite")


def test_a_backend_this_build_cannot_read_is_refused() -> None:
    with pytest.raises(declaration.StoreDeclarationError, match="must be one of"):
        declaration.switch_backend(full(), "mysql")


def test_what_is_returned_is_something_this_build_can_parse() -> None:
    """The declaration is parsed before it is handed back, so a file that cannot be read is never
    written. A store that only fails when something first touches it is the failure mode this
    whole command exists to remove."""
    switched = declaration.switch_backend(full(), "sqlite")

    target = declaration.parse(switched)
    assert "db_ops.sqlite" in str(target.sqlite_path)
