"""A command that needs a live server is answered by a fake one, so its answer's keys are checked (R16).

R16's answer side holds every key a successful answer carries to the reference - and only a real
answer shows the keys. The R09 guard's runs answer the commands that finish with nothing reachable;
37 commands answered only from a live database or host, so nothing checked what they said until
0.25.0. Here each runs **in this process, through `common.cli` and every line of its own code**,
with only the last step faked: the driver's connection - a server that answers by the statement it
is sent - and a `remote_exec` session's transport - a host that answers by the command.

A fake that answers by fragment proves the plumbing, not the SQL: whether `sys.database_files`
really has that column is the lab's question. What it does prove is that a command which got its
rows builds a whole answer, in the envelope, with keys the reference describes. Writing it found
`list-schemas` answering every success with `KeyError: 'database'` since 0.22.0 (1.68).
"""

from __future__ import annotations

import hashlib
import io
import json
import shlex
import sys
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from db_ops.common import cli, db_connect, remote_exec, sql_run

REPO = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# A server that answers by the statement it is sent
# --------------------------------------------------------------------------- #
class _Cursor:
    rowcount = -1

    def __init__(self, server: "_Server") -> None:
        self._server = server
        self._rows: list[tuple] = []
        self.description = None

    def execute(self, sql, params=()):
        self._server.executed.append(str(sql))
        columns, rows = self._server.answer(str(sql))
        self.description = [(name,) for name in columns] if columns else None
        self._rows = [tuple(row) for row in rows]

    def executemany(self, sql, rows):
        self._server.executed.append(str(sql))

    def fetchmany(self, size):
        taken, self._rows = self._rows[:size], self._rows[size:]
        return taken

    def fetchall(self):
        taken, self._rows = self._rows, []
        return taken

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def nextset(self):
        return False

    def close(self):
        return None


class _Connection:
    autocommit = True

    def __init__(self, server: "_Server") -> None:
        self._server = server

    def cursor(self):
        return _Cursor(self._server)

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


class _Server:
    """The first fragment a statement contains picks its ``(columns, rows)``; a statement nothing
    matches is one that returns no rows - a DDL, a `KILL`, an `EXEC`."""

    def __init__(self) -> None:
        self.answers: dict[str, tuple[list[str], list[tuple]]] = {}
        self.executed: list[str] = []

    def answer(self, sql: str) -> tuple[list[str], list[tuple]]:
        for fragment, answer in self.answers.items():
            if fragment in sql:
                return answer
        return [], []

    def connect(self, *_args, **_kwargs) -> _Connection:
        return _Connection(self)


@pytest.fixture
def server(monkeypatch) -> _Server:
    fake = _Server()
    monkeypatch.setattr(sql_run, "connect_target", fake.connect)
    monkeypatch.setattr(db_connect, "connect_engine", fake.connect)
    return fake


# --------------------------------------------------------------------------- #
# A host that answers by the command it is sent
# --------------------------------------------------------------------------- #
class _Host:
    """The first fragment a command or script contains picks its ``(exit code, stdout)``; anything
    else succeeds and prints nothing. Both transports - SSH and WinRM - answer through it.

    It also holds files (``files``: path -> bytes), which its SFTP serves and a few commands read
    the way the real ones would: `sha256sum` hashes what is there, `stat -c %s` sizes it, `tar -cf`
    makes the archive, `rm -f` removes - so a transfer that checks what landed checks these bytes.
    """

    def __init__(self) -> None:
        self.answers: dict[str, tuple[int, str]] = {}
        self.ran: list[str] = []
        self.files: dict[str, bytes] = {}

    def result(self, session, text: str) -> remote_exec.RemoteResult:
        self.ran.append(text)
        code, out = next((answer for fragment, answer in self.answers.items() if fragment in text),
                         None) or self._file_command(text)
        return remote_exec.RemoteResult(session.access.method, session.access.host, text[:200], code, out, "")

    def _file_command(self, text: str) -> tuple[int, str]:
        words = shlex.split(text.split("|")[0]) if text.strip() else []
        if "sha256sum" in words:
            path = words[words.index("sha256sum") + 1]
            if path not in self.files:
                return 1, ""
            return 0, f"{hashlib.sha256(self.files[path]).hexdigest()}  {path}\n"
        if words[:3] == ["stat", "-c", "%s"]:
            return (0, f"{len(self.files[words[3]])}\n") if words[3] in self.files else (1, "")
        # An archive is made wherever the pipeline runs tar: `find ... | tar -cf X --null -T -`.
        for segment in text.replace("&&", "|").split("|"):
            parts = shlex.split(segment) if segment.strip() else []
            if parts[:1] == ["tar"] and len(parts) > 2 and parts[1] in ("-cf", "-czf"):
                self.files[parts[2]] = b"an archive of " + " ".join(parts[3:]).encode("utf-8")
        if words[:2] == ["rm", "-f"]:
            for path in words[2:]:
                self.files.pop(path, None)
        return 0, ""

    def stream(self, command: str):
        """`cat X` streams X out; `cat > X` takes what is written and keeps it as X."""
        self.ran.append(command)
        words = shlex.split(command.split("&&")[-1])
        if words[:1] == ["cat"] and ">" in words:
            return _Stream(self, keep_as=words[words.index(">") + 1])
        if words[:1] == ["cat"]:
            return _Stream(self, sends=self.files.get(words[1], b""))
        code, out = next((answer for fragment, answer in self.answers.items() if fragment in command),
                         (0, ""))
        return _Stream(self, sends=out.encode("utf-8"), exit_code=code)


