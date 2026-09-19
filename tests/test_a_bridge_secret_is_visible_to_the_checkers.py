"""Why a secret named only by ``sql_access`` had to become visible to the two commands that audit.

A legacy-Oracle target is reached through a bridge rather than a driver, and it needs two secrets:
the login its connect string is built from, and the shared secret the bridge token is signed with.
Neither was looked at. ``check-credentials`` skipped the target outright as "no DB login by
design", and ``check-secret`` walked five config files, none of which reads ``sql_access``, and
answered ``NO_TARGET`` — "no config names this ref" — about a ref the configuration named. Every
collection for that target then failed on a secret both commands had just reported clean.

That is the worst shape a checker can have: it is the command you run to decide whether to look
further, so a clean answer from it stops the search.
"""

import json

import pytest

from db_ops.common import secret_check as sc


BRIDGE_REF = "TOKEN_198_51_100_7_ORACLE_BRIDGE"

INSTANCE = {
    "server_id": "ACME-8I", "db_type": "oracle", "ip": "198.51.100.7", "port": 1521,
    "service_name": "ORCL8I", "enabled": True, "metrics": {"enabled": True},
    "default_credential_name": "oracle_dba",
    "sql_access": {"method": "api", "bridge_url": "http://198.51.100.9:8900/run",
                   "secret_ref": BRIDGE_REF},
}

CREDENTIALS = [{
    "db_type": "oracle", "server_id": "ACME-8I", "service_name": "ORCL8I",
    "credentials": [{"credential_name": "oracle_dba", "username": "dba_user",
                     "password_ref": "ORACLE_198_51_100_7_DBA_USER", "role": "SYSDBA"}],
}]


# ---------------------------------------------------------------------------
# check-secret: the ref resolves to the target that names it
# ---------------------------------------------------------------------------
def test_a_ref_named_only_by_sql_access_is_no_longer_an_unknown_ref(tmp_path, monkeypatch):
    _config(monkeypatch, instances=[INSTANCE])

    target = sc.resolve_check_target(BRIDGE_REF, data_dir=tmp_path)

    assert target["kind"] == "bridge"
    assert target["source"] == "db_instances.json sql_access.secret_ref"


def test_the_configuration_beats_the_guess_the_key_name_makes(tmp_path, monkeypatch):
    """`ORACLE_BRIDGE` in a name resolves to "not a login", which is true and not the whole truth:
    the instance that names the ref says which bridge, which login and which service, so the
    secret is provable. A name is a guess; config is evidence, and evidence goes first."""
    _config(monkeypatch, instances=[INSTANCE])
    assert "ORACLE_BRIDGE" in BRIDGE_REF

    assert sc.resolve_check_target(BRIDGE_REF, data_dir=tmp_path)["kind"] == "bridge"


def test_a_bridge_ref_no_instance_names_is_still_reported_as_key_material(tmp_path, monkeypatch):
    """Nothing about the fallback changed — an estate that configures no bridge at all keeps the
    answer it had."""
    _config(monkeypatch, instances=[])

    assert sc.resolve_check_target(BRIDGE_REF, data_dir=tmp_path)["kind"] == "not_a_login"


def test_a_connect_ref_is_found_as_well_as_the_token_secret(tmp_path, monkeypatch):
    """Both fields name a secret and both were invisible. `connect_ref` holds a whole connect
    string, so the target carries which field named it rather than pretending they are the same."""
    instance = dict(INSTANCE, sql_access={"method": "subprocess", "connect_ref": "ORACLE8I_CONNECT_ACME"})
    _config(monkeypatch, instances=[instance])

    target = sc.resolve_check_target("ORACLE8I_CONNECT_ACME", data_dir=tmp_path)

    assert (target["kind"], target["field"]) == ("bridge", "connect_ref")


# ---------------------------------------------------------------------------
# check-secret: and it is proven by the bridge query its own description promised
# ---------------------------------------------------------------------------
def test_a_statement_that_runs_through_the_bridge_proves_the_secret(tmp_path, monkeypatch):
    ran = {}

    def fake_run_query(**kwargs):
        ran.update(kwargs)
        return {"columns": ["1"], "rows": [[1]], "row_count": 1, "truncated": False,
                "db_version": "8.1.7.0.0", "transport": "api"}

    _config(monkeypatch, instances=[INSTANCE])
    monkeypatch.setattr(sc.data_sources, "load_secret_text",
                        lambda *a, **k: {BRIDGE_REF: "shared", "ORACLE_198_51_100_7_DBA_USER": "pw"})
    monkeypatch.setattr(sc, "_port_open", lambda *a, **k: True)
    monkeypatch.setattr(sc.oracle_bridge, "run_query", fake_run_query)

    result = sc.check_ref(BRIDGE_REF, data_dir=tmp_path)

    assert result["status"] == "OK"
    # The credential is the target's own, which is what the collection uses — proving the secret
    # against some other login would prove nothing about the target that owns it.
    assert ran["credential"]["username"] == "dba_user"
    assert ran["sql_access"]["bridge_url"] == "http://198.51.100.9:8900/run"


