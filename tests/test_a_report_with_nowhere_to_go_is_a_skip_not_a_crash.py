"""A scheduled report that is built and cannot be sent records a skip, and does not raise.

`APP-REPORTS-CREATE` exited 1 on a bare install with::

    ERROR: cannot access local variable 'reason' where it is not associated with a value

`_run_one_scheduled_report` has two branches that record a skip. The first — nothing was built —
binds ``reason`` and returns. The second — something was built and the push queued none of it —
binds ``skipped_reason`` and then asked for ``reason``, a name that only exists on the path that
already returned. So the failure needed both halves to be true at once: a report that **was** built,
with **nowhere to send it**. That is not a corner case, it is every fresh install: `init` writes a
report schedule and no Telegram group, so the first scheduled run built a report, found no chat, and
died on the line that was trying to write down why it had skipped.

Caught by the full suite on 2026-09-20 and by nothing else — the focused runs during the work never
included the bare-install file, which is the one place both halves are true.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from db_ops.reports import metrics_reports


class RecordingStore:
    """Enough store for the scheduled path, remembering what it was told about the skip."""

    def __init__(self):
        self.state_writes = []

    def fetch_report_send_state(self, *, report_code, channel):
        return None

    def upsert_report_send_state(self, **kwargs):
        self.state_writes.append(kwargs)


@pytest.fixture()
def one_report(monkeypatch):
    """A report that builds, and a push that queues nothing — the two halves together."""
    monkeypatch.setattr(metrics_reports, "_migrate_legacy_report_send_state",
                        lambda **kwargs: None)
    monkeypatch.setattr(metrics_reports, "_create_scheduled_report",
                        lambda **kwargs: {"report_ids": [7], "skipped": []})
    monkeypatch.setattr(metrics_reports, "push_report_alerts",
                        lambda **kwargs: {"queued": 0, "skipped": [
                            {"report_id": 7, "level": "logging",
                             "reason": "Telegram group not configured for level=logging."}]})


def _run(store):
    return metrics_reports._run_one_scheduled_report(
        store=store,
        sqlite_path=":memory:",
        telegram_groups={},
        report_config={"report_code": "metrics_daily", "active": True},
        summary_limit=10,
        backup_days=7,
        evaluated_at=datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc),
        scheduler_trigger_time="",
        logger=None,
    )


def test_a_built_report_with_no_chat_configured_does_not_raise(one_report):
    """The crash, reproduced: this call used to end in UnboundLocalError."""
    store = RecordingStore()

    result = _run(store)

    assert result["report_code"] == "metrics_daily"
    assert result["queued"] == 0


def test_the_skip_says_why_it_could_not_be_sent(one_report):
    """And the reason is the push's own, not a placeholder: an operator reading
    `last_skipped_reason` should find "no Telegram group", which is a thing they can fix."""
    store = RecordingStore()

    _run(store)

    write = store.state_writes[-1]
    assert write["last_status"] == "skipped"
    assert "Telegram group not configured" in write["last_skipped_reason"]


def test_a_push_that_says_nothing_still_records_something(monkeypatch, one_report):
    """An empty reason would write an empty column, which reads as "no reason" rather than "not
    recorded"."""
    monkeypatch.setattr(metrics_reports, "push_report_alerts",
                        lambda **kwargs: {"queued": 0, "skipped": []})
    store = RecordingStore()

    _run(store)

    assert store.state_writes[-1]["last_skipped_reason"] == "push skipped"


def test_a_report_that_was_sent_clears_the_reason_and_anchors_the_cycle(monkeypatch, one_report):
    """The other branch, so the fix cannot have been to make both say the same thing."""
    monkeypatch.setattr(metrics_reports, "push_report_alerts",
                        lambda **kwargs: {"queued": 1, "skipped": []})
    store = RecordingStore()

    _run(store)

    write = store.state_writes[-1]
    assert write["last_status"] == "queued"
    assert write["last_skipped_reason"] == ""
    assert write["last_run_at"], "the anchor is the run's own start"
