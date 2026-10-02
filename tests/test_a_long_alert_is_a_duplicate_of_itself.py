"""A report alert queued in several parts is still found by the dedupe window.

A message too long for one Telegram body is queued as ``<source_id>:part:<n>``. The dedupe check
matched ``source_id`` exactly, so a long alert was never a duplicate of itself and
``push_report_alerts(dedupe_seconds=...)`` did nothing for exactly the reports most likely to storm
(review 0.25.0, F7.2). The parts match now, by an exact prefix: an id with ``_`` in it must not
match its neighbours, as it would under ``LIKE``.
"""

from __future__ import annotations

from db_ops.db.store import DbOpsStore


def _queue(store: DbOpsStore, source_id: str) -> None:
    store.insert_telegram_send_message(tlgchat_id="-100", message_text="x", source_type="reports",
                                       source_id=source_id)


def test_a_report_queued_in_parts_is_found_by_its_own_id(tmp_path):
    store = DbOpsStore(tmp_path / "db_ops.sqlite")
    _queue(store, "metrics_latest:critical:LAB_01:part:1")
    _queue(store, "metrics_latest:critical:LAB_01:part:2")

    assert store.recent_alert_exists(source_id="metrics_latest:critical:LAB_01", within_seconds=300)


def test_a_single_message_still_matches_exactly(tmp_path):
    store = DbOpsStore(tmp_path / "db_ops.sqlite")
    _queue(store, "backup_health:2026-10-01")

    assert store.recent_alert_exists(source_id="backup_health:2026-10-01", within_seconds=300)


def test_a_neighbour_s_id_is_not_a_match(tmp_path):
    store = DbOpsStore(tmp_path / "db_ops.sqlite")
    _queue(store, "metrics_latest:critical:LABX01:part:1")
    _queue(store, "metrics_latest:critical:LAB_01_B:part:1")

    assert not store.recent_alert_exists(source_id="metrics_latest:critical:LAB_01", within_seconds=300)
