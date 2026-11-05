"""When an attack shows, dbabrain stops deleting (0.35.0, 1.220 - the plan of 2026-10-29, A3.5).

* one lockdown at a time, recorded in the runtime store with who, when and why; turning it off
  names who and why it is safe; a row is never deleted;
* an unreadable store counts as ON - an unknown state never allows a delete;
* while on: ``prune-backups --apply`` and ``delete-backup`` are refused, a SQL Server restore loses
  its ``overwrite_existing`` (the step then refuses an ONLINE database), ad-hoc SQL that writes is
  refused, the offload copies but deletes nothing at the source;
* the scrub engages it by itself on three or more files changed or gone in one run (D6);
* ``control.cli lockdown`` turns it on and off, never without a reason and a typed yes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dbabrain.db import lockdown_store as L

pytestmark = pytest.mark.real_lockdown


@pytest.fixture
def store(tmp_path):
    return L.LockdownStore(tmp_path / "store.sqlite")


def test_one_lockdown_at_a_time_and_a_second_alarm_is_the_same_one(store):
    first = store.engage(reason="canary changed", by="scrub")
    again = store.engage(reason="three mismatches", by="scrub")
    assert first["already_on"] is False and again["already_on"] is True
    assert again["lockdown_id"] == first["lockdown_id"] and store.active()["reason"] == "canary changed"


def test_off_says_who_and_why_and_the_record_stays(store):
    store.engage(reason="canary changed", by="scrub")
    with pytest.raises(Exception, match="why it is safe"):
        store.release(by="dba", note="")
    ended = store.release(by="dba", note="files checked against the repository")
    assert ended["reason"] == "canary changed" and store.active() is None
    # The answer is the row as it now stands - ended, by whom, why - not the row read before.
    assert ended["ended_at"] and ended["ended_by"] == "dba" and ended["end_note"].startswith("files checked")
    assert store.release(by="dba", note="again") is None
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM lockdowns").fetchone()["n"] == 1


def test_on_needs_a_reason(store):
    with pytest.raises(Exception, match="why it is on"):
        store.engage(reason=" ", by="dba")


def test_an_unreadable_store_counts_as_on():
    assert L.lockdown_on(SimpleNamespace()) is True


def test_a_guard_names_the_lockdown(store):
    store.engage(reason="canary changed", by="scrub")
    with pytest.raises(L.Locked, match="prune-backups --apply is refused: a lockdown is on .*canary changed"):
        store.refuse_if_on("prune-backups --apply")


def test_prune_apply_is_refused_and_a_report_is_not(monkeypatch):
    from dbabrain.backup_restore import prune

    def locked(app_config, action):
        raise L.Locked(f"{action} is refused")

    monkeypatch.setattr(L, "refuse_if_locked", locked)
    with pytest.raises(L.Locked, match="prune-backups --apply"):
        prune.run_prune(app_config=None, config_path="x", apply=True)
    monkeypatch.setattr(prune, "load_backup_jobs", lambda _p: [])
    monkeypatch.setattr(prune, "DbaBrainStore", SimpleNamespace(from_config=lambda c: None))
    monkeypatch.setattr(prune, "_load_secrets", lambda **k: {})
    assert prune.run_prune(app_config=None, config_path="x", apply=False)["configured"] == 0


def test_a_sql_server_restore_loses_its_overwrite(monkeypatch):
    from dbabrain.backup_restore import restore_by_id

    full = {"path": "/b/S/FULL/S.bak", "first_lsn": 1, "last_lsn": 2, "checkpoint_lsn": 1}
    monkeypatch.setattr(restore_by_id, "_list_backup_files", lambda request: {
        "files": [full] if request["kinds"] == ["full"] else [], "newest_finished_at": "2026-11-01 01:00:00"})
    monkeypatch.setattr(restore_by_id, "_visible_dir", lambda job: "/b")
    monkeypatch.setattr(restore_by_id, "_sqlserver_port", lambda job, data_dir=None: 1433)
    job = SimpleNamespace(restore_id="R", env={"MSSQL_DATABASES": "S", "MSSQL_USER": "dba"}, env_secrets={},
                          overwrite_existing=True, checkdb=True)
    for on, expected in ((False, True), (True, False)):
        monkeypatch.setattr(L, "lockdown_on", lambda app_config=None, on=on: on)
        steps = restore_by_id._plan_sqlserver(job, {}, point_in_time="", host={"host": "192.0.2.40"}, data_dir=None)
        assert steps[0]["request"]["overwrite_existing"] is expected


def test_the_scrub_engages_it_on_three_changed_files(monkeypatch, tmp_path):
    import tests.test_every_backup_run_records_its_files_and_their_checksums as receipts
    from dbabrain.backup_restore import backup as backup_module
    from dbabrain.db.backup_catalog import BackupCatalogStore

    engaged = []

    class _Lockdowns:
        @classmethod
        def from_config(cls, config):
            return cls()

        def engage(self, *, reason, by):
            engaged.append((reason, by))
            return {"lockdown_id": 7, "already_on": False}

    monkeypatch.setattr(L, "LockdownStore", _Lockdowns)
    monkeypatch.setattr(backup_module, "verify_backup_files", lambda **k: {
        "checked": 20, "match": 17, "missing": [{"file": "c.bak"}],
        "mismatch": [{"file": "a.bak"}, {"file": "b.bak"}]})
    catalog = BackupCatalogStore(tmp_path / "catalog.sqlite")
    catalog.initialize()
    row = receipts._scheduled(monkeypatch, catalog, receipts._job(),
                              [receipts._line("A", "FULL", "/b/a.bak")])
    assert engaged and "2 changed and 1 gone" in engaged[0][0]
    assert "LOCKDOWN ON" in row["message"] and row["metadata"]["lockdown"]["lockdown_id"] == 7


def test_the_cli_refuses_without_a_reason_or_a_yes(monkeypatch, tmp_path, capsys):
    from dbabrain.control import cli as control_cli

    store = L.LockdownStore(tmp_path / "s.sqlite")
    monkeypatch.setattr(L.LockdownStore, "from_config", classmethod(lambda cls, config, key=None: store))
    monkeypatch.setattr("dbabrain.lib.config.load_config", lambda path=None: SimpleNamespace())
    assert control_cli.main(["lockdown", "on"]) == 2
    assert control_cli.main(["lockdown", "on", "--reason", "canary"]) == 1, "no yes, no change"
    assert control_cli.main(["lockdown", "on", "--reason", "canary", "--confirm", "yes"]) == 0
    assert store.active()["reason"] == "canary"
    assert control_cli.main(["lockdown", "off", "--note", "checked", "--confirm", "yes"]) == 0
    assert store.active() is None
