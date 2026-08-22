"""The stored `message_type`: what a producer declares, and what the send layer does with it.

The emoji used to be guessed from the message header. That works for a well-formed header and
fails silently everywhere else — most visibly on a report's `[part 2/2]` chunks, which start
mid-body with no header at all and so went out untagged while `[part 1/2]` carried ⚠️.

The producers always knew. `backup_restore` holds the phase and the level when it queues the
row; a report knows its level for every chunk. So the type is stored, and the header heuristic
stays only as the fallback for rows that declare nothing.
"""

import pytest

from db_ops.db.telegram_queue import message_type_for, queue_telegram_message
from db_ops.telegram.severity import MESSAGE_TYPES, decorate_message


class FakeStore:
    """Captures what a producer would have written to telegram_send_messages."""

    def __init__(self):
        self.rows = []

    def insert_telegram_send_message(self, **kwargs):
        self.rows.append(kwargs)
        return len(self.rows)


# ---------------------------------------------------------------------------
# Deriving the type from what a producer already has
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "kwargs,expected",
    [
        # Phase separates the two events that level cannot tell apart.
        ({"phase": "START", "level": "logging"}, "started"),
        ({"phase": "END", "level": "logging"}, "success"),
        ({"phase": "ERROR", "level": "error"}, "failed"),
        ({"phase": "RUNNING", "level": "logging"}, "running"),
        # Level alone, for producers with no phase.
        ({"level": "warning"}, "warning"),
        ({"level": "critical"}, "critical"),
        # `logging` is a declaration ("routine"), not an absence — see the dedicated test below.
        ({"level": "logging"}, "plain"),
        # A task's own status.
        ({"status": "done"}, "success"),
        ({"status": "error"}, "failed"),
        # Nothing supplied stays unset, so the header still decides.
        ({}, ""),
    ],
)
def test_type_is_derived_from_phase_level_or_status(kwargs, expected):
    assert message_type_for(**kwargs) == expected


def test_a_loud_level_is_not_overridden_by_an_optimistic_phase():
    """`phase=END, level=error` is a run that finished by failing.

    Reporting that with ✅ is worse than reporting nothing: the operator reads the symbol, not
    the sentence, and a green tick on a failed restore is how a broken run gets closed.
    """
    assert message_type_for(phase="END", level="error") == "failed"
    assert message_type_for(phase="END", level="critical") == "critical"


# ---------------------------------------------------------------------------
# The declared type wins; the header is only the fallback
# ---------------------------------------------------------------------------
def test_declared_type_beats_the_header():
    text = "LOGGING|host01|Restore workflow started."

    assert decorate_message(text).startswith("▶️")                    # header says started
    assert decorate_message(text, "failed").startswith("❌")          # producer overrules it


def test_a_chunk_with_no_header_still_gets_its_report_level():
    """The case the heuristic could never serve: a continuation chunk has nothing to read."""
    chunk = "[part 2/2]\n- LOCK_TRANSACTION_HOLDERS ... session_status=running, command=SELECT"

    assert decorate_message(chunk) == chunk                            # heuristic: nothing
    assert decorate_message(chunk, "warning").startswith("⚠️ ")        # declared: tagged


def test_plain_suppresses_the_guess_rather_than_merely_adding_nothing():
    """`plain` is a statement, not an absence — that is the whole reason it exists.

    A listing whose body mentions "error" would be tagged ❌ by a header rule that was never
    meant to judge it. NULL keeps guessing; `plain` stops.
    """
    listing = "SQL result for ACME-192-0-2-250: error_count column shown below"

    assert decorate_message(listing, "plain") == listing
    assert decorate_message(listing, None) == decorate_message(listing)  # NULL still guesses


def test_an_unknown_type_degrades_to_the_header_instead_of_failing():
    """A typo must not stop delivery: the wrong tag is cosmetic, a swallowed alert is an outage."""
    text = "ERROR|host01|Restore workflow FAILED."

    assert decorate_message(text, "definitely-not-a-type").startswith("❌")


# ---------------------------------------------------------------------------
# The shared entry point is what stores it
# ---------------------------------------------------------------------------
def test_queue_helper_stores_the_derived_type():
    store = FakeStore()

    queue_telegram_message(
        store=store, chat_id="-100", text="backup END: done", phase="END", level="logging",
    )

    assert store.rows[0]["message_type"] == "success"


def test_queue_helper_stores_null_when_nothing_is_known():
    """An unset type is a real state: it means "read the header", not "plain"."""
    store = FakeStore()

    queue_telegram_message(store=store, chat_id="-100", text="hello")

    assert store.rows[0]["message_type"] is None


def test_every_declared_type_maps_to_a_known_value():
    for message_type in MESSAGE_TYPES:
        # None of them may raise, and none may leak an emoji for `plain`.
        result = decorate_message("Some message body", message_type)
        assert result.endswith("Some message body")


# ---------------------------------------------------------------------------
# Vocabularies the producers actually emit
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "kwargs,expected",
    [
        # The SLA app concludes in its own words. PASSED/OK used to fall through to NULL, so a
        # compliance run that passed was indistinguishable from one nobody typed.
        ({"level": "sla", "status": "PASSED"}, "success"),
        ({"level": "sla", "status": "OK"}, "success"),
        ({"level": "sla", "status": "FAILED"}, "failed"),
        ({"level": "sla", "status": "AT_RISK"}, "warning"),
        # No data is not a pass: the SLI could not be computed, and reading that as "fine" is
        # how the one check that stopped working goes unnoticed.
        ({"level": "sla", "status": "NO_DATA"}, "warning"),
    ],
)
def test_the_sla_vocabulary_is_mapped(kwargs, expected):
    assert message_type_for(**kwargs) == expected


def test_logging_level_is_plain_not_unset():
    """`logging` is a producer saying "this is routine" — that is information, not silence.

    Left unset it meant "nobody knows", which sent the send layer back to guessing from the
    header. Every routine report was landing as NULL for exactly this reason.
    """
    assert message_type_for(level="logging") == "plain"
    assert message_type_for(level="info") == "plain"
    # ...but a phase still wins: a logging-level START is a start, not a plain message.
    assert message_type_for(level="logging", phase="START") == "started"
    assert message_type_for(level="logging", phase="END") == "success"


def test_notify_level_words_are_accepted_as_declared_types():
    """`error`/`warn`/`logging` are what everyone reaches for, because that is the vocabulary
    the rest of the config uses. Accepting them beats silently dropping a reasonable guess."""
    from db_ops.telegram.severity import normalize_message_type

    assert normalize_message_type("error") == "failed"
    assert normalize_message_type("warn") == "warning"
    assert normalize_message_type("logging") == "plain"
    assert normalize_message_type("FAILED") == "failed"
    assert normalize_message_type("nonsense") == ""
