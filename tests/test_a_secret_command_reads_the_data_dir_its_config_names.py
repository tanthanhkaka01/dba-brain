"""A secret-touching `common.cli` command reads the data dir of the config it was given, or refuses.

`rotate-password`, `check-secret`, `check-secret-literals` and `check-identifiers` read the secret
store and the inventory. They looked for `data_dir` on the loaded config - a field it does not have
- and fell back to the process's default data dir whenever the config could not be read. So
`rotate-password --config D:/other/config.json` rotated, read and rewrote the store of whatever root
the process stood in (owner decision G3.1). A named config must load, and its own `data/` is used;
with none named, the node's own data dir, as for every other config-file command.
"""

from __future__ import annotations

import json

import pytest

from db_ops.common import cli


def test_a_named_config_reads_its_own_data_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("DB_OPS_DATA_DIR", raising=False)
    config = tmp_path / "other_root" / "config.json"
    config.parent.mkdir()
    config.write_text("{}", encoding="utf-8")

    assert cli._config_data_dir(str(config), named=True) == (tmp_path / "other_root" / "data").resolve()


def test_no_config_named_is_the_node_s_own_data_dir():
    assert cli._config_data_dir("config.json", named=False) is None


@pytest.mark.parametrize("command", ["rotate-password", "check-secret", "check-secret-literals",
                                     "check-identifiers"])
def test_an_unreadable_named_config_is_refused_and_nothing_is_done(command, tmp_path, capsys):
    missing = tmp_path / "nowhere" / "config.json"
    request = tmp_path / "request.json"
    request.write_text("{}", encoding="utf-8")

    code = cli.main([command, "--config", str(missing), f"@{request}"])

    answer = json.loads(capsys.readouterr().out)
    assert code != 0 and answer["success"] is False
    assert "cannot be read" in answer["error"] and "never from a default" in answer["error"]
