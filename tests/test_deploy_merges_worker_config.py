"""What a deploy does with config the worker added while the master was not looking.

A deploy assembles its bundle from the master's `data/` and then overwrites the worker's copy
wholesale. Everything the bot registers at runtime — a SQL task from `/spbot_add_sql`, a chat or
a user the poller discovered — exists only on the worker until someone pulls it back. On
2026-07-31 a deploy silently deleted a task an operator had added minutes earlier; it survived
only because its run history recorded the resolved target. These tests pin the merge that makes
that impossible, and the direction it resolves conflicts in.
"""

from __future__ import annotations

import json
import stat as stat_mod

import pytest

from db_ops.control import worker_data


class _FakeStat:
    def __init__(self, size=10):
        self.st_size = size
        self.st_mode = stat_mod.S_IFREG | 0o644


class _FakeSFTP:
    def __init__(self, worker_files, unreadable=()):
        self.worker_files = dict(worker_files)
        # Present but not openable by this SSH user — `stat` needs only directory search, so it
        # still answers. That asymmetry is the whole point of the case.
        self.unreadable = set(unreadable)

    def stat(self, remote_path):
        name = remote_path.rsplit("/", 1)[-1]
        if name not in self.worker_files and name not in self.unreadable:
            raise IOError("missing")
        return _FakeStat()

    def get(self, remote_path, local_path):
        name = remote_path.rsplit("/", 1)[-1]
        if name in self.unreadable:
            raise IOError("Permission denied")
        if name not in self.worker_files:
            raise IOError("missing")
        with open(local_path, "w", encoding="utf-8") as fh:
            fh.write(self.worker_files[name])

    def close(self):
        pass


class _FakeClient:
    def __init__(self, sftp):
        self._sftp = sftp

    def open_sftp(self):
        return self._sftp

    def close(self):
        pass


def _task(sql_id, *, name, interval=300):
    return {"sql_id": sql_id, "target_no": 1, "server_id": "ACME-192-0-2-248",
            "db_type": "sqlserver", "sql_name": name,
            "time_window": {"repeat_interval": interval, "timeout": 1800}}


def _merge(tmp_path, monkeypatch, *, master, worker, dry_run=False, unreadable=()):
    for name, payload in master.items():
        (tmp_path / name).write_text(json.dumps(payload), encoding="utf-8")
    sftp = _FakeSFTP({name: json.dumps(payload) for name, payload in worker.items()},
                     unreadable=unreadable)
    monkeypatch.setattr(worker_data, "ssh_connect", lambda *a, **k: _FakeClient(sftp))
    return worker_data.merge_worker_config(
        host="h", user="u", password="p", to_master_path=str(tmp_path), dry_run=dry_run)


# --------------------------------------------------------------------------- #
# The rule
# --------------------------------------------------------------------------- #
def test_a_task_only_the_worker_has_survives_the_deploy(tmp_path, monkeypatch):
    """The exact loss that happened: sql_id 17 was added through the bot, the next deploy
    shipped the master's copy (8..16), and the entry was gone."""
    added = _merge(
        tmp_path, monkeypatch,
        master={"sql_targets.json": {"sql_targets": [_task(16, name="punch")]}},
        worker={"sql_targets.json": {"sql_targets": [
            _task(16, name="punch"), _task(17, name="innotex", interval=-1)]}},
    )

    merged = json.loads((tmp_path / "sql_targets.json").read_text(encoding="utf-8"))["sql_targets"]
    assert added == 1
    assert [item["sql_id"] for item in merged] == [16, 17]
    # ... and it keeps the manual schedule it was created with.
    assert merged[1]["time_window"]["repeat_interval"] == -1


def test_the_master_wins_a_shared_record_so_an_edit_is_not_reverted(tmp_path, monkeypatch):
    """A record on both sides was usually *edited* on the master. Taking the worker's copy would
    roll that edit back with nothing said — the worker still holds whatever it had before."""
    added = _merge(
        tmp_path, monkeypatch,
        master={"sql_targets.json": {"sql_targets": [_task(16, name="punch", interval=18000)]}},
        worker={"sql_targets.json": {"sql_targets": [_task(16, name="punch", interval=300)]}},
    )

    merged = json.loads((tmp_path / "sql_targets.json").read_text(encoding="utf-8"))["sql_targets"]
    assert added == 0
    assert merged[0]["time_window"]["repeat_interval"] == 18000


