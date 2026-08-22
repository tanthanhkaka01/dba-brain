"""Why password rotation is one operation instead of two.

A password lives in two places — the server and the secret store — and a rotation that updates one
without the other is worse than no rotation at all: db_ops keeps authenticating with a value the
server has forgotten, and the failure surfaces hours later as a metric that stopped collecting. The
tests below pin the ordering that keeps the two together, and the refusals that stop a half-applied
change from being recorded as done.
"""

import json

import pytest

from db_ops.common import password_rotation as rot


# ---------------------------------------------------------------------------
# Generated passwords have to survive the places they get pasted into
# ---------------------------------------------------------------------------
def test_a_generated_password_avoids_the_characters_that_break_connection_strings():
    """A quote or semicolon in a generated password truncates an ODBC connection string or ends a
    SQL literal early - weeks later, on one unlucky draw, on one host."""
    for _ in range(200):
        password = rot.generate_password()
        assert not set(password) & set("'\";\\{}[]`")


def test_a_generated_password_satisfies_four_class_complexity():
    for _ in range(200):
        password = rot.generate_password()
        assert any(c.islower() for c in password)
        assert any(c.isupper() for c in password)
        assert any(c.isdigit() for c in password)
        assert any(c in rot.PASSWORD_SYMBOLS for c in password)


def test_a_generated_password_never_starts_with_a_digit():
    # Some ODBC parsers mis-handle an unquoted leading digit.
    assert all(not rot.generate_password()[0].isdigit() for _ in range(200))


def test_two_generated_passwords_are_never_the_same():
    assert len({rot.generate_password() for _ in range(500)}) == 500


def test_a_password_shorter_than_the_floor_is_refused():
    with pytest.raises(rot.PasswordRotationError, match="at least"):
        rot.generate_password(8)


# ---------------------------------------------------------------------------
# The change statement is DDL, and DDL takes literals
# ---------------------------------------------------------------------------
def test_sqlserver_gets_the_self_service_form_with_the_old_password():
    """ALTER LOGIN ... OLD_PASSWORD is what lets a login change its own password without being
    granted ALTER ANY LOGIN - the grant a rotation account should not need."""
    statement = rot.build_change_statement("sqlserver", "dba_user", "NewPass1!", "OldPass1!")

    assert statement.startswith("ALTER LOGIN [dba_user]")
    assert "OLD_PASSWORD = N'OldPass1!'" in statement
    assert "?" not in statement          # DDL rejects parameter markers
    assert "@P1" not in statement


def test_oracle_uses_replace_and_postgres_does_not_need_the_old_password():
    oracle = rot.build_change_statement("oracle", "system", "NewPass1!", "OldPass1!")
    postgres = rot.build_change_statement("postgresql", "postgres", "NewPass1!", "OldPass1!")

    assert "REPLACE" in oracle
    assert "ALTER USER" in postgres and "OldPass1!" not in postgres


def test_an_embedded_quote_is_doubled_rather_than_ending_the_literal():
    """The generated alphabet has no quote, but an operator-supplied password may; the quoting has
    to hold anyway, or a chosen password becomes a syntax error at best."""
    statement = rot.build_change_statement("sqlserver", "dba", "a'b", "c'd")

    assert "N'a''b'" in statement and "N'c''d'" in statement


def test_an_identifier_quote_cannot_escape_the_identifier():
    statement = rot.build_change_statement("sqlserver", "we]ird", "NewPass1!", "OldPass1!")

    assert "[we]]ird]" in statement


def test_an_unsupported_engine_is_refused_rather_than_guessed_at():
    with pytest.raises(rot.PasswordRotationError, match="not implemented"):
        rot.build_change_statement("mongodb", "u", "n", "o")


