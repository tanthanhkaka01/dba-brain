"""Inventory report model ordering and capacity/status data used by the HTML template."""
from db_ops.reports.inventory_report import _build_backup, _disk_text, build_models, render_html, render_md


def _srv(server_id, **extra):
    s = {"server_id": server_id, "company_code": "ACME", "ip": "10.0.0.1",
         "databases": [], "inventory_status": {}}
    s.update(extra)
    return s


def test_default_server_order_follows_file_position():
    data = {"servers": [_srv("C"), _srv("A"), _srv("B")]}
    _scope, models = build_models(data)
    assert [m["server_order"] for m in models] == [1, 2, 3]
    assert [m["server_id"] for m in models] == ["C", "A", "B"]  # file order preserved


def test_explicit_server_order_wins_and_sorts():
    data = {"servers": [
        _srv("first", server_order=30),
        _srv("second", server_order=10),
        _srv("third", server_order=20),
    ]}
    _scope, models = build_models(data)
    # rendered order is by server_order ascending
    assert [m["server_id"] for m in models] == ["second", "third", "first"]
    assert [m["server_order"] for m in models] == [10, 20, 30]


def test_missing_order_defaults_but_explicit_still_sorts():
    # one server pins order 1, others default to their file position -> the pinned one leads
    data = {"servers": [_srv("x"), _srv("y", server_order=1), _srv("z")]}
    _scope, models = build_models(data)
    # y (order 1) first; x defaults to 1 (its index) too -> tie broken by server_id ("x" < "y")
    assert models[0]["server_order"] == 1
    orders = [m["server_order"] for m in models]
    assert orders == sorted(orders)


def test_disk_model_keeps_actual_capacity_and_metric_driven_severity():
    server = _srv(
        "disk-host",
        os_health={
            "disks": {
                "D:": {
                    "total_gb": 250.0,
                    "free_gb": 19.3,
                    "free_percent": 7.72,
                    "status": "WARNING",
                    "file_system_type": "NTFS",
                    "logical_volume_name": "Data",
                }
            }
        },
    )

    scope, models = build_models({"servers": [server]})
    disk = models[0]["disks"][0]

    assert disk == {
        "m": "D: Data",
        "free": 7.72,
        "freeGB": 19.3,
        "totalGB": 250.0,
        "st": "WARNING",
        "sev": "warn",
    }
    html = render_html(scope, models, [], "2026-07-22")
    markdown = render_md(scope, models, [], "2026-07-22")
    assert '"freeGB": 19.3' in html and '"totalGB": 250.0' in html
    assert "`D: Data` 19.3/250.0 GB free (7.72%)" in markdown
    assert "2026-03-30" not in html and "05-26" not in html


def test_a_violated_log_rpo_comes_from_the_per_database_policy_not_from_server_evidence():
    """One healthy database must not answer for the instance.

    The old rule read the newest LOG backup found anywhere on the server. On the ERP FCI that
    reported a coverage of Full+Diff+Log while three databases were 124 days past their RPO, and
    on the APPDB host it claimed DIFF coverage from evidence that covered one database of six.
    The verdict now comes from the per-database policy block, and it names the databases.
    """
    server = _srv(
        "backup-host",
        databases=[{"db_type": "sqlserver"}],
        backup_evidence={
            "FULL": {"latest_finish": "2030-01-02 03:00:00", "latest_age_hours": 12},
            "DIFF": {"latest_finish": "2030-01-02 09:00:00", "latest_age_hours": 6},
            "LOG": {"latest_finish": "2030-01-01 00:00:00", "latest_age_hours": 72},
        },
        backup_policy={
            "databases": [{"database": "SALESDB", "status": "CRITICAL", "reason": "LOG 3d old"}],
            "summary": {"status": "CRITICAL", "compliant": 0, "eligible": 1,
                        "reason": "1 of 1 database(s) outside policy — SALESDB: LOG 3d old",
                        "worstDatabases": ["SALESDB"],
                        "byType": {"FULL": {"required": 1, "compliant": 1, "state": "OK"},
                                   "DIFF": {"required": 0, "compliant": 0, "state": "NOT_REQUIRED"},
                                   "LOG": {"required": 1, "compliant": 0, "state": "VIOLATED"}}},
        },
    )

    backup = _build_backup(server)

    assert backup["logAgeHours"] == 72.0
    assert backup["logStale"] is True
    assert backup["worstDatabases"] == ["SALESDB"]
    assert "SALESDB" in backup["note"]
    # "chain broken" is an assertion no collected metric supports: it needs backup LSNs.
    assert "chain" not in backup["note"].lower()


def test_a_backup_within_policy_is_not_reported_stale():
    server = _srv(
        "backup-host",
        databases=[{"db_type": "sqlserver"}],
        backup_evidence={"FULL": {"latest_finish": "2030-01-02 03:00:00", "latest_age_hours": 12},
                         "LOG": {"latest_finish": "2030-01-02 09:00:00", "latest_age_hours": 1}},
        backup_policy={
            "databases": [{"database": "SALESDB", "status": "OK", "reason": ""}],
            "summary": {"status": "OK", "compliant": 1, "eligible": 1, "reason": "",
                        "worstDatabases": [],
                        "byType": {"FULL": {"required": 1, "compliant": 1, "state": "OK"},
                                   "DIFF": {"required": 0, "compliant": 0, "state": "NOT_REQUIRED"},
                                   "LOG": {"required": 1, "compliant": 1, "state": "OK"}}},
        },
    )

    backup = _build_backup(server)

    assert backup["logStale"] is False
    assert backup["note"] == "1/1 DB within policy"
    assert backup["cov"] == "Full+Log · DIFF not required"


def test_unknown_disk_capacity_still_shows_known_free_gb():
    assert _disk_text({"m": "E:", "freeGB": 63.44, "totalGB": None,
                       "free": None, "flag": "WARNING"}) == (
        "`E:` 63.44 GB free (capacity unknown)"
    )
