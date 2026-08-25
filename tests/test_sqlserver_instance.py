"""What a SQL Server backup leaves behind, and why this module has to put it back.

Oracle and PostgreSQL are backed up physically: RMAN `DUPLICATE` rebuilds from the datafiles and
users live inside the database, `pg_basebackup` copies the cluster and roles live in `pg_authid`.
SQL Server is backed up per user database, and `master`/`msdb`/`model` are excluded on purpose, so
a restored instance has the data and none of the logins, roles, Agent jobs or linked servers.

These tests exercise the parts that decide correctness without a live instance: the SQL that gets
generated, the ordering that must not move, the version gates, and the secret handling. Whether
the generated SQL runs is a question for a real instance; whether it says the right thing is a
question that can and should be answered offline, because the failures it prevents are silent
ones — a login recreated by name and not by SID looks fine in every listing and still cannot
connect to a single restored database.
"""

from __future__ import annotations

import json

import pytest

from conftest import write_sqlserver_instance_policy

from db_ops.common import sqlserver_instance as si

@pytest.fixture(autouse=True)
def _instance_policy(estate):
    """Every test here reads the instance-portability policy; give it one of its own."""
    write_sqlserver_instance_policy(estate.data_dir)



# --------------------------------------------------------------------------- #
# Fakes: enough of a cursor to drive the exporters
# --------------------------------------------------------------------------- #

class _FakeCursor:
    """Returns canned rows per SQL fragment, so an exporter can be run with no database."""

    def __init__(self, answers: dict[str, list[dict]]):
        self._answers = answers
        self._rows: list[dict] = []
        self.description = ()
        self.executed: list[str] = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        for fragment, rows in self._answers.items():
            if fragment in sql:
                self._rows = rows
                self.description = tuple((key,) for key in (rows[0] if rows else {}))
                return self
        self._rows = []
        self.description = ()
        return self

    def fetchall(self):
        return [tuple(row.values()) for row in self._rows]

    def nextset(self):
        return False


def _policy():
    return si.load_policy()


_INFO = {"build": "16.0.4235.1", "edition": "Developer Edition (64-bit)", "engine_edition": "3",
         "collation": "SQL_Latin1_General_CP1_CI_AS", "machine_name": "SRC01",
         "instance_name": None, "windows_auth_only": "0", "version_text": "Microsoft SQL Server 2022"}


# --------------------------------------------------------------------------- #
# Ordering
# --------------------------------------------------------------------------- #

def test_logins_are_replayed_before_the_databases_and_agent_jobs_after():
    """The phase split is the whole reason replay is not one pass.

    Logins must exist before the user databases are restored or every restored database's users
    are orphaned. Agent job steps name databases, so they must come after those databases exist —
    a job replayed too early fails on its first schedule, quietly, at 02:00.
    """
    policy = _policy()
    pre = si.artifacts_in_order(policy, phase=si.PRE_DATABASE)
    post = si.artifacts_in_order(policy, phase=si.POST_DATABASE)

    assert "logins" in pre and "agent_jobs" not in pre
    assert "agent_jobs" in post and "logins" not in post


def test_dependencies_come_before_the_things_that_need_them():
    order = si.artifacts_in_order(_policy())
    position = {name: index for index, name in enumerate(order)}

    # Role membership and permissions name logins; proxies name credentials; jobs attach to
    # schedules and run as proxies; alerts notify operators.
    assert position["logins"] < position["server_roles"] < position["permissions"]
    assert position["credentials"] < position["logins"]
    assert position["credentials"] < position["proxies"] < position["agent_jobs"]
    assert position["agent_schedules"] < position["agent_jobs"]
    assert position["operators"] < position["alerts"]
    # sp_configure enables Agent XPs and Database Mail XPs, so nothing may precede it.
    assert position["sp_configure"] == 0


# --------------------------------------------------------------------------- #
# Logins — the SID is the point
# --------------------------------------------------------------------------- #

def test_a_sql_login_keeps_its_sid_and_password_hash():
    """Recreating a login by name produces one that exists and cannot log in anywhere.

    The restored user databases still carry the source's SIDs in sys.database_principals; only a
    login with the *same* SID resolves them. The hash keeps existing connection strings working.
    """
    cursor = _FakeCursor({"sys.sql_logins": [{
        "name": "app_user", "type_desc": "SQL_LOGIN", "sid": b"\x01\x02\xab",
        "is_disabled": 0, "default_database_name": "APPDB", "default_language_name": "us_english",
        "password_hash": b"\x02\x00\xde\xad", "is_policy_checked": 1, "is_expiration_checked": 0,
    }]})

    sql = si._export_logins(cursor, _policy(), _INFO, "SRC")

    assert "CREATE LOGIN [app_user]" in sql
    assert "SID = 0x0102AB" in sql
    assert "PASSWORD = 0x0200DEAD HASHED" in sql
    assert "CHECK_POLICY = ON" in sql and "CHECK_EXPIRATION = OFF" in sql


