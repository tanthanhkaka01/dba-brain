"""`/spbot_run_sql_task` — running one configured SQL task on demand.

`/spbot_list_sql_tasks` could show which tasks exist but there was no way to run one; the
operator's only options were to wait for the schedule or open a shell on the worker.

The risky part is not the wiring, it is the blast radius: the CLI accepts `run-sql-id` only
with `--force`, and a forced run skips the time window, the repeat interval *and* the active
flag. So an id the listing hides as inactive still runs when named. That is deliberate — an
operator who disabled a schedule may still want one manual run — but it has to be stated where
someone types the command, and the clearance has to match what the command can do.
"""

import json
import re
from pathlib import Path

import pytest

from db_ops.telegram.command_processor import load_support_commands
from conftest import shipped_config

COMMANDS_PATH = shipped_config("telegram_support_commands.json")


@pytest.fixture(scope="module")
def command():
    found = [c for c in load_support_commands(COMMANDS_PATH) if c.command_text == "spbot_run_sql_task"]
    assert found, "spbot_run_sql_task is not registered"
    return found[0]


def test_it_invokes_the_runner_the_way_the_cli_demands(command):
    """`run-sql-id` raises without `--force`, so the argv cannot omit it."""
    argv = command.action_config["command_argv"]

    assert argv[:3] == ["{python}", "-m", "db_ops.sql_tasks.runner"]
    assert "run-sql-id" in argv
    assert "--sql-id" in argv and "{sql_id}" in argv
    assert "--force" in argv
    # --dry-run is a top-level flag on this CLI; a subcommand-level placement is a usage error.
    assert argv.index("--config") < argv.index("run-sql-id")


def test_it_runs_in_the_background_with_the_secret_key(command):
    """Configured task timeouts reach 7200s — a foreground reply would block the processor."""
    config = command.action_config

    assert config["background"] is True and config["detached"] is True
    assert config["requires_secret_key"] is True          # the task connects to a database
    assert config["timeout_seconds"] >= 7200


def test_the_sql_id_parameter_rejects_anything_but_a_number(command):
    """The value is interpolated into an argv, so the validator is the boundary."""
    pattern = command.action_config["parameters"][0]["pattern"]
    matches = re.compile(f"^(?:{pattern})$").match

    assert matches("8") and matches("16") and matches("12345")
    for hostile in ["", "abc", "8 16", "--force", "8; rm -rf /", "-1"]:
        assert not matches(hostile), f"{hostile!r} must be rejected"


#: The write/execute tier the 2026-07-31 audit put every command that can change data on. Read as a
#: floor, never as the value: an install is free to place a forced production run higher.
WRITE_TIER = 10


def test_clearance_matches_what_the_command_can_do():
    """A task may be an UPDATE against production, run outside its window.

    Asserted as a *floor and an ordering*, never as the value. The level was 3 until the
    2026-07-31 audit moved every write/execute command to 10, and pinning that literal made this
    test fail for a change that strengthened exactly what it guards. Requiring the three to be
    *equal* had the same defect one step up: this estate raised `/spbot_run_sql_task` to 50 — the
    tier of `/spbot_kill_spid` and `/spbot_shrink_log`, because a forced run ignores the window
    and the active flag — and the suite failed for a tightening. Clearance is a local policy
    decision (`test_telegram_commands_are_shippable` says the same about the shipped catalogue),
    so what this test owns is the direction: never below the write tier, and never within reach of
    a chat cleared only to read.
    """
    levels = {c.command_text: c.command_type for c in load_support_commands(COMMANDS_PATH)}

    executes_sql = ("spbot_run_sql_task", "spbot_restore", "spbot_add_sql")
    read_only = ("spbot_list_sql_tasks", "spbot_list_server_id", "spbot_report_metric_history",
                 "spbot_report_inventory", "spbot_status")

    below = {name: levels[name] for name in executes_sql if levels[name] < WRITE_TIER}
    assert not below, (
        f"a command that executes SQL sits below the write tier ({WRITE_TIER}): {below}")

    reading = max(levels[name] for name in read_only)
    outranked = {name: levels[name] for name in executes_sql if levels[name] <= reading}
    assert not outranked, (
        f"executing SQL must outrank every read-only command (highest read-only clearance is "
        f"{reading}): {outranked}")


def test_the_forced_run_is_confirmed_and_the_answer_reaches_the_cli(command):
    """Clearance says who may ask; it has never said how hard it is to ask.

    Raising this command to 50 put it beside `/spbot_kill_spid` in *who* may run it while it still
    cost nothing to run — a forced run of an UPDATE task needed no answer at all. The confirmation
    is the CLI's (`db_ops.common.confirm`, keyed on `emergency_operation`), so the argv has to
    carry the answer: a prompt the bot asks and then drops is theatre.
    """
    config = command.action_config
    argv = config["command_argv"]

    assert config["emergency_operation"] == "run-sql-task"
    assert argv[argv.index("--confirm") + 1] == "{confirm}"

    confirm = next(item for item in config["parameters"] if item["name"] == "confirm")
    assert confirm["required"] is True and confirm["pattern"] == "yes"
    assert "yes" in confirm["prompt_text"].lower()


