"""The shared `notify` object: who gets told about a unit of work, and where.

Covers both apps that carry it — backup_restore entries/sub-jobs and SQL targets — because
the point of the object is that they behave identically.

`notify` is a common config object like `time_window` — one shape (`db_ops/common/notify.py`)
that every app's entries carry, not a convention each app invents. It answers two questions
per entry: report every run, and report failures — each with an enable flag, a level, and an
optional explicit chat.

Two layers meet in `notify_route.notify_chat_id`, and the order matters: the **node** decides
whether a level may alert at all; the entry's `notify` object narrows that. An entry can go
quiet or point somewhere else — never switch on a level the node switched off.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from db_ops.backup_restore.config import (
    BACKUP_RESTORE_NOTIFY_DEFAULTS,
    merge_notify_configs,
    parse_backup_restore_notify,
)
from db_ops.backup_restore.events import emit_backup_restore_event
from db_ops.lib import telegram_route as app_client
from db_ops.lib import notify_route
from db_ops.lib.notify import NotifyConfig, NotifyConfigError, NotifyRule, parse_notify_config
from conftest import shipped_config


class _Config:
    def __init__(self, sqlite_path):
        self.sqlite_path = str(sqlite_path)


    @property
    def store(self):
        """SQLite store declaration matching this fake's sqlite_path (see db_ops.config)."""
        from db_ops.config import SqliteStoreConfig, StoreConfig
        from pathlib import Path as _Path

        return StoreConfig(sqlite=SqliteStoreConfig(path=_Path(str(self.sqlite_path))))

@pytest.fixture(autouse=True)
def _no_route_cache():
    # The cache lives with the transport now, which is the app's client - common holds no
    # state at all.
    app_client.clear_cache()
    yield
    app_client.clear_cache()


#: The node these tests assume: every standard level alerts, each to its own group.
NODE_CHATS = {"logging": "-100LOG", "warning": "-100WARN", "error": "-100ERR", "critical": "-100CRIT"}


def resolve(level, notify=None, *, chats=None):
    """The policy call, with the node's answer handed in - common fetches nothing now."""
    chats = NODE_CHATS if chats is None else chats
    route = {"enabled": bool(chats), "alert": level in chats, "chat_id": chats.get(level, "")}
    return notify_route.resolve_chat_id(level, notify, route=route, groups=chats)


@pytest.fixture()
def node(monkeypatch):
    """A node where logging/warning/error/critical all alert, each to its own group.

Only the app's client is stubbed: ``common`` fetches nothing any more, so there is nothing
    to stub there. Missing this stub is not a quiet failure - the test reaches the real Telegram
    config and asserts against a live chat id.
    """
    chats = dict(NODE_CHATS)

    def _route(level, use_cache=True):
        return {"enabled": True, "alert": level in chats, "chat_id": chats.get(level, "")}

    monkeypatch.setattr(app_client, "telegram_route", _route)
    monkeypatch.setattr(app_client, "telegram_groups", lambda **_: dict(chats))
    return chats


# ---------------------------------------------------------------------------
# The shape, and the legacy spellings it has to keep reading
# ---------------------------------------------------------------------------
def test_the_object_is_read_out_of_an_entry_the_way_time_window_is():
    """Same call shape as parse_time_window_config: hand it the entry, get the object."""
    entry = {
        "backup_id": "B1",
        "notify": {
            "logging_on_run": {"enabled": False, "telegram_chat": "logging", "chat_id": ""},
            "alert_on_error": {"enabled": True, "telegram_chat": "error", "chat_id": "-100DBA"},
        },
    }

    config = parse_notify_config(entry, context="backups[0]")

    assert config.logging_on_run.enabled is False
    assert config.alert_on_error.enabled is True
    assert config.alert_on_error.chat_id == "-100DBA"


def test_an_empty_object_means_the_callers_defaults():
    """`"notify": {}` is valid and says nothing — so an app that notifies by default still
    does, and one that does not still does not."""
    quiet_app = NotifyConfig(logging_on_run=NotifyRule(enabled=False),
                             alert_on_error=NotifyRule(enabled=False))

    assert parse_notify_config({"notify": {}}, defaults=quiet_app) == quiet_app
    assert parse_notify_config({}, defaults=BACKUP_RESTORE_NOTIFY_DEFAULTS) == BACKUP_RESTORE_NOTIFY_DEFAULTS


