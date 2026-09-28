"""A `common.cli` command works from its request alone - so it runs by hand on an empty configuration.

The rule (R09 in docs/rules.md), as the operator put it on 2026-09-25: `common.cli` takes a JSON
request, does its work **entirely from that request**, and answers in JSON - so that it can be run
by hand on a node whose configuration is completely empty. Restated on 2026-09-26, it has two
kinds of command and no third: **a command whose job is reading or writing a configuration file
may do so** - registering a task, storing a secret, checking the reference, reporting this node -
and it too runs on an empty one: it creates the file, or says what is not configured yet; **a
command that takes input, works and answers never looks anything up in configuration**.

A scan of the imports cannot see that, so this guard runs the real CLI, in a subprocess, against two
roots that hold nothing a command could use:

* an **empty** root - no `config.json`, an empty `data/`;
* a **poisoned** root - `config.json` and every `data/*.json` present and unreadable (`{poison`). A
  loader that reads a missing file as empty hides a read on the empty root; here any read fails.

Every command is in one of three classes, and the default - a command named nowhere - is the
strictest: it works from the request.

Writing it found four commands answering a broken configuration with a traceback instead of the
envelope (`list-targets`, `sql-command-add`, `sql-target-add`, `lift-example`), three registrars
refusing an empty install because the file they were about to write did not exist yet (`add-sql`,
`sql-command-add`, `sql-target-add`), and `inventory-summary` answering `[Errno 2]`.
"""

from __future__ import annotations

import concurrent.futures
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
POISON = "{poison"
#: What a JSON loader says about the poison. An answer carrying it read a configuration file.
POISON_READ = "Expecting property name enclosed in double quotes"