def test_a_windows_login_is_created_from_windows_without_a_sid():
    """A Windows SID belongs to the domain. Stating it would either fail or, worse, succeed
    against a stale SID and produce a login nobody can use."""
    cursor = _FakeCursor({"sys.sql_logins": [{
        "name": "CORP\\dba", "type_desc": "WINDOWS_LOGIN", "sid": b"\x01\x05\x00",
        "is_disabled": 0, "default_database_name": "master", "default_language_name": None,
        "password_hash": None, "is_policy_checked": None, "is_expiration_checked": None,
    }]})

    sql = si._export_logins(cursor, _policy(), _INFO, "SRC")

    assert "CREATE LOGIN [CORP\\dba] FROM WINDOWS;" in sql
    assert "SID =" not in sql


def test_a_login_with_no_hash_gets_a_random_password_not_a_known_one():
    """The login must exist so its SID resolves; it must not become loggable-into by anyone who
    reads the artifact.

    This used to assert ``PASSWORD = NEWID()`` literally, which pinned the intent to a spelling
    SQL Server rejects at parse time — ``PASSWORD`` takes a string literal, not an expression. The
    test passed for months because it only ever read the generated text; the statement had never
    been executed, because on the instance this was written against the password hash could always
    be read and this branch never ran. It is asserted by behaviour now: a random password, built
    where the parser will accept it.
    """
    cursor = _FakeCursor({"sys.sql_logins": [{
        "name": "svc", "type_desc": "SQL_LOGIN", "sid": b"\x09", "is_disabled": 0,
        "default_database_name": None, "default_language_name": None, "password_hash": None,
        "is_policy_checked": 0, "is_expiration_checked": 0,
    }]})

    sql = si._export_logins(cursor, _policy(), _INFO, "SRC")

    assert "PASSWORD = NEWID()" not in sql       # the spelling that cannot parse
    assert "NEWID()" in sql                      # still where the randomness comes from
    assert "DECLARE @pwd" in sql and "sp_executesql" in sql
    assert "SID = 0x09" in sql


def test_built_in_logins_are_not_exported():
    """`sa` and the ## certificate principals exist on every instance already; recreating them is
    at best a no-op and at worst an error that stops the file."""
    cursor = _FakeCursor({"sys.sql_logins": [
        {"name": "sa", "type_desc": "SQL_LOGIN", "sid": b"\x01", "is_disabled": 0,
         "default_database_name": "master", "default_language_name": None, "password_hash": b"\x02",
         "is_policy_checked": 1, "is_expiration_checked": 1},
        {"name": "##MS_PolicyProfile##", "type_desc": "SQL_LOGIN", "sid": b"\x02", "is_disabled": 0,
         "default_database_name": "master", "default_language_name": None, "password_hash": b"\x02",
         "is_policy_checked": 0, "is_expiration_checked": 0},
        {"name": "NT SERVICE\\SQLWriter", "type_desc": "WINDOWS_LOGIN", "sid": b"\x03",
         "is_disabled": 0, "default_database_name": "master", "default_language_name": None,
         "password_hash": None, "is_policy_checked": None, "is_expiration_checked": None},
    ]})

    sql = si._export_logins(cursor, _policy(), _INFO, "SRC")

    assert "CREATE LOGIN" not in sql


def test_hexlify_accepts_the_shapes_different_drivers_return():
    """pyodbc hands back bytes; other paths hand back hex text. Getting this wrong creates a
    login with the WRONG SID, which succeeds and orphans every restored database."""
    assert si._hexlify(b"\x0a\xff") == "0x0AFF"
    assert si._hexlify("0x0aff") == "0x0AFF"
    assert si._hexlify("0AFF") == "0x0AFF"
    assert si._hexlify(None) == ""


# --------------------------------------------------------------------------- #
# Host-specific settings must not be imposed on different hardware
# --------------------------------------------------------------------------- #

