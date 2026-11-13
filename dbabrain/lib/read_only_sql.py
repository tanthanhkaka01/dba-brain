"""One plain read: the check a statement passes before a read-only tool runs it (0.34.0, 1.260).

Moved down from ``telegram/ai_tools`` in 0.38.0 (1.312): the Telegram assistant and the MCP server
(``dbabrain mcp``) both run SQL a model wrote, and one rule for both is what keeps them saying the same
thing about the same statement. Pure: regular expressions over the text, no store, no server.

A guard, not the only one: the store query runs in a read-only transaction that is always rolled
back, and a monitored server's run is never committed.
"""

from __future__ import annotations

import re

from dbabrain.lib import errors

# --------------------------------------------------------------------------- #
# The read-only check
# --------------------------------------------------------------------------- #
#: Words that make a statement more than a read, on any of the engines dbabrain reaches. Matched on
#: the text with its strings and comments taken out, so a column called 'updated_at' or a literal
#: 'DELETE' in a WHERE clause is not a refusal. ``INTO`` covers SELECT ... INTO, which creates.
WRITE_WORDS = (
    "INSERT", "UPDATE", "DELETE", "MERGE", "UPSERT", "DROP", "ALTER", "CREATE", "TRUNCATE", "RENAME",
    "GRANT", "REVOKE", "DENY", "COPY", "CALL", "DO", "EXEC", "EXECUTE", "VACUUM", "ANALYZE", "REINDEX",
    "CLUSTER", "LOCK", "UNLOCK", "SET", "RESET", "COMMIT", "ROLLBACK", "BEGIN", "SAVEPOINT", "PREPARE",
    "DEALLOCATE", "LISTEN", "NOTIFY", "ATTACH", "DETACH", "PRAGMA", "REFRESH", "SECURITY", "LOAD", "IMPORT",
    "INTO", "KILL", "RECONFIGURE", "SHUTDOWN", "BACKUP", "RESTORE", "DBCC", "BULK", "OPENROWSET", "OPENQUERY",
    "OPENDATASOURCE", "WAITFOR", "USE", "DECLARE", "CHECKPOINT", "PURGE", "FLASHBACK", "AUDIT", "NOAUDIT",
    "COMMENT", "UNDROP", "DISCARD",
)
#: Functions that act though they are called from a SELECT.
WRITE_FUNCTIONS = re.compile(
    r"\b(pg_terminate_backend|pg_cancel_backend|pg_reload_conf|pg_rotate_logfile|pg_promote|set_config|"
    r"pg_sleep\w*|lo_\w+|dblink\w*|pg_read_file|pg_read_binary_file|pg_ls_\w+|pg_stat_file|pg_advisory\w*|"
    r"pg_switch_wal|pg_create_\w+|pg_drop_\w+|pg_replication_origin_\w+|pg_logical_emit_message|nextval|setval|"
    r"txid_current|pg_current_xact_id|xp_\w+|sp_\w+|dbms_\w+|utl_\w+|"
    # A statement handed over as text runs unread by this check (PostgreSQL), and MySQL's own that
    # wait, lock or read a server file - all found missing on the public demo, 2026-11-12 (1.344).
    r"query_to_xml\w*|cursor_to_xml\w*|ts_stat|sleep|benchmark|load_file|get_lock|release_lock|"
    r"release_all_locks|master_pos_wait|source_pos_wait)\s*\(", re.IGNORECASE)
