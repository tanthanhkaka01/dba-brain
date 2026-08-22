"""A tile that prints a rule beside a value that breaks it must not colour itself green.

`metrics.metric_overrides` in `data/db_instances.json` can lower a metric's status on one server —
"this finding is real but standing, and nobody is going to act on it". The collector applies the
remap and records what it did in the message (`Policy: severity remapped CRITICAL->LOGGING by
metric_overrides.`); everything downstream then sees the lowered status and nothing else.

On `ACME-192-0-2-249-HOST` that produced a page which contradicted itself. The CPU tile read:

    CPU                                    90.67%
    WARN >= 80% - CRITICAL >= 90%

in the ordinary colour of a healthy area, because the stored status was `LOGGING` — the deliberate
2026-08-14 override for that host's one-second CPU sample. Both halves were true and the tile as a
whole said something false: that 90.67% is fine under a rule that says 90% is critical.

Repainting the tile red is the wrong fix — it re-raises exactly what somebody decided not to be
alerted about, and the next person deletes the override to make the page quiet. The tile keeps the
colour the config asked for and says the missing half out loud: this value grades CRITICAL, and it
is silent by decision rather than by measurement.
"""

from db_ops.lib.health_model import downgraded_from
from db_ops.reports.server_report import build_areas, metric_label

NOW = 1_800_000_000
REMAP = "Policy: severity remapped CRITICAL->LOGGING by metric_overrides."


def _series(code, item, value, status, *, unit="percent", message=""):
    return {
        "code": code, "label": metric_label(code), "item": item, "unit": unit,
        "status": status, "numeric": True, "static": False,
        "last": value, "lastText": str(value), "lastAt": NOW - 300,
        "message": message, "min": value, "max": value, "avg": value,
        "points": [[NOW - 300, value, status]], "tier": "primary",
    }


def test_the_severity_an_override_removed_is_read_back_out_of_the_message():
    assert downgraded_from(f"Average CPU usage is 90.67 percent. {REMAP}") == "CRITICAL"
    assert downgraded_from("Policy: severity remapped WARNING->LOGGING by metric_overrides.") == "WARNING"


def test_a_message_with_no_remap_reports_no_downgrade():
    assert downgraded_from("Average CPU usage is 12.00 percent.") == ""
    assert downgraded_from("") == ""


def test_a_remap_that_raises_a_status_is_not_a_downgrade():
    """It already shows in the colour; a "silenced" marker on a louder alert would be a lie."""
    assert downgraded_from("Policy: severity remapped WARNING->CRITICAL by metric_overrides.") == ""


def test_a_silenced_reading_keeps_the_status_config_asked_for():
    """The page must not undo the decision — only stop hiding that one was made."""
    cpu = _series("OS_CPU_USAGE", "cpu_usage", 90.67, "LOGGING",
                  message=f"Average CPU usage is 90.67 percent. {REMAP}")
    area = next(a for a in build_areas([cpu]) if a["key"] == "cpu")

    assert area["status"] == "OK"
    assert area["value"] == "90.67%"
    assert area["downgradedFrom"] == "CRITICAL"


def test_an_ordinary_reading_carries_no_marker():
    cpu = _series("OS_CPU_USAGE", "cpu_usage", 12.0, "OK",
                  message="Average CPU usage is 12.00 percent.")
    area = next(a for a in build_areas([cpu]) if a["key"] == "cpu")

    assert area["status"] == "OK"
    assert area["downgradedFrom"] == ""


def test_a_silenced_reading_is_not_hidden_by_a_healthy_neighbour():
    """Both rank OK, so without the second sort key the tile would show whichever came first.

    `OS_CPU_USAGE` carries load average beside CPU percentage. A tile that answered with
    `load_average 0.89` would be true and useless: the number somebody silenced is the one the
    reader came to see.
    """
    silenced = _series("OS_CPU_USAGE", "cpu_usage", 90.67, "LOGGING",
                       message=f"Average CPU usage is 90.67 percent. {REMAP}")
    healthy = _series("SYSTEM_CPU_MEMORY", "cpu", 4.0, "OK", message="cpu 4 percent")
    area = next(a for a in build_areas([healthy, silenced]) if a["key"] == "cpu")

    assert area["sourceItem"] == "cpu_usage"
    assert area["downgradedFrom"] == "CRITICAL"


def test_a_real_problem_still_outranks_a_silenced_one():
    """Silencing raises a tile above OK, never above something actually alerting."""
    silenced = _series("OS_DISK_USAGE", "D:", 96.0, "LOGGING", message=f"D: 96 percent. {REMAP}")
    failing = _series("OS_DISK_USAGE", "E:", 99.0, "CRITICAL", message="E: 99 percent.")
    area = next(a for a in build_areas([silenced, failing]) if a["key"] == "disk_space")

    assert (area["status"], area["sourceItem"]) == ("CRITICAL", "E:")


def test_every_tile_carries_the_field_even_when_no_metric_reached_it():
    """One shape for every tile: the page reads `downgradedFrom` without knowing which kind it has."""
    areas = build_areas([_series("OS_CPU_USAGE", "cpu_usage", 12.0, "OK")])
    assert all("downgradedFrom" in area for area in areas)
    backup = next(a for a in areas if a["key"] == "backup")
    assert backup["downgradedFrom"] == ""