def test_a_login_the_bridge_rejects_is_an_auth_failure_not_a_connect_failure(tmp_path, monkeypatch):
    _config(monkeypatch, instances=[INSTANCE])
    monkeypatch.setattr(sc.data_sources, "load_secret_text",
                        lambda *a, **k: {BRIDGE_REF: "shared", "ORACLE_198_51_100_7_DBA_USER": "pw"})
    monkeypatch.setattr(sc, "_port_open", lambda *a, **k: True)
    monkeypatch.setattr(sc.oracle_bridge, "run_query", _raises(
        "Oracle bridge refused the statement: ORA-01017: invalid username/password"))

    assert sc.check_ref(BRIDGE_REF, data_dir=tmp_path)["status"] == "AUTH_FAILED"


def test_a_bridge_nobody_started_is_unreachable_and_says_where_to_start_it(tmp_path, monkeypatch):
    """The 8i bridge is started by hand and nothing restarts it, so down is the ordinary state to
    meet — and it says nothing about the secret. Reporting that as a bad credential is how a good
    secret gets rotated for nothing."""
    _config(monkeypatch, instances=[INSTANCE])
    monkeypatch.setattr(sc.data_sources, "load_secret_text", lambda *a, **k: {BRIDGE_REF: "shared"})
    monkeypatch.setattr(sc, "_port_open", lambda *a, **k: False)

    result = sc.check_ref(BRIDGE_REF, data_dir=tmp_path)

    assert result["status"] == "UNREACHABLE"
    assert "start it" in result["detail"]


def test_an_instance_with_no_credential_says_that_rather_than_blaming_the_bridge(tmp_path, monkeypatch):
    _config(monkeypatch, instances=[dict(INSTANCE, default_credential_name="")])
    monkeypatch.setattr(sc.data_sources, "load_secret_text", lambda *a, **k: {BRIDGE_REF: "shared"})
    monkeypatch.setattr(sc, "_port_open", lambda *a, **k: True)

    result = sc.check_ref(BRIDGE_REF, data_dir=tmp_path)

    assert result["status"] == "NO_TARGET"
    assert "no credential to build a connect string with" in result["detail"]


# ---------------------------------------------------------------------------
# check-credentials: the target is checked, not skipped
# ---------------------------------------------------------------------------
def test_check_credentials_no_longer_skips_a_target_it_cannot_prove(tmp_path, capsys):
    """The skip read "API-bridge targets carry no DB login by design". They carry one: the connect
    string is built from the target's own credential, exactly as a direct target's is."""
    from db_ops import cli

    _write_config(tmp_path, instance=dict(INSTANCE, default_credential_name=""))

    assert cli.main(["check-credentials", str(tmp_path)]) == 1
    problems = json.loads(capsys.readouterr().out)["data"]["problems"]
    assert any("no credential" in problem for problem in problems)


def test_an_api_target_that_names_no_token_secret_is_reported(tmp_path, capsys):
    from db_ops import cli

    access = {"method": "api", "bridge_url": "http://198.51.100.9:8900/run"}
    _write_config(tmp_path, instance=dict(INSTANCE, sql_access=access))

    assert cli.main(["check-credentials", str(tmp_path)]) == 1
    problems = json.loads(capsys.readouterr().out)["data"]["problems"]
    assert any("names no secret_ref" in problem for problem in problems)


def test_a_ref_that_is_named_but_not_in_the_store_is_reported(tmp_path, capsys, monkeypatch):
    """The shape the estate actually met: the secret existed on the master and not on the node
    running the collection. A ref that is named and absent fails exactly like one never named."""
    from db_ops import cli
    from db_ops.common import data_sources

    _write_config(tmp_path, instance=INSTANCE)
    (tmp_path / "encrypted_secret_text.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(data_sources, "load_secret_text",
                        lambda *a, **k: {"ORACLE_198_51_100_7_DBA_USER": "pw"})

    assert cli.main(["check-credentials", str(tmp_path)]) == 1
    problems = json.loads(capsys.readouterr().out)["data"]["problems"]
    assert any(BRIDGE_REF in problem and "not in the secret store" in problem for problem in problems)


def test_a_node_that_cannot_open_the_store_keeps_the_config_level_answer(tmp_path, capsys):
    """This command is documented as needing no key. Without one it must not turn every configured
    ref into a finding — "I could not look" and "it is not there" are different answers."""
    from db_ops import cli

    _write_config(tmp_path, instance=INSTANCE)

    assert cli.main(["check-credentials", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["problems"] == []


# ---------------------------------------------------------------------------
def _config(monkeypatch, *, instances):
    monkeypatch.setattr(sc.data_sources, "load_db_instances", lambda *a, **k: list(instances))
    monkeypatch.setattr(sc.data_sources, "load_all_credentials",
                        lambda *a, **k: {"oracle": list(CREDENTIALS)})
    monkeypatch.setattr(sc.data_sources, "load_remote_credentials", lambda *a, **k: [])


def _write_config(data_dir, *, instance):
    (data_dir / "db_instances.json").write_text(
        json.dumps({"db_instances": [instance]}), encoding="utf-8")
    (data_dir / "users.json").write_text(
        json.dumps({"database_credentials": CREDENTIALS}), encoding="utf-8")


def _raises(message):
    def fail(**kwargs):
        raise RuntimeError(message)
    return fail


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Nothing in this file may open a socket: the suite is offline, and a bridge check is the
    one place in it that would try."""
    monkeypatch.setattr(sc.host_probe, "probe_port",
                        lambda *a, **k: {"open": False, "reason": "test"})
