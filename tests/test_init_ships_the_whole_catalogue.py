"""A new install can collect every metric the package ships, not three of them.

Until 2026-08-23 the two halves of the metric catalogue shipped to different people. The
*collectors* — 150 SQL files, 29 shell and PowerShell scripts — went into the wheel as package
data, so `pip install dbabrain` had them all. The *catalogue* that names them did not: `db-ops
init` wrote three hand-written SQL Server metrics, and the 90-metric catalogue lived in
`data/metric_definitions.example.json`, which only exists for someone who clones the repository.

So an installed package carried every Oracle, MySQL, PostgreSQL, Docker and OS collector on disk
and could reach none of them. The gap was invisible from either side: the packaging test proved
the queries ship, the scaffold test proved `init` writes a valid catalogue, and both were true.

These tests are about the join between them, which is the thing that was missing:

- the catalogue ships at all, and does not drift from the example it was taken from;
- `init` writes it, so a first run has the whole set;
- every variant it names resolves to a file that actually ships — a catalogue entry pointing at a
  query that is not in the wheel is worse than no entry, because it fails at collection time on a
  reader's machine rather than here.
"""
from __future__ import annotations

import json

from db_ops import scaffold
from db_ops.lib.paths import builtin_asset_root

from conftest import shipped_config


def test_the_package_ships_the_whole_catalogue_not_the_starter_set():
    catalogue = scaffold.packaged_catalogue()
    assert catalogue is not scaffold.STARTER_METRICS
    assert len(catalogue["metrics"]) > 50, "the starter set is a fallback, not what init writes"


def test_the_shipped_catalogue_and_the_example_do_not_drift():
    example = json.loads(
        shipped_config("metric_definitions.json").read_text(encoding="utf-8"))
    assert scaffold.packaged_catalogue() == example, (
        "data/metric_definitions.example.json is the documented copy of the catalogue the package "
        "ships; re-copy it to db_ops/metrics/catalogue/ when you change one of them")


def test_a_first_run_gets_the_os_metrics(tmp_path):
    scaffold.initialise(tmp_path, app_name="probe")
    written = json.loads(
        (tmp_path / "data" / "metric_definitions.json").read_text(encoding="utf-8"))
    codes = {metric["metric_code"] for metric in written["metrics"]}
    assert {"OS_CPU_USAGE", "OS_MEMORY_USAGE", "OS_DISK_USAGE"} <= codes, (
        "the OS metrics need no database and are what works on a host before a credential does")


def test_every_variant_in_the_shipped_catalogue_resolves_to_a_shipped_file():
    root = builtin_asset_root("metrics")
    assert root is not None
    missing = []
    for metric in scaffold.packaged_catalogue()["metrics"]:
        # `supported: false` variants carry a reason, not a query — see the same note in
        # test_scaffold_first_run.py.
        files = [metric["file"]] if metric.get("file") else []
        files += [v["file"] for v in metric.get("variants", [])
                  if v.get("file") and v.get("supported") is not False]
        missing += [(metric["metric_code"], f) for f in files if not (root / f).is_file()]
    assert not missing, f"catalogue names files the package does not ship: {missing}"
