"""Why "cannot check this secret" has to be a fact about the estate, not a gap in the checker.

An audit that reports a secret as untestable invites the operator to delete it. So the burden is on
this module to look everywhere a ref can be named and to ask the host the right question, and to
distinguish clearly between the several very different things that all used to read "unreachable":
the host is gone, the host is up but has no scriptable way in, the credential is wrong, or this is
not a login at all. Each one leads somewhere different.

Every earlier misclassification in these tests is a real one, from the 2026-08-01 audit.
"""

import pytest

from db_ops.common import secret_check as sc


# ---------------------------------------------------------------------------
# Target resolution looks everywhere a ref can be named
# ---------------------------------------------------------------------------
def test_a_container_connection_supplies_the_published_non_default_port(tmp_path, monkeypatch):
    """docker_db_connections is where a container's real port lives (5442, 1522, 5435). Skipping it
    is why an earlier pass knocked on 5432, got nothing, and called a working secret unusable."""
    _write(tmp_path / "docker_db_connections.json", {"docker_db_connections": [
        {"id": "PG_R", "host": "10.0.0.9", "port": 5442, "username": "postgres",
         "password_env": "POSTGRE_10_0_0_9_POSTGRES"}]})
    _empty_config(monkeypatch)

    target = sc.resolve_check_target("POSTGRE_10_0_0_9_POSTGRES", data_dir=tmp_path)

    assert target["kind"] == "db"
    assert (target["ip"], target["port"]) == ("10.0.0.9", 5442)
    assert target["source"] == "docker_db_connections.json"


def test_cmd_access_states_the_protocol_so_it_is_not_guessed(tmp_path, monkeypatch):
    _empty_config(monkeypatch)
    monkeypatch.setattr(sc.data_sources, "load_db_instances", lambda *a, **k: [
        {"server_id": "S", "ip": "10.0.0.5",
         "cmd_access": {"method": "ssh", "host": "10.0.0.5", "port": 22,
                        "credential_name": "remote_x"}}])
    monkeypatch.setattr(sc.data_sources, "load_remote_credentials", lambda *a, **k: [
        {"host": "10.0.0.5", "credentials": [
            {"credential_name": "remote_x", "username": "tuser", "password_ref": "REMOTE_10_0_0_5_TUSER"}]}])

    target = sc.resolve_check_target("REMOTE_10_0_0_5_TUSER", data_dir=tmp_path)

    assert target["kind"] == "remote"
    assert target["method"] == "ssh"


def test_a_ref_no_config_names_falls_back_to_the_standard_key_name(tmp_path, monkeypatch):
    _empty_config(monkeypatch)

    target = sc.resolve_check_target("MSSQL_10_1_2_3_DBA_USER", data_dir=tmp_path)

    assert target["kind"] == "db"
    assert target["ip"] == "10.1.2.3"
    assert target["source"] == "key name"


def test_a_port_in_the_key_name_is_honoured(tmp_path, monkeypatch):
    """ORACLE_203_0_113_121_1522_SYS names a listener on 1522. Reading past the port and probing
    the default 1521 is how a reachable instance gets reported as a connect failure."""
    _empty_config(monkeypatch)

    target = sc.resolve_check_target("ORACLE_203_0_113_121_1522_SYS", data_dir=tmp_path)

    assert (target["ip"], target["port"]) == ("203.0.113.121", 1522)


def test_a_ref_nothing_names_and_no_standard_shape_is_the_only_honest_unknown(tmp_path, monkeypatch):
    _empty_config(monkeypatch)

    target = sc.resolve_check_target("SOMETHING_ARBITRARY", data_dir=tmp_path)

    assert target["kind"] == "unknown"


# ---------------------------------------------------------------------------
# Key material is not a login, and saying so is not the same as failing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("ref", [
    "TOKEN_203_0_113_188_BACKUP_ENC",
    "TOKEN_192_0_2_246_ORACLE_BRIDGE",
    "TOKEN_TELEGRAM_IT_DEV_CODE_SP_BOT",
])
def test_key_material_reports_what_it_is_rather_than_a_connection_failure(ref, tmp_path, monkeypatch):
    """A backup passphrase decrypts a file; there is no session to open. Reporting that as a failed
    check invites someone to delete a key that still decrypts live backup sets."""
    _empty_config(monkeypatch)

    target = sc.resolve_check_target(ref, data_dir=tmp_path)

    assert target["kind"] == "not_a_login"
    assert target["detail"]


