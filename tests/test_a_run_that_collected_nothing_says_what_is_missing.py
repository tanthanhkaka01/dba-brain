"""Why `target_count: 0` had to become a sentence.

"Not configured is a state, not a failure" is one of this project's rules, and it is only true
when the state says *what*. A metric collection that found nothing to collect from ended at
`target_count: 0` — accurate, and useless. Four different situations produce that number and each
needs a different action: no inventory at all, an estate switched off, metrics switched off, or a
`--db-type` / `--target-id` on this run that matched nothing. The operator could not tell which
one they were in without opening the inventory themselves, which is the work the tool was asked
to do.

Every other app in this tree names the missing piece when it skips. This one counted it.

And the summary has carried a `message` field since it was written while the CLI printed every
other field and not that one — so even what the run *did* say went to the store and not to the
person who had just asked.
"""

from db_ops.metrics.targets import explain_no_targets


def test_an_empty_inventory_names_the_file_and_the_command():
    answer = explain_no_targets([])

    assert "db_instances.json" in answer
    assert "instance-add" in answer


def test_an_estate_switched_off_is_not_reported_as_a_missing_inventory():
    """A node with instances registered and all of them disabled is a decision somebody made.
    Telling its operator to register an instance sends them to add a second copy of one they
    already have."""
    answer = explain_no_targets([_instance(enabled=False), _instance(enabled=False)])

    assert "all 2 instance(s)" in answer
    assert "`enabled: false`" in answer
    assert "instance-add" not in answer


def test_metrics_switched_off_is_distinguished_from_the_instance_being_off():
    """Two different flags, two different fixes: `enabled` is the instance, `metrics.enabled` is
    what this run reads. They were both "0 targets"."""
    answer = explain_no_targets([_instance(metrics=False)])

    assert "metrics.enabled" in answer
    assert "1 instance(s) are enabled" in answer


def test_a_db_type_filter_that_matched_nothing_says_what_the_estate_does_have():
    """The usual cause is a typo, and naming what is there turns a search through the config into
    a glance at one line."""
    answer = explain_no_targets(
        [_instance(db_type="sqlserver"), _instance(db_type="postgresql")], db_type="oracle")

    assert "--db-type oracle matched none" in answer
    assert "postgresql, sqlserver" in answer


def test_a_target_filter_that_matched_nothing_repeats_what_was_asked_for():
    answer = explain_no_targets([_instance()], target_id="ACME-1/sqlserver/SALESDB")

    assert "ACME-1/sqlserver/SALESDB" in answer


def test_an_inventory_that_should_have_produced_targets_is_named_as_a_defect():
    """The one case that is *not* a configuration state. Reporting it in the same words as the
    other four would hide a resolution bug behind advice to check the config."""
    answer = explain_no_targets([_instance()])

    assert "defect" in answer


def test_the_collect_summary_message_reaches_the_console(capsys):
    """It reached the store and stopped there: `_print_collect_summary` listed every count and
    not the one field carrying words."""
    from db_ops.metrics import cli

    cli._print_collect_summary(_summary(message="No metric targets: ... holds no instance."))

    printed = capsys.readouterr().out
    assert "target_count: 0" in printed
    assert "holds no instance" in printed


def test_a_run_with_nothing_to_say_says_nothing_extra(capsys):
    """The ordinary pass must not gain a blank line at the end of its summary."""
    from db_ops.metrics import cli

    cli._print_collect_summary(_summary(message=""))

    assert capsys.readouterr().out.strip().endswith("duration_seconds: 1.0")


# ---------------------------------------------------------------------------
def _instance(*, enabled=True, metrics=True, db_type="sqlserver"):
    item = {"server_id": "ACME-1", "db_type": db_type, "ip": "192.0.2.10", "enabled": enabled}
    if metrics is not True:
        item["metrics"] = {"enabled": metrics}
    return item


def _summary(*, message):
    from db_ops.metrics.models import CollectSummary

    return CollectSummary(
        run_id=1, target_count=0, metric_count=0, executed_count=0, skipped_interval_count=0,
        disabled_count=0, result_count=0, ok_count=0, error_count=0, warning_count=0,
        critical_count=0, no_data_count=0, started_at="", finished_at="", duration_seconds=1.0,
        message=message,
    )