def test_the_sql_targets_spelling_is_still_read():
    """sql_targets.json carries the two rules at the top level of an entry, not nested under
    `notify`. Those files are in production; both spellings normalize to one object."""
    entry = {
        "sql_id": 8,
        "logging_on_run": {"enabled": True, "telegram_chat": "logging", "chat_id": ""},
        "alert_on_error": {"enabled": True, "telegram_chat": "error", "chat_id": ""},
    }

    config = parse_notify_config(entry, context="sql_targets[0]")

    assert config.logging_on_run.enabled and config.logging_on_run.telegram_chat == "logging"
    assert config.alert_on_error.enabled and config.alert_on_error.telegram_chat == "error"


def test_the_boolean_spelling_is_still_read():
    config = parse_notify_config({"logging_on_run": True, "alert_on_error": False},
                                 defaults=NotifyConfig(
                                     logging_on_run=NotifyRule(enabled=False, telegram_chat="logging"),
                                     alert_on_error=NotifyRule(enabled=False, telegram_chat="error")))

    assert config.logging_on_run == NotifyRule(enabled=True, telegram_chat="logging")
    assert config.alert_on_error == NotifyRule(enabled=False, telegram_chat="error")


def test_a_nested_rule_wins_over_the_legacy_top_level_one():
    entry = {"logging_on_run": True, "notify": {"logging_on_run": {"enabled": False}}}

    assert parse_notify_config(entry).logging_on_run.enabled is False


def test_a_typo_is_rejected_at_parse_time():
    """It would otherwise parse cleanly and silently do nothing."""
    with pytest.raises(NotifyConfigError, match="unknown key\\(s\\) loging_on_run"):
        parse_notify_config({"notify": {"loging_on_run": {"enabled": False}}}, context="backups[0]")
    with pytest.raises(NotifyConfigError, match="telegram_chat must be one of"):
        parse_notify_config({"notify": {"alert_on_error": {"telegram_chat": "urgent"}}})


def test_a_level_the_config_defines_is_accepted(monkeypatch):
    """A group added with notify_level: "sla" defines that level. Requiring a code edit to
    use it would mean an image rebuild per group — and a config rejected until then."""
    from db_ops.lib import notify

    monkeypatch.setattr(notify, "known_chat_levels", lambda: notify.NOTIFY_CHAT_LEVELS + ("sla",))
    rule = parse_notify_config({"notify": {"alert_on_error": {"telegram_chat": "sla"}}}).alert_on_error
    assert rule.telegram_chat == "sla"
    assert rule.resolve_chat_id({"sla": "-100000002", "error": "-1"}) == "-100000002"


def test_known_levels_include_the_ones_this_deployment_configured():
    from db_ops.lib.notify import NOTIFY_CHAT_LEVELS, known_chat_levels

    levels = known_chat_levels()
    assert set(NOTIFY_CHAT_LEVELS) <= set(levels)  # standard levels always available
    assert len(set(levels)) == len(levels)  # no duplicates when config repeats a standard level


# ---------------------------------------------------------------------------
# Severity -> rule
# ---------------------------------------------------------------------------
def test_a_severity_maps_to_the_rule_that_governs_it():
    """Apps emit by severity, not by rule name, so the mapping is made once in common."""
    config = NotifyConfig()

    assert config.rule_for_level("logging") is config.logging_on_run
    for level in ("warning", "error", "critical"):
        assert config.rule_for_level(level) is config.alert_on_error, level


# ---------------------------------------------------------------------------
# Node gate vs entry narrowing
# ---------------------------------------------------------------------------
def test_with_no_notify_object_the_node_decides_alone(node):
    assert resolve("logging", None) == "-100LOG"
    assert resolve("error", None) == "-100ERR"


