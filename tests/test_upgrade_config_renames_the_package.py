"""`upgrade-config` points a node's configuration at the package's 0.29.0 name - and nothing else.

0.29.0 renamed `db_ops` to `dbabrain` with nothing kept for the old name: no second package, no
second command, no `DB_OPS_*` read as a fallback. A node's `data/` names the code in four ways - a
module path in an app or bot command, a path into the package, the `tools/db_ops` alias, the name
of an environment variable it hands a script - and each would stop working on the first start
after the upgrade. The step `package-rename` rewrites exactly those.

What it must not touch is as important, because the same letters name things that are not the
code: the store's schema and its tables (`db_ops.job_runs`), its database in an address, a folder
on a host (`/opt/db_ops/backup`), a record's id (`DB_OPS_STORE`), a secret's ref, a certificate. The
operator's line, 2026-10-12: *the database and schema the store uses stay - that is configuration,
not code*. Each case below is one of the shapes found in the estate's own configuration.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dbabrain.common import config_upgrade
from dbabrain.lib import package_rename

ENV = frozenset({"DBABRAIN_TARGET_HOST", "DBABRAIN_NEW_DB_PASSWORD", "DBABRAIN_SECRET_KEY"})


@pytest.mark.parametrize("before, after", [
    ("python -m db_ops.sql_tasks.runner --config config.json",
     "python -m dbabrain.sql_tasks.runner --config config.json"),
    ("db_ops.common.cli", "dbabrain.common.cli"),
    ("python -m db_ops init", "python -m dbabrain init"),
    ("tools/db_ops", "tools/dbabrain"),
    ("/app/tools/db_ops/data", "/app/tools/dbabrain/data"),
    ("../db_ops/sre/automation", "../dbabrain/sre/automation"),
    ("tools/db_ops/db_ops/metrics/collectors/x.sql", "tools/dbabrain/dbabrain/metrics/collectors/x.sql"),
    # A Windows node writes the same three with a backslash, and the daemon reads `tools\...` as
    # the alias: a rule that knew one slash left that node's `working_dir` resolving to nothing.
    (r"tools\db_ops", r"tools\dbabrain"),
    (r"..\db_ops\sre\automation", r"..\dbabrain\sre\automation"),
    (r"tools\db_ops\db_ops\metrics\collectors\x.sql", r"tools\dbabrain\dbabrain\metrics\collectors\x.sql"),
    (r"cd tools\\db_ops", r"cd tools\\dbabrain"),     # as raw text out of a JSON file: doubled
    # A path that ends at a component's own folder is a path into the package too.
    ("../db_ops/sre", "../dbabrain/sre"),
    ("tools/db_ops/db_ops/metrics", "tools/dbabrain/dbabrain/metrics"),
    (r"tools\db_ops\db_ops\metrics", r"tools\dbabrain\dbabrain\metrics"),
    ("db_ops/sre/automation/run.py", "dbabrain/sre/automation/run.py"),
    ("python db_ops/sre/tool.py --dry-run", "python dbabrain/sre/tool.py --dry-run"),
    ("/usr/lib/python3/site-packages/db_ops/common/cli.py",
     "/usr/lib/python3/site-packages/dbabrain/common/cli.py"),
    (r"D:\Projects\db_ops\db_ops\sre\automation", r"D:\Projects\db_ops\dbabrain\sre\automation"),
    ("DB_OPS_TARGET_HOST", "DBABRAIN_TARGET_HOST"),
])
def test_what_names_the_code_is_pointed_at_the_new_name(before, after) -> None:
    assert package_rename.renamed(before, ENV) == after


@pytest.mark.parametrize("value", [
    "/opt/db_ops/backup/dbops",            # a folder on the worker host
    "db_ops.job_runs",                     # a table in the store's schema
    "postgres@host:5433/db_ops",           # the store's database
    "DB_OPS_STORE",                        # a record's id
    "DB_OPS_STORE_PASSWORD",               # a secret's ref - not a variable this version reads
    "db_ops_backup_cert",                  # the certificate on the SQL Servers
    "the db_ops toolkit",                  # prose is the operator's
    r"cd D:\Projects\db_ops",              # the checkout's own folder keeps its name
    r"D:\Projects\db_ops\data\store_config.json",
    r"C:\tools\db_ops_deploy",             # a longer name that only starts with the alias
    # A folder on a host whose next part is named like a component is still a folder on a host:
    # what comes before it is not something that holds the package.
    "/opt/db_ops/reports/a.html",
    "/opt/db_ops/reports",
    "/opt/db_ops/db/dump.sql",
    r"C:\db_ops\reports\daily.html",
    "/db_ops/sre",
])
def test_a_value_that_lives_outside_the_code_keeps_its_name(value) -> None:
    assert package_rename.renamed(value, ENV) == value


def test_an_environment_name_is_renamed_only_when_this_version_reads_it() -> None:
    assert package_rename.renamed("DB_OPS_SOMETHING_RETIRED", ENV) == "DB_OPS_SOMETHING_RETIRED"
    assert package_rename.renamed("DB_OPS_SECRET_KEY", ENV) == "DBABRAIN_SECRET_KEY"


def test_the_names_this_version_reads_are_found_in_its_own_source() -> None:
    names = config_upgrade._env_names()
    assert {"DBABRAIN_SECRET_KEY", "DBABRAIN_NODE_ROLE", "DBABRAIN_TARGET_HOST"} <= names
    assert not any(name.startswith("DB_OPS") for name in names)


def _node(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    (data / "app_commands.json").write_text(json.dumps({"app_commands": [{
        "app_command_id": "APP-SQL_TASKS", "working_dir": "tools/db_ops",
        "command_text": "python -m db_ops.sql_tasks.runner --config config.json"}]}, indent=2) + "\n",
        encoding="utf-8")
    (data / "telegram_support_commands.json").write_text(json.dumps({"telegram_support_commands": [{
        "command_text": "/spbot_create_db_docker",
        "action_config": {"working_dir": "tools/db_ops",
                          "command_argv": ["{python}", "-m", "db_ops.sre.cli", "create-db-docker"],
                          "secret_env": ["DB_OPS_NEW_DB_PASSWORD"]}}]}, indent=2) + "\n", encoding="utf-8")
    (data / "store_config.json").write_text(json.dumps({"backend": "postgresql", "postgresql": {
        "host": "192.0.2.10", "port": 5433, "database_name": "db_ops", "schema": "db_ops",
        "password_ref": "DB_OPS_STORE_PASSWORD"}}, indent=2) + "\n", encoding="utf-8")
    (data / "db_instances.json").write_text(json.dumps({"db_instances": [{
        "server_id": "ACME-192-0-2-10-PG-5433",
        "metrics": {"collector_env": {"DB_OPS_TARGET_HOST": "192.0.2.10"}},
        "objects": {"db_ops.job_runs": {"rows": 10}}}]}, indent=2) + "\n", encoding="utf-8")
    return data


def test_a_plan_writes_nothing_and_names_each_change(tmp_path) -> None:
    data = _node(tmp_path)
    before = {path.name: path.read_bytes() for path in data.iterdir()}

    _, answer = config_upgrade.upgrade({"data_dir": str(data), "steps": ["package-rename"]})

    assert {path.name: path.read_bytes() for path in data.iterdir()} == before
    step = answer["steps"][0]
    assert step["step"] == "package-rename"
    assert {item["file"] for item in step["files"]} == {
        "app_commands.json", "telegram_support_commands.json", "db_instances.json"}


def test_the_rewrite_moves_the_code_and_leaves_the_store_alone(tmp_path) -> None:
    data = _node(tmp_path)
    store_before = (data / "store_config.json").read_bytes()

    config_upgrade.upgrade({"data_dir": str(data), "steps": ["package-rename"], "dry_run": False})

    command = json.loads((data / "app_commands.json").read_text(encoding="utf-8"))["app_commands"][0]
    assert command["working_dir"] == "tools/dbabrain"
    assert command["command_text"] == "python -m dbabrain.sql_tasks.runner --config config.json"
    bot = json.loads((data / "telegram_support_commands.json").read_text(encoding="utf-8"))
    action = bot["telegram_support_commands"][0]["action_config"]
    assert action["command_argv"] == ["{python}", "-m", "dbabrain.sre.cli", "create-db-docker"]
    assert action["secret_env"] == ["DBABRAIN_NEW_DB_PASSWORD"]
    instance = json.loads((data / "db_instances.json").read_text(encoding="utf-8"))["db_instances"][0]
    assert instance["metrics"]["collector_env"] == {"DBABRAIN_TARGET_HOST": "192.0.2.10"}
    assert "db_ops.job_runs" in instance["objects"], "a table in the store's schema is not the code"
    assert (data / "store_config.json").read_bytes() == store_before


def test_a_second_run_finds_nothing_to_do(tmp_path) -> None:
    data = _node(tmp_path)
    config_upgrade.upgrade({"data_dir": str(data), "steps": ["package-rename"], "dry_run": False})

    _, answer = config_upgrade.upgrade({"data_dir": str(data), "steps": ["package-rename"]})

    assert answer["steps"][0]["records_changed"] == 0


def test_it_runs_before_the_step_that_reads_the_new_module_names() -> None:
    """`moved-commands` looks for `dbabrain.common.cli`; a node still saying the old name would be
    passed over by it, so the rename has to come first."""
    assert config_upgrade.STEPS.index("package-rename") < config_upgrade.STEPS.index("moved-commands")