def test_memory_and_parallelism_are_exported_commented_out_with_the_source_value():
    """`max server memory` describes the machine. Replaying a 256 GB source's ceiling onto a
    32 GB target is worse than not replaying it — so it is visible, and inert."""
    cursor = _FakeCursor({"sys.configurations": [
        {"name": "max server memory (MB)", "value": 262144, "value_in_use": 262144,
         "is_dynamic": 1, "is_advanced": 1},
        {"name": "cost threshold for parallelism", "value": 50, "value_in_use": 50,
         "is_dynamic": 1, "is_advanced": 1},
    ]})

    sql, skipped = si._export_sp_configure(cursor, _policy(), _INFO, "SRC")

    assert "-- EXEC sp_configure 'max server memory (MB)', 262144;" in sql
    assert "SKIPPED: host-specific" in sql
    assert any("max server memory" in item for item in skipped)
    # The portable one is live.
    assert "\nEXEC sp_configure 'cost threshold for parallelism', 50;" in sql


def test_an_unclassified_setting_is_also_commented_out():
    """An option nobody has classified is an option nobody has decided about. Applying it by
    default is how a setting nobody chose ends up on a production rebuild."""
    cursor = _FakeCursor({"sys.configurations": [
        {"name": "some future option", "value": 7, "value_in_use": 7, "is_dynamic": 1,
         "is_advanced": 1},
    ]})

    sql, skipped = si._export_sp_configure(cursor, _policy(), _INFO, "SRC")

    assert "SKIPPED: unclassified" in sql
    assert any("some future option" in item for item in skipped)


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #

def test_a_credential_secret_becomes_a_placeholder_never_a_value():
    """SQL Server encrypts credential secrets with the service master key and offers no read
    path. The export names a reference; it never invents a value."""
    cursor = _FakeCursor({"sys.credentials": [
        {"name": "BackupToUrl", "credential_identity": "storageaccount"},
    ]})

    sql, refs = si._export_credentials(cursor, _policy(), _INFO, "MSSQL_2_115")

    assert "IDENTITY = 'storageaccount'" in sql
    assert "{{secret:MSSQL_2_115_CREDENTIAL_BACKUPTOURL}}" in sql
    assert refs == ["{{secret:MSSQL_2_115_CREDENTIAL_BACKUPTOURL}}"]


def test_replay_resolves_placeholders_and_reports_the_ones_it_cannot():
    text = "SECRET = '{{secret:A_CRED}}', PW = '{{secret:MISSING}}'"

    resolved, missing = si.resolve_secrets(text, {"A_CRED": "s3cr3t"})

    assert "s3cr3t" in resolved
    assert missing == ["MISSING"]
    assert "{{secret:MISSING}}" in resolved


def test_a_resolved_secret_containing_a_quote_cannot_break_out_of_its_literal():
    """The value goes into a SQL string literal. A single quote in a password would otherwise
    end the literal and turn the rest of the statement into syntax."""
    resolved, missing = si.resolve_secrets("SECRET = '{{secret:Q}}'", {"Q": "pa'ss"})

    assert resolved == "SECRET = 'pa''ss'"
    assert not missing


# --------------------------------------------------------------------------- #
# Identifier and literal quoting
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "name,expected",
    [("plain", "[plain]"), ("has space", "[has space]"), ("weird]name", "[weird]]name]")],
)
def test_identifiers_are_bracket_quoted(name, expected):
    assert si._quote_name(name) == expected


def test_literals_double_their_quotes():
    assert si._quote_string("O'Brien") == "'O''Brien'"
    assert si._quote_string(None) == "''"


# --------------------------------------------------------------------------- #
# Version gates
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("build,expected", [("16.0.4235.1", 16), ("11.0.7507.2", 11), ("", 0)])
def test_major_version_is_read_from_the_build_string(build, expected):
    assert si.major_version(build) == expected


def test_batches_split_on_go_the_way_sqlcmd_does():
    text = "CREATE LOGIN [a];\nGO\nCREATE LOGIN [b];\nGO\n"

    assert si._split_batches(text) == ["CREATE LOGIN [a];", "CREATE LOGIN [b];"]


def test_a_failing_batch_does_not_abort_the_rest_of_the_file():
    """A rebuild wants the 90% that applied plus a list of what did not, not a stop at the first
    linked server whose provider is missing."""
    class _Flaky(_FakeCursor):
        def execute(self, sql, params=None):
            if "BAD" in sql:
                raise RuntimeError("provider not registered")
            return super().execute(sql, params)

    cursor = _Flaky({})

    class _Conn:
        def commit(self):
            return None

    outcome = si._execute_artifact(cursor, _Conn(), "GOOD ONE;\nGO\nBAD TWO;\nGO\nGOOD THREE;")

    assert outcome["succeeded"] == 2
    assert outcome["failed"] == 1
    assert "provider not registered" in outcome["errors"][0]