def test_the_default_object_routes_exactly_like_no_object_at_all(node):
    """Adding `notify` to a config must be a no-op until a rule is actually set — otherwise
    every existing entry silently changes destination the day the object is introduced."""
    default = BACKUP_RESTORE_NOTIFY_DEFAULTS

    for level in ("logging", "warning", "error", "critical"):
        assert resolve(level, default) == resolve(level, None), level


def test_a_disabled_rule_sends_nothing(node):
    quiet_runs = NotifyConfig(logging_on_run=NotifyRule(enabled=False),
                              alert_on_error=NotifyRule(enabled=True))

    assert resolve("logging", quiet_runs) == ""
    # ...and the failure path on the same entry is untouched.
    assert resolve("error", quiet_runs) == "-100ERR"


def test_a_rule_can_redirect_to_another_level_or_an_explicit_chat(node):
    to_level = NotifyConfig(alert_on_error=NotifyRule(enabled=True, telegram_chat="warning"))
    to_chat = NotifyConfig(alert_on_error=NotifyRule(enabled=True, chat_id="-100DBA"))

    assert resolve("error", to_level) == "-100WARN"
    assert resolve("error", to_chat) == "-100DBA"


def test_an_entry_cannot_switch_on_a_level_the_node_switched_off():
    """Otherwise a per-entry block becomes a way to leak messages out of a muted node."""
    loud = NotifyConfig(alert_on_error=NotifyRule(enabled=True, chat_id="-100DBA"))

    assert resolve("error", loud, chats={}) == ""


# ---------------------------------------------------------------------------
# Inheritance: entry -> sub-job
# ---------------------------------------------------------------------------
def test_a_sub_job_inherits_its_entry_and_overrides_rule_by_rule():
    entry = parse_backup_restore_notify(
        {"notify": {"logging_on_run": {"enabled": False}, "alert_on_error": {"telegram_chat": "critical"}}},
        context="entry",
    )

    job = parse_backup_restore_notify(
        {"notify": {"logging_on_run": {"enabled": True}}}, context="job", inherit=entry
    )

    assert job.logging_on_run.enabled is True              # overridden
    assert job.alert_on_error.telegram_chat == "critical"  # inherited


def test_a_sub_job_with_no_object_of_its_own_takes_the_entrys():
    entry = parse_backup_restore_notify({"notify": {"logging_on_run": {"enabled": False}}}, context="entry")

    assert parse_backup_restore_notify({}, context="job", inherit=entry) == entry


# ---------------------------------------------------------------------------
# An event covering several entries
# ---------------------------------------------------------------------------
def test_a_rule_is_off_only_when_every_entry_turns_it_off():
    """A workflow event covers all selected entries; one entry's preference must not delete a
    message another entry is waiting for."""
    quiet = NotifyConfig(logging_on_run=NotifyRule(enabled=False))
    loud = NotifyConfig(logging_on_run=NotifyRule(enabled=True, telegram_chat="logging"))

    assert merge_notify_configs([quiet, quiet]).logging_on_run.enabled is False
    assert merge_notify_configs([quiet, loud]).logging_on_run.enabled is True
    assert merge_notify_configs([quiet, loud]).logging_on_run.telegram_chat == "logging"


# ---------------------------------------------------------------------------
# End to end through the emitter
# ---------------------------------------------------------------------------
def test_a_silenced_job_queues_no_message_but_is_still_recorded(tmp_path, node):
    """Silence is an alerting choice, not a logging one."""
    config = _Config(tmp_path / "runtime.sqlite")

    emit_backup_restore_event(
        app_config=config, command="backup", phase="START", level="logging",
        message="ACME_PG_LAB01_PRIMARY/wal started.",
        metadata={"backup_id": "ACME_PG_LAB01_PRIMARY", "job": "wal"},
        notify=NotifyConfig(logging_on_run=NotifyRule(enabled=False)),
    )

    with sqlite3.connect(str(config.sqlite_path)) as conn:
        assert conn.execute("SELECT count(*) FROM telegram_send_messages").fetchone()[0] == 0
        message, metadata = conn.execute(
            "SELECT message, metadata_json FROM job_runs ORDER BY log_id DESC LIMIT 1"
        ).fetchone()
    assert "backup_id=ACME_PG_LAB01_PRIMARY" in message
    assert json.loads(metadata)["backup_id"] == "ACME_PG_LAB01_PRIMARY"


