"""The AI assistant, asked for real, guides a new install from the AI key to backups and restores - opt-in, it spends tokens (0.39.0).

The operator, 2026-11-14 - the scenario, as test cases to run each time, switched on or off because
they cost tokens, all in English:

1. dbabrain 0.39 just installed, and the AI key added with one CLI command (``dbabrain ai-key``);
2. at the command line, the assistant asked how to set up Telegram - the bot token, the groups, the
   users and their permissions;
3. a Telegram user asks it, in the chat, how to build the install: add an instance, ...;
4. a Telegram user asks about backup and restore: add a backup, add a restore, run one by hand, run
   on a schedule, add the repository, restore to a point in time.

Every other test of the assistant uses a stand-in model and proves the plumbing. These ask the real
model, on a fresh tool root ``init`` wrote, and hold each answer to what a person could follow: in
English, naming the commands the question needs, and every ``dbabrain <app> <command>`` and
``/spbot_`` command it names one this version has. A Telegram question goes the way the bot's job
takes it - a message in the store from a user in the assistant's chat, answered by the job and the
reply queued - without sending anything to Telegram.

**Off unless asked**: ``DBABRAIN_LIVE_AI=1`` and the Anthropic key in ``DBABRAIN_LIVE_AI_KEY``.
Switched on with no key it fails, never skips. About a dozen questions; each answer, its tokens and
cost go to ``runtime/live_ai/<stamp>.md`` for a person to read.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
LIVE = os.environ.get("DBABRAIN_LIVE_AI", "").strip() == "1"
pytestmark = pytest.mark.skipif(not LIVE, reason="the live AI scenario spends tokens: DBABRAIN_LIVE_AI=1 runs it")

PASSPHRASE = "live-ai-scenario-passphrase"
CHAT, BOT, BOT_ID, ASKER = "-700", "dbabrain_live_test_bot", "4343", "700"
#: Letters only Vietnamese writes - the operator asked for English throughout.
NOT_ENGLISH = re.compile(r"[ăâđêôơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ]", re.IGNORECASE)
_DBABRAIN = re.compile(r"dbabrain ([a-z][a-z0-9-]+)(?: --config \S+)? ([a-z][a-z0-9-]+)(?![a-z0-9-])")
#: A bot command named in full - `/spbot_list_*` names a family, not a command.
_SPBOT = re.compile(r"/(spbot_[a-z0-9_]*[a-z0-9])(?![a-z0-9_*])")
TRANSCRIPT = REPO / "runtime" / "live_ai" / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.md"


def _key() -> str:
    key = os.environ.get("DBABRAIN_LIVE_AI_KEY", "").strip()
    if not key:
        pytest.fail("DBABRAIN_LIVE_AI=1 but DBABRAIN_LIVE_AI_KEY is empty: give the Anthropic key to run it")
    return key


def _env(root: Path) -> dict[str, str]:
    env = {name: value for name, value in os.environ.items()
           if not name.startswith("DBABRAIN") or name in ("DBABRAIN_LIVE_AI",)}
    env.update(DBABRAIN_HOME=str(root), DBABRAIN_SECRET_KEY=PASSPHRASE, PYTHONIOENCODING="utf-8",
               PYTHONPATH=str(REPO) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    return env


def _store(root: Path) -> Path:
    """The SQLite store ``init`` declares in ``data/store_config.json`` - created on first use."""
    declared = json.loads((root / "data" / "store_config.json").read_text(encoding="utf-8"))
    return root / declared["sqlite"]["path"]


def _record(title: str, question: str, answer: str, row: dict) -> None:
    TRANSCRIPT.parent.mkdir(parents=True, exist_ok=True)
    with TRANSCRIPT.open("a", encoding="utf-8") as out:
        out.write(f"## {title}\n\n**Q:** {question}\n\n{answer}\n\n_status {row.get('status')}, "
                  f"tokens {row.get('total_tokens')}, ${row.get('cost_usd')}_\n\n")


# --------------------------------------------------------------------------- scenario 1
@pytest.fixture(scope="module")
def fresh_root(tmp_path_factory):
    """dbabrain init, then the AI key added by the CLI command - the first step the guide gives."""
    from dbabrain.common import scaffold

    root = tmp_path_factory.mktemp("fresh_install")
    scaffold.initialise(root, app_name="live")
    done = subprocess.run([sys.executable, "-m", "dbabrain.common.cli", "ai-key", "-"], cwd=root, env=_env(root),
                          input=json.dumps({"value": _key()}), capture_output=True, text=True, encoding="utf-8",
                          timeout=120)
    answer = json.loads(done.stdout[done.stdout.find("{"):])
    assert answer["success"] is True, done.stdout + done.stderr
    settings = json.loads((root / "data" / "ai_assist.json").read_text(encoding="utf-8"))
    assert settings["enabled"] is True and settings["api_key_ref"]
    # The Telegram half of the scenario: the assistant's chat, one user switched on, no cooldown.
    from dbabrain.lib import ai_assist as A

    settings.update({"cooldown_seconds": 0, "require_mention": False, "max_message_age_seconds": 10 ** 9,
                     "users": [A.shipped_user(ASKER, "Live tester", active=True)]})
    (root / "data" / "ai_assist.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
    from dbabrain.db.store import DbaBrainStore

    DbaBrainStore(str(_store(root))).initialize()
    return root


def test_1_a_fresh_install_takes_the_ai_key_with_one_command(fresh_root):
    from dbabrain.lib.secret_text import load_secret_text_file

    settings = json.loads((fresh_root / "data" / "ai_assist.json").read_text(encoding="utf-8"))
    store = load_secret_text_file(fresh_root / "data" / "encrypted_secret_text.json", key=PASSPHRASE)
    assert store[settings["api_key_ref"]] == _key()


# --------------------------------------------------------------------------- the checks every answer meets
def _commands_exist(answer: str) -> list[str]:
    from dbabrain import cli

    from tests.test_every_command_a_guide_shows_would_run import _exists

    catalogue = json.loads((REPO / "dbabrain" / "telegram" / "catalogue" / "telegram_support_commands.json")
                           .read_text(encoding="utf-8"))
    bot_commands = {str(row.get("command_text")) for row in catalogue.get("telegram_support_commands", [])}
    missing = [f"dbabrain {app} {command}" for app, command in _DBABRAIN.findall(answer)
               if app in cli.APPS and app != "daemon" and not _exists(app, command)]
    missing += [f"/{name}" for name in _SPBOT.findall(answer) if name not in bot_commands]
    return missing


def _held(answer: str, *, any_of: list[tuple[str, ...]]) -> None:
    assert answer.strip(), "no answer"
    assert not NOT_ENGLISH.search(answer), "the answer is not in English"
    for group in any_of:
        assert any(word.lower() in answer.lower() for word in group), f"the answer names none of {group}"
    missing = _commands_exist(answer)
    assert not missing, f"the answer names commands this version does not have: {missing}"


# --------------------------------------------------------------------------- scenario 2: the command line
def _ask_cli(root: Path, question: str) -> tuple[str, dict]:
    done = subprocess.run([sys.executable, "-m", "dbabrain.telegram.cli", "ai-ask", question], cwd=root,
                          env=_env(root), capture_output=True, text=True, encoding="utf-8", timeout=600)
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    row = _last_request(root)
    assert row["status"] == "done", row
    return str(row["answer_text"] or ""), row


def _last_request(root: Path) -> dict:
    with sqlite3.connect(_store(root)) as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT * FROM ai_requests ORDER BY ai_request_id DESC LIMIT 1").fetchone())


CLI_QUESTIONS = [
    ("what next", "I have just installed dbabrain 0.39 and added the AI key. What do I set up next, step by step?",
     [("DBABRAIN_SECRET_KEY", "passphrase"), ("instance-add", "db_instances.json")]),
    ("telegram setup", "How do I connect Telegram to this install: where does the bot token go, how do I add the "
                       "groups and their levels, and how do I give users their permission levels?",
     [("secret-set", "use-bot"), ("group-level",), ("user-level",)]),
]


@pytest.mark.parametrize("title, question, any_of", CLI_QUESTIONS, ids=[q[0] for q in CLI_QUESTIONS])
def test_2_at_the_command_line_it_guides_the_setup(fresh_root, title, question, any_of):
    answer, row = _ask_cli(fresh_root, question)
    _record(f"CLI - {title}", question, answer, row)
    _held(answer, any_of=any_of)


# --------------------------------------------------------------------------- scenarios 3 and 4: Telegram
def _ask_telegram(root: Path, question: str) -> tuple[str, dict]:
    """A message from the test user in the assistant's chat, answered by the bot's own job."""
    from dbabrain.lib.secret_text import load_secret_text_file
    from dbabrain.telegram import ai_assist as job

    store = _store(root)
    now = datetime.now(timezone.utc)
    with sqlite3.connect(store) as conn:
        message_id = int(conn.execute("SELECT COALESCE(MAX(message_id), 0) + 1 FROM telegram_messages").fetchone()[0])
        raw = {"message_id": message_id, "date": int(now.timestamp()), "chat": {"id": int(CHAT), "title": "AI"},
               "from": {"id": int(ASKER), "first_name": "Live", "is_bot": False}, "text": question}
        conn.execute("INSERT INTO telegram_messages (message_id, message_date, chat_id, chat_type, user_id, text, "
                     "raw_json) VALUES (?, ?, ?, 'supergroup', ?, ?, ?)",
                     (message_id, int(now.timestamp()), CHAT, ASKER, question, json.dumps(raw)))
    settings, missing = job.load_settings(root / "data")
    assert settings is not None, missing
    secrets = load_secret_text_file(root / "data" / "encrypted_secret_text.json", key=PASSPHRASE)
    config = SimpleNamespace(store=str(store), timezone="+00:00", node_role="all",
                             telegram=SimpleNamespace(resolved_bot_token="", api_url="", telegram_bot_id=BOT_ID,
                                                      telegram_bot_username=BOT, level_chat_map={"ai_assist": CHAT}))
    runner = job.Runner(config=config, config_path=root / "config.json", settings=settings,
                        api_key=secrets[settings.api_key_ref], secrets=secrets, now=now)
    counts = runner.run()
    assert counts.get("answered") == 1, counts
    row = _last_request(root)
    assert row["status"] == "done" and row["question"] == question, row
    with sqlite3.connect(store) as conn:
        queued = conn.execute("SELECT COUNT(*) FROM telegram_send_messages WHERE source_type = 'ai_requests' "
                              "AND source_id = ?", (str(row["ai_request_id"]),)).fetchone()[0]
    assert queued >= 1, "the reply is queued for the bot"
    return str(row["answer_text"] or ""), row


BUILD_QUESTIONS = [
    ("add an instance", "How do I add a SQL Server instance for dbabrain to monitor, and check that it works?",
     [("instance-add",), ("check-credentials", "--dry-run", "check-secret")]),
    ("add a host", "How do I add a Linux host with no database, for OS metrics over SSH?",
     [("remote-credential-add", "cmd_access"), ("ssh",)]),
    ("run on a schedule", "How do I run dbabrain on a schedule so the collections and backups happen by themselves?",
     [("daemon",), ("app_commands", "app-command-set", "time_window")]),
]

BACKUP_QUESTIONS = [
    ("add a backup", "How do I add a backup of a SQL Server database with full, differential and log backups?",
     [("backup-add", "restore_config.json"), ("full",), ("log",)]),
    ("add a restore", "How do I add a restore drill that restores those backups onto another server?",
     [("restore-add", "restores"), ("target_server_id", "target")]),
    # On a fresh install nothing is registered yet: the run lines come from an entry's own runbook, so
    # the right answer is how to register and list the entries - never run lines written from memory.
    ("run by hand", "How do I run that backup by hand right now, and how do I run the restore by hand?",
     [("--force", "/spbot_backup", "backup --backup-id", "backup-add", "list-backups"),
      ("restore-workflow", "/spbot_restore", "restore-by-id", "restore-add", "list-restores")]),
    ("on a schedule", "How do I make the backup run every night at 1 AM and the log backup every 15 minutes?",
     [("time_window",), ("from_hour", "repeat_interval")]),
    ("add the repository", "How do I copy every backup to a backup repository on another server and encrypt it there?",
     [("offload",), ("offload_server_id", "repository")]),
    ("point in time", "How do I restore a database to a point in time, and check first what it would do?",
     [("point-in-time", "point_in_time"), ("--dry-run", "restore_plan", "dry run")]),
]


@pytest.mark.parametrize("title, question, any_of", BUILD_QUESTIONS, ids=[q[0] for q in BUILD_QUESTIONS])
def test_3_in_telegram_it_helps_build_the_install(fresh_root, title, question, any_of):
    answer, row = _ask_telegram(fresh_root, question)
    _record(f"Telegram - {title}", question, answer, row)
    _held(answer, any_of=any_of)


@pytest.mark.parametrize("title, question, any_of", BACKUP_QUESTIONS, ids=[q[0] for q in BACKUP_QUESTIONS])
def test_4_in_telegram_it_answers_backup_and_restore(fresh_root, title, question, any_of):
    answer, row = _ask_telegram(fresh_root, question)
    _record(f"Telegram - {title}", question, answer, row)
    _held(answer, any_of=any_of)