# ---------------------------------------------------------------------------
# Asking the right question of a mixed estate
# ---------------------------------------------------------------------------
def test_an_unstated_protocol_probes_ssh_before_winrm(monkeypatch):
    """The estate is mixed - Windows over WinRM, Ubuntu over SSH. Assuming one and reporting
    'unreachable' when it does not answer is the wrong question, not a fact about the host."""
    asked = []

    def port_open(host, port, timeout=3.0):
        asked.append(port)
        return False

    monkeypatch.setattr(sc, "_port_open", port_open)
    result = sc._check_remote({}, {"host": "10.0.0.1", "username": "u", "method": "", "port": None},
                              "pw", 5)

    assert asked[:3] == [sc.SSH_PORT, 5985, 5986]
    assert result["status"] == "UNREACHABLE"


def test_a_host_reachable_only_on_rdp_is_distinguished_from_a_host_that_is_gone(monkeypatch):
    """'No management port' and 'unreachable' lead to completely different follow-ups: enable WinRM,
    versus decommission the entry."""
    monkeypatch.setattr(sc, "_port_open", lambda host, port, timeout=3.0: port == 3389)

    result = sc._check_remote({}, {"host": "10.0.0.2", "username": "u", "method": "", "port": None},
                              "pw", 5)

    assert result["status"] == "NO_MANAGEMENT_PORT"
    assert "RDP" in result["detail"]


def test_oracle_without_a_declared_service_name_says_so_instead_of_a_driver_error(tmp_path, monkeypatch):
    """Oracle connects by service. 'Needs a service_name' is actionable; a raw DPY error is not."""
    _empty_config(monkeypatch)
    monkeypatch.setattr(sc, "_port_open", lambda *a, **k: True)

    result = sc._check_db({}, {"db_type": "oracle", "ip": "10.0.0.3", "port": 1522,
                               "username": "sys"}, "pw", 5)

    assert result["status"] == "NO_TARGET"
    assert "service_name" in result["detail"]


def test_a_service_name_declared_for_the_host_elsewhere_is_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(sc.data_sources, "load_all_credentials", lambda *a, **k: {})
    monkeypatch.setattr(sc.data_sources, "load_db_instances", lambda *a, **k: [
        {"ip": "10.0.0.3", "service_name": "FREEPDB1"}])

    assert sc.oracle_service_for_host("10.0.0.3") == "FREEPDB1"


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("message", [
    "Login failed for user 'x'", "password authentication failed", "ORA-01017",
    "Failed to authenticate the user", "permission denied",
])
def test_a_rejected_credential_is_auth_failed_not_a_connect_failure(message):
    """The two are acted on differently: one is a wrong password, the other is a network path."""
    assert sc._classify(message) == "AUTH_FAILED"


def test_anything_else_stays_a_connect_failure():
    assert sc._classify("timed out after 8s") == "CONNECT_FAILED"


def test_an_unknown_ref_is_reported_rather_than_treated_as_a_failed_login(tmp_path, monkeypatch):
    monkeypatch.setattr(sc.data_sources, "load_secret_text", lambda *a, **k: {"KNOWN": "x"})

    result = sc.check_ref("ABSENT", data_dir=tmp_path)

    assert result["status"] == "UNKNOWN_REF"


def test_an_empty_request_checks_the_whole_store(tmp_path, monkeypatch):
    monkeypatch.setattr(sc.data_sources, "load_secret_text",
                        lambda *a, **k: {"A": "x", "B": "y", "C": "z"})
    monkeypatch.setattr(sc, "check_ref",
                        lambda ref, **k: {"password_ref": ref, "status": "OK"})

    outcome = sc.check({}, data_dir=tmp_path)

    assert outcome["selected"] == 3
    assert outcome["summary"]["OK"] == 3


def test_a_bad_match_expression_is_reported_not_raised_as_a_regex_error(tmp_path, monkeypatch):
    monkeypatch.setattr(sc.data_sources, "load_secret_text", lambda *a, **k: {"A": "x"})

    with pytest.raises(sc.SecretCheckError, match="regular expression"):
        sc.check({"match": "([unclosed"}, data_dir=tmp_path)


# ---------------------------------------------------------------------------
def _write(path, payload):
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _empty_config(monkeypatch):
    monkeypatch.setattr(sc.data_sources, "load_db_instances", lambda *a, **k: [])
    monkeypatch.setattr(sc.data_sources, "load_all_credentials", lambda *a, **k: {})
    monkeypatch.setattr(sc.data_sources, "load_remote_credentials", lambda *a, **k: [])