# --------------------------------------------------------------------------- #
# Policy file
# --------------------------------------------------------------------------- #

def test_a_missing_policy_file_is_refused_rather_than_defaulted(tmp_path):
    """Every judgement about what is portable comes from that file. Falling back to a built-in
    list would give two different answers depending on whether anyone noticed it was gone."""
    with pytest.raises(si.SqlServerInstanceError, match="not found"):
        si.load_policy(tmp_path)


def test_every_artifact_in_the_policy_has_an_exporter():
    """A policy entry with no exporter would be silently absent from every bundle."""
    missing = sorted(set(si.artifacts_in_order(_policy())) - set(si._EXPORTERS))
    assert not missing, f"policy declares artifacts with no exporter: {missing}"


def test_every_exporter_is_declared_in_the_policy():
    """And an exporter with no policy entry never runs, which is the same bug facing the other
    way — the code looks complete and the bundle is short."""
    missing = sorted(set(si._EXPORTERS) - set(si.artifacts_in_order(_policy())))
    assert not missing, f"exporters not declared in the policy: {missing}"


def test_a_bundle_without_a_manifest_is_refused(tmp_path):
    with pytest.raises(si.SqlServerInstanceError, match="not an instance bundle"):
        si.read_bundle(tmp_path)


def test_read_bundle_returns_the_manifest(tmp_path):
    (tmp_path / si.MANIFEST_NAME).write_text(
        json.dumps({"schema_version": 1, "artifacts": ["logins"]}), encoding="utf-8"
    )

    root, manifest = si.read_bundle(tmp_path)

    assert root == tmp_path
    assert manifest["artifacts"] == ["logins"]


def test_the_public_role_is_not_recreated():
    """SQL Server reports is_fixed_role = 0 for `public`, so the "not fixed, therefore
    user-defined" test lets it through and the export asserts a CREATE for a role every instance
    already has. Found by running the export against a real instance."""
    cursor = _FakeCursor({"sys.server_role_members": [
        {"role_name": "public", "is_fixed_role": 0, "member_name": None, "owner_name": "sa"},
        {"role_name": "sysadmin", "is_fixed_role": 1, "member_name": "sa", "owner_name": "sa"},
        {"role_name": "app_readers", "is_fixed_role": 0, "member_name": "svc", "owner_name": "sa"},
    ]})

    sql = si._export_server_roles(cursor, _policy(), _INFO, "SRC")

    assert "CREATE SERVER ROLE [public]" not in sql
    assert "CREATE SERVER ROLE [app_readers]" in sql


def test_sa_is_never_added_to_a_server_role():
    """`ALTER SERVER ROLE ... ADD MEMBER [sa]` is rejected outright — sa is a *special* principal,
    not an ordinary login (error 15405). The logins exporter always skipped it; membership did
    not, and the first live replay lost every role statement in the file to that one line."""
    cursor = _FakeCursor({"sys.server_role_members": [
        {"role_name": "sysadmin", "is_fixed_role": 1, "member_name": "sa", "owner_name": "sa"},
        {"role_name": "sysadmin", "is_fixed_role": 1, "member_name": "##MS_Policy##", "owner_name": "sa"},
        {"role_name": "sysadmin", "is_fixed_role": 1, "member_name": "real_dba", "owner_name": "sa"},
    ]})

    sql = si._export_server_roles(cursor, _policy(), _INFO, "SRC")

    assert "ADD MEMBER [sa]" not in sql
    assert "ADD MEMBER [##MS_Policy##]" not in sql
    assert "ADD MEMBER [real_dba]" in sql


def test_every_statement_is_its_own_batch():
    """Without GO separators a file is one batch, so the executor's per-batch resilience becomes
    "one bad statement kills the file" — which is exactly what the first live trial did to all 22
    sp_configure statements."""
    cursor = _FakeCursor({"sys.configurations": [
        {"name": "clr enabled", "value": 0, "value_in_use": 0, "is_dynamic": 1, "is_advanced": 1},
        {"name": "nested triggers", "value": 1, "value_in_use": 1, "is_dynamic": 1, "is_advanced": 0},
    ]})

    sql, _ = si._export_sp_configure(cursor, _policy(), _INFO, "SRC")
    batches = si._split_batches(sql)

    # show advanced options, RECONFIGURE, the two portable settings, RECONFIGURE.
    assert len(batches) >= 5
    assert all("GO" not in batch.splitlines()[-1] for batch in batches)