class _Channel:
    def __init__(self, exit_code: int) -> None:
        self._exit_code = exit_code

    def shutdown_write(self):
        return None

    def recv_exit_status(self):
        return self._exit_code


class _Stream:
    """One command's (stdin, stdout, stderr), as `open_stream` hands them to a caller."""

    def __init__(self, host: _Host, *, sends: bytes = b"", keep_as: str = "", exit_code: int = 0):
        self._host, self._keep_as, self._written = host, keep_as, io.BytesIO()
        channel = _Channel(exit_code)
        self.stdin = SimpleNamespace(write=self._write, flush=lambda: None, close=self._close,
                                     channel=channel)
        self.stdout = io.BytesIO(sends)
        self.stdout.channel = channel
        self.stderr = io.BytesIO(b"")

    def _write(self, chunk):
        # paramiko's channel file takes text too, and encodes it: a caller writes file names so.
        self._written.write(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)

    def _close(self):
        if self._keep_as:
            self._host.files[self._keep_as] = self._written.getvalue()

    def as_tuple(self):
        return self.stdin, self.stdout, self.stderr


class _Sftp:
    """SFTP over the host's files. A directory is any prefix of a file, or one made here."""

    def __init__(self, host: _Host) -> None:
        self._host = host
        self._dirs: set[str] = {"/"}

    def _is_dir(self, path: str) -> bool:
        path = path.rstrip("/") or "/"
        return path in self._dirs or any(name.startswith(path + "/") for name in self._host.files)

    def stat(self, path):
        path = str(path)
        if path in self._host.files:
            return SimpleNamespace(st_size=len(self._host.files[path]), st_mtime=1_790_000_000,
                                   st_atime=1_790_000_000, st_mode=0o100644)
        if self._is_dir(path):
            return SimpleNamespace(st_size=0, st_mtime=1_790_000_000, st_atime=1_790_000_000,
                                   st_mode=0o040755)
        raise FileNotFoundError(path)

    def listdir_attr(self, path):
        base = str(path).rstrip("/")
        names = {name[len(base) + 1:].split("/")[0] for name in self._host.files
                 if name.startswith(base + "/")}
        return [SimpleNamespace(filename=name, **vars(self.stat(f"{base}/{name}"))) for name in sorted(names)]

    def mkdir(self, path, mode=0o777):
        self._dirs.add(str(path).rstrip("/"))

    def get(self, remote, local):
        Path(local).write_bytes(self._host.files[str(remote)])

    def put(self, local, remote):
        self._host.files[str(remote)] = Path(local).read_bytes()

    def putfo(self, reader, remote, file_size=0, **_kwargs):
        self._host.files[str(remote)] = reader.read()

    def open(self, path, mode="r"):
        path = str(path)
        if "w" in mode:
            return _Writer(self._host, path)
        return io.BytesIO(self._host.files[path])

    def remove(self, path):
        self._host.files.pop(str(path), None)

    def rename(self, old, new):
        self._host.files[str(new)] = self._host.files.pop(str(old))

    posix_rename = rename

    def utime(self, path, times):
        return None

    def chmod(self, path, mode):
        return None

    def close(self):
        return None


class _Writer(io.BytesIO):
    """A file opened for writing: what is written lands on the host when it is closed."""

    def __init__(self, host: _Host, path: str) -> None:
        super().__init__()
        self._host, self._path = host, path

    def write(self, data):
        # As paramiko's SFTPFile does: text is written as its UTF-8 bytes.
        return super().write(data.encode("utf-8") if isinstance(data, str) else data)

    def close(self):
        if not self.closed:
            self._host.files[self._path] = self.getvalue()
        super().close()


@pytest.fixture
def host(monkeypatch) -> _Host:
    fake = _Host()

    def run(session, command, **_kwargs):
        return fake.result(session, command if isinstance(command, str) else " ".join(map(str, command)))

    def run_script(session, script, **_kwargs):
        return fake.result(session, script.read_text(encoding="utf-8") if isinstance(script, Path) else str(script))

    for transport in (remote_exec.SshSession, remote_exec.WinrmSession):
        monkeypatch.setattr(transport, "run", run)
        monkeypatch.setattr(transport, "run_script", run_script)
    # Connected, as far as a caller that asks can tell: nothing is dialled.
    monkeypatch.setattr(remote_exec.SshSession, "client", property(lambda session: object()))
    sftp = _Sftp(fake)
    monkeypatch.setattr(remote_exec.SshSession, "sftp", lambda session: sftp)
    monkeypatch.setattr(remote_exec.SshSession, "open_stream",
                        lambda session, command, **_kwargs: fake.stream(command).as_tuple())
    return fake