# ---------------------------------------------------------------------------
# Selecting what to rotate never touches a value
# ---------------------------------------------------------------------------
def test_match_selects_on_ref_names_so_no_secret_is_decrypted_to_choose(monkeypatch):
    monkeypatch.setattr(rot.data_sources, "load_secret_text",
                        lambda *a, **k: {"MSSQL_1_1_1_1_DBA_USER": "x",
                                         "MSSQL_1_1_1_2_DBA_USER": "y",
                                         "REMOTE_1_1_1_3_ADMIN": "z"})

    assert rot.select_refs(match="DBA_USER") == [
        "MSSQL_1_1_1_1_DBA_USER", "MSSQL_1_1_1_2_DBA_USER"]


def test_an_unknown_ref_is_named_rather_than_silently_skipped(monkeypatch):
    monkeypatch.setattr(rot.data_sources, "load_secret_text", lambda *a, **k: {"KNOWN": "x"})

    with pytest.raises(rot.PasswordRotationError, match="MISSING"):
        rot.select_refs(refs=["KNOWN", "MISSING"])


def test_selecting_nothing_is_an_error_not_a_silent_no_op(monkeypatch):
    monkeypatch.setattr(rot.data_sources, "load_secret_text", lambda *a, **k: {"A": "x"})

    with pytest.raises(rot.PasswordRotationError, match="No password_ref selected"):
        rot.select_refs(match="nothing-matches-this")


# ---------------------------------------------------------------------------
# The ordering that keeps the server and the store together
# ---------------------------------------------------------------------------
class _Cursor:
    def __init__(self, log, fail_on_execute=False):
        self._log = log
        self._fail = fail_on_execute

    def execute(self, statement, *args):
        self._log.append(statement)
        if self._fail:
            raise RuntimeError("rejected by the server")


class _Connection:
    def __init__(self, log, fail_on_execute=False):
        self._log = log
        self._fail = fail_on_execute
        self.closed = False

    def cursor(self):
        return _Cursor(self._log, self._fail)

    def commit(self):
        self._log.append("COMMIT")

    def close(self):
        self.closed = True


def _target(**over):
    base = {"server_id": "S1", "db_type": "sqlserver", "ip": "10.0.0.1", "port": 1433,
            "username": "dba", "password": "OldPass1!", "database_name": "master",
            "service_name": "", "sqlserver_driver": "", "credential_name": "c"}
    base.update(over)
    return base


def _patch(monkeypatch, *, connect):
    monkeypatch.setattr(rot, "resolve_ref_target", lambda ref, **k: _target(password_ref=ref))
    monkeypatch.setattr(rot.sql_run, "connect_target", connect)


def test_the_new_password_is_verified_on_a_second_connection_not_the_one_that_changed_it(monkeypatch):
    """The session that issued the change stays authenticated, so re-running a query on it proves
    nothing about the new password. Only a fresh connect does."""
    log, opened = [], []

    def connect(target, **kwargs):
        opened.append(target["password"])
        return _Connection(log)

    _patch(monkeypatch, connect=connect)
    result = rot.rotate_ref("REF")

    assert result["status"] == "SUCCESS"
    # opened twice: once with the old password to change it, once with the new one to prove it
    assert len(opened) == 2
    assert opened[0] == "OldPass1!"
    assert opened[1] == result["_new_password"] != "OldPass1!"


