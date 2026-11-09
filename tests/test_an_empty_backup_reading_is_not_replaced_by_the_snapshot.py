"""An empty backup reading is the reading - the canonical file's static backup block never fills it in.

The metrics overlay writes ``backup_evidence: {}`` when the newest collection found no finished
backup. The fleet page read it with ``or``, so that empty dict fell through to the canonical file's
``backup.latest_by_type`` - a snapshot - and printed June's last FULL, with the age it had in June, as
the server's current backup (audit 2026-11-05, A3). Only a server the overlay has never reached reads
the static block.
"""

from __future__ import annotations

from dbabrain.lib.inventory_render import backup_latest_by_type

STATIC = {"backup": {"latest_by_type": {"FULL": {"latest_finish": "2026-06-20 01:22:11", "latest_age_hours": 3.0}}}}


def test_an_empty_overlay_answer_is_kept_empty():
    assert backup_latest_by_type({**STATIC, "backup_evidence": {}}) == {}


def test_the_overlays_answer_wins_over_the_snapshot():
    fresh = {"FULL": {"latest_finish": "2026-11-05 01:00:00", "latest_age_hours": 6.0}}
    assert backup_latest_by_type({**STATIC, "backup_evidence": fresh}) == fresh


def test_a_server_the_overlay_never_reached_reads_the_snapshot():
    assert backup_latest_by_type(STATIC)["FULL"]["latest_finish"] == "2026-06-20 01:22:11"
