"""What the target of a whole-instance restore holds now - read before it is replaced (0.39.0, 1.362).

A SQL Server restore lands database by database, and its guard asks about one name
(:func:`dbabrain.common.restorestep.sqlserver.existing_guard`). A PostgreSQL base backup and an
Oracle ``DUPLICATE`` have no such name to ask about: the backup *becomes* the target's data
directory, or rebuilds the target's whole database. Until 0.39.0 neither step read
``overwrite_existing`` at all - whatever the target held was replaced, and nothing asked. The
operator, 2026-11-13 (translated): *overwriting a DB must be set explicitly, off by default* - and,
asked of these two engines: *what does not overwriting mean for Oracle, PostgreSQL?*

It means this. Without ``overwrite_existing: true`` the step replaces a target only when the target
holds **no user data**:

* **PostgreSQL** - the data directory is absent or empty, or it is a cluster with no database of its
  own making and no relation of its own in the ones ``initdb`` made. Read from the directory, not
  from a running server: the target of a restore is often stopped or crash-looping from the run
  before, and the directory is the thing about to be replaced. A database or relation created after
  ``initdb`` has an OID of 16384 or more (``FirstNormalObjectId``), and a database is a folder under
  ``base/`` named by its OID - so the directory answers without a connection. A system catalog
  rewritten by ``VACUUM FULL`` also gets such a number: that reads as user data, which errs toward
  refusing.
* **Oracle** - the database is open and no schema that Oracle does not maintain owns a segment
  (``cdb_segments`` joined to ``cdb_users.oracle_maintained = 'N'``). An instance that is not open
  cannot say what it holds, and what cannot be read is not replaced.

**What cannot be read is refused**, never assumed empty: the restore says what it found, or that it
could not look, and that ``overwrite_existing: true`` is how a target meant to be replaced - a
repeated drill's own last restore - says so.

Values and parsing only; the two restore steps run the commands.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

__all__ = ["Holds", "FIRST_NORMAL_OBJECT_ID", "oracle_script", "postgresql_command", "read_oracle",
           "read_postgresql", "refusal"]

#: PostgreSQL's ``FirstNormalObjectId``: every object created after ``initdb`` has this OID or more.
FIRST_NORMAL_OBJECT_ID = 16384

_DONE = "DBABRAIN_HOLDS_DONE"


@dataclass(frozen=True)
class Holds:
    """``read``: the target answered. ``empty``: it holds no user data. ``what``: one sentence."""

    read: bool
    empty: bool
    what: str


def postgresql_command(data_dir: str) -> str:
    """POSIX shell that prints what ``data_dir`` holds, one fact a line, and a last line when it ran
    to its end - so a listing cut short is not read as an empty directory."""
    d = shlex.quote(data_dir.rstrip("/"))
    first = FIRST_NORMAL_OBJECT_ID
    return (
        f"d={d}; "
        'if [ ! -e "$d" ]; then echo STATE=absent; '
        'elif [ ! -r "$d" ] || [ ! -x "$d" ]; then echo STATE=unreadable; '
        'elif [ ! -e "$d/PG_VERSION" ]; then '
        'if [ -z "$(ls -A "$d" 2>/dev/null)" ]; then echo STATE=empty; else echo STATE=not_a_cluster; fi; '
        'else echo STATE=cluster; '
        'for p in "$d"/base/*; do n=${p##*/}; case "$n" in ""|*[!0-9]*) continue ;; esac; '
        f'if [ "$n" -ge {first} ]; then echo "DB=$n"; else '
        f"c=$(ls \"$p\" 2>/dev/null | awk '/^[0-9]+$/ && $1 >= {first}' | wc -l); "
        'echo "REL=$n:$c"; fi; done; '
        'for t in "$d"/pg_tblspc/*; do [ -e "$t" ] && echo "TS=${t##*/}"; done; '
        f"fi; echo {_DONE}")


def read_postgresql(text: str, data_dir: str) -> Holds:
    """:func:`postgresql_command`'s output as a verdict."""
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    state = next((line[6:] for line in lines if line.startswith("STATE=")), "")
    if _DONE not in lines or not state:
        return Holds(False, False, f"what {data_dir} holds could not be read (the listing did not finish)")
    if state in ("absent", "empty"):
        return Holds(True, True, f"{data_dir} is {state}")
    if state == "unreadable":
        return Holds(False, False, f"{data_dir} cannot be read by the user the restore runs as")
    if state == "not_a_cluster":
        return Holds(True, False, f"{data_dir} holds files that are not a PostgreSQL cluster (no PG_VERSION)")
    databases = [line[3:] for line in lines if line.startswith("DB=")]
    relations = 0
    for line in lines:
        if line.startswith("REL="):
            try:
                relations += int(line.rpartition(":")[2])
            except ValueError:
                return Holds(False, False, f"what {data_dir} holds could not be read ({line})")
    tablespaces = [line[3:] for line in lines if line.startswith("TS=")]
    found = []
    if databases:
        found.append(f"{len(databases)} database(s) of its own (OID {', '.join(databases[:5])}"
                     + (", ..." if len(databases) > 5 else "") + ")")
    if relations:
        found.append(f"{relations} relation(s) of its own in the databases initdb made")
    if tablespaces:
        found.append(f"{len(tablespaces)} tablespace(s)")
    if not found:
        return Holds(True, True, f"{data_dir} is a cluster with no database and no relation of its own")
    return Holds(True, False, f"the cluster in {data_dir} holds " + ", ".join(found))