def test_a_failed_verify_rolls_the_password_back_and_reports_failed(monkeypatch):
    """This is the one window where a host could end up with a password nothing recorded. The
    rollback uses the value this process still holds, which is why it has to happen inline."""
    log = []
    calls = {"n": 0}

    def connect(target, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:          # the verify attempt
            raise RuntimeError("login failed")
        return _Connection(log)

    _patch(monkeypatch, connect=connect)
    result = rot.rotate_ref("REF")

    assert result["status"] == "FAILED"
    assert "_new_password" not in result          # nothing is offered for storage
    assert result["rollback"] == "rolled back to the previous password"
    assert "OLD_PASSWORD" in log[-1]              # the rollback statement ran


def test_a_host_whose_current_password_already_fails_is_skipped_not_guessed_at(monkeypatch):
    def connect(target, **kwargs):
        raise RuntimeError("login failed for user")

    _patch(monkeypatch, connect=connect)
    result = rot.rotate_ref("REF")

    assert result["status"] == "SKIPPED"
    assert "not usable" in result["detail"]


def test_a_rejected_change_statement_leaves_the_store_untouched(monkeypatch):
    log = []
    _patch(monkeypatch, connect=lambda target, **k: _Connection(log, fail_on_execute=True))

    result = rot.rotate_ref("REF")

    assert result["status"] == "FAILED"
    assert "_new_password" not in result


def test_dry_run_proves_reachability_without_changing_anything(monkeypatch):
    log = []
    _patch(monkeypatch, connect=lambda target, **k: _Connection(log))

    result = rot.rotate_ref("REF", dry_run=True)

    assert result["status"] == "READY"
    assert log == []                              # no statement was ever issued


def test_an_operator_supplied_password_is_used_instead_of_a_generated_one(monkeypatch):
    log = []
    _patch(monkeypatch, connect=lambda target, **k: _Connection(log))

    result = rot.rotate_ref("REF", new_password="ChosenByPolicy1!")

    assert result["_new_password"] == "ChosenByPolicy1!"


# ---------------------------------------------------------------------------
# Nothing leaks, and each target gets its own value
# ---------------------------------------------------------------------------
def test_every_target_gets_a_different_password(monkeypatch):
    """One shared new password rebuilds the weakness a rotation is run to remove: a leak on any
    host becomes a leak on all of them."""
    log = []
    monkeypatch.setattr(rot.data_sources, "load_secret_text",
                        lambda *a, **k: {"A_DBA": "x", "B_DBA": "y", "C_DBA": "z"})
    monkeypatch.setattr(rot, "resolve_ref_target", lambda ref, **k: _target(password_ref=ref))
    monkeypatch.setattr(rot.sql_run, "connect_target", lambda t, **k: _Connection(log))

    outcome = rot.rotate({"match": "_DBA"})

    values = [item["_new_password"] for item in outcome["results"]]
    assert len(set(values)) == 3
    assert outcome["summary"]["SUCCESS"] == 3


def test_strip_secrets_removes_the_password_before_anything_prints_it(monkeypatch):
    log = []
    monkeypatch.setattr(rot.data_sources, "load_secret_text", lambda *a, **k: {"A_DBA": "x"})
    monkeypatch.setattr(rot, "resolve_ref_target", lambda ref, **k: _target(password_ref=ref))
    monkeypatch.setattr(rot.sql_run, "connect_target", lambda t, **k: _Connection(log))

    outcome = rot.strip_secrets(rot.rotate({"match": "_DBA"}))

    assert "_new_password" not in json.dumps(outcome)


def test_a_ref_no_instance_uses_is_skipped_with_a_reason(monkeypatch):
    monkeypatch.setattr(rot.data_sources, "load_all_credentials", lambda *a, **k: {})
    monkeypatch.setattr(rot.data_sources, "load_db_instances", lambda *a, **k: [])

    result = rot.rotate_ref("ORPHAN_REF")

    assert result["status"] == "SKIPPED"
    assert "not referenced by any credential" in result["detail"]


# ---------------------------------------------------------------------------
# A key name is a label, not configuration
# ---------------------------------------------------------------------------
def test_a_ref_with_no_db_instance_is_refused_until_the_operator_opts_in(monkeypatch):
    """The standard name carries the IP, which makes guessing tempting. It stays a guess, so the
    refusal names the two ways to proceed instead of quietly picking one."""
    monkeypatch.setattr(rot.data_sources, "load_all_credentials", lambda *a, **k: {})
    monkeypatch.setattr(rot.data_sources, "load_db_instances", lambda *a, **k: [])

    result = rot.rotate_ref("MSSQL_10_1_2_3_DBA_USER")

    assert result["status"] == "SKIPPED"
    assert "allow_name_host" in result["detail"]


def test_allow_name_host_takes_the_target_from_the_standard_key_name(monkeypatch):
    log = []
    monkeypatch.setattr(rot.data_sources, "load_all_credentials", lambda *a, **k: {})
    monkeypatch.setattr(rot.data_sources, "load_db_instances", lambda *a, **k: [])
    monkeypatch.setattr(rot.data_sources, "load_secret_text",
                        lambda *a, **k: {"MSSQL_10_1_2_3_DBA_USER": "OldPass1!"})
    monkeypatch.setattr(rot.sql_run, "connect_target", lambda t, **k: _Connection(log))

    result = rot.rotate_ref("MSSQL_10_1_2_3_DBA_USER", allow_name_host=True)

    assert result["status"] == "SUCCESS"
    assert result["host"] == "10.1.2.3"
    assert result["username"] == "dba_user"


def test_a_name_that_is_not_the_standard_scheme_yields_no_target():
    assert rot.target_from_ref_name("TOKEN_TELEGRAM_IT_DEV_CODE_SP_BOT") is None
    assert rot.target_from_ref_name("GRAFANA_192_0_2_104_ADMIN") is None
    assert rot.target_from_ref_name("MSSQL_10_1_2_3_DBA")["db_type"] == "sqlserver"


# ---------------------------------------------------------------------------
# Both stores move together or the next deploy undoes the rotation
# ---------------------------------------------------------------------------
def test_persist_writes_the_plaintext_source_as_well_as_the_encrypted_blob(tmp_path, monkeypatch):
    """The deploy regenerates encrypted_secret_text.json from the gitignored plaintext source, so a
    rotation that only updated the encrypted blob would be silently reverted on the next deploy."""
    from db_ops.lib import secret_text

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    key = "passphrase"
    secret_text.encrypt_secret_text_file(
        _write_json(tmp_path / "src.json", {"A_DBA": "old-a", "B_DBA": "old-b"}),
        data_dir / secret_text.ENCRYPTED_SECRET_TEXT_FILENAME, key)
    plain = _write_json(tmp_path / "secret_text.json", {"A_DBA": "old-a", "B_DBA": "old-b"})
    monkeypatch.setenv("DB_OPS_SECRET_KEY", key)

    outcome = {"results": [
        {"password_ref": "A_DBA", "status": "SUCCESS", "_new_password": "new-a"},
        {"password_ref": "B_DBA", "status": "FAILED", "_new_password": "never-stored"},
    ]}
    written = rot.persist_rotated(outcome, data_dir=data_dir, plaintext_store=plain)

    assert written == 1
    assert json.loads(plain.read_text(encoding="utf-8")) == {"A_DBA": "new-a", "B_DBA": "old-b"}
    assert secret_text.load_secret_text(data_dir, key=key)["A_DBA"] == "new-a"
    assert secret_text.load_secret_text(data_dir, key=key)["B_DBA"] == "old-b"


def test_persist_never_leaves_the_new_password_on_the_result(tmp_path, monkeypatch):
    """The result object is what gets printed and logged next; the value must not survive the write."""
    from db_ops.lib import secret_text

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    secret_text.encrypt_secret_text_file(
        _write_json(tmp_path / "src.json", {"A_DBA": "old-a"}),
        data_dir / secret_text.ENCRYPTED_SECRET_TEXT_FILENAME, "passphrase")
    monkeypatch.setenv("DB_OPS_SECRET_KEY", "passphrase")

    outcome = {"results": [{"password_ref": "A_DBA", "status": "SUCCESS", "_new_password": "new-a"}]}
    rot.persist_rotated(outcome, data_dir=data_dir)

    assert "new-a" not in json.dumps(outcome)


def _write_json(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path
