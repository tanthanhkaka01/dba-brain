"""The nine records that decide when every app runs, and the years nothing could edit them.

Every other kind of record in ``data/`` had a registrar. A SQL task has two, an instance has one, a
remote credential has one. ``app_commands.json`` — which holds the schedule of every app on the node —
had none, so the only way to change one was to open the file on whichever node you were looking at.

That is what §1.11 measured: on 2026-09-21 the estate, the 0.21.0 soak node and the **shipped
catalogue** held three different schedules for ``APP-BACKUP-RESTORE`` at the same time — 60/300,
30/30 and 300/300. The documented "300 → 30" fix had been applied by hand on one node, so a new
install still got 300 and there was nowhere to see the disagreement from.

These tests are about the two properties that make the command worth having over an editor: it
**refuses** what an editor would happily let you do, and it **says what it changed**.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from db_ops.common import app_command_admin


@pytest.fixture()
def estate(tmp_path):
    """A data dir with one app command and the reference that describes the shape."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "app_commands.json").write_text(json.dumps({
        "app_commands": [
            {"app_command_id": "1", "app_ord": 1, "app_code": "APP-BACKUP-RESTORE",
             "app_name": "backup_restore", "display_name": "Backup / Restore",
             "log_scope": "backup", "working_dir": "tools/db_ops",
             "command_text": "python -m db_ops.backup_restore.cli backup",
             "active": True, "node_role": "worker", "run_mode": "async", "max_parallel": 4,
             "time_window": {"repeat_interval": 300, "retry_interval": 300, "timeout": 7200},
             "note": "as shipped"},
            {"app_command_id": "2", "app_ord": 2, "app_code": "APP-METRICS",
             "app_name": "metrics", "display_name": "Metrics", "log_scope": "metrics",
             "working_dir": "tools/db_ops", "command_text": "python -m db_ops.metrics.cli collect",
             "active": True, "node_role": "all", "run_mode": "sync", "max_parallel": 1,
             "time_window": {"repeat_interval": 120, "retry_interval": 60, "timeout": 1800},
             "note": ""},
        ]}, indent=4), encoding="utf-8")
    # The command reads the field list from the reference, so the reference has to be here.
    #
    # Sourced from the PACKAGED catalogue rather than from `data/`. The three copies are byte
    # identical, but only this one exists everywhere: the public tree ships `data/*.example.json`
    # and no `data/shared_config_objects.json`, so a fixture reading that path passes here and
    # errors in `dba-brain` - which is what it did, fifteen times, and is the same shape as the
    # four public-tree-only failures that made v0.20.0's CI red.
    import shutil

    from db_ops.lib import shared_objects
    shutil.copy(Path(shared_objects.__file__).resolve().parents[1]
                / "common" / "catalogue" / "shared_config_objects.json",
                data / "shared_config_objects.json")
    return data


def read(estate, code="APP-BACKUP-RESTORE"):
    rows = json.loads((estate / "app_commands.json").read_text(encoding="utf-8"))["app_commands"]
    return [r for r in rows if r["app_code"] == code][0]


def set_it(estate, **request):
    request.setdefault("app_code", "APP-BACKUP-RESTORE")
    request["data_dir"] = str(estate)
    return app_command_admin.set_app_command(request)


# --------------------------------------------------------------------------- #
# Saying what changed — the reason this beats an editor
# --------------------------------------------------------------------------- #
def test_a_window_edit_names_the_field_that_moved_not_the_whole_block(estate):
    """`time_window <fourteen keys> -> <fourteen keys>` is technically the change and tells nobody
    which number moved. The first run of this command printed exactly that, truncated."""
    out = set_it(estate, time_window={"repeat_interval": 30})

    assert out["written"] is True
    assert out["message"] == "APP-BACKUP-RESTORE: time_window.repeat_interval 300 -> 30"
    assert out["changes"] == [
        {"field": "time_window.repeat_interval", "from": 300, "to": 30}]


def test_changing_nothing_says_so_and_writes_nothing(estate):
    """Also found by running it: the first version reported `written: true` for a value that was
    already in place, because a stored three-key window never equals the canonical fourteen-key form.
    Both sides are normalised before the comparison now — a no-op that claims a write is the same
    kind of noise this command exists to remove."""
    before = (estate / "app_commands.json").read_text(encoding="utf-8")

    out = set_it(estate, time_window={"repeat_interval": 300})

    assert out["written"] is False
    assert out["changes"] == []
    assert "already carries those values" in out["message"]
    assert (estate / "app_commands.json").read_text(encoding="utf-8") == before