def test_a_target_is_identified_by_sql_id_and_target_no_together(tmp_path, monkeypatch):
    """One task runs against several servers as target_no 1..N. Keying on sql_id alone would
    treat a second server as a duplicate of the first and drop it."""
    second = {**_task(15, name="engine"), "target_no": 2, "server_id": "ACME-192-0-2-111"}
    added = _merge(
        tmp_path, monkeypatch,
        master={"sql_targets.json": {"sql_targets": [_task(15, name="engine")]}},
        worker={"sql_targets.json": {"sql_targets": [_task(15, name="engine"), second]}},
    )

    merged = json.loads((tmp_path / "sql_targets.json").read_text(encoding="utf-8"))["sql_targets"]
    assert added == 1
    assert [(i["sql_id"], i["target_no"]) for i in merged] == [(15, 1), (15, 2)]


def test_new_chats_and_users_the_bot_discovered_are_carried_over(tmp_path, monkeypatch):
    """The poller writes every chat and user it sees into these files. They only ever exist on
    the worker, so a deploy without the merge un-registers them and the bot stops answering
    there until someone notices."""
    added = _merge(
        tmp_path, monkeypatch,
        master={
            "telegram_groups.json": {"telegram_groups": [
                {"group_id": "-100", "title": "Ops - Logging", "allow_command": 100}]},
            "telegram_users.json": {"telegram_users": [{"user_id": "1", "user_type": 100}]},
        },
        worker={
            "telegram_groups.json": {"telegram_groups": [
                # the master raised allow_command for this one; the worker still has the default
                {"group_id": "-100", "title": "Ops - Logging", "allow_command": 0},
                {"group_id": "-200", "title": "New group", "allow_command": 0}]},
            "telegram_users.json": {"telegram_users": [
                {"user_id": "1", "user_type": 100}, {"user_id": "2", "user_type": 0}]},
        },
    )

    groups = json.loads((tmp_path / "telegram_groups.json").read_text(encoding="utf-8"))["telegram_groups"]
    users = json.loads((tmp_path / "telegram_users.json").read_text(encoding="utf-8"))["telegram_users"]
    assert added == 2
    assert [g["group_id"] for g in groups] == ["-100", "-200"]
    assert groups[0]["allow_command"] == 100, "the master's permission edit must not be reverted"
    assert [u["user_id"] for u in users] == ["1", "2"]


def test_dry_run_reports_without_writing(tmp_path, monkeypatch):
    added = _merge(
        tmp_path, monkeypatch,
        master={"sql_commands.json": {"sql_commands": [{"sql_id": 16}]}},
        worker={"sql_commands.json": {"sql_commands": [{"sql_id": 16}, {"sql_id": 17}]}},
        dry_run=True,
    )

    on_disk = json.loads((tmp_path / "sql_commands.json").read_text(encoding="utf-8"))
    assert added == 1
    assert [item["sql_id"] for item in on_disk["sql_commands"]] == [16]


def test_a_file_missing_on_either_side_is_skipped_not_emptied(tmp_path, monkeypatch):
    """A worker that predates a config file, or a master that does not ship one, must not end up
    with an empty list written over the side that does have records."""
    added = _merge(
        tmp_path, monkeypatch,
        master={"sql_targets.json": {"sql_targets": [_task(16, name="punch")]}},
        worker={},  # nothing on the worker at all
    )

    merged = json.loads((tmp_path / "sql_targets.json").read_text(encoding="utf-8"))["sql_targets"]
    assert added == 0
    assert [item["sql_id"] for item in merged] == [16]


def test_a_file_that_exists_but_cannot_be_read_aborts_instead_of_being_skipped(tmp_path, monkeypatch):
    """The failure that made the merge useless without looking broken.

    The worker container runs as root and rewrote `db_instances.json` as root 0600; the master
    reads the worker over SFTP as `tuser` and got permission denied. That was caught by the same
    `except IOError` as a missing file, printed "not on worker", and the deploy carried on to
    overwrite the operator's toggle with the master's copy. Absent means "there is nothing to
    merge"; unreadable means "there may be everything to merge and I cannot see it" — and the
    step that follows destroys it. Nothing has been built or shipped yet, so refusing is free.
    """
    with pytest.raises(worker_data.WorkerConfigUnreadable) as excinfo:
        _merge(
            tmp_path, monkeypatch,
            master={"db_instances.json": {"db_instances": [{"server_id": "ACME-1", "ip": "10.0.0.1"}]}},
            worker={},
            unreadable=("db_instances.json",),
        )

    # The message has to name the file and point at ownership, or the operator retries the deploy.
    assert "db_instances.json" in str(excinfo.value)
    assert "ownership" in str(excinfo.value)
    # And the master's copy is left exactly as it was — no partial merge written.
    on_disk = json.loads((tmp_path / "db_instances.json").read_text(encoding="utf-8"))
    assert on_disk["db_instances"] == [{"server_id": "ACME-1", "ip": "10.0.0.1"}]


