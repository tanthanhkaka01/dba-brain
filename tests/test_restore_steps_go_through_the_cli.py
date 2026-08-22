"""A restore step is a ``common`` CLI call, and what comes back is the envelope's ``data``.

``backup_restore.restore_by_id`` used to import ``restorekey``, ``restorestep`` and
``verifyrestore`` and call them in-process. They are operations on another machine — import a
certificate, apply a backup, prove the database is usable — which is what the ``common`` CLI is
the contract for, so on 2026-08-15 they became subprocess calls.

Nothing covered ``_execute`` before: ``tests/test_restore_reporting.py`` stubs the workflow a
level above it, so the conversion would have shipped untested against a production restore path.
These tests are that cover. They stub ``subprocess.run`` rather than run a real restore — what is
being pinned is the *contract between the app and the CLI*, which is exactly the part a real
restore would not exercise deterministically.

The behaviours that matter, and why each is here rather than assumed:

* the request goes in on **stdin**, because it carries resolved passwords and argv is visible to
  ``ps`` on the machine running it;
* the caller keeps seeing the same dict it always saw — the CLI wraps it in ``data``;
* ``success: false`` **raises**, because a failed step is fatal to the restore and returning it as
  data would make it look like a step that simply found nothing to do;
* output that is not JSON raises too, and says what the command actually printed — a crash that
  reads as an empty result is how a restore reports success having done nothing.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from db_ops.backup_restore import restore_by_id


ALL_STEPS = ["restore-key", "restore-full", "restore-diff", "restore-log", "verify-restore"]


class _Recorder:
    """Stands in for ``subprocess.run`` and remembers how it was called."""

    def __init__(self, *, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr
        self.argv: list[str] = []
        self.stdin: str | None = None

    def __call__(self, argv, *, input=None, capture_output=None, text=None, **kwargs):
        self.argv, self.stdin = list(argv), input
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


def _envelope(**over) -> str:
    body = {"success": True, "operation": "restore-full", "message": "",
            "error": None, "data": {"ok": True}, "metrics": {}}
    body.update(over)
    return json.dumps(body)


@pytest.mark.parametrize("step", ALL_STEPS)
def test_each_step_runs_its_own_cli_command(step: str, monkeypatch) -> None:
    recorder = _Recorder(stdout=_envelope())
    monkeypatch.setattr(restore_by_id.subprocess, "run", recorder)

    restore_by_id._execute(step, {"target": "X"})

    assert recorder.argv[1:] == ["-m", "db_ops.common.cli", step, "-"], recorder.argv
    assert recorder.argv[0] == sys.executable


def test_the_request_goes_in_on_stdin_not_argv(monkeypatch) -> None:
    """A restore request carries resolved passwords; argv is readable by anyone running ``ps``."""
    recorder = _Recorder(stdout=_envelope())
    monkeypatch.setattr(restore_by_id.subprocess, "run", recorder)
    request = {"target": "X", "password": "hunter2"}

    restore_by_id._execute("restore-full", request)

    assert json.loads(recorder.stdin) == request
    assert not any("hunter2" in token for token in recorder.argv)


def test_the_caller_gets_the_data_the_command_answered_with(monkeypatch) -> None:
    """The CLI wraps what these functions always returned; unwrapping keeps every caller working."""
    monkeypatch.setattr(restore_by_id.subprocess, "run",
                        _Recorder(stdout=_envelope(data={"ok": True, "restored": 3})))

    assert restore_by_id._execute("restore-full", {}) == {"ok": True, "restored": 3}


def test_a_failed_step_raises_with_the_reason(monkeypatch) -> None:
    monkeypatch.setattr(restore_by_id.subprocess, "run", _Recorder(
        stdout=_envelope(success=False, error="backup file not found", data={}), returncode=1))

    with pytest.raises(restore_by_id.RestoreByIdError, match="backup file not found"):
        restore_by_id._execute("restore-full", {})


def test_output_that_is_not_json_raises_and_quotes_what_was_printed(monkeypatch) -> None:
    """A crashed command must not read as an empty result — that is a restore reporting success
    having done nothing."""
    monkeypatch.setattr(restore_by_id.subprocess, "run", _Recorder(
        stdout="Traceback (most recent call last):", returncode=1, stderr="ImportError: boom"))

    with pytest.raises(restore_by_id.RestoreByIdError, match="ImportError: boom"):
        restore_by_id._execute("restore-full", {})


def test_a_command_that_cannot_start_raises(monkeypatch) -> None:
    def _explode(*_args, **_kwargs):
        raise OSError("no interpreter")

    monkeypatch.setattr(restore_by_id.subprocess, "run", _explode)

    with pytest.raises(restore_by_id.RestoreByIdError, match="could not run"):
        restore_by_id._execute("restore-full", {})


def test_an_unknown_step_is_refused_before_anything_runs(monkeypatch) -> None:
    def _explode(*_args, **_kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("an unknown step must not reach subprocess")

    monkeypatch.setattr(restore_by_id.subprocess, "run", _explode)

    with pytest.raises(restore_by_id.RestoreByIdError, match="unknown step"):
        restore_by_id._execute("restore-everything", {})


def test_no_deadline_is_imposed_here(monkeypatch) -> None:
    """A full restore legitimately runs for hours. The app command that schedules it carries the
    window, and the daemon kills the parent — one timeout, at the level that knows the number."""
    recorder = _Recorder(stdout=_envelope())
    captured: dict = {}

    def _capture(argv, **kwargs):
        captured.update(kwargs)
        return recorder(argv, **kwargs)

    monkeypatch.setattr(restore_by_id.subprocess, "run", _capture)
    restore_by_id._execute("restore-full", {})

    assert captured.get("timeout") is None


def test_every_step_name_is_a_real_common_cli_command() -> None:
    """The names are the contract. A step this app can emit but the CLI cannot answer is a restore
    that fails at the step, in production, with the databases already half-applied."""
    from db_ops.common import cli as common_cli

    source = open(common_cli.__file__, encoding="utf-8").read()
    for step in restore_by_id._STEP_COMMANDS:
        assert f'"{step}"' in source, f"db_ops.common.cli has no {step} command"
