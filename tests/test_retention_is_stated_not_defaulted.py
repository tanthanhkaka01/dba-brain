"""How long staged backups are kept must be *stated*, and the cleanup must be *visible*.

Asked on 2026-09-11 by an operator who found a 15-day-old `.bak` still on a restore target and went
looking for the setting that should have removed it. Four things came out of that:

* The field exists and was **optional** — six of this estate's fourteen restore entries carried
  none at all and were silently running on whatever the release's default happened to be. An
  absent field reads exactly like a considered one.
* The backup side's own retention was optional in the same way. A backup directory nobody prunes
  fills the disk, and "keep forever" should be something somebody wrote down.
* There were **two fields and two units** for one idea — `target_retention_seconds` on a restore
  and `retention_days` on a backup job — which the operator asked about directly: *"why both?"*
  They are one field now, `cleanup_retention`, in seconds, read by both halves and acted on
  differently: a backup prunes what it wrote, a restore prunes what it staged.
* `delete-backup` had **no `--dry-run`**, though the engine underneath has always accepted one — so
  "what would this remove" could only be answered by removing it.
* The cleanup phase wrote **nothing to the store**. Across 254 recorded workflow runs there was not
  one row saying whether retention had pruned anything; the only trace was a log line inside the
  worker container. `SUCCESS` covered both "pruned thirteen files" and "scanned nothing".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from db_ops.backup_restore.config import (
    DEFAULT_CLEANUP_RETENTION,
    parse_cleanup_retention,
)

EIGHT_DAYS = 8 * 24 * 3600


def test_the_recommended_default_is_eight_days():
    # Eight, not fourteen: the full backup is weekly, so an 8-day window always keeps the newest
    # full and every incremental chained to it, with a margin rather than a tight fit.
    assert DEFAULT_CLEANUP_RETENTION == EIGHT_DAYS


def test_an_absent_retention_is_refused_rather_than_defaulted():
    with pytest.raises(ValueError) as excinfo:
        parse_cleanup_retention({}, context="backup_restore.restores[0]")
    message = str(excinfo.value)
    assert "required" in message
    # The refusal has to carry the recommended value and what 0 means, or the operator has to go
    # and read the source to answer it.
    assert str(EIGHT_DAYS) in message
    assert "0 removes the age gate" in message
    assert "backup_restore.restores[0]" in message


def test_zero_is_allowed_and_means_no_age_gate():
    # Not "keep everything": the chain rule alone then decides, which is "clear what the next
    # restore does not need".
    assert parse_cleanup_retention({"cleanup_retention": 0}, context="x") == 0


def test_a_negative_retention_is_refused():
    with pytest.raises(ValueError):
        parse_cleanup_retention({"cleanup_retention": -1}, context="x")


def test_a_non_numeric_retention_is_refused():
    with pytest.raises(ValueError):
        parse_cleanup_retention({"cleanup_retention": "eight days"}, context="x")


#: This estate's own restore config, which is **not** part of the distribution - `data/*.json` does
#: not ship, only `data/*.example.json`. The two checks below are about what this operator actually
#: runs, so they are worth keeping and worth running here; in a public checkout there is nothing for
#: them to read and a hard failure there says "the product is broken" about a file that was never
#: meant to be present.
ESTATE_RESTORE_CONFIG = Path("data/restore_config.json")


def _estate_entries(section: str):
    if not ESTATE_RESTORE_CONFIG.is_file():
        pytest.skip("no estate restore_config.json in this tree - the distribution ships examples only")
    with ESTATE_RESTORE_CONFIG.open(encoding="utf-8") as handle:
        return json.load(handle)["backup_restore"][section]


def test_every_restore_entry_in_this_estate_states_its_retention():
    """The six that did not were all inactive, which is why nobody noticed."""
    entries = _estate_entries("restores")
    missing = [entry.get("restore_id") for entry in entries
               if entry.get("cleanup_retention") is None]
    assert not missing, f"restore entries with no cleanup_retention: {missing}"


def test_every_backup_job_in_this_estate_states_its_retention():
    entries = _estate_entries("backups")
    missing = [f"{entry.get('backup_id')}.{job.get('job')}"
               for entry in entries for job in (entry.get("jobs") or [])
               if job.get("cleanup_retention") in (None, "")]
    assert not missing, f"backup jobs with no cleanup_retention: {missing}"


def test_a_backup_job_without_retention_is_refused(tmp_path):
    from db_ops.backup_restore.backup import load_backup_jobs

    config = {"backup_restore": {"backups": [{
        "backup_id": "B1", "db_type": "sqlserver", "server_id": "S1", "active": True,
        "backup_dir": "/backup",
        "jobs": [{"job": "database", "script": "x.ps1"}],
    }]}}
    path = tmp_path / "restore_config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        load_backup_jobs(path)
    assert "cleanup_retention is required" in str(excinfo.value)


def test_delete_backup_offers_a_dry_run():
    """The engine has always accepted `dry_run`; only the flag was missing."""
    from db_ops.backup_restore import cli

    args = cli.parse_args(["delete-backup", "--retention-seconds", "691200", "--dry-run"])
    assert args.dry_run is True
    assert cli.parse_args(["delete-backup"]).dry_run is False


def test_the_delete_override_is_in_seconds_and_the_hours_form_is_converted_in_the_open():
    """It used to be the other way round: the flag was hours and the *config* was divided.

    `max(1, seconds // 3600)` sat between the configured value and the engine, so a retention of
    less than an hour became an hour and nothing in the output said so. A setting the tool quietly
    replaces is worse than one it refuses.
    """
    from db_ops.backup_restore.cli import _retention_override

    assert _retention_override(1800, None) == 1800
    assert _retention_override(None, 48) == 48 * 3600
    # Nothing given means "whatever the entry states", which is now always something.
    assert _retention_override(None, None) is None
    # Seconds win when both are given: the deprecated spelling never overrides the current one.
    assert _retention_override(90, 48) == 90


def test_one_field_name_and_one_unit_for_both_halves():
    """The backup job and the restore entry read the same key through the same parser."""
    from db_ops.lib import cleanup_retention

    assert cleanup_retention.FIELD == "cleanup_retention"
    assert cleanup_retention.parse({"cleanup_retention": 3600}, context="x") == 3600
    # The old spellings still load a config written before this, each converted to seconds rather
    # than guessed at.
    assert cleanup_retention.parse({"retention_days": 14}, context="x") == 14 * 86400
    assert cleanup_retention.parse({"target_retention_seconds": 600}, context="x") == 600


def test_a_backup_job_states_its_retention_in_seconds_and_days_are_derived():
    """`retention_days` survives as a *derived* reading, for the backup scripts' env var and the
    day-based planner. It is not a second setting: nothing writes it and nothing parses it."""
    import dataclasses

    from db_ops.backup_restore.backup import BackupJob

    assert "retention_days" not in {f.name for f in dataclasses.fields(BackupJob)}


def test_the_cleanup_phase_announces_itself_to_the_store():
    """`DELETE_START` / `DELETE_DONE`, like every other phase of the workflow.

    Without them the store held `copy_start`, `copy_done`, `metadata_start`, `metadata_done`,
    `start`, `end`, `error` — and nothing at all for the one phase that deletes files.
    """
    import inspect

    from db_ops.backup_restore import cli

    source = inspect.getsource(cli)
    assert '_say("DELETE_START"' in source
    assert '_say("DELETE_DONE"' in source
    # The counts, not just the verdict: SUCCESS alone could not tell thirteen deletions from none.
    assert '"files_considered": _considered' in source
    assert '"deleted": _deleted' in source
