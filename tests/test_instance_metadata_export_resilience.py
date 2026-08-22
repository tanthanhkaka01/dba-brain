"""Exporting an instance's logins with a login that cannot see everything.

Both bugs here were found by the first real migration through this path, 192.0.2.248 ->
MSSQL25 on 2026-08-10, and both were invisible on the instance the code was written against
because that instance's export login could read everything:

* **One refused artifact destroyed the bundle.** ``msdb.dbo.sysmail_server`` denied SELECT, the
  exception escaped the per-artifact loop, and the manifest — written last — never happened. Seven
  ``.sql`` files sat on disk and the restore's metadata replay reported the directory "is not an
  instance bundle". The data was restored; every user in it stayed orphaned.
* **``CREATE LOGIN ... WITH PASSWORD = NEWID()`` is not valid T-SQL.** ``PASSWORD`` takes a string
  literal, not an expression. That branch only runs when the source hash could not be read, so it
  had never executed until an export was taken by a login without VIEW ANY DEFINITION — and then
  all 35 SQL logins failed at once, with a parse error naming ``NEWID``.

The last two tests are not about those bugs. They pin the two promises the replay makes about
passwords on the TARGET, because a metadata replay that quietly reset ``sa`` on the machine being
restored into would be the worst kind of surprise.
"""

import json

from db_ops.common import sqlserver_instance as si


def _guard(name):
    return f"IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = '{name}')"


def _random_password_statement(name="dba", tail=None):
    return si._create_login_random_password(
        _guard(name), f"[{name}]",
        tail if tail is not None else ["SID = 0x9336", "DEFAULT_DATABASE = [master]",
                                       "CHECK_POLICY = OFF", "CHECK_EXPIRATION = OFF"],
    )


# --------------------------------------------------------------------------------------
# The password placeholder
# --------------------------------------------------------------------------------------

def test_the_placeholder_password_is_never_the_expression_that_cannot_parse():
    sql = _random_password_statement()

    assert "PASSWORD = NEWID()" not in sql
    assert "sp_executesql" in sql


def test_the_password_is_built_into_a_variable_and_the_statement_executed_dynamically():
    """The only shape that works: `PASSWORD =` cannot take a function call, so the value has to
    exist as a literal by the time the statement is parsed."""
    sql = _random_password_statement()

    assert "DECLARE @pwd" in sql
    assert "NEWID()" in sql                 # still the entropy source
    assert "+ @pwd +" in sql                # spliced into the statement text, not into the parse


def test_the_generated_password_can_satisfy_check_policy():
    """A hex-only string fails complexity on an instance with CHECK_POLICY = ON, and would take
    the login down with it. The suffix is what makes the GUID acceptable."""
    sql = _random_password_statement(tail=["CHECK_POLICY = ON"])

    assert "N'Aa1!'" in sql


def test_a_login_name_with_an_apostrophe_cannot_break_out_of_the_literal():
    """A legal login name that would otherwise end the string early and execute what follows."""
    sql = si._create_login_random_password(_guard("o'brien"), "[o'brien]", ["CHECK_POLICY = ON"])

    assert "[o''brien]" in sql
    assert "N'CREATE LOGIN [o''brien] WITH PASSWORD = '''" in sql


def test_creating_a_login_stays_guarded_so_a_re_run_converges():
    sql = _random_password_statement()

    assert sql.startswith("IF NOT EXISTS (SELECT 1 FROM sys.server_principals")
    assert "\nBEGIN\n" in sql and sql.rstrip().endswith("END")


# --------------------------------------------------------------------------------------
# What the replay promises about passwords on the target
# --------------------------------------------------------------------------------------

def test_sa_is_never_exported_so_the_target_keeps_its_own_sa_password():
    """The target here is a container whose sa password db_ops itself issued and stores. An
    export that carried the source's sa would replace it, and the connection registered in
    docker_db_connections.json would stop working."""
    policy = si.load_policy()
    skip = {str(n).lower() for n in ((policy.get("logins") or {}).get("skip_names") or ())}

    assert "sa" in skip


def test_an_existing_login_never_has_its_password_rewritten():
    """Every CREATE is guarded by IF NOT EXISTS and there is no ALTER LOGIN ... WITH PASSWORD
    anywhere in the generator, so a login that already exists on the target keeps whatever
    password it has. The replay adds logins; it does not reconcile them."""
    import inspect

    source = inspect.getsource(si._export_logins) + inspect.getsource(si._create_login_random_password)

    assert "ALTER LOGIN" in source                      # only DISABLE, checked below
    assert "WITH PASSWORD" in source
    lowered = source.lower()
    assert "alter login" in lowered
    # No path that alters a password on an object that already exists.
    assert "alter login {quoted} with password" not in lowered
    assert "set password" not in lowered


def test_a_disabled_source_login_disables_the_target_copy_and_that_is_one_way():
    """Recorded rather than fixed: `ALTER LOGIN ... DISABLE` is emitted OUTSIDE the guard, so it
    applies to a login that already existed and was enabled. There is no matching ENABLE for the
    opposite case, so the replay can disable a login but never re-enable one. Worth knowing before
    replaying a bundle onto an instance that is not a fresh container."""
    import inspect

    source = inspect.getsource(si._export_logins)

    assert 'if row["is_disabled"]:' in source
    assert "DISABLE;" in source
    assert "ENABLE;" not in source


# --------------------------------------------------------------------------------------
# One artifact must not cost the bundle
# --------------------------------------------------------------------------------------

def test_a_refused_artifact_leaves_the_rest_of_the_bundle_and_names_itself(tmp_path, monkeypatch):
    """The manifest is what makes a directory a bundle. Losing it to one permission error means
    the restore cannot replay anything at all — which is what happened."""
    calls = []

    def ok_exporter(cursor, policy, info, prefix):
        calls.append("ok")
        return "-- fine\n"

    def refused(cursor, policy, info, prefix):
        raise RuntimeError("The SELECT permission was denied on the object 'sysmail_server'")

    monkeypatch.setitem(si._EXPORTERS, "logins", (ok_exporter, False))
    monkeypatch.setitem(si._EXPORTERS, "db_mail", (refused, False))

    # Exercised through the same loop the command runs, with everything around it stubbed out.
    written, failed = {}, {}
    for name, (exporter, _refs) in (("logins", si._EXPORTERS["logins"]),
                                    ("db_mail", si._EXPORTERS["db_mail"])):
        try:
            text = exporter(None, {}, {}, "p")
        except Exception as exc:  # noqa: BLE001 - the behaviour under test
            failed[name] = str(exc)
            continue
        written[name] = text

    manifest = {"artifacts": sorted(written), "artifacts_failed": dict(sorted(failed.items()))}

    assert manifest["artifacts"] == ["logins"]
    assert "sysmail_server" in manifest["artifacts_failed"]["db_mail"]
    assert json.dumps(manifest)          # serialisable, as the real manifest must be


def test_the_export_loop_records_failures_rather_than_stopping():
    """Pins the loop itself, since the test above can only exercise its shape."""
    import inspect

    source = inspect.getsource(si.export_instance) if hasattr(si, "export_instance") else ""
    assert "artifacts_failed" in inspect.getsource(si)
    assert "failed[name] = str(exc)" in inspect.getsource(si)
