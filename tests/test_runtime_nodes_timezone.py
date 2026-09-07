"""Which clock each node is running on, and the line the display zone must never cross.

Master and worker share one PostgreSQL store and each reads its own `config.json`. Nothing in the
store could say whether the two agreed about the hour — and a `time_window` firing at the wrong
time on one of them looks exactly like a schedule that was never due. `runtime_nodes` is the row
that answers it.

The second half of this file is the more important one. Making the *display* clock configurable is
only safe while the *stored* clock is not, because every range query in the tool
(`report_exists_on_local_date`, the metric retention cutoff, the queue's stale-claim window)
compares `%Y-%m-%dT%H:%M:%SZ` text lexically. A timestamp written in +07 would sort between two
UTC ones and be silently included in, or excluded from, a window seven hours wide.
"""

from datetime import datetime, timezone

import pytest

from db_ops.db import DbOpsStore
from db_ops.lib import timezone as tz


@pytest.fixture(autouse=True)
def _restore_zone():
    before = tz.display_declaration()
    yield
    tz.bind_display_timezone(before)


@pytest.fixture()
def store(tmp_path):
    store = DbOpsStore(tmp_path / "db_ops.sqlite")
    store.initialize()
    return store


def test_a_node_records_the_setting_and_the_offset_it_resolved_to(store):
    store.record_runtime_node(
        node_id="pc-master", node_role="master", hostname="DB-THANH",
        timezone_name="Asia/Ho_Chi_Minh", utc_offset_minutes=420,
        tz_abbreviation="+07", app_version="0.9.1")

    row = store.list_runtime_nodes()[0]
    assert row["node_id"] == "pc-master"
    assert row["timezone"] == "Asia/Ho_Chi_Minh"     # the setting, writable back into a config
    assert row["utc_offset_minutes"] == 420          # the snapshot, true as of updated_at


def test_two_nodes_that_disagree_about_the_hour_both_show_up(store):
    """The question the table exists to answer. One row each, so a worker in UTC beside a master
    in +07 is visible instead of being the reason a nightly window never opened."""
    store.record_runtime_node(node_id="pc-master", node_role="master", hostname="DB-THANH",
                              timezone_name="Asia/Ho_Chi_Minh", utc_offset_minutes=420)
    store.record_runtime_node(node_id="ubuntu-worker", node_role="worker", hostname="worker-1",
                              timezone_name="UTC", utc_offset_minutes=0)

    zones = {row["node_id"]: row["utc_offset_minutes"] for row in store.list_runtime_nodes()}
    assert zones == {"pc-master": 420, "ubuntu-worker": 0}


def test_a_node_reporting_again_updates_its_row_rather_than_adding_one(store):
    store.record_runtime_node(node_id="pc-master", node_role="master", hostname="DB-THANH",
                              timezone_name="UTC", utc_offset_minutes=0)
    store.record_runtime_node(node_id="pc-master", node_role="master", hostname="DB-THANH",
                              timezone_name="Asia/Ho_Chi_Minh", utc_offset_minutes=420)

    rows = store.list_runtime_nodes()
    assert len(rows) == 1
    assert rows[0]["timezone"] == "Asia/Ho_Chi_Minh"


def test_when_a_node_was_first_seen_survives_every_later_report(store):
    """History, and the only record of it. An upsert that reset `first_seen_at` would make every
    node look like it joined the cluster on the day it was last restarted."""
    store.record_runtime_node(node_id="pc-master", node_role="master", hostname="DB-THANH",
                              timezone_name="UTC", utc_offset_minutes=0)
    first_seen = store.list_runtime_nodes()[0]["first_seen_at"]

    store.record_runtime_node(node_id="pc-master", node_role="master", hostname="DB-THANH",
                              timezone_name="+07:00", utc_offset_minutes=420)

    assert store.list_runtime_nodes()[0]["first_seen_at"] == first_seen


# --------------------------------------------------------------------------------------------- #
# The invariant
# --------------------------------------------------------------------------------------------- #

def test_a_row_written_under_a_shifted_display_zone_is_still_stored_in_utc(store):
    """The whole of the timezone work rests on this.

    Every range query in the tool compares the stored text lexically, which only works because the
    text is one zone. A row written at 09:06 +07 must be on disk as 02:06Z, or it sorts between
    two UTC rows and lands in the wrong seven-hour window.
    """
    tz.bind_display_timezone("Asia/Ho_Chi_Minh")
    report_id = store.insert_report(
        report_code="rp_daily", report_name="Daily", report_type="BACKUP_HEALTH",
        report_level="logging", report_text="body")

    with store.connect() as conn:
        created_at = conn.execute(
            "SELECT created_at FROM reports WHERE report_id = ?", (report_id,)).fetchone()["created_at"]

    assert created_at.endswith("Z"), created_at
    written = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert abs((written - datetime.now(timezone.utc)).total_seconds()) < 120, (
        f"{created_at} is not this moment in UTC - it looks like a local clock reached a column")


def test_the_node_row_itself_is_timestamped_in_utc_like_everything_else(store):
    """Including the table that records the display zone. A row about which clock a node shows
    is exactly the row a reader would expect to be in that clock, and it must not be."""
    tz.bind_display_timezone("Asia/Ho_Chi_Minh")
    store.record_runtime_node(node_id="pc-master", node_role="master", hostname="DB-THANH",
                              timezone_name="Asia/Ho_Chi_Minh", utc_offset_minutes=420)

    row = store.list_runtime_nodes()[0]
    for column in ("first_seen_at", "updated_at"):
        assert row[column].endswith("Z"), (column, row[column])
        written = datetime.strptime(row[column], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        assert abs((written - datetime.now(timezone.utc)).total_seconds()) < 120, column