def oracle_script() -> str:
    """SQL*Plus text that prints ``USER_SEGMENTS=<n>`` for an open database, and up to ten of the
    schemas that own them. Any error ends it without that line - which reads as *could not look*."""
    owned = ("from cdb_segments s where exists (select 1 from cdb_users u where u.username = s.owner "
             "and u.con_id = s.con_id and u.oracle_maintained = 'N')")
    return ("set heading off feedback off pagesize 0 linesize 300\n"
            "whenever sqlerror exit 1\n"
            "select 'OPEN_MODE=' || open_mode from v$database;\n"
            f"select 'USER_SEGMENTS=' || count(*) {owned};\n"
            f"select 'USER_SCHEMA=' || owner from (select distinct s.owner {owned} order by 1) where rownum <= 10;\n"
            f"select '{_DONE}' from dual;\n"
            "exit\n")


def read_oracle(text: str, sid: str) -> Holds:
    """:func:`oracle_script`'s output as a verdict."""
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    name = sid or "the target instance"
    count = next((line[len("USER_SEGMENTS="):] for line in lines if line.startswith("USER_SEGMENTS=")), "")
    if _DONE not in lines or not count.isdigit():
        reason = next((line for line in lines if line.startswith(("ORA-", "SP2-"))), "")
        return Holds(False, False, f"what {name} holds could not be read - it is not open, or did not answer"
                     + (f" ({reason[:160]})" if reason else ""))
    if int(count) == 0:
        return Holds(True, True, f"{name} is open and no schema of its own owns a segment")
    schemas = [line[len("USER_SCHEMA="):] for line in lines if line.startswith("USER_SCHEMA=")]
    return Holds(True, False, f"{name} holds {count} segment(s) owned by its own schema(s)"
                 + (f" ({', '.join(schemas)}{', ...' if len(schemas) >= 10 else ''})" if schemas else ""))


def refusal(engine: str, holds: Holds) -> str:
    """The sentence a restore stops with when its target may not be replaced."""
    what = {"postgresql": "a base backup becomes the target's whole data directory",
            "oracle": "a duplicate rebuilds the target's whole database"}.get(engine, "this restore replaces the whole target")
    lead = holds.what if holds.read else f"{holds.what}, and what cannot be read is not replaced"
    return (f"{lead}; {what}, and overwrite_existing is not true. Nothing was restored. State "
            "overwrite_existing: true on the restore entry when replacing this target is the intent "
            "(a drill run again over its own last restore), or give the restore a target that holds nothing.")
