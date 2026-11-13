"""dbabrain's AI assistant has a name, and the name is configuration (0.38.0, 1.326).

The operator, 2026-11-11: *dbabrain's AI - on the worker and on the PC - has no name yet; name it,
set in the config json.* A name is a value that points outside the code: a second estate will call
its assistant something else, and a name written into a prompt, a reply and a page is three places
to forget when it changes. So ``data/ai_assist.json`` states ``assistant_name`` - required, like
every field of that file - and every place the assistant speaks or is addressed reads it:

* the model is told its name in the system prompt;
* every Telegram reply the job writes itself carries it: the *working on it* line, the footer, the
  help, the usage, a failure, a refusal;
* a message that calls the assistant by its name is addressed to it, as an ``@mention`` of the bot
  is - and the name in the middle of a sentence is people talking *about* it, left alone;
* ``upgrade-config`` writes the shipped name into a node's file that states none, and leaves a
  node's own name as it is;
* there is no name in code to fall back to: a file without one is refused by name.

The console reads the same field; ``test_console_shows_the_ai_assistant.py`` holds that.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.test_the_ai_assistant_answers_in_its_chat_and_keeps_everything import (
    BOT, OPERATOR, Model, _message, _root, _rows, _run)

from dbabrain.lib import ai_assist as A
from dbabrain.telegram import ai_assist as job

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED = A.shipped_document()["assistant_name"]
#: A node that named its assistant itself: nothing below may depend on the shipped name.
OTHER = "Ada"
NODE = {"role": "worker", "backend": "postgresql", "timezone": "+07:00"}


def _settings(**changes) -> A.AiAssistSettings:
    return A.parse_ai_assist({**A.shipped_document(), "users": [], **changes})


# --------------------------------------------------------------------------- #
# The field
# --------------------------------------------------------------------------- #
def test_the_name_is_a_field_of_the_file_and_a_file_without_it_is_refused_by_name():
    assert _settings().assistant_name == SHIPPED
    assert _settings(assistant_name=f"  {OTHER} ").assistant_name == OTHER
    document = A.shipped_document()
    del document["assistant_name"]
    with pytest.raises(A.AiAssistConfigError, match="missing field.*assistant_name"):
        A.parse_ai_assist(document)


@pytest.mark.parametrize("wrong", ["", "   ", "/help", "@ada", "two\nlines", "x" * 41, 5, None, ["Ada"]])
def test_a_name_that_cannot_be_said_is_refused(wrong):
    """Empty has nothing to say and no fallback in code; a leading ``/`` reads as a bot command, and
    the ``@`` of a mention is the writer's; a second line would break the reply's first line."""
    with pytest.raises(A.AiAssistConfigError, match="assistant_name must be a name"):
        _settings(assistant_name=wrong)


def test_upgrade_config_writes_the_shipped_name_and_leaves_a_nodes_own(tmp_path):
    from dbabrain.common.config_upgrade import _stated_ai_assist

    before = {key: value for key, value in A.shipped_document().items()
              if key not in ("assistant_name", "backup_runbook_enabled")}
    (tmp_path / "ai_assist.json").write_text(json.dumps({**before, "enabled": True}), encoding="utf-8")
    answer = _stated_ai_assist(tmp_path, dry_run=False, before_write=lambda path: None)
    saved = json.loads((tmp_path / "ai_assist.json").read_text(encoding="utf-8"))
    assert saved["assistant_name"] == SHIPPED and saved["enabled"] is True
    assert {"where": "ai_assist.json", "field": "assistant_name", "value": "the shipped value"} in \
        answer["files"][0]["changes"]
    A.parse_ai_assist(saved)
    # A second pass has nothing to write, and a name the node chose is never replaced.
    (tmp_path / "ai_assist.json").write_text(json.dumps({**saved, "assistant_name": OTHER}), encoding="utf-8")
    again = _stated_ai_assist(tmp_path, dry_run=False, before_write=lambda path: None)
    assert again["files"][0]["changes"] == [] and again["files"][0]["written"] is False
    assert json.loads((tmp_path / "ai_assist.json").read_text(encoding="utf-8"))["assistant_name"] == OTHER


def test_a_node_with_no_file_is_given_the_shipped_one_with_its_name(tmp_path):
    from dbabrain.common.config_upgrade import _stated_ai_assist

    _stated_ai_assist(tmp_path, dry_run=False, before_write=lambda path: None)
    created = json.loads((tmp_path / "ai_assist.json").read_text(encoding="utf-8"))
    assert created["assistant_name"] == SHIPPED and created["enabled"] is False
    A.parse_ai_assist(created)


def test_every_copy_of_the_file_states_the_name():
    """The packaged seed `init` writes from, the public example, and this tree's own file when it
    has one (a public checkout does not)."""
    copies = [REPO_ROOT / "dbabrain" / "telegram" / "catalogue" / "ai_assist.json",
              REPO_ROOT / "data" / "ai_assist.example.json", REPO_ROOT / "data" / "ai_assist.json"]
    read = [json.loads(path.read_text(encoding="utf-8-sig")) for path in copies if path.is_file()]
    assert len(read) >= 2
    for document in read:
        assert A.stated_name(document), "a copy that states no name is one upgrade-config has to mend"
        A.parse_ai_assist(document)
    assert read[0]["assistant_name"] == read[1]["assistant_name"] == SHIPPED


def test_the_reference_describes_the_field_as_required_and_never_empty():
    from dbabrain.lib import shared_objects

    entry = next(item for item in shared_objects.load(REPO_ROOT / "data") if item["object"] == "ai_assist_settings")
    field = next(item for item in entry["fields"] if item["field"] == "assistant_name")
    assert field["required"] is True and field["constraint"]["allow_empty"] is False
    assert field["default"] == json.dumps(SHIPPED)
    assert [item["field"] for item in entry["fields"]][:-2] == [spec.name for spec in A.SETTINGS_FIELDS], \
        "the reference lists the settings in the order the code states them, then prices and users"
    problems = shared_objects.check_value(field["constraint"], "", where="ai_assist.json", field="assistant_name")
    assert problems, "an empty name is a violation check-objects names"


def test_the_shipped_name_is_written_once_in_the_code():
    """In ``lib/ai_assist.py``, as the value ``upgrade-config`` writes - and nowhere a reader could
    take for a fallback: no prompt, no reply, no page, no guide says it."""
    found = []
    for path in sorted((REPO_ROOT / "dbabrain").rglob("*")):
        if path.suffix not in (".py", ".md", ".txt", ".html", ".sh", ".sql") or "__pycache__" in path.parts:
            continue
        count = len(re.findall(re.escape(SHIPPED), path.read_text(encoding="utf-8", errors="replace")))
        if count:
            found.append((path.relative_to(REPO_ROOT).as_posix(), count))
    assert found == [("dbabrain/lib/ai_assist.py", 1)]


# --------------------------------------------------------------------------- #
# Where it speaks
# --------------------------------------------------------------------------- #
def test_the_model_is_told_its_name_and_a_renamed_node_tells_it_the_new_one():
    shipped = job.system_prompt(_settings(), node=NODE)
    assert shipped.startswith(f"Your name is {SHIPPED}.") and f"users call you {SHIPPED}" in shipped
    renamed = job.system_prompt(_settings(assistant_name=OTHER), node=NODE)
    assert renamed.startswith(f"Your name is {OTHER}.") and SHIPPED not in renamed
    assert "The name changes nothing about what you may read or do." in renamed
    # The rest of the prompt is the same text: only the name's own lines differ.
    assert shipped.replace(SHIPPED, OTHER) == renamed


def test_every_reply_the_job_writes_itself_carries_the_name(tmp_path):
    root = _root(tmp_path, assistant_name=OTHER, private_chat_enabled=False)
    _message(root, 1, OPERATOR, "how are the backups?")
    _message(root, 2, OPERATOR, "/help")
    _message(root, 3, OPERATOR, "/usage")
    _message(root, 4, "999", "and me?")
    _run(root)
    sent = [row["message_text"] for row in
            _rows(root, "SELECT message_text FROM telegram_send_messages ORDER BY send_tlgmsg_id")]
    assert sent[0] == f"{OTHER} is working on it (claude-sonnet-5-5)..."
    assert f"\n\n-- {OTHER} · claude-sonnet-5-5 · " in sent[1], "the footer signs the answer"
    assert any(text.startswith(f"{OTHER}, dbabrain's AI assistant - write a question in this group") for text in sent)
    assert any(text.startswith(f"{OTHER} (AI assistant) - usage\nSystem - month") for text in sent)
    assert any(f"you are not switched on for {OTHER}, dbabrain's AI assistant" in text for text in sent)
    assert not any(SHIPPED in text for text in sent), "no reply says a name the node did not state"
    notes = {row["note"] for row in _rows(root, "SELECT note FROM telegram_send_messages")}
    assert notes == {f"{OTHER} (AI assistant)", f"{OTHER} (AI assistant) - started"}


def test_a_failure_is_said_in_its_name(tmp_path):
    root = _root(tmp_path, assistant_name=OTHER)
    _message(root, 1, OPERATOR, "first?")

    class Breaks(Model):
        def __call__(self, params):
            raise TimeoutError("no answer from the API")

    _run(root, Breaks())
    replies = [row["message_text"] for row in _rows(root, "SELECT message_text FROM telegram_send_messages")]
    assert any(text.startswith(f"{OTHER} failed: TimeoutError: no answer from the API") for text in replies)


# --------------------------------------------------------------------------- #
# Where it is addressed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text, question", [
    (f"{OTHER}, backup hôm nay ổn không?", "backup hôm nay ổn không?"),
    (f"{OTHER.lower()} ơi backup hôm nay ổn không?", "ơi backup hôm nay ổn không?"),
    (f"@{OTHER.upper()} is the store backed up?", "is the store backed up?"),
    (f"can you check the store, @{OTHER}?", "can you check the store, ?"),
])
def test_a_message_that_calls_it_by_name_is_answered_without_the_name_in_the_question(tmp_path, text, question):
    root = _root(tmp_path, require_mention=True, assistant_name=OTHER)
    _message(root, 1, OPERATOR, text)
    model = Model()
    assert _run(root, model)["answered"] == 1
    row = _rows(root, "SELECT status, question FROM ai_requests")[0]
    assert row["status"] == "done" and row["question"] == question
    asked = model.calls[0]["messages"][-1]["content"][-1]["text"].split("<dbabrain_context>")[0]
    assert OTHER.lower() not in asked.lower(), "the name addresses the question; it is not part of it"


@pytest.mark.parametrize("text", [
    f"did {OTHER} answer you yesterday?",       # about it, not to it
    f"{OTHER}x is a typo",                      # another word that begins with it
    f"{OTHER.lower()}@example.com sent this",   # an address
    f"{SHIPPED}, are you there?",               # the name another node ships, not this node's
])
def test_the_name_in_passing_is_people_talking_and_is_neither_answered_nor_kept(tmp_path, text):
    root = _root(tmp_path, require_mention=True, assistant_name=OTHER)
    _message(root, 1, OPERATOR, text)
    model = Model()
    _run(root, model)
    assert model.calls == []
    row = _rows(root, "SELECT status, question, reason FROM ai_requests")[0]
    assert row["status"] == "stopped" and row["question"] == ""
    assert "not called by its name" in row["reason"]
    assert _rows(root, "SELECT * FROM telegram_send_messages") == []


def test_the_name_alone_answers_with_the_help_as_a_bare_mention_does(tmp_path):
    root = _root(tmp_path, require_mention=True, assistant_name=OTHER)
    _message(root, 1, OPERATOR, OTHER)
    _message(root, 2, OPERATOR, f"@{BOT}")
    _run(root)
    texts = [row["message_text"] for row in _rows(root, "SELECT message_text FROM telegram_send_messages")]
    help_line = f"@mention the bot with a question, or start the message with {OTHER}"
    assert len(texts) == 2 and all(help_line in text for text in texts)


def test_the_bots_mention_and_a_reply_to_it_still_address_it(tmp_path):
    """Being called by name is one more way in; it takes none away."""
    root = _root(tmp_path, require_mention=True, assistant_name=OTHER)
    _message(root, 1, OPERATOR, f"@{BOT} backup hôm nay ổn không?")
    assert _run(root)["answered"] == 1
    assert _rows(root, "SELECT question FROM ai_requests")[0]["question"] == "backup hôm nay ổn không?"


def test_called_by_name_is_a_rule_of_its_own():
    assert A.called_by_name("Ada: status?", "Ada") == (True, "status?")
    assert A.called_by_name("status, ada?", "Ada") == (False, "status, ada?")
    assert A.called_by_name("anything", "") == (False, "anything"), "no name, nobody is called"
    assert A.called_by_name("A.I. what now", "A.I.") == (True, "what now"), "a name is text, not a pattern"