@pytest.fixture(autouse=True)
def _nothing_configured(monkeypatch, tmp_path):
    """An empty tool root: an operation answers from its request (R09), so nothing here is read."""
    (tmp_path / "data").mkdir()
    monkeypatch.setenv("DB_OPS_HOME", str(tmp_path))
    monkeypatch.setenv("DB_OPS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)


def _answer(monkeypatch, capsys, command: str, request: dict) -> dict:
    """Run one command as its caller does - the request on stdin - and read the envelope."""
    capsys.readouterr()
    # Bytes underneath, as a real stdin: the reader takes `sys.stdin.buffer`, not the text layer.
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(request).encode("utf-8")),
                                                       encoding="utf-8"))
    cli.main([command, "-"])
    out = capsys.readouterr().out
    body, _end = json.JSONDecoder().raw_decode(out[out.index("{"):])
    return body


def _described() -> dict[str, set[str]]:
    reference = json.loads((REPO / "db_ops" / "common" / "catalogue" / "shared_config_objects.json")
                           .read_text(encoding="utf-8"))
    return {command: {field["field"] for field in entry["fields"]}
            for entry in reference["shared_config_objects"] if entry.get("kind") == "output"
            for command in entry.get("commands") or []}


def _holds(body: dict, command: str) -> None:
    """The run succeeded, and every key it answered is one the reference describes."""
    assert body.get("success") is True, f"{command}: {body.get('error') or body.get('message')}"
    assert isinstance(body.get("data"), dict), f"{command}: no data object"
    undescribed = set(body["data"]) - _described()[command]
    assert not undescribed, f"{command} answers keys the reference does not describe: {undescribed}"


# --------------------------------------------------------------------------- #
# What the requests state (lib.data_sources.request_fill finishes them so in the apps)
# --------------------------------------------------------------------------- #
_SQL = {"db_type": "sqlserver", "host": "192.0.2.10", "port": 1433, "username": "u", "password": "p",
        "server_id": "LAB"}
_RULES = {"level": 10, "confirmations": 0, "challenge": "", "effects": []}
_CONFIRMED = {"confirm": True, "assume_yes": True, "authorized_by": "guard", "reason": "the R16 guard"}
_EMERGENCY = {"target": "LAB", "connection": _SQL, "rules": _RULES, **_CONFIRMED}
_DATABASE_COLUMNS = ["name", "database_id", "state", "recovery_model", "compatibility_level",
                     "collation", "is_read_only", "is_system", "has_access"]
_DATABASES = [("master", 1, "ONLINE", "SIMPLE", 160, "SQL_Latin1_General_CP1_CI_AS", 0, 1, 1),
              ("APPDB", 5, "ONLINE", "FULL", 160, "SQL_Latin1_General_CP1_CI_AS", 0, 0, 1)]
_JOB = (["name", "enabled", "category", "owner", "description", "source"],
        [("nightly", 1, "Database Maintenance", "sa", "", "agent")])
_SSH = {"method": "ssh", "host": "192.0.2.20", "port": 22, "username": "u", "password": "p",
        "auth_type": "password", "timeout_seconds": 3}
_WINRM = {"method": "winrm", "host": "192.0.2.21", "port": 5985, "username": "u", "password": "p",
          "platform": "windows", "timeout_seconds": 3}
#: A Linux host's facts, as `host_ops`' script prints them: one tab-separated fact a line.
_LINUX_FACTS = "\n".join([
    "hostname\tpg01", "os\tUbuntu 24.04 LTS", "remote_time\t2026-09-28T09:00:00",
    "uptime_seconds\t86400", "last_boot\t2026-09-27 09:00:00", "is_admin\tfalse",
    "disk\t/\t41943040\t104857600", "service\tpostgresql\tactive\tenabled"])
#: A Windows host's facts and the SQL Server setup probe, as the two PowerShell scripts answer.
_WINDOWS_FACTS = json.dumps({
    "whoami": "APPDB-DB\\admin", "is_admin": True, "hostname": "APPDB-DB",
    "os": "Microsoft Windows Server 2019 Standard", "last_boot": "2026-09-27T20:06:56",
    "uptime_days": 0.5, "remote_time": "2026-09-28T08:00:00",
    "disks": [{"mount": "C:", "free_gb": 42.5, "total_gb": 120.0}],
    "services": [{"name": name, "status": "Running", "start_type": "Automatic"}
                 for name in ("MSSQL$APPDB", "SQLAgent$APPDB", "SQLBrowser")],
    "reboot_pending": {"required": False, "reasons": [], "pending_file_rename_count": 0}})
_SETUP_PROBE = json.dumps({"registry_version": "16.0.1000.6", "registry_patch_level": "16.0.4265.3",
                           "registry_edition": "Developer Edition", "setup_running": ""})