# --------------------------------------------------------------------------- #
# The pure function
# --------------------------------------------------------------------------- #
def test_merge_keeps_master_order_and_appends_worker_records():
    master = [{"sql_id": 8}, {"sql_id": 11}]
    worker = [{"sql_id": 11}, {"sql_id": 17}, {"sql_id": 18}]

    merged, added = worker_data.merge_record_lists(
        master=master, worker=worker, key_fields=("sql_id",))

    assert [item["sql_id"] for item in merged] == [8, 11, 17, 18]
    assert added == [("17",), ("18",)]


@pytest.mark.parametrize("junk", [None, "text", 42, []])
def test_non_records_in_either_list_are_ignored(junk):
    """A hand-edited config with a stray value must not crash a deploy."""
    merged, added = worker_data.merge_record_lists(
        master=[{"sql_id": 8}, junk], worker=[junk, {"sql_id": 17}], key_fields=("sql_id",))

    assert added == [("17",)]
    assert {item["sql_id"] for item in merged if isinstance(item, dict)} == {8, 17}


# --------------------------------------------------------------------------- #
# db_instances.json: the worker EDITS records, it does not append them
# --------------------------------------------------------------------------- #
def _instance(server_id, *, metrics=None, ip="10.0.0.1", report_policy=None):
    record = {"server_id": server_id, "ip": ip, "port": 1433, "db_type": "sqlserver",
              "default_credential_name": "cred", "metrics": metrics or {"enabled": True}}
    if report_policy is not None:
        record["report_policy"] = report_policy
    return record


def test_a_metric_switched_off_through_the_bot_survives_the_deploy(tmp_path, monkeypatch):
    """spbot_metric_toggle edits an existing record, so a union by server_id would keep the
    master's copy whole and throw the toggle away — the file would look merged while the metric
    the operator switched off overnight quietly came back on."""
    applied = _merge(
        tmp_path, monkeypatch,
        master={"db_instances.json": {"db_instances": [
            _instance("ACME-1", metrics={"enabled": True, "collector_env": {"SVC": "x"}})]}},
        worker={"db_instances.json": {"db_instances": [
            _instance("ACME-1", metrics={
                "enabled": True,
                "collector_env": {"SVC": "x"},
                "disabled_collector_types": ["cmd"],
                "metric_overrides": {"OS_DISK_USAGE": {"enabled": False}},
            })]}},
    )

    merged = json.loads((tmp_path / "db_instances.json").read_text(encoding="utf-8"))["db_instances"]
    assert applied == 2
    assert merged[0]["metrics"]["disabled_collector_types"] == ["cmd"]
    assert merged[0]["metrics"]["metric_overrides"]["OS_DISK_USAGE"]["enabled"] is False


def test_the_master_keeps_the_inventory_fields_the_worker_never_owns(tmp_path, monkeypatch):
    """Connection details are master-owned. A worker running a stale copy must not be able to
    push an old ip or credential back over an inventory correction — that would point a
    collector at the wrong host and look like a network fault."""
    applied = _merge(
        tmp_path, monkeypatch,
        master={"db_instances.json": {"db_instances": [_instance("ACME-1", ip="10.0.0.99")]}},
        worker={"db_instances.json": {"db_instances": [
            _instance("ACME-1", ip="10.0.0.1", metrics={"enabled": False})]}},
    )

    merged = json.loads((tmp_path / "db_instances.json").read_text(encoding="utf-8"))["db_instances"]
    assert merged[0]["ip"] == "10.0.0.99", "master's inventory correction must win"
    assert merged[0]["metrics"]["enabled"] is False, "worker's toggle must win"
    assert applied == 1


