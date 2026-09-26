"""`server_id` is the only key for a machine: every record has one, and no two share it (rules R21).

Everything joins on it - the metrics collector groups a pass by it so one machine is touched one
call at a time, reports and the fleet page key their rows by it, `check-credentials` names targets
by it. An ip cannot do that job: several instances can share one (HA lab containers on one host,
different ports), and one machine can answer on two. A record without a `server_id`, or two with
the same one, is the inventory lying about which machine is which, and every join inherits it.
`instance-add` refuses a duplicate as it registers (`test_instance_add.py`); this holds the files
themselves to it, since a hand edit goes around the registrar.
"""

from __future__ import annotations

import collections
import json
from pathlib import Path

import pytest

DATA = Path(__file__).resolve().parents[1] / "data"
INVENTORIES = [path for path in (DATA / "db_instances.json", DATA / "db_instances.example.json")
               if path.is_file()]


@pytest.mark.parametrize("path", INVENTORIES, ids=lambda path: path.name)
def test_every_record_has_a_server_id_and_none_is_used_twice(path):
    records = json.loads(path.read_text(encoding="utf-8-sig")).get("db_instances", [])
    ids = [str(record.get("server_id") or "").strip() for record in records]
    assert "" not in ids, f"{path.name}: {ids.count('')} record(s) without a server_id"
    twice = sorted(key for key, count in collections.Counter(ids).items() if count > 1)
    assert not twice, f"{path.name}: server_id used by more than one record: {twice}"


def test_there_is_an_inventory_to_check():
    assert INVENTORIES, "neither data/db_instances.json nor its example is here"
