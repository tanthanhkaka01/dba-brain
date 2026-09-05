"""How long has DBA Brain been up on this node — the question `self-status` could not answer.

It reports the machine's uptime happily, and until 2026-09-05 that was the only one it had. The
operator asked for the other, and the reason it was missing is structural rather than an oversight:
`self-status` runs as a short-lived process the bot spawned to ask, not as the scheduler, and it
deliberately opens no store — it is the one status that still answers when the store is what is
down. So neither the process nor the database could tell it.

The daemon leaves a file instead. These tests are about the file being *believed only when it
should be*: a hard kill leaves it behind, and a reader that trusted the timestamp alone would
report a daemon as up since a stop three days ago.
"""

from __future__ import annotations

import datetime as dt
import json

from db_ops.lib import daemon_state


def test_a_running_daemon_reports_hours_since_it_started(tmp_path):
    started = dt.datetime(2026, 9, 5, 7, 53, 6, tzinfo=dt.timezone.utc)
    daemon_state.record_start(tmp_path, version="0.9.1", node_role="worker", now=started)

    facts = daemon_state.uptime(tmp_path, now=started + dt.timedelta(hours=2, minutes=21))

    assert facts["status"] == "running"
    assert facts["hours"] == 2.35
    assert facts["since"] == "2026-09-05T07:53:06Z"
    assert facts["version"] == "0.9.1" and facts["node_role"] == "worker"


def test_a_file_whose_pid_is_gone_is_stale_and_says_so(tmp_path):
    """The case that makes the file worth checking rather than reading.

    `taskkill /F` and SIGKILL both leave it behind. Believing the timestamp there reports a daemon
    that has been "up" since a stop days ago — the most confident possible way to be wrong about
    the one thing this line exists to say. It is reported as *stale* rather than as *stopped*,
    because the difference is real: the daemon died without unwinding.
    """
    daemon_state.record_start(tmp_path, pid=999_999,
                              now=dt.datetime(2026, 9, 1, 0, 0, tzinfo=dt.timezone.utc))

    facts = daemon_state.uptime(tmp_path)

    assert facts["status"] == "stale"
    assert facts["hours"] is None, "a stale file must not produce an uptime"
    assert facts["since"] == "2026-09-01T00:00:00Z"


def test_no_file_means_stopped_which_is_a_different_fact(tmp_path):
    assert daemon_state.uptime(tmp_path)["status"] == "stopped"


def test_a_clean_stop_removes_the_file_so_stopped_means_stopped(tmp_path):
    daemon_state.record_start(tmp_path)
    assert daemon_state.state_path(tmp_path).is_file()

    daemon_state.clear(tmp_path)

    assert daemon_state.uptime(tmp_path)["status"] == "stopped"


def test_a_file_this_did_not_write_is_reported_unreadable_rather_than_guessed(tmp_path):
    daemon_state.state_path(tmp_path).write_text(
        json.dumps({"pid": 1, "started_at": "yesterday"}), encoding="utf-8")

    facts = daemon_state.uptime(tmp_path)

    assert facts["status"] == "unreadable" and facts["hours"] is None


def test_a_runtime_directory_that_cannot_be_written_does_not_stop_the_daemon(tmp_path, monkeypatch):
    """Recording the start is a courtesy to a later reader, never a precondition for running."""
    monkeypatch.setattr(daemon_state.Path, "mkdir",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("read-only")))

    daemon_state.record_start(tmp_path / "nope")  # must not raise

    assert daemon_state.uptime(tmp_path / "nope")["status"] == "stopped"


# --------------------------------------------------------------------------------------------- #
# The line self-status prints
# --------------------------------------------------------------------------------------------- #
def test_both_uptimes_are_shown_because_they_answer_different_questions(tmp_path):
    """A node whose daemon restarted an hour ago sits on a host that has been up for a week.
    Reporting one of those as "uptime" answers whichever question the reader did not ask."""
    from db_ops.common import self_status

    daemon_state.record_start(tmp_path)
    text = self_status.render({
        "uptime": {"hours": 165.4, "since": "2026-08-29T10:30:17Z", "source": "GetTickCount64"},
        "db_ops_uptime": self_status.db_ops_uptime(tmp_path),
    })

    assert "uptime    : 165.40 h  (host up since 2026-08-29T10:30:17Z)" in text
    assert "db_ops up : 0.00 h  (since " in text


def test_the_report_says_not_running_rather_than_leaving_the_line_out(tmp_path):
    """An absent line reads as "nothing to say"; this line saying "not running" is the answer."""
    from db_ops.common import self_status

    text = self_status.render({"db_ops_uptime": self_status.db_ops_uptime(tmp_path)})

    assert "db_ops up : not running" in text


def test_a_caller_that_does_not_know_the_runtime_dir_gets_unknown_not_a_wrong_answer():
    from db_ops.common import self_status

    facts = self_status.db_ops_uptime(None)

    assert facts["status"] == "unknown"
    assert "db_ops up : unknown" in self_status.render({"db_ops_uptime": facts})
