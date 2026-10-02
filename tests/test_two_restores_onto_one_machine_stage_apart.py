"""Two restores onto one machine stage into folders of their own - whichever instances they target.

A staging directory is a path on a *machine*. The copy into it is a mirror: what is there and not
at the source is removed as "gone at the source". So two entries sharing one ``target_backup_dir``
delete each other's files, and since 0.26.0 that is refused when the entry is loaded.

The check compared ``target_server_id``, and one machine carries several instances. On the 0.26.0
soak the three lab restores - onto the SQL Server, the PostgreSQL and the Oracle of one VM - shared
``/opt/db_ops/backup/restore_from_251``, each naming a different target, and passed. Every hour the
Oracle copy then removed the 2,700 files the PostgreSQL copy had staged thirty seconds earlier, and
``pg_combinebackup`` lost its base backup in mid-run: **21 failures in 24 cycles**, each reported
as a restore error and none as what it was.

The machine comes from the inventory beside the restore file (``cmd_access.host``, else ``ip``).
Where the inventory does not hold an instance, the instance stands for itself, as before.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from db_ops.backup_restore import restore_script

LAB = "198.51.100.252"


def _entry(restore_id: str, target: str, folder: str, **fields) -> dict:
    entry = {
        "restore_id": restore_id, "db_type": "postgresql", "server_id": f"SRC-{restore_id}",
        "target_server_id": target, "backup_dir": "/backup", "script": "restore.sh",
        "target_backup_dir": folder, "source_backup_host_dir": "/opt/backup/src",
        "cleanup_retention": 7200,
    }
    entry.update(fields)
    return entry


def _root(tmp_path: Path, entries: list[dict], instances: list[dict] | None) -> str:
    path = tmp_path / "restore_config.json"
    path.write_text(json.dumps({"backup_restore": {"restores": entries}}), encoding="utf-8")
    if instances is not None:
        (tmp_path / "db_instances.json").write_text(
            json.dumps({"db_instances": instances}), encoding="utf-8")
    return str(path)


THREE_ON_ONE_VM = [
    {"server_id": "LAB252-MSSQL", "db_type": "sqlserver", "ip": LAB, "port": 1433},
    {"server_id": "LAB252-PG", "db_type": "postgresql", "ip": LAB, "port": 5432},
    {"server_id": "LAB252-ORA", "db_type": "oracle", "ip": LAB, "port": 1521},
]


def test_the_soaks_three_restores_into_one_folder_are_refused(tmp_path):
    shared = "/opt/db_ops/backup/restore_from_251"
    with pytest.raises(ValueError) as refused:
        restore_script.load_script_restores(_root(tmp_path, [
            _entry("LAB_MSSQL", "LAB252-MSSQL", shared, db_type="sqlserver"),
            _entry("LAB_PG", "LAB252-PG", shared),
            _entry("LAB_ORA", "LAB252-ORA", shared, db_type="oracle"),
        ], THREE_ON_ONE_VM))
    message = str(refused.value)
    assert "one directory holds the other" in message
    assert LAB in message and "LAB252-MSSQL" in message and "LAB252-PG" in message


def test_a_folder_each_on_the_same_machine_is_what_it_asks_for(tmp_path):
    jobs = restore_script.load_script_restores(_root(tmp_path, [
        _entry("LAB_MSSQL", "LAB252-MSSQL", "/opt/db_ops/backup/restore_from_251_mssql", db_type="sqlserver"),
        _entry("LAB_PG", "LAB252-PG", "/opt/db_ops/backup/restore_from_251_pg"),
        _entry("LAB_ORA", "LAB252-ORA", "/opt/db_ops/backup/restore_from_251_ora", db_type="oracle"),
    ], THREE_ON_ONE_VM))
    assert [job.restore_id for job in jobs] == ["LAB_MSSQL", "LAB_PG", "LAB_ORA"]
    assert jobs.unusable == {}


def test_one_folder_inside_another_on_the_same_machine_is_refused_too(tmp_path):
    with pytest.raises(ValueError, match="one directory holds the other"):
        restore_script.load_script_restores(_root(tmp_path, [
            _entry("LAB_PG", "LAB252-PG", "/opt/db_ops/backup/restore_from_251"),
            _entry("LAB_ORA", "LAB252-ORA", "/opt/db_ops/backup/restore_from_251/oracle", db_type="oracle"),
        ], THREE_ON_ONE_VM))


def test_the_same_path_on_two_machines_is_two_folders(tmp_path):
    instances = [{"server_id": "A-PG", "db_type": "postgresql", "ip": "198.51.100.10"},
                 {"server_id": "B-PG", "db_type": "postgresql", "ip": "198.51.100.11"}]
    jobs = restore_script.load_script_restores(_root(tmp_path, [
        _entry("TO_A", "A-PG", "/opt/db_ops/restore_staging/pg"),
        _entry("TO_B", "B-PG", "/opt/db_ops/restore_staging/pg"),
    ], instances))
    assert [job.restore_id for job in jobs] == ["TO_A", "TO_B"]


def test_the_machine_is_the_address_commands_are_sent_to_when_one_is_stated(tmp_path):
    """Two instances behind one jump address stage on that one machine."""
    instances = [{"server_id": "A-PG", "db_type": "postgresql", "ip": "198.51.100.10",
                  "cmd_access": {"host": "198.51.100.99"}},
                 {"server_id": "B-PG", "db_type": "postgresql", "ip": "198.51.100.11",
                  "cmd_access": {"host": "198.51.100.99"}}]
    with pytest.raises(ValueError, match="198.51.100.99"):
        restore_script.load_script_restores(_root(tmp_path, [
            _entry("TO_A", "A-PG", "/opt/db_ops/restore_staging/pg"),
            _entry("TO_B", "B-PG", "/opt/db_ops/restore_staging/pg"),
        ], instances))


def test_a_retired_entry_sharing_a_machines_folder_yields_to_the_live_one(tmp_path):
    jobs = restore_script.load_script_restores(_root(tmp_path, [
        _entry("OLD_PG", "LAB252-PG", "/opt/db_ops/backup/restore_from_251", active=False),
        _entry("LIVE_ORA", "LAB252-ORA", "/opt/db_ops/backup/restore_from_251", db_type="oracle"),
    ], THREE_ON_ONE_VM))
    assert [job.restore_id for job in jobs] == ["LIVE_ORA"]
    assert "one directory holds the other" in jobs.unusable["OLD_PG"]


def test_with_no_inventory_an_instance_stands_for_itself_as_before(tmp_path):
    shared = "/opt/db_ops/restore_staging/pg"
    jobs = restore_script.load_script_restores(_root(tmp_path, [
        _entry("TO_X", "X", shared), _entry("TO_Y", "Y", shared)], None))
    assert [job.restore_id for job in jobs] == ["TO_X", "TO_Y"]
    with pytest.raises(ValueError, match="one directory holds the other"):
        restore_script.load_script_restores(_root(tmp_path, [
            _entry("TO_X", "X", shared), _entry("AGAIN_X", "X", shared)], None))