def test_several_fields_are_all_reported(estate):
    out = set_it(estate, active=False, max_parallel=2,
                 time_window={"repeat_interval": 30, "weekdays": [1, 7]})

    moved = {c["field"] for c in out["changes"]}
    assert moved == {"active", "max_parallel",
                     "time_window.repeat_interval", "time_window.weekdays"}


# --------------------------------------------------------------------------- #
# A window edit is PARTIAL — the fault this command was written to stop
# --------------------------------------------------------------------------- #
def test_editing_one_window_field_keeps_the_rest(estate):
    """Replacing the whole block would silently drop `timeout` or `weekdays` from a record whose
    caller only meant to retune the interval — the shape of §1.11 itself, applied to the fix for it.
    """
    set_it(estate, time_window={"repeat_interval": 30})
    window = read(estate)["time_window"]

    assert window["repeat_interval"] == 30
    assert window["retry_interval"] == 300
    assert window["timeout"] == 7200


def test_weekdays_survives_a_later_interval_edit(estate):
    """The specific loss that would be invisible: a weekday-gated command retuned by someone who
    never mentioned days would start running every day."""
    set_it(estate, time_window={"weekdays": [7]})

    set_it(estate, time_window={"repeat_interval": 60})

    assert read(estate)["time_window"]["weekdays"] == [7]


# --------------------------------------------------------------------------- #
# What it refuses, and why each refusal is not an oversight
# --------------------------------------------------------------------------- #
# `app_code` is in FROZEN_FIELDS as defence but cannot be reached through this path, and that is
# right rather than a gap: it is the SELECTOR. A request naming a different app_code does not rename
# this record, it addresses that one - which is why renaming is impossible here rather than refused.
@pytest.mark.parametrize("field,value", [
    ("command_text", "python -m db_ops.metrics.cli collect"),
    ("log_scope", "other"),
    ("app_command_id", "99"),
    ("working_dir", "/tmp"),
    ("app_name", "renamed"),
])
def test_identity_and_wiring_cannot_be_edited(estate, field, value):
    """`command_text` names the module that runs, so pointing it elsewhere is a release rather than a
    configuration edit; the rest are how other records and log files address this one. An editor lets
    you do every one of them, which is why they are named here rather than merely discouraged."""
    with pytest.raises(app_command_admin.AppCommandAdminError) as raised:
        set_it(estate, **{field: value})

    assert field in str(raised.value)
    assert "Editable:" in str(raised.value)


def test_a_field_that_is_not_a_field_is_refused_with_the_list(estate):
    with pytest.raises(app_command_admin.AppCommandAdminError) as raised:
        set_it(estate, frequency=5)

    message = str(raised.value)
    assert "'frequency' is not a field" in message
    assert "time_window" in message and "max_parallel" in message


def test_it_will_not_invent_an_app_command(estate):
    """A tenth record would schedule code that does not exist. Adding one is a release that ships the
    app it names, so the refusal lists what there is instead of creating what was asked for."""
    with pytest.raises(app_command_admin.AppCommandAdminError) as raised:
        app_command_admin.set_app_command(
            {"app_code": "APP-INVENTED", "active": True, "data_dir": str(estate)})

    message = str(raised.value)
    assert "does not create one" in message
    assert "APP-BACKUP-RESTORE" in message and "APP-METRICS" in message


def test_an_edit_with_no_fields_is_refused_rather_than_written(estate):
    with pytest.raises(app_command_admin.AppCommandAdminError):
        app_command_admin.set_app_command(
            {"app_code": "APP-METRICS", "data_dir": str(estate)})


def test_a_value_the_reference_refuses_is_not_written(estate):
    """The edited record is checked against `shared_config_objects.json` before it reaches disk, so a
    bad value is a refusal rather than something the next daemon sweep discovers."""
    before = (estate / "app_commands.json").read_text(encoding="utf-8")

    with pytest.raises(app_command_admin.AppCommandAdminError):
        set_it(estate, time_window={"weekdays": [0]})

    assert (estate / "app_commands.json").read_text(encoding="utf-8") == before


# --------------------------------------------------------------------------- #
# The field list is read, not restated
# --------------------------------------------------------------------------- #
def test_the_editable_fields_come_from_the_reference(estate):
    """`app_command` is described field by field as data (§1.10) precisely so that a second list does
    not have to be kept in step with it. A hand-written list here would be a second opinion."""
    import inspect

    fields = set(app_command_admin.editable_fields(data_dir=estate))

    assert "time_window" in fields and "active" in fields
    assert fields.isdisjoint(app_command_admin.FROZEN_FIELDS)
    source = inspect.getsource(app_command_admin.editable_fields)
    assert "shared_objects.field_names" in source