def test_the_confirmation_is_answered_before_the_task_s_own_parameters(command):
    """Which side of `task_params` the answer sits on is the whole difficulty.

    A confirmation was tried here once and removed (the sql_id 24 note in `sql_commands.json` says
    why): a value typed after the id fills the task's OWN declared parameters, so
    `/spbot_run_sql_task 24 yes` bound `yes` to that task's first parameter and the operator
    confirmed nothing. Putting it *behind* `task_params` is worse, not better — a `consume_rest`
    value is `" ".join(args[position - 1:])`, so anything stored after it is also *inside* it, and
    the word `yes` would be appended to the parameters the task actually runs with.

    In front of it, both readings are unambiguous: `confirm` owns exactly one slot, `task_params`
    owns everything after it.
    """
    parameters = {item["name"]: item for item in command.action_config["parameters"]}
    rest = next(item for item in parameters.values() if item.get("consume_rest"))

    assert int(parameters["confirm"]["position"]) < int(rest["position"])


@pytest.mark.parametrize("args, confirm, task_params", [
    # Typed in one line: the answer is one word, and the rest is the task's own.
    (["24", "yes", "2026-09-01", "2026-09-02"], "yes", "2026-09-01 2026-09-02"),
    # Answered one prompt at a time: the parameters are asked first, so the empty slot in the
    # middle is the confirmation, filled last.
    (["24", "yes", "2026-09-01"], "yes", "2026-09-01"),
    # A task that declares no parameters at all.
    (["9", "yes"], "yes", ""),
])
def test_the_answer_never_lands_inside_the_task_s_parameters(args, confirm, task_params, tmp_path):
    """The failure the removed confirmation actually caused, pinned against the real binder.

    `cli_action_values` is what turns the collected answers into the argv, so this is the layer
    where a confirmation that steals a parameter — or a parameter that reads as a confirmation —
    shows up. Anything else is an opinion about a JSON file.
    """
    from db_ops.telegram.command_processor import cli_action_values

    found = [c for c in load_support_commands(COMMANDS_PATH) if c.command_text == "spbot_run_sql_task"]
    values = cli_action_values(command=found[0], args=list(args), config_path=str(tmp_path / "config.json"))

    assert values["confirm"] == confirm
    assert values.get("task_params", "") == task_params


def test_a_run_that_skips_the_confirmation_is_refused_before_it_starts(tmp_path):
    """`/spbot_run_sql_task 24 2026-09-01` is the old habit; it must not run the task.

    The value in the confirmation's slot is a date, not `yes`, so the regex refuses it — and the
    error names the way out rather than repeating the rule.
    """
    from db_ops.telegram.command_processor import TelegramCommandError, cli_action_values

    found = [c for c in load_support_commands(COMMANDS_PATH) if c.command_text == "spbot_run_sql_task"]
    with pytest.raises(TelegramCommandError) as refused:
        cli_action_values(command=found[0], args=["24", "2026-09-01"],
                          config_path=str(tmp_path / "config.json"))

    assert "yes" in str(refused.value) and "sql_id" in str(refused.value)


def test_the_operator_is_told_a_forced_run_ignores_the_active_flag(command):
    """The listing hides inactive tasks, which reads as "not runnable" — but a named id runs."""
    prompt = command.action_config["parameters"][0]["prompt_text"].lower()
    start_text = command.action_config["start_text"].lower()

    assert "active" in prompt, "the prompt must say a forced run ignores the active flag"
    assert "list_sql_tasks" in prompt, "the prompt must say where sql_id comes from"
    assert "forced" in start_text or "ignores" in start_text


def test_it_is_offered_in_the_botfather_menu():
    """The bot executes from the JSON, but an operator finds commands in the menu file."""
    menu = Path("data/telegram_support_commands.md").read_text(encoding="utf-8")

    assert "spbot_run_sql_task - " in menu


def test_the_telegram_backstop_outlives_every_configured_task_timeout():
    """The Telegram ceiling must never be the thing that decides how long a SQL task may run.

    `/spbot_run_sql_task` dispatches the runner detached, and
    `command_processor.check_cli_background_tasks` **SIGKILLs** it once the background task is
    older than the command's `timeout_seconds` (command_processor.py:2517-2521). That kill is
    blind to the task: at the old value of 7200 a folder task of six files at ~2h each was killed
    during file 2, mid-statement, with the run row left `running` until the stale sweep caught it.

    So the two numbers are coupled, and nothing enforced it. The real per-task bound is
    `time_window.timeout` on the sql_target — the statement timeout for each file *and* the
    stale-run threshold. This pins the backstop at or above all of them, so raising a target's
    timeout past the Telegram ceiling fails here instead of in production.
    """
    import json
    from pathlib import Path

    commands = json.loads(
        shipped_config("telegram_support_commands.json").read_text(encoding="utf-8")
    )["telegram_support_commands"]
    backstop = next(c for c in commands
                    if c["command_text"] == "spbot_run_sql_task")["action_config"]["timeout_seconds"]

    targets = json.loads(shipped_config("sql_targets.json").read_text(encoding="utf-8"))
    targets = targets["sql_targets"] if isinstance(targets, dict) else targets

    over = {
        f"sql_id={t['sql_id']} target_no={t['target_no']}": (t.get("time_window") or {}).get("timeout")
        for t in targets
        if int((t.get("time_window") or {}).get("timeout") or 0) > int(backstop)
    }

    assert not over, (
        f"These sql_targets allow a task to run longer than /spbot_run_sql_task's {backstop}s "
        f"backstop, so a Telegram-started run would be SIGKILLed before finishing: {over}. "
        f"Raise action_config.timeout_seconds on spbot_run_sql_task to match."
    )