_PATCH_SERVER = (["server_name", "instance_name", "product_version", "product_level", "update_level",
                  "update_reference", "edition", "is_clustered", "is_hadr"],
                 [("APPDB-DB\\APPDB", "APPDB", "16.0.4265.3", "RTM", "CU26", "KB5093420",
                   "Developer Edition (64-bit)", "0", "0")])
_INSTANCE_SERVER = (["build", "edition", "engine_edition", "collation", "machine_name", "instance_name",
                     "windows_auth_only", "version_text"],
                    [("16.0.4265.3", "Developer Edition (64-bit)", "3", "SQL_Latin1_General_CP1_CI_AS",
                      "APPDB-DB", "APPDB", "0", "Microsoft SQL Server 2022")])


# --------------------------------------------------------------------------- #
# The database commands
# --------------------------------------------------------------------------- #
def test_run_sql(server, monkeypatch, capsys):
    server.answers["select 1 as n"] = (["n"], [(1,)])
    _holds(_answer(monkeypatch, capsys, "run-sql", {"connection": _SQL, "sql": "select 1 as n"}), "run-sql")


def test_list_databases(server, monkeypatch, capsys):
    server.answers["FROM sys.databases d"] = (_DATABASE_COLUMNS, _DATABASES)
    body = _answer(monkeypatch, capsys, "list-databases", {"connection": _SQL})
    _holds(body, "list-databases")
    assert [db["name"] for db in body["data"]["databases"]] == ["APPDB"], "system databases hidden"


def test_list_schemas_answers_instead_of_raising_on_its_own_message(server, monkeypatch, capsys):
    """Its message read `data['database']` after 0.22.0 renamed the key `database_name`: every
    success came back as `KeyError: 'database'` - the Telegram upload's schema prompt among them."""
    server.answers["FROM sys.schemas s"] = (["name", "owner", "schema_id"], [("sales", "dbo", 5)])
    body = _answer(monkeypatch, capsys, "list-schemas", {"connection": _SQL, "database_name": "APPDB"})
    _holds(body, "list-schemas")
    assert "in APPDB on LAB" in body["message"]


def test_list_jobs(server, monkeypatch, capsys):
    server.answers["FROM msdb.dbo.sysjobs AS j"] = _JOB
    _holds(_answer(monkeypatch, capsys, "list-jobs", {"connection": _SQL}), "list-jobs")


def test_trace_session(server, monkeypatch, capsys):
    _holds(_answer(monkeypatch, capsys, "trace-session", {"connection": _SQL, "session_id": 55}),
           "trace-session")


def test_shrink_log(server, monkeypatch, capsys):
    server.answers["CROSS JOIN (SELECT log_reuse_wait_desc"] = (
        ["name", "size_mb", "used_mb", "physical_name", "log_reuse_wait_desc", "recovery_model_desc"],
        [("APPDB_log", 1024.0, 10.0, "/var/opt/mssql/data/APPDB_log.ldf", "NOTHING", "SIMPLE")])
    server.answers["WHERE f.name ="] = (["size_mb", "used_mb"], [(64.0, 1.0)])
    body = _answer(monkeypatch, capsys, "shrink-log", {**_EMERGENCY, "database_name": "APPDB", "size_mb": 64})
    _holds(body, "shrink-log")
    assert any("DBCC SHRINKFILE" in sql for sql in server.executed)


def test_kill_spid(server, monkeypatch, capsys):
    server.answers["FROM sys.dm_exec_sessions AS s"] = (
        ["login", "host", "program", "status", "open_tran", "idle_s", "db", "blocking", "tran_age"],
        [("app", "web01", "app.exe", "sleeping", 0, 30, "APPDB", 0, -1)])
    _holds(_answer(monkeypatch, capsys, "kill-spid", {**_EMERGENCY, "spid": 55}), "kill-spid")
    assert any(sql.strip().startswith("KILL 55") for sql in server.executed)


def test_start_job(server, monkeypatch, capsys):
    server.answers["OUTER APPLY"] = (["enabled", "last_start", "running", "last_stop"],
                                     [(1, "2026-09-27 01:00:00", 0, "2026-09-27 01:05:00")])
    _holds(_answer(monkeypatch, capsys, "start-job", {**_EMERGENCY, "job_name": "nightly"}), "start-job")


def test_disable_job(server, monkeypatch, capsys):
    server.answers["FROM msdb.dbo.sysjobs AS j"] = _JOB
    _holds(_answer(monkeypatch, capsys, "disable-job", {**_EMERGENCY, "job_name": "nightly"}),
           "disable-job")


def test_create_table_from_xlsx(server, monkeypatch, capsys, tmp_path):
    sheet = tmp_path / "rows.csv"
    sheet.write_text("region,amount\nnorth,1\nsouth,2\n", encoding="utf-8")
    _holds(_answer(monkeypatch, capsys, "create-table-from-xlsx", {
        "connection": _SQL, "database_name": "APPDB", "table_name": "sales", "file_path": str(sheet)}),
        "create-table-from-xlsx")