def test_an_artifact_with_nothing_to_say_still_renders_a_valid_file():
    cursor = _FakeCursor({})

    sql = si._export_operators(cursor, _policy(), _INFO, "SRC")

    assert "nothing to replay for operators" in sql
    # It parses, and it executes nothing: a comment-only batch must not be counted as a
    # statement that ran, or an empty artifact reports "1 ok" for having done nothing.
    assert not any(si._is_executable(batch) for batch in si._split_batches(sql))


def test_an_option_this_edition_cannot_do_is_skipped_not_failed():
    """`xp_cmdshell` and `Ole Automation Procedures` exist on Windows and not on Linux, so a
    Windows-to-Linux replay meets them every time. Calling that a failure makes a correct replay
    look broken, and a FAIL line that is usually wrong is a FAIL line nobody reads."""
    class _Picky(_FakeCursor):
        def execute(self, sql, params=None):
            if "xp_cmdshell" in sql:
                raise RuntimeError("The specified option 'xp_cmdshell' is not supported by this "
                                   "edition of SQL Server and cannot be changed using sp_configure.")
            return super().execute(sql, params)

    class _Conn:
        def commit(self):
            return None

    outcome = si._execute_artifact(
        _Picky({}), _Conn(),
        "EXEC sp_configure 'clr enabled', 0;\nGO\nEXEC sp_configure 'xp_cmdshell', 0;\nGO\n",
    )

    assert outcome["succeeded"] == 1
    assert outcome["failed"] == 0
    assert len(outcome["unsupported"]) == 1
    assert outcome["status"] == "WARN"


def test_a_genuine_error_is_still_a_failure():
    class _Broken(_FakeCursor):
        def execute(self, sql, params=None):
            raise RuntimeError("Incorrect syntax near 'FROM'.")

    class _Conn:
        def commit(self):
            return None

    outcome = si._execute_artifact(_Broken({}), _Conn(), "SELECT 1;\nGO\n")

    assert outcome["status"] == "FAIL"
    assert outcome["failed"] == 1


def test_show_advanced_options_is_put_back_the_way_the_source_had_it():
    """The file turns the switch on so advanced options can be set. Leaving it on is a change the
    bundle never intended: the first live trial altered exactly one value on the target, and this
    was it."""
    cursor = _FakeCursor({"sys.configurations": [
        {"name": "show advanced options", "value": 0, "value_in_use": 0, "is_dynamic": 1,
         "is_advanced": 0},
        {"name": "clr enabled", "value": 0, "value_in_use": 0, "is_dynamic": 1, "is_advanced": 1},
    ]})

    sql, _ = si._export_sp_configure(cursor, _policy(), _INFO, "SRC")
    batches = [b for b in si._split_batches(sql) if si._is_executable(b)]

    assert "EXEC sp_configure 'show advanced options', 1;" in batches[0]
    assert batches[-2].strip() == "EXEC sp_configure 'show advanced options', 0;"
    assert batches[-1].strip() == "RECONFIGURE;"


def test_the_orphan_query_never_switches_database():
    """`USE db; SELECT ...` returns after the USE, whose result set does not exist — so every
    read raised, the caller recorded 0, and the gate announced "no orphaned users" for databases
    it had never managed to look at. A quiet clean result on the one number this capability
    exists to produce."""
    assert "USE " not in si._ORPHANS_SQL
    assert "{db}.sys.database_principals" in si._ORPHANS_SQL
    assert "USE " not in si._DB_CRYPTO_SQL
    assert "{db}.sys.symmetric_keys" in si._DB_CRYPTO_SQL


def test_a_database_that_cannot_be_read_is_unknown_not_zero():
    class _Partial(_FakeCursor):
        def execute(self, sql, params=None):
            if "sys.databases" in sql:
                return super().execute(sql, params)
            if "[locked]" in sql:
                raise RuntimeError("The database is not accessible.")
            self._rows = [{"orphan_count": 0}]
            self.description = (("orphan_count",),)
            return self

    cursor = _Partial({"sys.databases": [{"name": "ok_db"}, {"name": "locked"}]})

    found = {row["database_name"]: row for row in si._orphans(cursor)}

    assert found["ok_db"] == {"database_name": "ok_db", "orphan_count": 0, "readable": True}
    assert found["locked"]["orphan_count"] is None
    assert found["locked"]["readable"] is False
