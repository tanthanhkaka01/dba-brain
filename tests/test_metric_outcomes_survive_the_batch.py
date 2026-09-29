"""What a metric stores is the same whether it ran in-process or in `common.cli metric-batch`.

`metrics` executed its SQL and scripts in-process until 0.24.0, importing four `common` modules
under a measured exemption (rules R03, `test_app_common_imports.py::EXEMPT_APPS`): one `common`
process per execution was 43 s of interpreter start-up on an average pass and 138 s on the worst,
longer than the interval between passes. The way out is one process per *target* - a batch - and
that moves the per-metric timeout, the connect/execute split that grades a failure, the raw
streams a failed script leaves, and the legacy-Oracle bridge path across a process boundary.

That is a behaviour change to the estate's own monitoring, so this guard was written **before**
it, against the in-process code, and its expectations are what that code stored. Every scenario
fakes only the lowest layer both designs share - the driver connect, `subprocess.run`,
`remote_exec.run_script`, the bridge call - and runs the real per-target loop, so everything
between the fake and the stored row is the production path. After the move the same scenarios
must store the same rows: status, message, error type, raw streams, exit code.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from db_ops.common import db_connect, oracle_bridge, remote_exec
from db_ops.metrics import collector
from db_ops.metrics.models import MetricDefinition, MetricTarget, MetricVariant

SECRETS = {"PW_REF": "pw", "ENC_REF": "enc-pass"}
STARTED = datetime(2026, 9, 26, 3, 0, tzinfo=timezone.utc)
NO_OVERRIDES = {"instance_overrides": []}


# --------------------------------------------------------------------------- #
# The lowest layer, faked
# --------------------------------------------------------------------------- #
class _Cursor:
    def __init__(self, columns: list[str], rows: list[tuple], fail: Exception | None) -> None:
        self._columns, self._rows, self._fail, self._pos = columns, list(rows), fail, 0
        self.rowcount = -1

    def execute(self, _sql, *_params):
        if self._fail is not None:
            raise self._fail

    @property
    def description(self):
        return [(name,) for name in self._columns] or None

    def fetchmany(self, size):
        out = self._rows[self._pos:self._pos + size]
        self._pos += len(out)
        return out

    def fetchall(self):
        return self.fetchmany(len(self._rows))

    def nextset(self):
        return False

    def close(self):
        pass


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def close(self):
        pass


ROW_COLUMNS = ["metric_item", "metric_value", "metric_unit", "status", "message"]

#: host -> what connecting to it does. A callable gets the connect kwargs (the per-database case).
SQL_HOSTS: dict[str, Any] = {
    "10.0.0.1": ("rows", [("disk C", "12", "pct", "OK", "free=88%")]),
    "10.0.0.2": ("rows", []),
    "10.0.0.3": ("connect-fails", RuntimeError("Login timeout expired")),
    "10.0.0.4": ("execute-fails", RuntimeError("Invalid object name 'sys.nope'. (208)")),
}


def _per_database(kwargs: dict) -> _Connection:
    database = kwargs.get("database") or ""
    if database in {"", "postgres"}:
        # The listing connection: the first query asks pg_database, in `db_catalog.databases`'
        # shape since 0.25.0 - the caller filters on these two flags, not the query.
        return _Connection(_Cursor(["name", "allow_connections", "is_template"],
                                   [("app1", True, False), ("app2", True, False),
                                    ("template1", True, True)], None))
    if database == "app2":
        return _Connection(_Cursor([], [], RuntimeError('permission denied for schema app2')))
    return _Connection(_Cursor(ROW_COLUMNS, [(f"{database} :: bloat", "3", "pct", "OK", "ok")], None))


def fake_connect_engine(**kwargs) -> _Connection:
    host = kwargs["host"]
    if host == "10.0.0.5":
        return _per_database(kwargs)
    kind, value = SQL_HOSTS[host]
    if kind == "connect-fails":
        raise value
    if kind == "execute-fails":
        return _Connection(_Cursor([], [], value))
    return _Connection(_Cursor(ROW_COLUMNS, value, None))


def fake_run_script(access, script, **kwargs):
    host = access["host"]
    now = {"method": access.get("method", "ssh"), "host": host, "command": "script"}
    if host == "10.1.0.1":
        return remote_exec.RemoteResult(**now, exit_code=0, duration_seconds=0.25,
                                        stdout='[{"metric_item": "cpu", "metric_value": "7", '
                                               '"metric_unit": "pct", "status": "OK", "message": "cpu=7"}]')
    if host == "10.1.0.2":
        return remote_exec.RemoteResult(**now, exit_code=2, stdout="partial", stderr="boom",
                                        duration_seconds=0.5)
    if host == "10.1.0.3":
        return remote_exec.RemoteResult(**now, exit_code=0, stdout="not json", duration_seconds=0.1)
    if host == "10.1.0.4":
        raise remote_exec.RemoteAuthError("Authentication failed for ops@10.1.0.4.", method="ssh",
                                          host=host, duration_seconds=0.3)
    if host == "10.1.0.5":
        raise remote_exec.RemoteTimeoutError("SSH command timed out after 5 seconds on 10.1.0.5:22.",
                                             method="ssh", host=host, command="bash -s",
                                             stdout="half", stderr="", duration_seconds=5.0)
    if host == "10.1.0.6":
        raise remote_exec.RemoteTimeoutError("SSH connect to ops@10.1.0.6:22 timed out after 10 seconds.",
                                             method="ssh", host=host, duration_seconds=10.0)
    raise AssertionError(f"no remote scenario for {host}")


def fake_subprocess_run(command, **kwargs):
    name = Path(str(command[-1])).name
    if name == "local_ok.sh":
        return subprocess.CompletedProcess(command, 0, stdout='[{"metric_item": "mem", "metric_value": "40", '
                                                             '"metric_unit": "pct", "status": "WARNING", '
                                                             '"message": "mem=40"}]', stderr="")
    if name == "local_exit.sh":
        return subprocess.CompletedProcess(command, 3, stdout="", stderr="no such service")
    if name == "local_slow.sh":
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout"), output="started", stderr="")
    if name == "docker_stats.sh":
        env = kwargs.get("env") or {}
        return subprocess.CompletedProcess(
            command, 0, stderr="",
            stdout=f'[{{"metric_item": "{env.get("DOCKER_CONTAINER")}", "metric_value": "1", '
                   '"metric_unit": "state", "status": "OK", "message": "running"}]')
    raise AssertionError(f"no local scenario for {command}")


def fake_bridge(**kwargs):
    if kwargs.get("host") == "10.2.0.1":
        return [{"METRIC_ITEM": "sessions", "METRIC_VALUE": "4", "METRIC_UNIT": "count",
                 "STATUS": "OK", "MESSAGE": "sessions=4"}]
    if kwargs.get("host") == "10.2.0.3":
        raise oracle_bridge.LegacyOracleError(
            "Oracle bridge at http://127.0.0.1:8765 did not answer ([WinError 10061] refused).")
    raise oracle_bridge.LegacyOracleError("Legacy Oracle run failed: ORA-12541: TNS:no listener")


# --------------------------------------------------------------------------- #
# The scenarios: (name, metric, target)
# --------------------------------------------------------------------------- #
def _metric(code: str, path: Path | None, **kwargs) -> MetricDefinition:
    return MetricDefinition(**{"metric_code": code, "db_type": "all", "category": "test",
                               "default_importance": 1, "active": True, "path": path, **kwargs})


def _target(target_id: str, ip: str, **kwargs) -> MetricTarget:
    return MetricTarget(**{"target_id": target_id, "server_id": target_id, "ip": ip,
                           "db_type": "sqlserver", "db_name": "SVC-LABEL", "credential_name": "c",
                           "port": 1433, "credential": {"username": "monitor", "password_ref": "PW_REF"},
                           **kwargs})


def _ssh(ip: str, **kwargs) -> dict:
    return {"enabled": True, "method": "ssh", "host": ip, "auth_type": "password",
            "credential_name": "os", **kwargs}


def scenarios(tmp: Path) -> list[tuple[str, MetricDefinition, MetricTarget]]:
    sql = tmp / "check.sql"
    sql.write_text("SELECT 1", encoding="utf-8")
    script = tmp / "remote.sh"
    script.write_text("echo '[]'", encoding="utf-8")
    local_ok, local_exit, local_slow = (tmp / "local_ok.sh", tmp / "local_exit.sh", tmp / "local_slow.sh")
    docker_script = tmp / "docker_stats.sh"
    for path in (local_ok, local_exit, local_slow, docker_script):
        path.write_text("echo", encoding="utf-8")
    per_db = tmp / "per_db.sql"
    per_db.write_text("SELECT 1", encoding="utf-8")
    os_credential = {"username": "ops", "password_ref": "PW_REF"}
    linux = {"platform": "linux", "cmd_credential": os_credential}
    bridge = {"method": "api", "bridge_url": "http://127.0.0.1:8765", "timeout_seconds": 5}
    return [
        ("sql rows", _metric("SQL_ROWS", sql), _target("T-SQL-ROWS", "10.0.0.1")),
        ("sql empty is no data", _metric("SQL_EMPTY", sql), _target("T-SQL-EMPTY", "10.0.0.2")),
        ("sql empty is ok", _metric("SQL_EMPTY_OK", sql, empty_result_is_ok=True),
         _target("T-SQL-EMPTY-OK", "10.0.0.2")),
        ("sql connect fails", _metric("SQL_CONNECT", sql, connection_error_severity="CRITICAL",
                                      execution_error_severity="WARNING"), _target("T-SQL-CONNECT", "10.0.0.3")),
        ("sql execute fails", _metric("SQL_EXECUTE", sql, connection_error_severity="CRITICAL",
                                      execution_error_severity="WARNING"), _target("T-SQL-EXECUTE", "10.0.0.4")),
        ("sql no credential", _metric("SQL_NO_CRED", sql, connection_error_severity="CRITICAL"),
         _target("T-SQL-NO-CRED", "10.0.0.1", credential=None)),
        ("sql unresolvable password", _metric("SQL_BAD_REF", sql, connection_error_severity="CRITICAL"),
         _target("T-SQL-BAD-REF", "10.0.0.1", credential={"username": "m", "password_ref": "MISSING"})),
        ("postgres per database", _metric("PG_PER_DB", None, db_type="postgresql", variants=[
            MetricVariant(name="pg", db_type="postgresql", path=per_db, per_database=True)]),
         _target("T-PG", "10.0.0.5", db_type="postgresql", port=5432, db_name="postgres")),
        ("ssh rows", _metric("SSH_ROWS", script, collector_type="cmd"),
         _target("T-SSH-ROWS", "10.1.0.1", cmd_access=_ssh("10.1.0.1"), **linux)),
        ("ssh non-zero exit", _metric("SSH_EXIT", script, collector_type="cmd"),
         _target("T-SSH-EXIT", "10.1.0.2", cmd_access=_ssh("10.1.0.2"), **linux)),
        ("ssh stdout not json", _metric("SSH_NOT_JSON", script, collector_type="cmd"),
         _target("T-SSH-JSON", "10.1.0.3", cmd_access=_ssh("10.1.0.3"), **linux)),
        ("ssh auth rejected", _metric("SSH_AUTH", script, collector_type="cmd",
                                      connection_error_severity="CRITICAL"),
         _target("T-SSH-AUTH", "10.1.0.4", cmd_access=_ssh("10.1.0.4"), **linux)),
        ("ssh command timed out", _metric("SSH_SLOW", script, collector_type="cmd",
                                          connection_error_severity="CRITICAL"),
         _target("T-SSH-SLOW", "10.1.0.5", cmd_access=_ssh("10.1.0.5"), **linux)),
        ("ssh connect timed out", _metric("SSH_NO_CONNECT", script, collector_type="cmd",
                                          connection_error_severity="CRITICAL"),
         _target("T-SSH-NOCONN", "10.1.0.6", cmd_access=_ssh("10.1.0.6"), **linux)),
        ("ssh with an env secret", _metric("SSH_ENV", script, collector_type="cmd"),
         _target("T-SSH-ENV", "10.1.0.1", cmd_access=_ssh("10.1.0.1"),
                 metrics_config={"collector_env": {"OS_SERVICE_NAMES": "cron"},
                                 "env_secrets": {"BACKUP_ENCRYPTION_PASSWORD": "ENC_REF"}}, **linux)),
        ("env secret missing", _metric("SSH_ENV_MISSING", script, collector_type="cmd"),
         _target("T-SSH-ENV-MISSING", "10.1.0.1", cmd_access=_ssh("10.1.0.1"),
                 metrics_config={"env_secrets": {"X_PASSWORD": "NOT_THERE"}}, **linux)),
        ("cmd_access error recorded", _metric("CMD_BROKEN", script, collector_type="cmd"),
         _target("T-CMD-BROKEN", "10.1.0.1", cmd_access={"enabled": True, "method": "ssh",
                                                          "error": "cmd_access.credential_name is required"},
                 **linux)),
        ("local rows", _metric("LOCAL_ROWS", local_ok, collector_type="cmd"),
         _target("T-LOCAL-OK", "127.0.0.1", cmd_access={"enabled": True, "method": "local"}, **linux)),
        ("local non-zero exit", _metric("LOCAL_EXIT", local_exit, collector_type="cmd"),
         _target("T-LOCAL-EXIT", "127.0.0.1", cmd_access={"enabled": True, "method": "local"}, **linux)),
        ("local timeout", _metric("LOCAL_SLOW", local_slow, collector_type="cmd", default_timeout=3),
         _target("T-LOCAL-SLOW", "127.0.0.1", cmd_access={"enabled": True, "method": "local"}, **linux)),
        ("docker local", _metric("DOCKER_STATE", docker_script, collector_type="docker"),
         _target("T-DOCKER", "10.3.0.1", container_name="pg_lab_01")),
        ("bridge rows", _metric("BRIDGE_ROWS", sql, db_type="oracle"),
         _target("T-BRIDGE-OK", "10.2.0.1", db_type="oracle", port=1521, sql_access=bridge,
                 service_name="ORCL")),
        ("bridge down", _metric("BRIDGE_DOWN", sql, db_type="oracle", connection_error_severity="CRITICAL"),
         _target("T-BRIDGE-DOWN", "10.2.0.2", db_type="oracle", port=1521, sql_access=bridge,
                 service_name="ORCL")),
        ("bridge process down", _metric("BRIDGE_GONE", sql, db_type="oracle",
                                        connection_error_severity="CRITICAL"),
         _target("T-BRIDGE-GONE", "10.2.0.3", db_type="oracle", port=1521, sql_access=bridge,
                 service_name="ORCL")),
    ]


class _Store:
    def __init__(self) -> None:
        self.results: list = []

    def insert_results(self, *, run_id, results):
        self.results.extend(results)
        return len(results)


def _outcome(result) -> tuple:
    return (result.metric_code, result.metric_item, result.metric_value, result.metric_unit,
            result.status, result.message, result.raw_stdout or None, result.raw_stderr or None,
            result.exit_code, result.error_type)


@pytest.fixture()
def lowest_layer_faked(monkeypatch):
    monkeypatch.setattr(db_connect, "connect_engine", fake_connect_engine)
    monkeypatch.setattr(remote_exec, "run_script", fake_run_script)
    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(oracle_bridge, "run_bridge_query", fake_bridge)
    # `metric-batch` runs in this process (conftest), so these fakes are what it reaches.


def collect(tmp_path: Path) -> dict[str, list[tuple]]:
    outcomes: dict[str, list[tuple]] = {}
    for name, metric, target in scenarios(tmp_path):
        store = _Store()
        collector._collect_target(
            target=target, definitions=[metric], overrides=NO_OVERRIDES, secrets=SECRETS,
            store=store, run_id=1, started=STARTED, dry_run=False, force=True,
            include_windowed=True, tally=collector._Tally())
        outcomes[name] = [_outcome(result) for result in store.results]
    return outcomes


#: What the in-process collector stored for each scenario, recorded on 2026-09-26 before the batch
#: existed. One entry has changed since, by decision rather than by the move: a dead 8i listener
#: (ORA-12541, the "bridge down" scenario) graded as an *execute* failure, so a metric's
#: connection_error_severity never reached it; the operator had it graded as the connect failure
#: it is (0.24.0). "bridge process down" is the other half of that decision - the bridge itself not
#: answering is the monitoring's failure, not the target's, and stays an execute failure.
EXPECTED: dict[str, list[tuple]] = {'sql rows': [('SQL_ROWS', 'disk C', '12', 'pct', 'OK', 'free=88%', None, None, None, None)],
     'sql empty is no data': [('SQL_EMPTY',
                               None,
                               None,
                               None,
                               'NO_DATA',
                               'Collector returned no rows.',
                               None,
                               None,
                               None,
                               'CHECK_FAILED')],
     'sql empty is ok': [('SQL_EMPTY_OK',
                          None,
                          None,
                          None,
                          'OK',
                          'SQL returned no rows.',
                          None,
                          None,
                          None,
                          None)],
     'sql connect fails': [('SQL_CONNECT',
                            None,
                            None,
                            None,
                            'CRITICAL',
                            'Connection failed: Login timeout expired',
                            None,
                            None,
                            None,
                            'CONNECT_FAILED')],
     'sql execute fails': [('SQL_EXECUTE',
                            None,
                            None,
                            None,
                            'WARNING',
                            "SQL execution failed: Invalid object name 'sys.nope'. (208)",
                            None,
                            None,
                            None,
                            'QUERY_FAILED')],
     'sql no credential': [('SQL_NO_CRED',
                            None,
                            None,
                            None,
                            'CRITICAL',
                            'Credential not found for target T-SQL-NO-CRED.',
                            None,
                            None,
                            None,
                            'CHECK_FAILED')],
     'sql unresolvable password': [('SQL_BAD_REF',
                                    None,
                                    None,
                                    None,
                                    'CRITICAL',
                                    'Credential could not be resolved: Password ref not found in environment or '
                                    'secret_text.json: MISSING',
                                    None,
                                    None,
                                    None,
                                    'CHECK_FAILED')],
     'postgres per database': [('PG_PER_DB', 'app1 :: bloat', '3', 'pct', 'OK', 'ok', None, None, None, None),
                               ('PG_PER_DB',
                                'app2 :: collection failed',
                                '0',
                                'summary',
                                'WARNING',
                                'db=app2, collection failed: SQL execution failed: permission denied for schema '
                                'app2',
                                None,
                                None,
                                None,
                                'CHECK_FAILED')],
     'ssh rows': [('SSH_ROWS',
                   'cpu',
                   '7',
                   'pct',
                   'OK',
                   'cpu=7',
                   '[{"metric_item": "cpu", "metric_value": "7", "metric_unit": "pct", "status": "OK", '
                   '"message": "cpu=7"}]',
                   None,
                   0,
                   None)],
     'ssh non-zero exit': [('SSH_EXIT',
                            None,
                            None,
                            None,
                            'WARNING',
                            'Command exited with code 2.',
                            'partial',
                            'boom',
                            2,
                            'CHECK_FAILED')],
     'ssh stdout not json': [('SSH_NOT_JSON',
                              None,
                              None,
                              None,
                              'WARNING',
                              'Command stdout is not valid JSON: Expecting value.',
                              'not json',
                              None,
                              0,
                              'CHECK_FAILED')],
     'ssh auth rejected': [('SSH_AUTH',
                            None,
                            None,
                            None,
                            'CRITICAL',
                            'Authentication failed for ops@10.1.0.4.',
                            None,
                            None,
                            None,
                            'AUTH_FAILED')],
     'ssh command timed out': [('SSH_SLOW',
                                None,
                                None,
                                None,
                                'WARNING',
                                'SSH command timed out after 5 seconds on 10.1.0.5:22.',
                                'half',
                                None,
                                None,
                                'CONNECT_FAILED')],
     'ssh connect timed out': [('SSH_NO_CONNECT',
                                None,
                                None,
                                None,
                                'CRITICAL',
                                'SSH connect to ops@10.1.0.6:22 timed out after 10 seconds.',
                                None,
                                None,
                                None,
                                'CONNECT_FAILED')],
     'ssh with an env secret': [('SSH_ENV',
                                 'cpu',
                                 '7',
                                 'pct',
                                 'OK',
                                 'cpu=7',
                                 '[{"metric_item": "cpu", "metric_value": "7", "metric_unit": "pct", "status": '
                                 '"OK", "message": "cpu=7"}]',
                                 None,
                                 0,
                                 None)],
     'env secret missing': [('SSH_ENV_MISSING',
                             None,
                             None,
                             None,
                             'WARNING',
                             'SSH_ENV_MISSING on T-SSH-ENV-MISSING: env_secrets maps X_PASSWORD to secret ref '
                             "'NOT_THERE', which is not in the secret store. Add it, or pass --key/--key-base64 "
                             'so the store can be read.',
                             None,
                             None,
                             None,
                             'CHECK_FAILED')],
     'cmd_access error recorded': [('CMD_BROKEN',
                                    None,
                                    None,
                                    None,
                                    'WARNING',
                                    'cmd_access.credential_name is required',
                                    None,
                                    None,
                                    None,
                                    'CHECK_FAILED')],
     'local rows': [('LOCAL_ROWS',
                     'mem',
                     '40',
                     'pct',
                     'WARNING',
                     'mem=40',
                     '[{"metric_item": "mem", "metric_value": "40", "metric_unit": "pct", "status": "WARNING", '
                     '"message": "mem=40"}]',
                     None,
                     0,
                     'CHECK_FAILED')],
     'local non-zero exit': [('LOCAL_EXIT',
                              None,
                              None,
                              None,
                              'WARNING',
                              'Command exited with code 3.',
                              None,
                              'no such service',
                              3,
                              'CHECK_FAILED')],
     'local timeout': [('LOCAL_SLOW',
                        None,
                        None,
                        None,
                        'WARNING',
                        'Command timed out after 3 seconds.',
                        'started',
                        None,
                        None,
                        'CONNECT_FAILED')],
     'docker local': [('DOCKER_STATE',
                       'pg_lab_01',
                       '1',
                       'state',
                       'OK',
                       'running',
                       '[{"metric_item": "pg_lab_01", "metric_value": "1", "metric_unit": "state", "status": '
                       '"OK", "message": "running"}]',
                       None,
                       0,
                       None)],
     'bridge rows': [('BRIDGE_ROWS', 'sessions', '4', 'count', 'OK', 'sessions=4', None, None, None, None)],
     'bridge down': [('BRIDGE_DOWN',
                      None,
                      None,
                      None,
                      'CRITICAL',
                      'Legacy Oracle run failed: ORA-12541: TNS:no listener',
                      None,
                      None,
                      None,
                      'CONNECT_FAILED')],
     'bridge process down': [('BRIDGE_GONE',
                              None,
                              None,
                              None,
                              'WARNING',
                              'Oracle bridge at http://127.0.0.1:8765 did not answer ([WinError 10061] refused).',
                              None,
                              None,
                              None,
                              'CHECK_FAILED')]}


@pytest.mark.parametrize("name", list(EXPECTED))
def test_a_metric_stores_what_it_stored_before_the_batch(lowest_layer_faked, tmp_path, name):
    assert collect(tmp_path)[name] == EXPECTED[name]


def test_every_scenario_has_an_expectation(tmp_path):
    assert [name for name, _m, _t in scenarios(tmp_path)] == list(EXPECTED)
