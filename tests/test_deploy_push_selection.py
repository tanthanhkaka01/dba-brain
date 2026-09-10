"""A partial deploy must ship what was named, and refuse everything else by saying why.

`deploy --type ... --file-name ...` exists because the full deploy is the wrong shape for the
change that happens ten times a day: a threshold in a `data/*.json`, one new `.sql` under
`assets/tasks`. Those directories are bind mounts on the worker and the scheduler re-reads its
commands every scan, so the image build and container restart were buying nothing for them.

What makes a push safe is not the transport, which is one SFTP call. It is this selection: the
manifest still decides which files may travel, a master-only file is refused **by name** rather
than dropped quietly, and a pattern that matches nothing stops the run instead of shipping the
empty set and printing success. That last one is the failure `config_sync` grew `unknown_files`
to prevent, and a push repeats it the moment a typo reads as a completed deploy.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from db_ops.lib import deploy_selection
from db_ops.lib.deploy_selection import PushSelectionError, normalise_type, select_push_files

MANIFEST = {
    "data_files": [
        {"file": "sql_targets.json", "app_code": "SQL", "kind": "config", "transfer": "merge"},
        {"file": "reports_config.json", "app_code": "REPORTS", "kind": "config", "transfer": "push"},
        {"file": "database-inventory.json", "app_code": "REPORTS", "kind": "config",
         "transfer": "pull"},
        {"file": "sre_test_config.json", "app_code": "SRE", "kind": "fixture", "transfer": "local"},
    ]
}


@pytest.fixture()
def tool_root(tmp_path: Path) -> Path:
    """A master tree with one of every case the selection has to distinguish."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "data_files.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
    for name in ("sql_targets.json", "reports_config.json", "database-inventory.json",
                 "sre_test_config.json"):
        (data / name).write_text("{}", encoding="utf-8")
    (data / "ssh_keys").mkdir()
    (data / "ssh_keys" / "worker.key").write_text("key", encoding="utf-8")
    tasks = tmp_path / "assets" / "tasks"
    (tasks / "oracle").mkdir(parents=True)
    (tasks / "sqlserver").mkdir(parents=True)
    (tasks / "oracle" / "019_get_job_details.sql").write_text("select 1", encoding="utf-8")
    (tasks / "sqlserver" / "008_mapping.sql").write_text("select 1", encoding="utf-8")
    (tasks / "sqlserver" / "__pycache__").mkdir()
    (tasks / "sqlserver" / "__pycache__" / "junk.pyc").write_text("x", encoding="utf-8")
    return tmp_path


def _select(tool_root: Path, push_type: str, *names: str) -> list[str]:
    return [item.relative for item in select_push_files(
        tool_root=tool_root, push_type=push_type, names=names,
        data_dir=tool_root / "data")]


def test_a_type_typed_the_windows_way_means_the_same_tree(tool_root) -> None:
    """PowerShell hands over `assets\\tasks`; the worker's paths are posix. One spelling wins."""
    assert normalise_type("assets\\tasks") == "assets/tasks"
    assert normalise_type("assets/tasks/") == "assets/tasks"
    assert normalise_type("Config") == "config"
    assert normalise_type("data") == "config"


def test_a_type_outside_the_pushable_trees_is_refused_with_the_choices(tool_root) -> None:
    with pytest.raises(PushSelectionError) as caught:
        normalise_type("logs")
    assert "--type config" in str(caught.value)
    assert "full deploy" in str(caught.value)


def test_config_means_the_catalogued_json_and_not_a_directory_listing(tool_root) -> None:
    """The manifest decides. `data/ssh_keys` and the master-only fixture are not in the answer."""
    assert _select(tool_root, "config") == [
        "data/database-inventory.json",
        "data/reports_config.json",
        "data/sql_targets.json",
    ]


def test_one_named_config_file_is_the_whole_push(tool_root) -> None:
    assert _select(tool_root, "config", "sql_targets.json") == ["data/sql_targets.json"]


def test_a_master_only_file_is_refused_by_name(tool_root) -> None:
    """Reported as "never travels", not as "not found": the filename is spelled correctly."""
    with pytest.raises(PushSelectionError) as caught:
        _select(tool_root, "config", "sre_test_config.json")
    assert "master-only" in str(caught.value)
    assert "transfer=local" in str(caught.value)


def test_a_name_that_matches_nothing_stops_the_push(tool_root) -> None:
    """The alternative is uploading the empty set and printing success."""
    with pytest.raises(PushSelectionError) as caught:
        _select(tool_root, "config", "sql_target.json")
    assert "matches no catalogued data file" in str(caught.value)
    assert "sql_targets.json" in str(caught.value)


def test_an_asset_subtree_carries_every_sql_under_it_and_no_build_litter(tool_root) -> None:
    assert _select(tool_root, "assets/tasks") == [
        "assets/tasks/oracle/019_get_job_details.sql",
        "assets/tasks/sqlserver/008_mapping.sql",
    ]


def test_a_glob_selects_inside_the_subtree(tool_root) -> None:
    assert _select(tool_root, "assets/tasks", "oracle/*.sql") == [
        "assets/tasks/oracle/019_get_job_details.sql"]
    assert _select(tool_root, "assets/tasks", "008_mapping.sql") == [
        "assets/tasks/sqlserver/008_mapping.sql"]


def test_two_names_select_both_and_never_the_same_file_twice(tool_root) -> None:
    assert _select(tool_root, "assets/tasks", "*.sql", "008_mapping.sql") == [
        "assets/tasks/oracle/019_get_job_details.sql",
        "assets/tasks/sqlserver/008_mapping.sql",
    ]


def test_the_inventory_lands_where_the_reports_app_reads_it_too(tool_root) -> None:
    """A deploy writes database-inventory.json to data/ AND runtime/reports/; a push must match.

    Half of it would leave the worker rendering yesterday's inventory out of the other copy —
    with both files present, so nothing looks missing.
    """
    selected = select_push_files(tool_root=tool_root, push_type="config",
                                 names=["database-inventory.json"],
                                 data_dir=tool_root / "data")
    assert [item.targets for item in selected] == [
        ("data/database-inventory.json", "runtime/reports/database-inventory.json")]


def test_ssh_keys_are_addressable_as_their_own_tree(tool_root) -> None:
    """They travel in a bundle and are not catalogued config, so `--type config` cannot mean them."""
    assert _select(tool_root, "data/ssh_keys") == ["data/ssh_keys/worker.key"]


def test_the_pushable_trees_are_the_bundles_own(tool_root) -> None:
    """A new estate directory has to be added here deliberately, not discovered at runtime."""
    assert deploy_selection.PUSHABLE_DIRS == ("assets", "data/ssh_keys")
