"""Registering an instance has to put it on the report, not only into the collector.

The canonical `database-inventory.json` is seeded **once**, on the first workflow run, and the
health overlay only ever updates servers that file already lists. Between those two rules there was
no way in for anything registered afterwards, and the gap is invisible from every direction that
matters: the instance is collected from, alerted on, and given its own index-usage page, while the
fleet page does not mention it.

Measured on 2026-09-12 on a node whose canonical file held **one** server and whose
`db_instances.json` registered **43**. The seed had run on the afternoon the node was stood up,
when one was all there was, and forty-two instances registered over the following day never
appeared. The operator's words: *"list server ra rất nhiều, nhưng report inventory chỉ có 1, phải
tự động thêm vào report khi add instance."*
"""

from __future__ import annotations

from db_ops.lib.inventory_render import (
    adopt_new_servers,
    reportable_servers,
    seed_inventory,
)


def test_an_instance_registered_after_the_seed_is_adopted_on_the_next_run():
    canonical = {"servers": [{"server_id": "LAB-192-0-2-1", "ip": "192.0.2.1", "databases": []}]}
    registered = seed_inventory([
        {"server_id": "LAB-192-0-2-1", "ip": "192.0.2.1", "databases": []},
        {"server_id": "ACME-192-0-2-9", "ip": "192.0.2.9", "databases": [{"db_type": "sqlserver"}]},
    ])

    adopted = adopt_new_servers(canonical, registered)

    assert adopted == ["ACME-192-0-2-9"]
    assert [s["server_id"] for s in canonical["servers"]] == ["LAB-192-0-2-1", "ACME-192-0-2-9"]


def test_what_the_operator_wrote_into_the_canonical_file_is_never_overwritten():
    # The canonical inventory is the operator's richer description of the fleet - that is the whole
    # reason it exists as a file rather than being derived every run. Adoption is add-only.
    canonical = {"servers": [{"server_id": "ACME-192-0-2-9", "ip": "192.0.2.9",
                              "owner": "platform team", "databases": [{"db_type": "sqlserver"}]}]}
    registered = seed_inventory([{"server_id": "ACME-192-0-2-9", "ip": "10.0.0.9",
                                  "databases": [{"db_type": "oracle"}]}])

    assert adopt_new_servers(canonical, registered) == []
    assert canonical["servers"][0]["owner"] == "platform team"
    assert canonical["servers"][0]["ip"] == "192.0.2.9"


def test_adoption_is_idempotent_so_a_daily_workflow_does_not_grow_the_file():
    canonical = {"servers": []}
    registered = seed_inventory([{"server_id": "ACME-192-0-2-9", "ip": "192.0.2.9",
                                  "databases": []}])

    assert adopt_new_servers(canonical, registered) == ["ACME-192-0-2-9"]
    assert adopt_new_servers(canonical, registered) == []
    assert len(canonical["servers"]) == 1


def test_a_record_with_no_server_id_is_skipped_rather_than_added_as_an_empty_row():
    # `server_id` is the only key this project joins on; a row without one would be a server that
    # no overlay could ever match and no page could name.
    canonical = {"servers": []}

    assert adopt_new_servers(canonical, {"servers": [{"ip": "192.0.2.9"}, {"server_id": "  "}]}) == []
    assert canonical["servers"] == []


def test_the_flags_that_decide_a_report_are_the_ones_the_register_already_has():
    """A plain registration is reported; a fourth opinion on "enabled" would be a bug.

    db_ops already cascades `enabled` -> `metrics` -> `reports` -> `alerts`, each defaulting to the
    one before. Adoption reads the third, so an instance added with no flags at all is collected
    **and** appears on the fleet page - which is what makes "add an instance and it shows up" true
    without the operator learning a second switch - while `reports: {"enabled": false}` keeps a
    machine collected and off the page, and `enabled: false` stops both.
    """
    plain = {"server_id": "ACME-192-0-2-2", "databases": [{"db_type": "sqlserver"}]}
    off_report = {"server_id": "ACME-192-0-2-3",
                  "databases": [{"db_type": "sqlserver", "reports": {"enabled": False}}]}
    off_all = {"server_id": "ACME-192-0-2-4",
               "databases": [{"db_type": "sqlserver", "enabled": False}]}

    kept = [s["server_id"] for s in reportable_servers([plain, off_report, off_all])]

    assert kept == ["ACME-192-0-2-2"]
