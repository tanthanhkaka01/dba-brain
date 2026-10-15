"""`upgrade-config` writes into a record the certificate name it had been leaving to a default.

0.29.0 gave the backup certificate's default the tool's own name: `dbabrain_backup_cert`. A default
is configuration nobody wrote down, and this one names an object on a server. A SQL Server backup
job that named no certificate has been encrypting with `db_ops_backup_cert` since its first run;
with the default merely renamed, its next run would have looked for a certificate the server does
not hold, created it, and encrypted with a key no restore target has - a backup that reads as done
and cannot be restored where it is meant to be.

So the step `earlier-defaults` runs first: every record that named none is told the name it has
been running on, and from then on the code's default decides only for a record made after the
upgrade. The estate's own file, the day this was written: 9 SQL Server backup jobs and 3
script-driven restores named no certificate, and 3 backups and one share-driven restore did.

What it must not do is as much the point: touch another engine, replace a name a record states, or
invent a certificate for a restore that takes its own from somewhere else.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from dbabrain.backup_restore import config as backup_restore_config
from dbabrain.common import config_upgrade, restorekey
from dbabrain.lib import earlier_defaults, lab_folders, sqlserver_certificate

REPO = Path(__file__).resolve().parents[1]
EARLIER = "db_ops_backup_cert"


def _document() -> dict:
    return {"backup_restore": {
        "backups": [
            {"backup_id": "ACME_MSSQL_FULL", "db_type": "sqlserver", "server_id": "ACME-MSSQL",
             "jobs": [{"job": "full", "script": "mssql_backup_database.sh"},
                      {"job": "log", "script": "mssql_backup_database.sh", "env": {"BACKUP_LEVEL": "LOG"}}]},
            {"backup_id": "ACME_MSSQL_OWN_CERT", "db_type": "sqlserver", "server_id": "ACME-MSSQL",
             "jobs": [{"job": "full", "script": "mssql_backup_database.sh",
                       "env": {"BACKUP_CERT_NAME": "acme_backup_cert"}}]},
            {"backup_id": "ACME_MSSQL_OFF", "db_type": "sqlserver", "server_id": "ACME-MSSQL",
             "active": False, "jobs": [{"job": "full", "script": "mssql_backup_database.sh"}]},
            {"backup_id": "ACME_ORA_DB", "db_type": "oracle", "server_id": "ACME-ORA",
             "jobs": [{"job": "level0", "script": "oracle_rman_database.sh"}]},
            {"backup_id": "ACME_PG_BASE", "db_type": "postgresql", "server_id": "ACME-PG",
             "jobs": [{"job": "base", "script": "pg_basebackup.sh", "env": {"PGUSER": "postgres"}}]},
        ],
        "restores": [
            {"restore_id": "ACME_MSSQL_SCRIPT", "db_type": "sqlserver", "script": "mssql_restore.sh",
             "env": {"MSSQL_USER": "sa"}},
            {"restore_id": "ACME_PG_SCRIPT", "db_type": "postgresql", "script": "pg_restore_basebackup.sh"},
            {"restore_id": "ACME_SHARE_NO_NAME",
             "source": {"backup_share": r"\\192.0.2.250\SQLBK", "backup_certificate": {"password_ref": "ENC_REF"}}},
            {"restore_id": "ACME_SHARE_NAMED",
             "source": {"backup_certificate": {"name": "acme_backup_cert", "password_ref": "ENC_REF"}}},
            {"restore_id": "ACME_SHARE_API",
             "source": {"certificate_api_url": "https://192.0.2.5/cert", "certificate_api_token_ref": "T"}},
            {"restore_id": "ACME_PG_STAGED", "db_type": "postgresql", "script": "pg_restore_basebackup.sh",
             "target_server_id": "ACME-PG-2", "target_backup_dir": "/opt/acme/staging/pg"},
            {"restore_id": "ACME_PG_OWN_MARKER", "db_type": "postgresql", "script": "pg_restore_basebackup.sh",
             "target_server_id": "ACME-PG-2", "target_backup_dir": "/opt/acme/staging/pg2",
             "staging_marker": ".acme-staging"},
        ]}}


def _by_id(document: dict, kind: str, key: str) -> dict:
    id_field = "backup_id" if kind == "backups" else "restore_id"
    return next(item for item in document["backup_restore"][kind] if item[id_field] == key)


def test_a_sqlserver_backup_job_that_names_no_certificate_is_told_the_one_it_ran_on() -> None:
    document = _document()

    changes = earlier_defaults.state_backup_certificate(document)

    full, log = _by_id(document, "backups", "ACME_MSSQL_FULL")["jobs"]
    assert full["env"] == {"BACKUP_CERT_NAME": EARLIER}, "a job without an env gets one"
    assert log["env"] == {"BACKUP_LEVEL": "LOG", "BACKUP_CERT_NAME": EARLIER}, "what its env said stays"
    assert {"where": "backups[ACME_MSSQL_FULL].jobs[full]", "field": "env.BACKUP_CERT_NAME",
            "value": EARLIER} in changes


def test_a_record_that_states_a_certificate_keeps_it() -> None:
    document = _document()

    changes = earlier_defaults.state_backup_certificate(document)

    assert _by_id(document, "backups", "ACME_MSSQL_OWN_CERT")["jobs"][0]["env"] == {
        "BACKUP_CERT_NAME": "acme_backup_cert"}
    assert _by_id(document, "restores", "ACME_SHARE_NAMED")["source"]["backup_certificate"]["name"] == \
        "acme_backup_cert"
    assert not [item for item in changes if "OWN_CERT" in item["where"] or "SHARE_NAMED" in item["where"]]


def test_an_entry_that_is_switched_off_is_stated_too() -> None:
    """It ran on the default when it ran, and switching it on again must not change its certificate."""
    document = _document()

    earlier_defaults.state_backup_certificate(document)

    assert _by_id(document, "backups", "ACME_MSSQL_OFF")["jobs"][0]["env"] == {"BACKUP_CERT_NAME": EARLIER}


def test_another_engine_is_left_alone() -> None:
    document = _document()
    before = json.dumps([_by_id(document, "backups", "ACME_ORA_DB"), _by_id(document, "backups", "ACME_PG_BASE"),
                         _by_id(document, "restores", "ACME_PG_SCRIPT")])

    earlier_defaults.state_backup_certificate(document)

    assert json.dumps([_by_id(document, "backups", "ACME_ORA_DB"), _by_id(document, "backups", "ACME_PG_BASE"),
                       _by_id(document, "restores", "ACME_PG_SCRIPT")]) == before


def test_each_kind_of_restore_is_stated_where_that_kind_reads_it() -> None:
    document = _document()

    earlier_defaults.state_backup_certificate(document)

    script = _by_id(document, "restores", "ACME_MSSQL_SCRIPT")
    assert script["env"] == {"MSSQL_USER": "sa", "BACKUP_CERT_NAME": EARLIER}
    share = _by_id(document, "restores", "ACME_SHARE_NO_NAME")["source"]["backup_certificate"]
    assert share == {"name": EARLIER, "password_ref": "ENC_REF"}
    assert "env" not in _by_id(document, "restores", "ACME_SHARE_NO_NAME"), "a share restore reads no env"


def test_a_restore_that_takes_its_certificate_from_somewhere_else_is_given_none() -> None:
    document = _document()

    earlier_defaults.state_backup_certificate(document)

    api = _by_id(document, "restores", "ACME_SHARE_API")
    assert "backup_certificate" not in api["source"] and "env" not in api


def test_a_second_run_finds_nothing_to_state() -> None:
    document = _document()
    first = earlier_defaults.state_backup_certificate(document)
    after_first = json.dumps(document)

    assert len(first) == 5, "three backup jobs, the script restore, the share restore"
    assert earlier_defaults.state_backup_certificate(document) == []
    assert json.dumps(document) == after_first


# --------------------------------------------------------------------------- #
# The staging marker: a file on the restore target, written by the copy that first used the folder
# --------------------------------------------------------------------------- #
EARLIER_MARKER = ".dbops-staging"


def test_an_entry_that_stages_on_its_target_is_told_the_marker_its_folder_carries() -> None:
    """Retention prunes every staged file and never the marker, so between two runs the folder
    holds the marker alone. Under a default with another name the next copy would have met a
    folder with one unknown file and nothing that is at the source - and refused it."""
    document = _document()

    changes = earlier_defaults.state_staging_marker(document)

    assert _by_id(document, "restores", "ACME_PG_STAGED")["staging_marker"] == EARLIER_MARKER
    assert changes == [{"where": "restores[ACME_PG_STAGED]", "field": "staging_marker",
                        "value": EARLIER_MARKER}]


def test_an_entry_that_stages_nowhere_or_names_its_marker_is_left_alone() -> None:
    document = _document()

    earlier_defaults.state_staging_marker(document)

    assert "staging_marker" not in _by_id(document, "restores", "ACME_MSSQL_SCRIPT"), "it has no staging folder"
    assert "staging_marker" not in _by_id(document, "restores", "ACME_SHARE_NO_NAME"), "a share restore has none"
    assert _by_id(document, "restores", "ACME_PG_OWN_MARKER")["staging_marker"] == ".acme-staging"
    assert earlier_defaults.state_staging_marker(document) == [], "a second run finds nothing"


def test_the_marker_an_entry_states_reaches_the_copy_and_the_prune(monkeypatch, tmp_path) -> None:
    """Both, or the prune looks for a marker the copy never wrote and refuses the folder."""
    import types

    from dbabrain.backup_restore import restore_script

    path = tmp_path / "restore_config.json"
    path.write_text(json.dumps({"backup_restore": {"restores": [{
        "restore_id": "ACME_PG_STAGED", "db_type": "postgresql", "server_id": "ACME-PG",
        "target_server_id": "ACME-PG-2", "backup_dir": "/var/lib/postgresql/backup",
        "script": "pg_restore_basebackup.sh", "target_backup_dir": "/opt/acme/staging/pg",
        "source_backup_host_dir": "/opt/acme/backup/pg", "target_container": "pg_lab",
        "cleanup_retention": 7200, "staging_marker": EARLIER_MARKER,
        "time_window": {"from_hour": 2, "to_hour": 5, "repeat_interval": 72000,
                        "retry_interval": 600, "timeout": 7200}}]}}), encoding="utf-8")
    job = restore_script.load_script_restores(path)[0]
    assert job.staging_marker == EARLIER_MARKER
    sent: dict[str, dict] = {}

    def run(command, request, **_kwargs):
        sent[command] = request
        return {"include": []} if command == "backup-chain" else {"copied": 0, "skipped": 0, "pruned": 0}

    monkeypatch.setattr(restore_script, "_ssh_login", lambda target, **_kwargs: {"host": "192.0.2.49"})
    monkeypatch.setattr(restore_script.common_cli, "run", run)
    endpoint = types.SimpleNamespace(container_name="")

    restore_script.transfer_backup_to_target(job, source=endpoint, target=endpoint, prune=True)

    assert sent["copy-backup-dir"]["staging_marker"] == EARLIER_MARKER
    assert sent["prune-staged-backups"]["staging_marker"] == EARLIER_MARKER


def test_an_entry_that_names_no_marker_sends_none_and_takes_the_copys_default(monkeypatch) -> None:
    import types

    from dbabrain.backup_restore import restore_script
    from dbabrain.common import backup_copy
    from dbabrain.lib.time_window import TimeWindow

    job = restore_script.ScriptRestore(
        restore_id="PG_NEW", db_type="postgresql", server_id="SRC", backup_dir="/backup",
        script="restore.sh", time_window=TimeWindow(), target_server_id="DST",
        target_backup_dir="/opt/acme/staging/new", source_backup_host_dir="/opt/acme/backup/pg")
    sent: dict[str, dict] = {}

    def run(command, request, **_kwargs):
        sent[command] = request
        return {"include": []} if command == "backup-chain" else {"copied": 0, "skipped": 0}

    monkeypatch.setattr(restore_script, "_ssh_login", lambda target, **_kwargs: {"host": "192.0.2.49"})
    monkeypatch.setattr(restore_script.common_cli, "run", run)
    endpoint = types.SimpleNamespace(container_name="")

    restore_script.transfer_backup_to_target(job, source=endpoint, target=endpoint, prune=False)

    assert "staging_marker" not in sent["copy-backup-dir"]
    assert backup_copy.marker_name(sent["copy-backup-dir"].get("staging_marker")) == ".dbabrain-staging"


def _node(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    (data / "restore_config.json").write_text(json.dumps(_document(), indent=2) + "\n", encoding="utf-8")
    return data


def test_a_plan_writes_nothing_and_names_each_record(tmp_path) -> None:
    data = _node(tmp_path)
    before = (data / "restore_config.json").read_bytes()

    _, answer = config_upgrade.upgrade({"data_dir": str(data), "steps": ["earlier-defaults"]})

    assert (data / "restore_config.json").read_bytes() == before
    assert not (data / "sre_config.json").exists(), "a plan creates nothing either"
    step = answer["steps"][0]
    restores = next(item for item in step["files"] if item["file"] == "restore_config.json")
    assert step["step"] == "earlier-defaults" and restores["records_changed"] == 6
    assert {item["where"] for item in restores["changes"]} == {
        "backups[ACME_MSSQL_FULL].jobs[full]", "backups[ACME_MSSQL_FULL].jobs[log]",
        "backups[ACME_MSSQL_OFF].jobs[full]", "restores[ACME_MSSQL_SCRIPT]", "restores[ACME_SHARE_NO_NAME]",
        "restores[ACME_PG_STAGED]"}


def test_the_file_is_copied_before_it_is_written_and_the_next_run_plans_nothing(tmp_path) -> None:
    data = _node(tmp_path)
    before = (data / "restore_config.json").read_bytes()

    _, answer = config_upgrade.upgrade({"data_dir": str(data), "steps": ["earlier-defaults"], "dry_run": False})

    kept = Path(answer["backup_dir"]) / "restore_config.json"
    assert kept.read_bytes() == before, "what the step replaced can be put back"
    written = json.loads((data / "restore_config.json").read_text(encoding="utf-8"))
    assert _by_id(written, "backups", "ACME_MSSQL_FULL")["jobs"][0]["env"] == {"BACKUP_CERT_NAME": EARLIER}
    _, again = config_upgrade.upgrade({"data_dir": str(data), "steps": ["earlier-defaults"]})
    assert again["steps"][0]["records_changed"] == 0


# --------------------------------------------------------------------------- #
# The lab folders: constants in the code until 0.29.0, a node's to state since
# --------------------------------------------------------------------------- #
EARLIER_FOLDERS = {"containers_dir": "/opt/db_ops/containers", "backup_mount": "/opt/db_ops/backup"}


def test_a_node_is_told_the_two_lab_folders_it_has_been_using() -> None:
    """The code holds no folder any more and refuses a lab command that finds none stated - so the
    folders every earlier version used are written where the node states its own."""
    document = {"app_name": "dbabrain_sre", "sre": {"vmware": {}}}

    changes = earlier_defaults.state_lab_folders(document)

    assert document["sre"]["docker_db"] == EARLIER_FOLDERS
    assert document["sre"]["vmware"] == {} and document["app_name"] == "dbabrain_sre"
    assert [(item["where"], item["field"]) for item in changes] == [
        ("sre.docker_db", "containers_dir"), ("sre.docker_db", "backup_mount")]
    assert earlier_defaults.state_lab_folders(document) == [], "a second run finds nothing"


def test_a_folder_the_node_already_states_is_kept() -> None:
    document = {"sre": {"docker_db": {"containers_dir": "/srv/labs"}}}

    changes = earlier_defaults.state_lab_folders(document)

    assert document["sre"]["docker_db"] == {"containers_dir": "/srv/labs",
                                            "backup_mount": "/opt/db_ops/backup"}
    assert [item["field"] for item in changes] == ["backup_mount"]


def test_the_step_writes_the_folders_into_the_nodes_own_file_and_keeps_a_copy(tmp_path) -> None:
    data = _node(tmp_path)
    (data / "sre_config.json").write_text(json.dumps({"app_name": "dbabrain_sre", "sre": {}}, indent=2) + "\n",
                                          encoding="utf-8")
    before = (data / "sre_config.json").read_bytes()

    _, answer = config_upgrade.upgrade({"data_dir": str(data), "steps": ["earlier-defaults"], "dry_run": False})

    assert (Path(answer["backup_dir"]) / "sre_config.json").read_bytes() == before
    assert lab_folders.load(data) == lab_folders.LabFolders(**EARLIER_FOLDERS)


def test_a_node_without_the_file_is_given_the_shipped_one_which_states_them(tmp_path) -> None:
    """A file holding the two folders alone would not be a sre_config.json anything else reads."""
    data = _node(tmp_path)

    config_upgrade.upgrade({"data_dir": str(data), "steps": ["earlier-defaults"], "dry_run": False})

    shipped = REPO / "dbabrain" / "sre" / "catalogue" / "sre_config.json"
    assert json.loads((data / "sre_config.json").read_text(encoding="utf-8-sig")) == json.loads(
        shipped.read_text(encoding="utf-8-sig"))
    assert lab_folders.load(data) == lab_folders.LabFolders(**EARLIER_FOLDERS)


def test_a_node_states_its_lab_folders_in_one_block_and_a_node_without_it_states_none(tmp_path) -> None:
    assert lab_folders.load(tmp_path) == lab_folders.LabFolders("", ""), "no file: nothing stated"
    assert lab_folders.read({"sre": {"docker_db": {"containers_dir": " /srv/labs ", "backup_mount": ""}}}) == lab_folders.LabFolders(
        "/srv/labs", "")
    assert lab_folders.read({"sre": {}}) == lab_folders.LabFolders()


def test_the_code_holds_no_lab_folder_to_fall_back_to() -> None:
    """The operator, 2026-10-12: a field, so the code hard-codes nothing and falls back to nothing."""
    from dbabrain.common import cli_docker_db
    from dbabrain.lib import docker_db_spec

    assert not hasattr(docker_db_spec, "DEFAULT_CONTAINERS_DIR")
    assert not hasattr(docker_db_spec, "DEFAULT_BACKUP_MOUNT")
    with pytest.raises(ValueError, match="containers_dir is not stated.*sre.docker_db.containers_dir"):
        cli_docker_db._create({"name": "LAB_5432", "engine": "postgres", "version": "18", "dry_run": True})
    with pytest.raises(ValueError, match="containers_dir is not stated"):
        cli_docker_db._move({"name": "LAB_5432", "source": {"host": "192.0.2.49"},
                             "destination": {"host": "192.0.2.50"}})
    with pytest.raises(ValueError, match="backup_mount is not stated.*sre.docker_db.backup_mount"):
        cli_docker_db._create({"name": "LAB_1433", "engine": "mssql", "version": "2025-latest",
                               "containers_dir": "/srv/labs", "dry_run": True})


def test_it_runs_after_the_steps_that_move_a_record_into_this_versions_shape() -> None:
    """It reads `db_type`, `jobs` and `env` where this version keeps them."""
    steps = list(config_upgrade.STEPS)
    assert steps.index("earlier-defaults") > steps.index("field-names")
    assert steps.index("earlier-defaults") > steps.index("restore-machine-ids")


def test_one_default_name_serves_every_place_that_takes_one() -> None:
    """Three Python modules and three scripts each held a copy; a copy that drifts encrypts with
    one certificate and restores with another."""
    name = sqlserver_certificate.DEFAULT_NAME
    assert name == "dbabrain_backup_cert"
    assert restorekey.DEFAULT_CERT_NAME == name
    assert backup_restore_config.DEFAULT_BACKUP_CERT_NAME == name

    scripts = REPO / "dbabrain" / "common"
    bash = re.compile(r'cert_name="\$\{BACKUP_CERT_NAME:-([A-Za-z0-9_]+)\}"')
    for path in (scripts / "backup_scripts" / "sqlserver" / "mssql_backup_database.sh",
                 scripts / "restore_scripts" / "sqlserver" / "mssql_restore.sh"):
        assert bash.findall(path.read_text(encoding="utf-8")) == [name], path.name
    powershell = (scripts / "backup_scripts" / "sqlserver" / "mssql_backup_database.ps1").read_text(encoding="utf-8")
    assert re.findall(r"\$env:BACKUP_CERT_NAME \} else \{ '([A-Za-z0-9_]+)' \}", powershell) == [name]


def test_no_default_of_the_package_is_the_earlier_name_again() -> None:
    """The earlier name stays in the package where a failure is told and in the rules that state
    it - never as a value the code would fall back to."""
    as_a_value = re.compile(r"""["']db_ops_backup_cert["']|:-db_ops_backup_cert|["']\.dbops-staging["']""")
    found = []
    for path in (REPO / "dbabrain").rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".sh", ".ps1", ".json", ".j2"}:
            continue
        if path.name == "earlier_defaults.py":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if as_a_value.search(line):
                found.append(f"{path.relative_to(REPO).as_posix()}:{number}")
    assert not found, found
