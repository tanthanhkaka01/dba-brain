"""`metrics health-summary` and `health-summary-latest` must actually produce their summary.

Both commands render through one function, and that function opened with

    rows_by_target = rows_by_target(rows)

Assigning to the name makes it local for the whole function, so the call on the right-hand side
resolved to the not-yet-assigned local and raised `UnboundLocalError` before anything ran. Every
invocation of `health-summary`, `health-summary-latest` and its two aliases crashed on any
non-empty result set.

It survived because the *empty* case returns one line earlier, and because the sibling on line 307
calls `rows_by_target(rows)` inline and is fine — so the module imports, the function exists, and
nothing that reads the source looks wrong. It was found by running the commands for real while
writing a quickstart, which is the only thing that would have found it: a shadowing bug is
invisible to every check that does not execute the branch.

These tests call the formatter with rows rather than the CLI, because the defect is in the
formatter and a test that shells out would report a different failure for a dozen other reasons.
"""

from __future__ import annotations

import pytest

from db_ops.metrics import cli as metrics_cli


class _Row:
    """The attributes `rows_by_target` and the formatter read, and nothing else."""

    def __init__(self, target_id: str, metric_code: str, status: str, **over: object) -> None:
        self.target_id = target_id
        self.metric_code = metric_code
        self.status = status
        self.server_id = over.get("server_id", target_id)
        self.ip = over.get("ip", "192.0.2.10")
        self.db_name = over.get("db_name", "SALESDB")
        self.metric_item = over.get("metric_item", "")
        self.metric_value = over.get("metric_value", "1")
        self.metric_unit = over.get("metric_unit", "")
        self.message = over.get("message", "")
        self.importance = over.get("importance", 3)
        self.collected_at = over.get("collected_at", "2026-08-22T07:00:00Z")
        self.db_type = over.get("db_type", "sqlserver")
        self.service_name = over.get("service_name", "")
        self.target_no = over.get("target_no", 1)

    def __getitem__(self, key: str) -> object:
        return getattr(self, key)

    def get(self, key: str, default: object = None) -> object:
        return getattr(self, key, default)


def _rows() -> list[_Row]:
    return [
        _Row("T1", "INSTANCE_STATUS", "OK"),
        _Row("T1", "BACKUP_AGE", "WARNING", message="No full backup found"),
        _Row("T2", "INSTANCE_STATUS", "CRITICAL", server_id="ACME-192-0-2-11", ip="192.0.2.11"),
    ]


@pytest.mark.parametrize("latest", [False, True])
def test_the_summary_renders_instead_of_raising(latest: bool) -> None:
    """The whole defect in one assertion: it used to raise before producing a character."""
    text = metrics_cli._format_health_summary(_rows(), run_id=None if latest else 7, latest=latest)

    assert text, "the formatter produced nothing"
    assert "HEALTH SUMMARY" in text


def test_every_target_gets_one_line_and_the_worst_status_on_it() -> None:
    """A summary that silently dropped a target would pass the test above and be useless.

    The label is the service name plus the address, not `server_id` — worth pinning, because a
    reader scanning this output identifies a machine by the address they would connect to.
    """
    text = metrics_cli._format_health_summary(_rows(), run_id=7)
    lines = [line for line in text.splitlines() if not line.startswith("[")]

    assert len(lines) == 2, f"expected one line per target, got {lines}"
    assert "192.0.2.10" in text and "192.0.2.11" in text
    # T1 has an OK and a WARNING; the line must report the worse of the two.
    first = next(line for line in lines if "192.0.2.10" in line)
    assert "WARNING" in first, f"the worst status for the target is not on its line: {first}"


def test_an_empty_result_set_still_says_so() -> None:
    """The early return is what hid the defect; it is also the correct behaviour, so pin it."""
    text = metrics_cli._format_health_summary([], run_id=7)

    assert "No metric results." in text


def test_the_imported_helper_is_not_shadowed() -> None:
    """The rule, not the symptom: rebinding an imported name inside a function that calls it.

    Stated against the module rather than the one line, so the same mistake in another formatter
    fails here too — it is a shadowing class, and this file exists because it cost a working
    command for an unknown length of time.
    """
    import ast
    import inspect

    source = inspect.getsource(metrics_cli)
    imported = {
        alias.asname or alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    shadowed: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        assigned = {
            target.id
            for statement in ast.walk(node)
            if isinstance(statement, ast.Assign)
            for target in statement.targets
            if isinstance(target, ast.Name)
        }
        called = {
            inner.func.id
            for inner in ast.walk(node)
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
        }
        for name in sorted(assigned & called & imported):
            shadowed.append(f"{node.name}(): {name}")

    assert not shadowed, (
        "These functions assign to an imported name they also call, which makes the name local "
        "for the whole function and raises UnboundLocalError at the call:\n  "
        + "\n  ".join(shadowed)
    )
