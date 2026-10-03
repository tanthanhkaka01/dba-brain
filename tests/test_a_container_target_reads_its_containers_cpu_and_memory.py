"""A database in a container shows its container's CPU and memory, not "not collected".

On the 0.26 node the PostgreSQL target on port 5433 read CPU *not collected* and Memory *not
collected* on its server page (the 0.26 sheet, Q3 G4b). It is a container: it has no host login, so
no OS collector runs for it - and the container's own CPU and memory were being collected every five
minutes beside its SQL metrics, under the same target, by DOCKER_CONTAINER_STATS. The page simply
did not look there, because that collector names its items after the container
(``db_ops_store:cpu``) and an area selected items by exact name only.

The container's readings are the last choice of each area: a target with OS or SQL Server readings
keeps showing those, and only the container's CPU and memory - never its I/O, PIDs or restarts -
answer for it.
"""

from db_ops.reports.server_report import build_areas, metric_label

NOW = 1_800_000_000


def _series(code, item, value, status="OK", *, unit="percent"):
    return {
        "code": code, "label": metric_label(code), "item": item, "unit": unit,
        "status": status, "numeric": True, "static": False,
        "last": value, "lastText": str(value), "lastAt": NOW - 300,
        "message": "", "min": value, "max": value, "avg": value,
        "points": [[NOW - 300, value, status]], "tier": "primary",
    }


def _container(name="db_ops_store", *, cpu=79.82, memory=6.43, memory_status="OK"):
    return [
        _series("DOCKER_CONTAINER_STATS", f"{name}:cpu", cpu),
        _series("DOCKER_CONTAINER_STATS", f"{name}:memory", memory, memory_status),
        _series("DOCKER_CONTAINER_STATS", f"{name}:pids", 43.0, "LOGGING", unit="count"),
        _series("DOCKER_CONTAINER_STATS", f"{name}:restart_count", 272.0, unit="count"),
    ]


def _area(series, key):
    return next(a for a in build_areas(series) if a["key"] == key)


def test_a_container_target_reads_its_containers_cpu_and_memory():
    cpu, memory = _area(_container(), "cpu"), _area(_container(), "memory")

    assert (cpu["status"], cpu["value"], cpu["sourceItem"]) == ("OK", "79.82%", "db_ops_store:cpu")
    assert (memory["status"], memory["value"], memory["sourceItem"]) == ("OK", "6.43%", "db_ops_store:memory")


def test_the_containers_memory_warning_colours_the_area():
    memory = _area(_container(memory=93.1, memory_status="WARNING"), "memory")

    assert memory["status"] == "WARNING" and memory["value"] == "93.1%"


def test_a_target_with_os_readings_keeps_showing_them():
    """The OS row is what the area is about; the container's is the fallback, not a rival."""
    series = [_series("OS_CPU_USAGE", "cpu_usage", 12.0), *_container(cpu=79.82)]

    cpu = _area(series, "cpu")

    assert cpu["sourceCode"] == "OS_CPU_USAGE" and cpu["value"] == "12%"


def test_only_cpu_and_memory_of_the_container_belong_to_those_areas():
    rows = [_series("DOCKER_CONTAINER_STATS", "db_ops_store:pids", 43.0, unit="count"),
            _series("DOCKER_CONTAINER_STATS", "db_ops_store:cpu_throttled", 5.0)]

    assert _area(rows, "cpu")["value"] == "not collected"
    assert _area(rows, "memory")["value"] == "not collected"


def test_a_stopped_container_is_not_told_to_look_at_its_cpu():
    """The action falls back to the area's note; for the container that would now be CPU's."""
    from db_ops.reports.server_health import ACTIONS, metric_action

    assert metric_action("DOCKER_CONTAINER_STATS") == ACTIONS["DOCKER_CONTAINER_STATS"]
    assert "docker" in metric_action("DOCKER_CONTAINER_STATS")
