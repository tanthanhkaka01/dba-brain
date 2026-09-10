"""What a node creates for itself has to come back, or it dies with the node.

`db_ops` is the origin for code and for deliberate configuration, and every runtime node is a copy
of it. One flow runs the other way and it is not optional: the bot and the web console *create*
config on whichever node is running. A `/spbot_create_db_docker` run registers a lab container and
stores its SA password there — and if that node is replaced, the container survives with a
password nobody holds.

`deploy --merge` and `worker-pull-data-config` already do this for the worker **container**, over
SSH. The estate moved to an ordinary directory on a PC, where neither applies, and the carry-back
was done by hand: on 2026-09-09 a soak node held a secret ref and a `docker_db_connections` record
that this tree did not have.

The merge rules themselves are `test_deploy_merges_worker_config.py`'s subject. What is held down
here is that the local path uses **those** rules rather than a second implementation, and that the
two files a node is *supposed* to differ on are never dragged back.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from db_ops.control import worker_data


def write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@pytest.fixture()
def estate(tmp_path):
    """A master and a node that agree, ready to be pulled apart."""
    master, node = tmp_path / "master", tmp_path / "node"
    for root in (master, node):
        write(root / "docker_db_connections.json", {"docker_db_connections": [
            {"id": "MSSQL25", "engine": "mssql", "host": "192.0.2.115", "port": 1433}]})
        write(root / "sql_commands.json", {"sql_commands": [{"sql_id": 1, "sql_name": "one"}]})
    return {"master": master, "node": node}


def read(root: Path, name: str) -> list:
    doc = json.loads((root / name).read_text(encoding="utf-8"))
    return doc[next(iter(doc))]


def test_a_container_the_bot_registered_on_the_node_comes_back(estate) -> None:
    """The measured case: a lab container created through Telegram existed only on the node."""
    node_rows = read(estate["node"], "docker_db_connections.json")
    node_rows.append({"id": "MSSQL_192_0_2_115_1453", "engine": "mssql", "port": 1453})
    write(estate["node"] / "docker_db_connections.json", {"docker_db_connections": node_rows})

    changed = worker_data.merge_node_config(
        from_node_path=estate["node"], to_master_path=str(estate["master"]))

    assert changed == 1
    assert [r["id"] for r in read(estate["master"], "docker_db_connections.json")] == [
        "MSSQL25", "MSSQL_192_0_2_115_1453"]


def test_the_master_wins_a_key_both_sides_have(estate) -> None:
    """The same rule the SSH path applies, because it is the same function underneath."""
    write(estate["node"] / "docker_db_connections.json", {"docker_db_connections": [
        {"id": "MSSQL25", "engine": "mssql", "host": "10.0.0.9", "port": 9999}]})

    worker_data.merge_node_config(
        from_node_path=estate["node"], to_master_path=str(estate["master"]))

    kept = read(estate["master"], "docker_db_connections.json")[0]
    assert kept["port"] == 1433 and kept["host"] == "192.0.2.115"


def test_a_dry_run_reports_and_writes_nothing(estate) -> None:
    node_rows = read(estate["node"], "docker_db_connections.json")
    node_rows.append({"id": "NEW", "engine": "mssql"})
    write(estate["node"] / "docker_db_connections.json", {"docker_db_connections": node_rows})

    changed = worker_data.merge_node_config(
        from_node_path=estate["node"], to_master_path=str(estate["master"]), dry_run=True)

    assert changed == 1
    assert [r["id"] for r in read(estate["master"], "docker_db_connections.json")] == ["MSSQL25"]


def test_a_file_the_node_does_not_have_is_skipped_not_emptied(estate) -> None:
    (estate["node"] / "docker_db_connections.json").unlink()

    worker_data.merge_node_config(
        from_node_path=estate["node"], to_master_path=str(estate["master"]))

    assert [r["id"] for r in read(estate["master"], "docker_db_connections.json")] == ["MSSQL25"]


def test_a_path_that_is_not_a_node_is_refused(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="point --from at"):
        worker_data.merge_node_config(from_node_path=tmp_path / "nope")


def test_the_two_files_a_node_is_meant_to_differ_on_are_never_carried_back() -> None:
    """`store_config.json` is what says "this node writes to its own sqlite" — carrying it back
    would point the master at a test node's file. `telegram_config.json` holds `update_offset`,
    the getUpdates cursor: runtime bookkeeping wearing a config file's name, and a newer offset
    copied onto a node that has not consumed those updates skips messages, silently."""
    assert worker_data.NEVER_CARRIED_BACK == ("store_config.json", "telegram_config.json")

    merged = {name for name, _key, _fields in worker_data.MERGED_ON_DEPLOY}
    merged |= {name for name, _key, _fields, _paths in worker_data.FIELD_MERGED_ON_DEPLOY}
    assert not merged & set(worker_data.NEVER_CARRIED_BACK), (
        "a file listed as never-carried-back must not also be in a merge plan")
