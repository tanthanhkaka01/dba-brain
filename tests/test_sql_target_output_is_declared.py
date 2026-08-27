"""What a SQL task does with its result set is written down, not inferred.

`output` decides whether a task sends an `.xlsx`, a `.txt`, the rows pasted into the run message,
or nothing at all — and until 2026-08-27 an absent block silently meant `plain`. Thirteen of this
estate's seventeen targets had no block, so reading `sql_targets.json` could not answer "what does
`/spbot_run_sql_task 22` send back?"; the answer lived in a default inside the loader.

The old docstring already had the right rule and applied it to one value only: *"`none` is a
choice the operator makes, never one inferred from silence."* Every format is a choice. These
tests hold both halves of making that true — the shipped configuration declares one everywhere,
and the loader refuses an entry that does not.
"""

from __future__ import annotations

import json

import pytest

from conftest import shipped_data_dir
from db_ops.lib.task_output import FILE_OUTPUT_FORMATS, OUTPUT_FORMATS
from db_ops.sql_tasks.runner import _target_notify, _target_output

DATA_DIR = shipped_data_dir()


def _targets() -> list[dict]:
    path = DATA_DIR / "sql_targets.json"
    if not path.is_file():
        return []
    return json.loads(path.read_bytes().decode("utf-8-sig")).get("sql_targets") or []


# -- the configuration says so ----------------------------------------------------------------- #

def test_every_target_declares_what_it_does_with_its_result() -> None:
    undeclared = [f"sql_id={t.get('sql_id')} target_no={t.get('target_no')}"
                  for t in _targets() if not isinstance(t.get("output"), dict)]

    assert not undeclared, (
        f"sql_targets entries with no 'output' block: {undeclared}. A task's delivery is a "
        "decision; one that is not written down is one nobody can read back.")


def test_every_target_declares_where_its_messages_go() -> None:
    undeclared = [f"sql_id={t.get('sql_id')} target_no={t.get('target_no')}"
                  for t in _targets() if not isinstance(t.get("notify"), dict)]

    assert not undeclared, f"sql_targets entries with no 'notify' block: {undeclared}"


def test_every_declared_format_is_one_the_runner_knows() -> None:
    wrong = [(t.get("sql_id"), (t.get("output") or {}).get("format"))
             for t in _targets()
             if isinstance(t.get("output"), dict)
             and str((t.get("output") or {}).get("format") or "").lower() not in OUTPUT_FORMATS]

    assert not wrong, f"unknown output.format: {wrong}; must be one of {OUTPUT_FORMATS}"


def test_a_target_that_sends_a_file_says_which_chat() -> None:
    """A file has to go somewhere. `plain` and `none` may leave the chat to the notify rules."""
    homeless = [t.get("sql_id") for t in _targets()
                if isinstance(t.get("output"), dict)
                and str(t["output"].get("format") or "").lower() in FILE_OUTPUT_FORMATS
                and not (str(t["output"].get("telegram_chat") or "").strip()
                         or str(t["output"].get("chat_id") or "").strip())]

    assert not homeless, f"file output with neither telegram_chat nor chat_id: {homeless}"


# -- and the loader will not accept anything else ------------------------------------------------ #

def test_a_target_with_no_output_is_refused_by_name() -> None:
    """Refused, not defaulted — and the message says what to add.

    The thirteen targets that relied on the old default now say `plain` in the file, which is
    exactly what they were already doing, so this changed no delivery. It only removed the
    silence.
    """
    with pytest.raises(RuntimeError, match="'output' is required"):
        _target_output({"sql_id": 99, "target_no": 1})


def test_a_target_with_no_notify_is_refused_by_name() -> None:
    with pytest.raises(RuntimeError, match="'notify' is required"):
        _target_notify({"sql_id": 99, "target_no": 1})


def test_a_declared_block_still_parses() -> None:
    parsed = _target_output({
        "sql_id": 24, "target_no": 1,
        "output": {"format": "txt", "telegram_chat": "sql", "chat_id": ""},
    })

    assert parsed["output_format"] == "txt"
    assert parsed["output_chat"] == "sql"


def test_an_empty_format_still_means_none() -> None:
    """A block that is *present* and says nothing is a deliberate "send status only".

    That is the one inference the old docstring defended, and it survives: the operator wrote the
    block, so the silence inside it is theirs rather than the file's.
    """
    assert _target_output({"sql_id": 1, "target_no": 1, "output": {}})["output_format"] == "none"


def test_an_unknown_format_is_refused() -> None:
    with pytest.raises(RuntimeError, match="output.format must be one of"):
        _target_output({"sql_id": 1, "target_no": 1, "output": {"format": "pdf"}})


# -- a statement the driver cannot send ---------------------------------------------------- #

def test_a_lone_surrogate_is_refused_with_the_string_that_carried_it() -> None:
    """`'utf-16-le' codec can't encode character ... in position 350` names only a codec.

    That is what /spbot_run_sql_task 18 reported twice on 2026-08-27, and it is not enough to act
    on: it does not say which string, which file, or what was around it. The script named in the
    run has its two non-ASCII characters at 286 and 346 and contains no 0x97 byte at all, so the
    statement that reached the driver was not simply that file's text — and nothing in the error
    said so.

    The guard reports the position, the byte the surrogate is escaping, what that byte is in the
    Windows ANSI code page, and the characters either side. The next occurrence identifies the
    string instead of restarting the search.
    """
    from db_ops.sql_tasks.runner import check_sql_text_is_encodable

    with pytest.raises(RuntimeError) as caught:
        check_sql_text_is_encodable(
            "SELECT 1 -- padding to give it somewhere to sit " + chr(0xDC97) + " and after",
            source="SQLSERVER-018 target=1")

    message = str(caught.value)
    assert "U+DC97" in message
    assert "0x97" in message
    assert "context:" in message
    assert "SQLSERVER-018 target=1" in message


def test_text_that_merely_contains_an_em_dash_is_fine() -> None:
    """The character is not the problem; a *half* of a surrogate pair is.

    Worth pinning, because the obvious over-correction is to ban non-ASCII from SQL scripts, and
    the two em dashes in the script that failed are ordinary comment punctuation in a valid
    UTF-8 file.
    """
    from db_ops.sql_tasks.runner import check_sql_text_is_encodable

    check_sql_text_is_encodable("SELECT 1 -- happens — the CLI parses it", source="X")


def test_the_context_survives_a_second_bad_character() -> None:
    """The report must be printable even when the window holds more of what broke it.

    An error message that cannot itself be encoded is how this defect wasted an afternoon.
    """
    from db_ops.sql_tasks.runner import check_sql_text_is_encodable

    with pytest.raises(RuntimeError) as caught:
        check_sql_text_is_encodable(f"A{chr(0xDC97)}B{chr(0xDC80)}C", source="X")

    str(caught.value).encode("utf-8")
