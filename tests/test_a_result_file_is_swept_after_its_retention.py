"""Result files under ``runtime/output`` are swept by age, and nothing outside that folder is touched.

Query results, xlsx exports and config exports - often business data - were written there and
never removed: the folder only grew (review 0.25.0, F4.3). The daemon now deletes files older than
``output_retention_days`` (``config.json``, default 7; 0 keeps them for ever), hourly, by age only.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from db_ops.jobs import daemon
from db_ops.lib.config import parse_config


@pytest.fixture(autouse=True)
def fresh_sweep_clock(monkeypatch):
    monkeypatch.setitem(daemon._OUTPUT_SWEEP_STATE, "last_swept_at", float("-inf"))


def _file(path: Path, *, age_days: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    stamp = time.time() - age_days * 86400
    os.utime(path, (stamp, stamp))
    return path


def test_an_old_result_file_goes_and_a_new_one_stays(tmp_path):
    old = _file(tmp_path / "output" / "sql_tasks" / "result_old.xlsx", age_days=10)
    new = _file(tmp_path / "output" / "telegram" / "config_exports" / "export_new.json", age_days=1)
    beside = _file(tmp_path / "db_ops.sqlite", age_days=30)

    removed = daemon.sweep_output_files(runtime_dir=tmp_path, retention_days=7)

    assert removed == 1
    assert not old.exists() and new.exists() and beside.exists()


def test_zero_keeps_every_file(tmp_path):
    old = _file(tmp_path / "output" / "sql_tasks" / "result_old.xlsx", age_days=400)

    assert daemon.sweep_output_files(runtime_dir=tmp_path, retention_days=0) == 0
    assert old.exists()


def test_the_sweep_runs_hourly_not_every_tick(tmp_path):
    _file(tmp_path / "output" / "a.csv", age_days=10)
    daemon.sweep_output_files(runtime_dir=tmp_path, retention_days=7, now=1000.0)
    later = _file(tmp_path / "output" / "b.csv", age_days=10)

    assert daemon.sweep_output_files(runtime_dir=tmp_path, retention_days=7, now=1060.0) == 0
    assert later.exists()


def test_the_retention_is_read_from_config_json_and_refused_when_negative(tmp_path):
    assert parse_config({}, base_dir=tmp_path).output_retention_days == 7
    assert parse_config({"output_retention_days": 30}, base_dir=tmp_path).output_retention_days == 30
    with pytest.raises(ValueError, match="output_retention_days"):
        parse_config({"output_retention_days": -1}, base_dir=tmp_path)
