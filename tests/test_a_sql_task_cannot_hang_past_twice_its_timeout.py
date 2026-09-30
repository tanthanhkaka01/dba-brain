"""A SQL task's run must end - at twice its own timeout at the latest.

On 2026-09-30 ``SQL033-RUN-ENGINE-V2-2AM`` logged the start of its third target at 02:00 +07 and
nothing after it for thirteen hours, until the worker's container was stopped for an upgrade; its
``timeout_seconds`` was 7200. Two days earlier the same task was found stale after 35 hours
(0.26.0 §1.70).

The timeout reached the driver as a *query* timeout, and a query timeout is per call - it restarts
on every batch and every ``nextset()``, so a procedure answering a stream of small results never
trips it. The ``run-sql`` child itself was started with no deadline, and the run's claim, held by a
live pid, is never reaped. Nothing bounded the run at all.

So the runner gives the child a wall-clock deadline, and the transport says a child stopped at its
deadline in words that cannot be mistaken for one that never started.
"""

from __future__ import annotations

import sys

from db_ops.lib.common_cli import CommandSpec
from db_ops.lib.time_window import TimeWindow
from db_ops.sql_tasks import runner
from db_ops.sql_tasks.runner import SqlCommand, SqlTarget
from db_ops.transport import process


def _target(timeout: int) -> SqlTarget:
    return SqlTarget(sql_id=33, target_no=3, server_id="ACME-LAB", db_type="sqlserver",
                     service_name="S", instance_name="", credential_name="c",
                     time_window=TimeWindow(timeout=timeout), active=True,
                     database_name="labtest", output_format="none")


def _command() -> SqlCommand:
    return SqlCommand(sql_id=33, sql_code="SQL033-ENGINE", sql_name="engine", db_type="sqlserver",
                      script_type="single", script_path=None, script_paths=(), script_files=(),
                      active=True)


def test_the_deadline_is_twice_the_timeout_and_the_connect():
    assert runner.run_deadline_seconds(7200) == 7200 * 2 + runner.DEFAULT_CONNECT_TIMEOUT_SECONDS


def test_the_run_sql_child_is_started_with_that_deadline(monkeypatch):
    """The 2026-09-30 call passed none, so the child could run for ever."""
    seen = {}

    def fake_run(command, request, **kwargs):
        seen.update(kwargs)
        return True, {"result_sets": []}, ""

    monkeypatch.setattr(runner.common_cli, "run_allowing_failure", fake_run)
    runner.execute_on_target(command=_command(), target=_target(7200), database={}, credential={},
                             password="x", sql_text="exec dbo.engine")

    assert seen["timeout_seconds"] == runner.run_deadline_seconds(7200)


def test_a_child_past_its_deadline_is_stopped_and_said_to_be():
    spec = CommandSpec(command="run-sql", executable=sys.executable,
                       args=("-c", "import time; time.sleep(30)"), stdin=b"", timeout_seconds=1)

    result = process.execute(spec)

    assert result.returncode is None
    assert "deadline" in result.error and "could not run" not in result.error