#: Oracle packages, and its URI types, whose functions act - a request over the network, a wait, a
#: job, Java - or run a statement this check never reads, though called from a SELECT. Matched with
#: their member (`utl_http.request(`): the `dbms_\w+(` above never matched a real call, which always
#: has one. **Named, not all of `dbms_*`:** dbms_metadata, dbms_lob, dbms_xplan, dbms_rowid, utl_raw
#: and their like only read, and a DBA's ordinary SELECT is full of them.
ACTING_PACKAGES = re.compile(
    r"\b(utl_http|utl_tcp|utl_smtp|utl_mail|utl_file|utl_inaddr|dbms_scheduler|dbms_job|dbms_java|dbms_pipe|"
    r"dbms_lock|dbms_alert|dbms_ldap|dbms_network_acl_admin|dbms_xmlquery|dbms_xmlgen|dbms_xmlsave|dbms_sql|"
    r"dbms_aq|dbms_aqadm|dbms_ddl|dbms_cloud|dbms_backup_restore|dbms_system|dbms_datapump|"
    r"dbms_export_extension|dbms_aw|dbms_xdb|dbms_fga|dbms_rls|dbms_output|dbms_flashback)\s*\.\s*\w+\s*\(|"
    r"\b(httpuritype|xdburitype|dburitype)\s*\(", re.IGNORECASE)
_WRITE_WORDS = re.compile(r"\b(" + "|".join(WRITE_WORDS) + r")\b", re.IGNORECASE)
_STRINGS = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|\$\$.*?\$\$", re.DOTALL)
#: Literals only - a double-quoted name is an IDENTIFIER, and "web_users" is still that table.
_LITERALS = re.compile(r"'(?:[^']|'')*'|\$\$.*?\$\$", re.DOTALL)
_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
#: A comment some engine RUNS: MySQL's /*! ... */ executes its text, so taking comments out would
#: hide whatever it holds from every check below.
_EXECUTED_COMMENT = re.compile(r"/\*\s*!")


class ReadOnlyRefusal(errors.RequestError):
    """A statement that is not one plain read."""


def read_only_sql(sql: str, *, blocked_tables: list[str] | tuple[str, ...] = ()) -> str:
    """The statement, trimmed, when it is one SELECT (or WITH ... SELECT) and touches no blocked
    table - else :class:`ReadOnlyRefusal` naming why. A guard, not the only one: the store query
    runs in a read-only transaction, and a monitored server's run is never committed."""
    text = str(sql or "").strip().rstrip(";").strip()
    if not text:
        raise ReadOnlyRefusal("the SQL is empty.")
    if _EXECUTED_COMMENT.search(_LITERALS.sub("''", text)):
        raise ReadOnlyRefusal("a /*! ... */ comment is executed by MySQL - write the statement without it.")
    bare = _COMMENTS.sub(" ", _STRINGS.sub("''", text))
    names = _COMMENTS.sub(" ", _LITERALS.sub("''", text))
    if ";" in bare:
        raise ReadOnlyRefusal("one statement only - no ';' inside it.")
    first = bare.strip().split(None, 1)[0].upper() if bare.strip() else ""
    if first not in ("SELECT", "WITH", "(", "VALUES") and not first.startswith("("):
        raise ReadOnlyRefusal(f"only a SELECT (or WITH ... SELECT) is run; this starts with {first or 'nothing'}.")
    word = _WRITE_WORDS.search(bare)
    if word:
        raise ReadOnlyRefusal(f"{word.group(1).upper()} is not allowed - the tools only read.")
    # A call is read with a quoted name's quotes taken off: "pg_sleep"(600) and `sleep`(600) are the
    # same calls, and `bare` - which drops a double-quoted name whole, so that a column called
    # "update" is no refusal - hid them from this check (found on the public demo, 2026-11-12).
    called = names.replace('"', "").replace("`", "")
    function = WRITE_FUNCTIONS.search(called)
    if function:
        raise ReadOnlyRefusal(f"{function.group(1)}() is not allowed - it acts, it does not read.")
    package = ACTING_PACKAGES.search(called)
    if package:
        raise ReadOnlyRefusal(f"{(package.group(1) or package.group(2)).lower()} is not allowed - it acts, or runs "
                              "a statement this check cannot read.")
    for table in blocked_tables:
        if re.search(rf"\b{re.escape(table)}\b", names, re.IGNORECASE):
            raise ReadOnlyRefusal(f"table {table} is not readable by the assistant.")
    return text