def test_verify_restore(server, monkeypatch, capsys):
    server.answers["FROM sys.databases d"] = (_DATABASE_COLUMNS, _DATABASES)
    server.answers["sys.tables"] = (["n"], [(12,)])
    _holds(_answer(monkeypatch, capsys, "verify-restore", {
        "db_type": "sqlserver", "target": {"host": "192.0.2.10", "username": "u", "password": "p"},
        "database_names": ["APPDB"]}), "verify-restore")


def _a_patchable_instance(server: _Server, host: _Host) -> None:
    """An instance a CU may go onto: standalone, sysadmin, backed up, idle - and its host healthy."""
    server.answers["SERVERPROPERTY('ProductUpdateLevel')"] = _PATCH_SERVER
    server.answers["IS_SRVROLEMEMBER('sysadmin')"] = (["is_sysadmin"], [(1,)])
    server.answers["HAS_DBACCESS"] = (_DATABASE_COLUMNS, _DATABASES)
    server.answers["msdb.dbo.backupset"] = (["name", "last_full", "age_hours"],
                                            [("APPDB", "2026-09-28 01:00:00", 2.0)])
    server.answers["user_sessions"] = (["user_sessions"], [(0,)])
    host.answers["registry_patch_level"] = (0, _SETUP_PROBE)
    host.answers["pending_file_rename_count"] = (0, _WINDOWS_FACTS)


def test_sqlserver_precheck(server, host, monkeypatch, capsys):
    _a_patchable_instance(server, host)
    _holds(_answer(monkeypatch, capsys, "sqlserver-precheck", {
        "target": "LAB", "access": _WINRM, "connection": _SQL, "policy": {}, "kb": "KB5093420"}),
        "sqlserver-precheck")


def test_sqlserver_apply_cu(server, host, monkeypatch, capsys):
    _a_patchable_instance(server, host)
    # The staged update, as the probe reports it: there, Microsoft's, signed.
    host.answers["registry_patch_level"] = (0, json.dumps({
        **json.loads(_SETUP_PROBE), "installer_exists": True, "installer_size": 734003200,
        "installer_modified": "2026-09-20T10:00:00", "installer_product_version": "16.0.4265.3",
        "installer_signature_status": "Valid",
        "installer_signer": "CN=Microsoft Corporation, O=Microsoft Corporation"}))
    _holds(_answer(monkeypatch, capsys, "sqlserver-apply-cu", {
        "target": "LAB", "access": _WINRM, "connection": _SQL, "policy": {}, "rules": _RULES,
        "kb": "KB5093420", "installer": r"C:\cu\SQLServer2022-KB5093420-x64.exe", "skip_hash": True,
        "dry_run": True, **_CONFIRMED}), "sqlserver-apply-cu")


def test_sqlserver_verify_build(server, host, monkeypatch, capsys):
    _a_patchable_instance(server, host)
    _holds(_answer(monkeypatch, capsys, "sqlserver-verify-build", {
        "target": "LAB", "access": _WINRM, "connection": _SQL, "expected_build": "16.0.4265.3",
        "policy": {"sql_reconnect_timeout_seconds": 3, "connect_timeout_seconds": 3}}),
        "sqlserver-verify-build")


def _instance_policy() -> dict:
    shipped = json.loads((REPO / "db_ops" / "common" / "catalogue" / "sqlserver_instance_policy.json")
                         .read_text(encoding="utf-8"))
    return shipped.get("sqlserver_instance_policy", shipped)


def test_sqlserver_export_verify_and_replay_instance(server, monkeypatch, capsys, tmp_path):
    """Export writes the bundle the other two read, so one test runs all three, in that order."""
    server.answers["SERVERPROPERTY('EngineEdition')"] = _INSTANCE_SERVER
    server.answers["COUNT(*) AS n"] = (["n"], [(0,)])
    bundle = tmp_path / "bundle"
    for command, request in (
        ("sqlserver-export-instance", {"output_dir": str(bundle)}),
        ("sqlserver-verify-instance", {"bundle_dir": str(bundle)}),
        ("sqlserver-replay-instance", {"bundle_dir": str(bundle), "secrets": {}, "dry_run": True,
                                       "rules": _RULES, **_CONFIRMED}),
    ):
        _holds(_answer(monkeypatch, capsys, command, {
            "target": "LAB", "connection": _SQL, "policy": _instance_policy(), **request}), command)


# --------------------------------------------------------------------------- #
# The host commands
# --------------------------------------------------------------------------- #
def test_host_facts(host, monkeypatch, capsys):
    host.answers["uptime_seconds"] = (0, _LINUX_FACTS)
    _holds(_answer(monkeypatch, capsys, "host-facts", {"target": "LAB", "access": _SSH, "policy": {}}),
           "host-facts")


