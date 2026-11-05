"""A prune that would delete most of an entry's files at once is refused (0.35.0, 1.221).

The ransomware plan of 2026-10-29, phase 2: a retention window removes the oldest few files each run.
Half the folder in one run is something else - a window cut by mistake, a clock gone wrong, or a
folder ransomware has rewritten. ``prune-backups --apply`` refuses it whole (nothing deleted, not
"the first half"), records an ERROR with the reason, and goes past it only with
``--allow-mass-delete``. The newest chain is never removed either way (``_keep_newest_chain``).
"""

from __future__ import annotations

import pytest

from dbabrain.backup_restore import prune
from dbabrain.lib import backupfiles_retention as R


@pytest.mark.parametrize("obsolete, total, refused", [
    (3, 20, False), (10, 20, False), (11, 20, True), (6, 9, False), (20, 20, True),
])
def test_more_than_half_of_ten_or_more_files_is_refused(obsolete, total, refused):
    assert bool(R.mass_delete_refusal(obsolete, total)) is refused


def _plan(obsolete: int, total: int):
    return {"obsolete_paths": [f"/b/f{i}.bak" for i in range(obsolete)],
            "counts": {"obsolete": obsolete, "keep": total - obsolete, "total": total}}


def test_a_mass_delete_deletes_nothing_and_says_why(monkeypatch):
    calls = []
    monkeypatch.setattr(prune.common_cli, "run", lambda command, request: calls.append(command) or {"files": []})
    monkeypatch.setattr(R, "plan_retention", lambda *a, **k: _plan(15, 20))
    result = prune._prune_one({"delete": True, "cleanup_retention": 86400, "mode": "age",
                               "host": {}, "path": "/b"}, secrets={})
    assert result["deleted"] is None and "15 of 20 files" in result["refused"]
    assert calls == ["list-backup-files"], "delete-files is never called"


def test_the_operator_can_say_it_is_meant(monkeypatch):
    calls = []
    monkeypatch.setattr(prune.common_cli, "run",
                        lambda command, request: calls.append(command) or {"files": [], "counts": {"deleted": 15}})
    monkeypatch.setattr(R, "plan_retention", lambda *a, **k: _plan(15, 20))
    result = prune._prune_one({"delete": True, "allow_mass_delete": True, "cleanup_retention": 86400,
                               "mode": "age", "host": {}, "path": "/b"}, secrets={})
    assert "refused" not in result and calls == ["list-backup-files", "delete-files"]


def test_a_report_only_run_is_never_refused(monkeypatch):
    monkeypatch.setattr(prune.common_cli, "run", lambda command, request: {"files": []})
    monkeypatch.setattr(R, "plan_retention", lambda *a, **k: _plan(15, 20))
    result = prune._prune_one({"delete": False, "cleanup_retention": 86400, "mode": "age",
                               "host": {}, "path": "/b"}, secrets={})
    assert "refused" not in result, "nothing is deleted, so there is nothing to refuse"
