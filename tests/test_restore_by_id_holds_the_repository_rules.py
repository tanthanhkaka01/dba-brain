"""`restore-by-id` holds a repository entry to the rules `restore-workflow` does (0.39.0, 1.361).

Read on 2026-11-13, mapping the restore half of the new flow: the workflow refuses a repository
entry that restores in place (nothing would come from the repository) or whose engine the
repository does not hold, before anything runs - and `restore-by-id`, the second door to the same
restore, skipped those refusals. Its copy's first message also named the source server as where the
copy came from, for a copy that came from the repository.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.test_a_restore_reports_every_step_in_order import _job, _said, drill  # noqa: F401 - the fixture

from dbabrain.backup_restore import restore_by_id, restore_script

# Its restore targets are lab machines the tests do not list in an inventory; a target whose
# environment nothing states is refused since 0.39.0 (D-39.6) - see tests/conftest.py.
pytestmark = pytest.mark.usefixtures("restore_targets_are_lab")


@pytest.fixture
def repository_job(monkeypatch):
    # The workflow's rule asks for a script restore; these jobs are its shape, built by hand.
    monkeypatch.setattr(restore_script, "ScriptRestore", SimpleNamespace)

    def make(**change):
        job = _job()
        job.restore_from, job.action = "repository", "restore"
        for name, value in change.items():
            setattr(job, name, value)
        return job

    return make


def test_an_in_place_repository_entry_is_refused_before_anything_runs(drill, repository_job):  # noqa: F811
    with pytest.raises(restore_by_id.RestoreByIdError, match="an in-place entry copies nothing"):
        drill(repository_job(is_remote=False, target_server_id="LAB-192-0-2-49"))
    assert drill.state["calls"] == [] and drill.state["ops"] == []


def test_an_engine_the_repository_does_not_hold_is_refused(drill, repository_job):  # noqa: F811
    with pytest.raises(restore_by_id.RestoreByIdError, match="restore_from repository is built for"):
        # Oracle reads the repository since 0.40.0 (1.380); MySQL still does not.
        drill(repository_job(db_type="mysql"))
    assert drill.state["calls"] == []


def test_the_copy_says_it_comes_from_the_repository(drill, repository_job):  # noqa: F811
    drill(repository_job())
    # Step 2 of the eleven (1.375): where it copies to, and that it is the repository's copies of the source.
    assert _said(drill, "COPY_START") == ("2. Copy to LAB-192-0-2-50 started - from the repository: the encrypted "
                                         "copies of LAB-192-0-2-49, decrypted on the way")


def test_a_copy_from_the_server_still_names_the_server(drill):  # noqa: F811
    drill(_job())
    assert _said(drill, "COPY_START") == ("2. Copy to LAB-192-0-2-50 started - from LAB-192-0-2-49: the backup "
                                         "files, each proven on arrival")