def test_host_service(host, monkeypatch, capsys):
    host.answers["uptime_seconds"] = (0, _LINUX_FACTS)
    _holds(_answer(monkeypatch, capsys, "host-service", {
        "target": "LAB", "access": _SSH, "policy": {}, "action": "status", "services": ["postgresql"],
        "rules": _RULES, **_CONFIRMED}), "host-service")


def test_host_restart(host, monkeypatch, capsys):
    host.answers["uptime_seconds"] = (0, _LINUX_FACTS)
    _holds(_answer(monkeypatch, capsys, "host-restart", {
        "target": "LAB", "access": _SSH, "policy": {}, "rules": _RULES, "dry_run": True, **_CONFIRMED}),
        "host-restart")


# --------------------------------------------------------------------------- #
# Moving files
# --------------------------------------------------------------------------- #
_LOGIN = {"host": "192.0.2.20", "username": "u", "password": "p"}


def test_fetch_file(host, monkeypatch, capsys, tmp_path):
    host.files["/backup/APPDB_full.bak"] = b"a full backup"
    _holds(_answer(monkeypatch, capsys, "fetch-file", {
        "target": "LAB", "access": _SSH, "remote_path": "/backup/APPDB_full.bak",
        "local_path": str(tmp_path / "in" / "APPDB_full.bak")}), "fetch-file")
    assert (tmp_path / "in" / "APPDB_full.bak").read_bytes() == b"a full backup"


def test_send_file(host, monkeypatch, capsys, tmp_path):
    (tmp_path / "APPDB_full.bak").write_bytes(b"a full backup")
    _holds(_answer(monkeypatch, capsys, "send-file", {
        "target": "LAB", "access": _SSH, "local_path": str(tmp_path / "APPDB_full.bak"),
        "remote_path": "/restore/APPDB_full.bak"}), "send-file")
    assert host.files["/restore/APPDB_full.bak"] == b"a full backup"


def test_relay_file(host, monkeypatch, capsys):
    host.files["/backup/APPDB_full.bak"] = b"a full backup"
    _holds(_answer(monkeypatch, capsys, "relay-file", {
        "source": {"target": "A", "access": _SSH, "path": "/backup/APPDB_full.bak"},
        "destination": {"target": "B", "access": _SSH, "path": "/restore/APPDB_full.bak"}}), "relay-file")
    assert host.files["/restore/APPDB_full.bak"] == b"a full backup"


def test_pack_files(host, monkeypatch, capsys):
    host.files["/backup/a.bkp"] = b"piece a"
    _holds(_answer(monkeypatch, capsys, "pack-files", {
        "target": "LAB", "access": _SSH, "folder": "/backup", "include": ["*.bkp"],
        "archive_path": "/stage/pieces.tar"}), "pack-files")


def test_pack_backup(host, monkeypatch, capsys):
    host.files["/backup/APPDB_full.bak"] = b"a full backup"
    _holds(_answer(monkeypatch, capsys, "pack-backup", {
        "host": _LOGIN, "files": ["/backup/APPDB_full.bak"], "archive_path": "/stage/APPDB.tar"}),
        "pack-backup")


def test_pull_file(host, monkeypatch, capsys, tmp_path):
    host.files["/stage/APPDB.tar"] = b"an archive"
    _holds(_answer(monkeypatch, capsys, "pull-file", {
        "host": _LOGIN, "remote_path": "/stage/APPDB.tar", "local_path": str(tmp_path / "APPDB.tar")}),
        "pull-file")


def test_push_file(host, monkeypatch, capsys, tmp_path):
    (tmp_path / "APPDB.tar").write_bytes(b"an archive")
    _holds(_answer(monkeypatch, capsys, "push-file", {
        "host": _LOGIN, "remote_path": "/restore/APPDB.tar", "local_path": str(tmp_path / "APPDB.tar")}),
        "push-file")


# --------------------------------------------------------------------------- #
# A restore's copy between hosts
# --------------------------------------------------------------------------- #
_STAGED = "/opt/db_ops/backup/PG_LAB_A"