def test_a_failure_on_that_same_job_still_alerts(tmp_path, node):
    """Silencing the routine chatter must not silence the thing worth waking up for."""
    config = _Config(tmp_path / "runtime.sqlite")

    emit_backup_restore_event(
        app_config=config, command="backup", phase="ERROR", level="error",
        message="Backup ACME_PG_LAB01_PRIMARY/wal finished: error (exit 1)",
        metadata={"backup_id": "ACME_PG_LAB01_PRIMARY", "job": "wal"},
        notify=NotifyConfig(logging_on_run=NotifyRule(enabled=False),
                            alert_on_error=NotifyRule(enabled=True)),
    )

    with sqlite3.connect(str(config.sqlite_path)) as conn:
        chat_id, text = conn.execute(
            "SELECT tlgchat_id, message_text FROM telegram_send_messages ORDER BY send_tlgmsg_id DESC LIMIT 1"
        ).fetchone()
    assert str(chat_id) == "-100ERR"
    assert "backup_id=ACME_PG_LAB01_PRIMARY" in text


# ---------------------------------------------------------------------------
# What the shipped config actually asks for
# ---------------------------------------------------------------------------
def test_frequent_backup_jobs_stay_quiet_and_every_job_still_alerts_on_failure():
    """The operator's intent, per sub-job — expressed as the rule, not a list of job names:
    a job that runs a few times a day is worth a run message; one that runs every few minutes
    (archivelog, WAL, log) would drown the group. Failures stay on for all of them, which is
    the half that must never be switched off."""
    from db_ops.backup_restore.backup import load_backup_jobs

    HOURLY = 3600
    jobs = load_backup_jobs(shipped_config("restore_config.json"))
    assert jobs, "expected backup jobs in the shipped config"

    for job in jobs:
        interval = job.time_window.repeat_interval or 0
        frequent = 0 < interval <= HOURLY
        assert job.notify.logging_on_run.enabled is not frequent, (
            f"{job.label} runs every {interval}s; "
            f"logging_on_run should be {'off' if frequent else 'on'}"
        )
        assert job.notify.alert_on_error.enabled is True, job.label


def test_sql_targets_carry_the_object_and_not_the_legacy_spelling():
    """The two rules are nested under `notify`, like `time_window` beside them. Both files
    are read by the same parser; keeping half of one in the old spelling is how a shared
    convention quietly stops being one."""
    raw = json.loads(shipped_config("sql_targets.json").read_text(encoding="utf-8"))
    entries = raw["sql_targets"]

    assert entries, "expected SQL targets in the shipped config"
    for entry in entries:
        assert "notify" in entry, entry.get("sql_id")
        assert set(entry["notify"]) == {"logging_on_run", "alert_on_error"}, entry.get("sql_id")
        # No entry still carries the rules at the top level.
        assert "logging_on_run" not in entry and "alert_on_error" not in entry, entry.get("sql_id")


def test_a_newly_added_sql_task_is_written_in_the_canonical_form(tmp_path, monkeypatch):
    """Otherwise the file drifts back to the legacy spelling one `add-sql` at a time."""
    from db_ops.common import config_admin

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "sql_targets.json").write_text(json.dumps({"sql_targets": []}), encoding="utf-8")
    (data_dir / "sql_commands.json").write_text(json.dumps({"sql_commands": []}), encoding="utf-8")

    config_admin.add_sql_task(
        data_dir=data_dir,
        tool_root=tmp_path,
        server_id="ACME-192-0-2-250",
        db_type="sqlserver",
        service_name="APPDB-PROD",
        instance_name="APPDB",
        credential_name="cred",
        sql_name="Nightly cleanup",
        sql_text="SELECT 1;",
    )

    entry = json.loads((data_dir / "sql_targets.json").read_text(encoding="utf-8"))["sql_targets"][0]
    assert set(entry["notify"]) == {"logging_on_run", "alert_on_error"}
    assert "logging_on_run" not in entry and "alert_on_error" not in entry