def test_collector_env_is_not_dragged_along_with_the_toggles(tmp_path, monkeypatch):
    """The worker-owned paths are leaves for a reason: metrics also holds collector_env and
    severity_map, which the toggle never writes. Overlaying the whole metrics object would
    revert a master edit to those."""
    _merge(
        tmp_path, monkeypatch,
        master={"db_instances.json": {"db_instances": [
            _instance("ACME-1", metrics={"enabled": True, "collector_env": {"SVC": "new"}})]}},
        worker={"db_instances.json": {"db_instances": [
            _instance("ACME-1", metrics={"enabled": False, "collector_env": {"SVC": "old"}})]}},
    )

    merged = json.loads((tmp_path / "db_instances.json").read_text(encoding="utf-8"))["db_instances"]
    assert merged[0]["metrics"]["collector_env"] == {"SVC": "new"}
    assert merged[0]["metrics"]["enabled"] is False


def test_a_path_absent_on_the_worker_is_left_as_the_master_has_it(tmp_path, monkeypatch):
    """Absent is not the same as null. Writing the worker's missing key as None would resurrect
    a toggle the worker no longer has, as a value nothing knows how to read."""
    applied = _merge(
        tmp_path, monkeypatch,
        master={"db_instances.json": {"db_instances": [
            _instance("ACME-1", metrics={"enabled": True, "disabled_collector_types": ["sql"]})]}},
        worker={"db_instances.json": {"db_instances": [
            _instance("ACME-1", metrics={"enabled": True})]}},
    )

    merged = json.loads((tmp_path / "db_instances.json").read_text(encoding="utf-8"))["db_instances"]
    assert applied == 0
    assert merged[0]["metrics"]["disabled_collector_types"] == ["sql"]


def test_a_lab_database_registered_on_the_worker_is_added_whole(tmp_path, monkeypatch):
    """create-db-docker registers a new server the master has never heard of. That record can
    only be an addition, so it comes across intact rather than field by field."""
    applied = _merge(
        tmp_path, monkeypatch,
        master={"db_instances.json": {"db_instances": [_instance("ACME-1")]}},
        worker={"db_instances.json": {"db_instances": [
            _instance("ACME-1"), _instance("PGLAB-5433", ip="192.0.2.249")]}},
    )

    merged = json.loads((tmp_path / "db_instances.json").read_text(encoding="utf-8"))["db_instances"]
    assert applied == 1
    assert [i["server_id"] for i in merged] == ["ACME-1", "PGLAB-5433"]


def test_report_policy_disabled_codes_come_from_the_worker(tmp_path, monkeypatch):
    """Enabling a metric also removes it from the legacy disabled_metric_codes list, which
    blocks collection the same way. Merging only the metrics block would leave the metric
    switched on and still not collected."""
    _merge(
        tmp_path, monkeypatch,
        master={"db_instances.json": {"db_instances": [
            _instance("ACME-1", report_policy={"enabled": True,
                                              "disabled_metric_codes": ["OS_DISK_USAGE"],
                                              "severity_overrides": {"X": "WARNING"}})]}},
        worker={"db_instances.json": {"db_instances": [
            _instance("ACME-1", report_policy={"enabled": True,
                                              "disabled_metric_codes": [],
                                              "severity_overrides": {}})]}},
    )

    merged = json.loads((tmp_path / "db_instances.json").read_text(encoding="utf-8"))["db_instances"]
    assert merged[0]["report_policy"]["disabled_metric_codes"] == []
    # ... but severity_overrides is not a worker-owned path, so the master's stays.
    assert merged[0]["report_policy"]["severity_overrides"] == {"X": "WARNING"}


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("indent", [1, 2, 4])
def test_the_file_keeps_the_indent_it_already_used(tmp_path, monkeypatch, indent):
    """These configs are read by people and tracked in git, and they do not agree on indent:
    db_instances.json is 1 space, the Telegram files 2, the SQL ones 4. Re-indenting one on a
    merge turns a two-line change into a whole-file diff and buries what actually changed."""
    path = tmp_path / "sql_commands.json"
    path.write_text(json.dumps({"sql_commands": [{"sql_id": 16}]}, indent=indent) + "\n",
                    encoding="utf-8")
    sftp = _FakeSFTP({"sql_commands.json": json.dumps(
        {"sql_commands": [{"sql_id": 16}, {"sql_id": 17}]})})
    monkeypatch.setattr(worker_data, "ssh_connect", lambda *a, **k: _FakeClient(sftp))

    worker_data.merge_worker_config(host="h", user="u", password="p",
                                    to_master_path=str(tmp_path))

    second_line = path.read_text(encoding="utf-8").splitlines()[1]
    assert len(second_line) - len(second_line.lstrip(" ")) == indent