def _a_backup_directory(host: _Host) -> None:
    host.files[f"{_STAGED}/base/20260925T004710Z_FULL/backup_label"] = b"a base backup"
    host.files[f"{_STAGED}/wal/000000010000000000000001"] = b"a WAL segment"
    # What `backup-chain` lists: an unknown chain is refused now, not copied whole (review 0.25.0, G2.9).
    host.answers["ls -1d"] = (0, f"{_STAGED}/base/20260925T004710Z_FULL\n")
    # What the copy asks its target before the first file moves (0.26.0): room for the files, x2.
    host.answers["df -Pk"] = (0, "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                                 "/dev/sda1 104857600 1048576 103809024 1% /\n")


def test_backup_chain(host, monkeypatch, capsys):
    _a_backup_directory(host)
    _holds(_answer(monkeypatch, capsys, "backup-chain", {
        "db_type": "postgresql", "source": _LOGIN, "source_dir": _STAGED}), "backup-chain")


def test_copy_backup_dir(host, monkeypatch, capsys):
    _a_backup_directory(host)
    _holds(_answer(monkeypatch, capsys, "copy-backup-dir", {
        "source": _LOGIN, "source_dir": _STAGED, "target": _LOGIN,
        "target_dir": "/opt/db_ops/backup/pg_restore_from_a"}), "copy-backup-dir")


def test_prune_staged_backups(host, monkeypatch, capsys):
    _a_backup_directory(host)
    _holds(_answer(monkeypatch, capsys, "prune-staged-backups", {
        "target": _LOGIN, "target_dir": _STAGED, "cleanup_retention": 86400}),
        "prune-staged-backups")


def test_move_db_docker(host, monkeypatch, capsys):
    # The instance, as the source's Docker describes it: one container, its volume, port and net.
    host.answers.update({
        "docker ps -a --filter": (0, "pg_lab_a-db-1\n"),
        "{{.Config.Image}}": (0, "postgres:18\n"),
        "{{range .Mounts}}": (0, "pg_lab_a_data \n"),
        "PortBindings": (0, "15432 \n"),
        "com.docker.compose.service": (0, "db\n"),
        "--size": (0, "1024\n"),
        "docker network ls": (0, "pg_lab_a_default\n"),
        "docker network inspect": (0, "10.77.1.0/24 \n"),
    })
    _holds(_answer(monkeypatch, capsys, "move-db-docker", {
        "name": "PG_LAB_A", "engine": "postgres",
        "source": {"label": "LAB-A", "host": "192.0.2.20", "port": 22, "username": "u", "password": "p"},
        "destination": {"label": "LAB-B", "host": "192.0.2.21", "port": 22, "username": "u",
                        "password": "p"}, "dry_run": True}),
        "move-db-docker")


def test_run_sqlcmd(host, monkeypatch, capsys):
    host.answers["sqlcmd"] = (0, "n\n-\n1\n\n(1 rows affected)\n")
    _holds(_answer(monkeypatch, capsys, "run-sqlcmd", {
        "sql": "SELECT 1 AS n", "instance": "localhost,1433", "username": "sa", "password": "p",
        "via": "ssh", "host": _LOGIN}), "run-sqlcmd")


# --------------------------------------------------------------------------- #
# A share: `smbclient` and `cmdkey` are this node's own processes, answered here
# --------------------------------------------------------------------------- #
#: A recursive `smbclient ls` after `cd "SQLBK"`: one file at the top, one a folder down.
_SMB_LS = "\n".join([
    "  .                                   D        0  Mon Sep 28 01:00:00 2026",
    "  APPDB_full.bak                      A  1048576  Mon Sep 28 01:00:00 2026",
    "",
    "\\SQLBK\\LOG",
    "  APPDB_log.trn                       A     2048  Mon Sep 28 01:15:00 2026",
    ""])


class _Process:
    """What `subprocess.Popen` hands `smb._smbclient`: an `ls` lists the share, anything else succeeds."""

    pid = 1
    returncode = 0

    def __init__(self, args, **_kwargs) -> None:
        self._stdout = _SMB_LS if args[-1].endswith("ls") else ""

    def communicate(self, timeout=None):
        return self._stdout, ""

    def kill(self):
        return None


@pytest.fixture
def smb_node(monkeypatch):
    """`smbclient` and `cmdkey`, faked where `common.smb` starts them - this node, not a host."""
    import subprocess

    from db_ops.common import smb

    ran: list[list[str]] = []

    def popen(args, **kwargs):
        ran.append(list(args))
        return _Process(args, **kwargs)

    def run(args, **_kwargs):
        ran.append(list(args))
        return subprocess.CompletedProcess(args, 0, "CMDKEY: Credential added successfully.", "")

    monkeypatch.setattr(smb, "subprocess", SimpleNamespace(
        Popen=popen, run=run, PIPE=subprocess.PIPE, TimeoutExpired=subprocess.TimeoutExpired,
        CompletedProcess=subprocess.CompletedProcess))
    return ran


_SHARE = {"host": "192.0.2.30", "share": "backup", "username": "LAB\\svc_backup", "password": "p",
          "backend": "smbclient"}


def test_smb_list(monkeypatch, capsys, smb_node):
    body = _answer(monkeypatch, capsys, "smb-list", {**_SHARE, "path": "SQLBK", "suffixes": [".bak", ".trn"]})
    _holds(body, "smb-list")
    assert sorted(item["path"] for item in body["data"]["files"]) == ["APPDB_full.bak", "LOG\\APPDB_log.trn"]


def test_smb_delete(monkeypatch, capsys, smb_node):
    body = _answer(monkeypatch, capsys, "smb-delete", {**_SHARE, "paths": ["SQLBK\\LOG\\APPDB_log.trn"]})
    _holds(body, "smb-delete")
    assert body["data"]["deleted"] == 1


def test_smb_credential(monkeypatch, capsys, smb_node):
    # On Windows it runs cmdkey (faked); elsewhere it answers that there is nothing to store.
    _holds(_answer(monkeypatch, capsys, "smb-credential",
                   {"target": "192.0.2.30", "username": "LAB\\svc_backup", "password": "p"}),
           "smb-credential")


# --------------------------------------------------------------------------- #
# The two commands with a server on both ends of a job the store or a plan decides
# --------------------------------------------------------------------------- #
def test_copy_schema(monkeypatch, capsys, server):
    """A plan: it reads only the source, so the fake answers the catalogue and nothing is written."""
    server.answers.update({
        "SCHEMA_ID(": (["id"], [(5,)]),
        "FROM sys.tables t": (["name"], [("config",)]),
        "FROM sys.columns c\n        JOIN sys.types ty": (
            ["name", "type_name", "max_length", "precision", "scale", "is_nullable", "is_identity",
             "is_rowguidcol", "collation_name", "computed_definition", "is_persisted", "seed_value",
             "increment_value", "default_name", "default_definition"],
            [("id", "int", 4, 10, 0, 0, 1, 0, None, None, None, 1, 1, None, None),
             ("value", "nvarchar", 200, 0, 0, 1, 0, 0, "SQL_Latin1_General_CP1_CI_AS", None, None,
              None, None, None, None)]),
    })
    source = {"target": "LAB-A", "database_name": "APPDB_TEST", "schema": "sched", "connection": _SQL}
    destination = {"target": "LAB-B", "database_name": "APPDB", "schema": "sched",
                   "connection": {**_SQL, "host": "192.0.2.11", "server_id": "LAB-B"}}
    body = _answer(monkeypatch, capsys, "copy-schema", {"source": source, "destination": destination})
    _holds(body, "copy-schema")
    assert body["data"]["plan"]["tables"] == ["config"]


def test_rotate_password(monkeypatch, capsys, server, tmp_path):
    """Its job is the store (R09's first kind), so it reads a real one - on this test's own root: the
    inventory names the login, `users.json` its ref, and the encrypted store its current password.
    A dry run proves the password on the (fake) server and changes nothing."""
    from db_ops.lib import data_sources, secret_text

    data = tmp_path / "data"
    # The module-level default is the checkout's own data/; it must never be the one read here.
    monkeypatch.setattr(data_sources, "DEFAULT_DATA_DIR", data)
    monkeypatch.setenv("DB_OPS_SECRET_KEY", "a key for this test only")
    (data / "db_instances.json").write_text(json.dumps({"db_instances": [{
        "server_id": "LAB", "db_type": "sqlserver", "ip": "192.0.2.10", "port": 1433,
        "instance_name": "MSSQLSERVER", "default_credential_name": "lab_dba", "active": True}]}),
        encoding="utf-8")
    (data / "users.json").write_text(json.dumps({"database_credentials": [{
        "server_id": "LAB", "db_type": "sqlserver", "instance_name": "MSSQLSERVER",
        "credentials": [{"credential_name": "lab_dba", "username": "dba", "password_ref": "LAB_DBA"}]}]}),
        encoding="utf-8")
    secret_text.set_secret_text(data, "LAB_DBA", "the current password")

    body = _answer(monkeypatch, capsys, "rotate-password", {"refs": ["LAB_DBA"], "dry_run": True})
    _holds(body, "rotate-password")
    assert [item["status"] for item in body["data"]["results"]] == ["READY"]


#: Every command a test here answers. The R09 guard's baseline (ANSWER_NOT_YET_SEEN) is what
#: neither its runs nor these answer - it reads this set.
ANSWERED_WITH_A_FAKE_SERVER = frozenset({
    "run-sql", "list-databases", "list-schemas", "list-jobs", "trace-session", "shrink-log",
    "kill-spid", "start-job", "disable-job", "create-table-from-xlsx", "verify-restore",
    "sqlserver-precheck", "sqlserver-apply-cu", "sqlserver-verify-build", "sqlserver-export-instance",
    "sqlserver-verify-instance", "sqlserver-replay-instance", "host-facts", "host-service",
    "host-restart", "fetch-file", "send-file", "relay-file", "pack-files", "pack-backup",
    "pull-file", "push-file", "backup-chain", "copy-backup-dir", "prune-staged-backups",
    "move-db-docker", "run-sqlcmd", "smb-list", "smb-delete", "smb-credential", "copy-schema",
    "rotate-password",
})


def test_every_command_named_answered_is_answered_by_a_test_here():
    """The set above is what the R09 guard subtracts; a name in it with no test would hide a command."""
    tested = {name[len("test_"):].split("_answers")[0].replace("_", "-")
              for name in globals() if name.startswith("test_") and name != _THIS}
    tested |= {"sqlserver-export-instance", "sqlserver-verify-instance", "sqlserver-replay-instance"} \
        if "test_sqlserver_export_verify_and_replay_instance" in globals() else set()
    assert ANSWERED_WITH_A_FAKE_SERVER <= tested, sorted(ANSWERED_WITH_A_FAKE_SERVER - tested)


_THIS = "test_every_command_named_answered_is_answered_by_a_test_here"