def _commands() -> list[str]:
    spec = importlib.util.spec_from_file_location("contract", REPO / "tests" / "test_common_cli_json_contract.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return sorted(set(module.ALL_COMMANDS) | set(module.STDIN_ONLY_COMMANDS))


COMMANDS = _commands()

#: Commands whose job is reading or writing a configuration file: they register, edit, store,
#: check, list or render it, so the file is the work. They still run on an empty configuration.
#: Every other command is an operation and is held to its request alone.
CONFIGURATION_IS_ITS_JOB = frozenset({
    "add-sql", "metric-toggle", "instance-add", "remote-credential-add", "sql-command-add",
    "sql-target-add", "app-command-set", "secret-set", "rotate-password", "check-secret",
    "check-secret-literals", "check-identifiers", "check-objects", "check-references",
    "describe-object", "standardize-field-names", "upgrade-config", "list-targets",
    "inventory-summary", "build-showcase",
    # 0.24.0: the tool root's own commands, moved from the root package (rules R41). Creating the
    # configuration, printing its guide, encrypting its store, carrying it to another machine.
    "init", "guide", "encrypt-secret", "export-data", "import-data",
    # 0.24.0: checking the configuration's logins is reading it (rules R41 moved it here).
    "check-credentials",
    # 0.24.0, reviewed against the restated rule (2026-09-26): each of these writes a configuration
    # file and nothing else - `metric-severity` a remap into db_instances.json, `lift-example` a
    # data/*.example.json from its source, refused when a real name would cross.
    "metric-severity", "lift-example",
    # 0.24.0, the same review: it reports this node - what app_commands.json schedules here - so
    # its configuration is part of what it answers.
    "self-status",
})

#: `init` writes a whole tool root into the directory it is given. Its runs here each get a directory
#: of their own, so the roots every other command reads stay as empty (or as broken) as they were.
_OWN_DIRECTORY = {"init": {"root": "init_bare_root"}}

#: Operations that cannot yet be run from a complete request: they look a fact up in the data
#: folder that the request could state. The debt of R09 - it may only shrink (STILL_LOOKS_UP_AT_0_24_0).
STILL_LOOKS_UP: dict[str, str] = {
    # Empty since 0.24.0 - rule R09 holds without exception. The apps finish every request before
    # they call (`lib.data_sources.request_fill`): the login as `connection` / `access`, the
    # maintenance or instance `policy`, the confirmation `rules`, a replay's `secrets`.
}

#: The debt as written down on 2026-09-25 - a literal, so a command added back to the list above has
#: something to be compared with.
STILL_LOOKS_UP_AT_0_24_0 = frozenset({
    "authorize", "sqlserver-export-instance", "sqlserver-replay-instance", "metric-severity",
    "lift-example", "run-cmd", "host-facts", "host-service", "host-restart", "shrink-log",
    "kill-spid", "start-job", "disable-job", "sqlserver-precheck", "sqlserver-apply-cu",
    "sqlserver-verify-build", "list-databases", "list-schemas", "list-jobs", "db-status",
    "create-table-from-xlsx", "copy-schema", "trace-session",
})

WORKS_FROM_THE_REQUEST = frozenset(COMMANDS) - CONFIGURATION_IS_ITS_JOB - frozenset(STILL_LOOKS_UP)


# --------------------------------------------------------------------------- #
# Running the real CLI against a root with nothing in it
# --------------------------------------------------------------------------- #
def _make_root(base: Path, kind: str) -> Path:
    root = base / kind
    (root / "data").mkdir(parents=True)
    if kind == "poisoned":
        (root / "config.json").write_text(POISON, encoding="utf-8")
        for name in os.listdir(REPO / "data"):
            if name.endswith(".json"):
                (root / "data" / name).write_text(POISON, encoding="utf-8")
    return root


def _run(root: Path, command: str, request: dict, *, key: str = "") -> dict:
    env = {name: value for name, value in os.environ.items() if not name.startswith("DB_OPS")}
    env.update(DB_OPS_HOME=str(root), DB_OPS_DATA_DIR=str(root / "data"), PYTHONPATH=str(REPO),
               PYTHONIOENCODING="utf-8")
    if key:
        env["DB_OPS_SECRET_KEY"] = key
    process = subprocess.run(
        [sys.executable, "-m", "db_ops.common.cli", command, "-"], input=json.dumps(request),
        capture_output=True, text=True, encoding="utf-8", cwd=root, env=env, timeout=120)
    try:
        body = json.loads(process.stdout)
    except json.JSONDecodeError:
        body = None
    return {"code": process.returncode, "body": body, "stderr": process.stderr[-2000:]}


def _said(result: dict) -> str:
    body = result["body"] or {}
    return f"{body.get('error') or ''} {body.get('message') or ''} {result['stderr']}"


def _cases(work: Path) -> dict[str, tuple[str, dict, str]]:
    """Complete requests that need no host: (command, request, expected). ``ok`` must succeed;
    ``reached`` must get as far as its own work - a connection refused, not a file read.

    Names paths under ``work`` and writes nothing: the parametrize below calls it at collection time
    with the working directory, and :func:`_prepare_work` makes the files where the runs happen."""
    backups = work / "backups"
    return {
        "due-check": ("due-check", {"time_window": {"repeat_interval": 60}}, "ok"),
        # The deploy's drift gate, answered unattended: the reply is in the request.
        "ask": ("ask", {"prompt": "  adopt / keep / abort ? ", "choices": ["adopt", "keep", "abort"],
                        "answer": "keep"}, "ok"),
        "list-backup-files": ("list-backup-files", {"db_type": "postgresql", "path": str(backups)}, "ok"),
        "create-db-docker": ("create-db-docker", {
            "name": "LAB_GUARD", "engine": "postgres", "version": "18", "mode": "single",
            "host": "192.0.2.10", "ssh_user": "u", "ssh_password": "p", "password": "x",
            "dry_run": True}, "ok"),
        "run-sql": ("run-sql", {
            "connection": {"db_type": "postgresql", "host": "127.0.0.1", "port": 1, "username": "u",
                           "password": "p", "database": "d"},
            "sql": "select 1", "connect_timeout_seconds": 3}, "reached"),
        "probe-host": ("probe-host", {"host": "127.0.0.1", "port": 1, "timeout_seconds": 2}, "reached"),
        # A key login states itself: the username and (here, the agent's) key. `sre` sends exactly
        # this, so a users.json it never needed must not be able to stop it.
        "run-cmd": ("run-cmd", {
            "access": {"method": "ssh", "host": "127.0.0.1", "port": 1, "username": "u",
                       "auth_type": "key", "timeout_seconds": 3},
            "command": "true", "confirm": True, "assume_yes": True}, "reached"),
        # The metrics app's execution: the target, its password and the items - an item that
        # cannot connect answers with its error, and the batch itself succeeds.
        "metric-batch": ("metric-batch", {
            "target": {"target_id": "LAB", "db_type": "postgresql", "host": "127.0.0.1", "port": 1,
                       "username": "u", "password": "p"},
            "items": [{"id": "X", "kind": "sql", "sql": "select 1", "timeout_seconds": 3}]}, "ok"),
        **_stated_cases(work),
        # A Windows share (0.24.0, R10): the host, the share and the login are the request. None
        # here, so the UNC way is taken and stores no login - smb-credential is left to its refusal,
        # because on a Windows machine a complete one would write to its credential manager.
        "smb-list": ("smb-list", {**_NO_SHARE, "path": "SQLBK"}, "reached:cannot be reached"),
        "smb-get": ("smb-get", {**_NO_SHARE, "remote_path": "SQLBK/x.bak",
                                "local_path": str(work / "fetched" / "x.bak")}, "ok"),
        "smb-delete": ("smb-delete", {**_NO_SHARE, "paths": ["SQLBK/old.trn"]}, "reached:1 failed"),
        # The file transfers (0.24.0): the host login is `access`, per side for a relay. Until then
        # a bare server_id was resolved against db_instances.json and users.json here.
        "fetch-file": ("fetch-file", {"target": "LAB", "access": _SSH, "remote_path": "/tmp/x.bkp",
                                      "local_path": str(work / "fetched" / "x.bkp")}, _SSH_REACHED),
        "send-file": ("send-file", {"target": "LAB", "access": _SSH, "local_path": str(work / "outgoing.bkp"),
                                    "remote_path": "/tmp/x.bkp"}, _SSH_REACHED),
        "pack-files": ("pack-files", {"target": "LAB", "access": _SSH, "folder": "/tmp",
                                      "include": ["*.bkp"], "archive_path": "/tmp/pieces.tar"},
                       _SSH_REACHED),
        "relay-file": ("relay-file", {
            "source": {"target": "A", "access": _SSH, "path": "/tmp/a.tar"},
            "destination": {"target": "B", "access": _SSH, "path": "/tmp/b.tar"}}, _SSH_REACHED),
    }


def _prepare_work(work: Path) -> None:
    """The files the cases name: a backup folder to list, a sheet to load, a file to send."""
    (work / "backups").mkdir(exist_ok=True)
    (work / "rows.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (work / "outgoing.bkp").write_bytes(b"not a backup")


#: What a request states when its app has finished it (`lib.data_sources.request_fill`): a SQL login,
#: a host login, the operation's price. Nothing listens on port 1, so each command gets exactly as
#: far as its own work - and the phrase after `reached:` is the proof it got there.
_SQL = {"db_type": "sqlserver", "host": "127.0.0.1", "port": 1, "username": "u", "password": "p",
        "server_id": "LAB"}
_SSH = {"method": "ssh", "host": "127.0.0.1", "port": 1, "username": "u", "auth_type": "key",
        "timeout_seconds": 3}
_WINRM = {"method": "winrm", "host": "127.0.0.1", "port": 1, "username": "u", "password": "p",
          "platform": "windows", "timeout_seconds": 3}
_RULES = {"level": 10, "confirmations": 0, "challenge": "", "effects": []}
#: A share nothing serves, reached the UNC way - a fetch that finds nothing is an answer (exit 1).
_NO_SHARE = {"host": "127.0.0.1", "share": "no_such_share", "backend": "unc", "timeout_seconds": 5}
_CONFIRMED = {"confirm": True, "assume_yes": True, "authorized_by": "guard", "reason": "the R09 guard"}
_SQL_REACHED = "reached:connect to 127.0.0.1:1"
_SSH_REACHED = "reached:SSH connection to 127.0.0.1:1"


def _stated_cases(work: Path) -> dict[str, tuple[str, dict, str]]:
    """The 22 commands that looked a fact up until 0.24.0, each with the request its app now sends."""
    sheet = work / "rows.csv"
    shipped = json.loads((REPO / "db_ops" / "common" / "catalogue" / "sqlserver_instance_policy.json")
                         .read_text(encoding="utf-8"))
    policy = shipped.get("sqlserver_instance_policy", shipped)
    emergency = {"target": "LAB", "connection": _SQL, "rules": _RULES, **_CONFIRMED}
    return {
        "authorize": ("authorize", {"operation": "kill-spid", "target_id": "LAB", "rules": _RULES,
                                    **_CONFIRMED}, "ok"),
        "shrink-log": ("shrink-log", {**emergency, "database_name": "d", "size_mb": 1}, _SQL_REACHED),
        "kill-spid": ("kill-spid", {**emergency, "spid": 55}, _SQL_REACHED),
        "start-job": ("start-job", {**emergency, "job_name": "j"}, _SQL_REACHED),
        "disable-job": ("disable-job", {**emergency, "job_name": "j"}, _SQL_REACHED),
        "list-databases": ("list-databases", {"connection": _SQL, "timeout_seconds": 3}, _SQL_REACHED),
        "list-schemas": ("list-schemas", {"connection": _SQL, "database_name": "d", "timeout_seconds": 3},
                         _SQL_REACHED),
        "list-jobs": ("list-jobs", {"connection": _SQL, "timeout_seconds": 3}, _SQL_REACHED),
        # An unreachable instance is an answer, not a failure: the status says so.
        "db-status": ("db-status", {"connection": _SQL, "timeout_seconds": 3}, "ok"),
        "create-table-from-xlsx": ("create-table-from-xlsx", {
            "connection": _SQL, "database_name": "d", "table_name": "t", "file_path": str(sheet),
            "timeout_seconds": 3}, _SQL_REACHED),
        "trace-session": ("trace-session", {"connection": _SQL, "spid": 55, "timeout_seconds": 3},
                          _SQL_REACHED),
        "copy-schema": ("copy-schema", {
            "source": {"connection": _SQL, "database_name": "d", "schema": "s"},
            "destination": {"connection": _SQL, "database_name": "d2"}, "mode": "plan",
            "timeout_seconds": 3, "lock_timeout_seconds": 1}, _SQL_REACHED),
        "host-facts": ("host-facts", {"target": "LAB", "access": _SSH, "policy": {}}, _SSH_REACHED),
        "host-service": ("host-service", {"target": "LAB", "access": _SSH, "policy": {}, "action": "status",
                                          "services": ["x"], "rules": _RULES, **_CONFIRMED}, _SSH_REACHED),
        "host-restart": ("host-restart", {"target": "LAB", "access": _SSH, "policy": {}, "rules": _RULES,
                                          "dry_run": True, **_CONFIRMED}, _SSH_REACHED),
        "sqlserver-precheck": ("sqlserver-precheck", {
            "target": "LAB", "access": _WINRM, "connection": _SQL, "policy": {}, "kb": "KB1"}, _SQL_REACHED),
        "sqlserver-apply-cu": ("sqlserver-apply-cu", {
            "target": "LAB", "access": _WINRM, "connection": _SQL, "policy": {}, "rules": _RULES, "kb": "KB1",
            "installer": "x.exe", "skip_hash": True, "dry_run": True, **_CONFIRMED}, _SQL_REACHED),
        "sqlserver-verify-build": ("sqlserver-verify-build", {
            "target": "LAB", "access": _WINRM, "connection": _SQL, "expected_build": "16.0.1",
            "policy": {"sql_reconnect_timeout_seconds": 3, "connect_timeout_seconds": 3}}, _SQL_REACHED),
        "sqlserver-export-instance": ("sqlserver-export-instance", {
            "target": "LAB", "connection": _SQL, "policy": policy, "output_dir": str(work / "bundle")},
            _SQL_REACHED),
        # The bundle is the replay's own input, named in the request: reading it is its work.
        "sqlserver-replay-instance": ("sqlserver-replay-instance", {
            "target": "LAB", "connection": _SQL, "policy": policy, "secrets": {},
            "bundle_dir": str(work / "no_bundle"), "dry_run": True, "rules": _RULES, **_CONFIRMED},
            "reached:manifest.json not found"),
    }


#: Registrars and readers of the configuration, each with a request that must SUCCEED on an empty
#: root - the file it writes is created, or it answers that nothing is configured yet.
_ON_AN_EMPTY_ROOT = {
    "sql-command-add": {"display_name": "count the drill", "db_type": "sqlserver", "sql_text": "select 1"},
    "instance-add": {"server_id": "LAB-192-0-2-10", "db_type": "sqlserver", "ip": "192.0.2.10", "port": 1433},
    "remote-credential-add": {"server_id": "LAB-192-0-2-10", "username": "tuser",
                              "password_ref": "LAB_OS_PASSWORD", "host": "192.0.2.10"},
    "secret-set": {"ref": "LAB_PASSWORD", "value": "not-a-real-password"},
    "list-targets": {}, "check-objects": {}, "check-references": {}, "describe-object": {},
    "standardize-field-names": {}, "upgrade-config": {}, "check-secret": {},
    # init creates the root it is given; guide prints this build's guide.
    "init": {"root": "fresh_root"}, "guide": {},
    # Nothing configured is nothing unresolvable: it checks zero targets and says so.
    "check-credentials": {},
    # This node, reported with nothing configured: the zone's default, no app scheduled.
    "self-status": {},
}
_TEST_KEY = "a-throwaway-passphrase-for-this-test-only"


@pytest.fixture(scope="module")
def answers(tmp_path_factory) -> dict:
    """Every run this file asserts on, made once and in parallel - about a hundred processes."""
    base = tmp_path_factory.mktemp("common_from_request")
    empty, poisoned = _make_root(base, "empty"), _make_root(base, "poisoned")
    work = base / "work"
    work.mkdir()
    _prepare_work(work)
    jobs = {("poisoned-bare", command): (poisoned, command, _OWN_DIRECTORY.get(command, {}), "")
            for command in COMMANDS}
    jobs.update({("empty-bare", command): (empty, command, _OWN_DIRECTORY.get(command, {}), _TEST_KEY)
                 for command in CONFIGURATION_IS_ITS_JOB})
    jobs.update({("case", name): (poisoned, command, request, "")
                 for name, (command, request, _expected) in _cases(work).items()})
    jobs.update({("empty-case", command): (empty, command, request, _TEST_KEY)
                 for command, request in _ON_AN_EMPTY_ROOT.items()})
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {key: pool.submit(_run, root, command, request, key=secret)
                   for key, (root, command, request, secret) in jobs.items()}
    return {key: future.result() for key, future in futures.items()}


# --------------------------------------------------------------------------- #
# The classes
# --------------------------------------------------------------------------- #
def test_every_class_names_only_commands_that_exist_and_no_command_twice():
    named = CONFIGURATION_IS_ITS_JOB | frozenset(STILL_LOOKS_UP)
    assert not (named - set(COMMANDS)), f"not a common.cli command: {sorted(named - set(COMMANDS))}"
    assert not (CONFIGURATION_IS_ITS_JOB & frozenset(STILL_LOOKS_UP))


def test_the_commands_still_looking_up_only_shrink():
    """A command that moves to working from its request leaves the list; none may join it."""
    assert frozenset(STILL_LOOKS_UP) <= STILL_LOOKS_UP_AT_0_24_0


def test_every_proven_case_is_a_command_that_works_from_its_request(tmp_path):
    assert {command for command, _request, _expected in _cases(tmp_path).values()} <= WORKS_FROM_THE_REQUEST


# --------------------------------------------------------------------------- #
# What every command must do with nothing configured
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("command", COMMANDS)
def test_a_broken_configuration_is_answered_in_the_envelope_never_a_traceback(answers, command):
    """R15 holds with the configuration broken: whatever went wrong comes back as JSON on stdout."""
    result = answers[("poisoned-bare", command)]
    assert result["body"] is not None and "success" in result["body"], (
        f"{command} answered a broken configuration with no envelope: {result['stderr'][-400:]}")


@pytest.mark.parametrize("command", sorted(WORKS_FROM_THE_REQUEST))
def test_an_operation_refuses_an_empty_request_without_opening_the_configuration(answers, command):
    """What is missing is in the request, so that is what the refusal names - not a file."""
    said = _said(answers[("poisoned-bare", command)])
    assert POISON_READ not in said, f"{command} read the configuration before its request: {said[:300]}"


@pytest.mark.parametrize("name", sorted(_cases(Path("."))))
def test_an_operation_runs_from_a_complete_request_on_a_broken_configuration(answers, tmp_path, name):
    """The rule itself: everything in the request, nothing read - so the poison is never touched."""
    command, request, expected = _cases(tmp_path)[name]
    if "'method': 'ssh'" in repr(request):
        # Without the [ssh] extra (the core install `ci` tests) an SSH operation stops at "paramiko
        # is required" - its refusal of a missing driver, before the work this case proves.
        pytest.importorskip("paramiko")
    result = answers[("case", name)]
    said = _said(result)
    assert result["body"] is not None, f"{command}: no envelope: {result['stderr'][-400:]}"
    assert POISON_READ not in said, f"{command} read the configuration: {said[:300]}"
    if expected == "ok":
        assert result["body"]["success"] is True, f"{command}: {said[:300]}"
    elif expected.startswith("reached:"):
        # Past every refusal of a missing field, into its own work.
        assert expected.split(":", 1)[1] in said, f"{command} stopped short of its work: {said[:300]}"


@pytest.mark.parametrize("command", sorted(_ON_AN_EMPTY_ROOT))
def test_a_command_that_edits_the_configuration_runs_on_an_empty_one(answers, command):
    """A registrar creates the file it writes; a reader answers that nothing is configured."""
    result = answers[("empty-case", command)]
    assert result["body"] is not None and result["body"]["success"] is True, (
        f"{command} refused an empty install: {_said(result)[:300]}")


@pytest.mark.parametrize("command", sorted(CONFIGURATION_IS_ITS_JOB))
def test_an_empty_configuration_is_said_in_words_not_as_a_raw_error(answers, command):
    """On an empty root a refusal names what is missing - never a bare `[Errno 2]` or a traceback."""
    result = answers[("empty-bare", command)]
    said = _said(result)
    assert result["body"] is not None, f"{command}: no envelope: {result['stderr'][-400:]}"
    assert "[Errno" not in said and "Traceback" not in said, f"{command}: {said[:300]}"


# --------------------------------------------------------------------------- #
# What the answers carry (R16, the answer side)
# --------------------------------------------------------------------------- #
#: Commands no run above answers successfully - most need a live database or host, the rest have
#: no complete request here yet - so their answer keys are checked by nothing. The debt of R16's
#: answer side: a command gains a run that succeeds, and leaves the list. Held equal to what the runs
#: show, so the count docs/rules.md carries is true, and a run that stops succeeding is noticed.
ANSWER_NOT_YET_SEEN = frozenset({
    "add-sql", "app-command-set", "backup-chain", "backup-database", "build-showcase",
    "check-identifiers", "check-secret-literals", "copy-backup-dir", "copy-schema",
    "create-table-from-xlsx", "delete-file", "delete-files", "disable-job",
    "encrypt-secret", "export-data", "fetch-file", "host-facts", "host-restart", "host-service",
    "import-data", "inventory-summary", "kill-spid", "lift-example", "list-databases", "list-jobs",
    "list-schemas", "metric-severity", "metric-toggle", "move-db-docker", "pack-backup",
    "pack-files", "prune-backup-files", "prune-staged-backups", "pull-file", "push-file",
    "relay-file", "restore-diff", "restore-full", "restore-key", "restore-log",
    "restore-metadata", "rotate-password", "run-cmd", "run-sql", "run-sqlcmd", "send-file",
    "shrink-log", "smb-credential", "smb-delete", "smb-list", "sql-target-add", "sqlserver-apply-cu", "sqlserver-export-instance",
    "sqlserver-precheck", "sqlserver-replay-instance", "sqlserver-verify-build",
    "sqlserver-verify-instance", "start-job", "trace-session", "verify-restore"
})


def _answer_fields() -> dict[str, set[str]]:
    reference = json.loads((REPO / "db_ops" / "common" / "catalogue" / "shared_config_objects.json")
                           .read_text(encoding="utf-8"))
    return {command: {field["field"] for field in entry["fields"]}
            for entry in reference["shared_config_objects"] if entry.get("kind") == "output"
            for command in entry.get("commands") or []}


def test_every_key_a_successful_answer_carries_is_described(answers, tmp_path):
    """R16 from the answer's side: the reference says what each command answers under `data`.

    The reference suite holds every described field to the code that produces it - it cannot see a
    key the code writes and the reference never mentions, because only a real answer shows it. The
    runs above are real answers, so they are checked here rather than made again. Writing it found
    three: `check-references` answering `data_dir`, `list-backup-files` answering `unreadable` and
    `timezone` answering `config_error`.
    """
    described = _answer_fields()
    cases = _cases(tmp_path)
    checked: set[str] = set()
    undescribed: dict[str, set[str]] = {}
    for (run_kind, name), result in answers.items():
        # A "case" run is keyed by the case's name; every other run by its command.
        command = cases[name][0] if run_kind == "case" else name
        body = result["body"] or {}
        if not body.get("success") or not isinstance(body.get("data"), dict):
            continue
        assert command in described, f"{command} answers and no output entry describes it"
        checked.add(command)
        extra = set(body["data"]) - described[command]
        if extra:
            undescribed.setdefault(command, set()).update(extra)
    assert not undescribed, f"answer keys the reference does not describe: {undescribed}"
    unseen = set(COMMANDS) - checked
    assert not unseen - ANSWER_NOT_YET_SEEN, (
        "no run here answers these, so nothing checks their keys - add a case: "
        f"{sorted(unseen - ANSWER_NOT_YET_SEEN)}")
    assert not ANSWER_NOT_YET_SEEN - unseen, (
        f"these answer now - take them off ANSWER_NOT_YET_SEEN: {sorted(ANSWER_NOT_YET_SEEN - unseen)}")
