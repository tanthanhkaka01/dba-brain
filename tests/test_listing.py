"""The shared rule behind every /spbot_list_* reply."""

from pathlib import Path

from db_ops.lib.listing import active_only, hidden_note, is_active
from conftest import shipped_config


def test_an_entry_with_no_active_field_counts_as_on():
    """Every config in this project treats a missing `active` as on. Reading it as off would
    hide entries the scheduler runs happily - the listing would contradict the scheduler."""
    assert is_active({"name": "x"}) is True
    assert is_active({"name": "x", "active": False}) is False


def test_active_only_reports_what_it_dropped():
    """The count is returned with the list, not left to the caller to compute, so a listing
    cannot show the filtered result while forgetting to say anything was filtered."""
    kept, hidden = active_only([{"active": True}, {"active": False}, {"active": False}])

    assert len(kept) == 1
    assert hidden == 2


def test_active_only_reads_objects_as_well_as_dicts():
    class Job:
        def __init__(self, active): self.active = active

    kept, hidden = active_only([Job(True), Job(False)])

    assert len(kept) == 1 and hidden == 1


def test_a_listing_that_hid_nothing_adds_no_footnote():
    assert hidden_note(0) == ""


def test_the_footnote_says_how_to_bring_an_entry_back():
    """Someone reading "3 hidden" needs to know the fix is a config flag, not a lost file."""
    note = hidden_note(3, noun="backup")

    assert "3 inactive backups hidden" in note
    assert "active:true" in note


def test_the_footnote_is_ascii_so_it_survives_a_windows_console():
    """This line is printed to a cp1252 console as well as sent to Telegram; a listing must
    not die on its own footnote."""
    hidden_note(2, noun="target").encode("cp1252")


def test_one_hidden_entry_is_not_pluralised():
    assert "1 inactive target hidden" in hidden_note(1, noun="target")


def test_every_telegram_command_is_documented():
    """data/telegram_support_commands.md is what an operator reads and what gets pasted into
    BotFather. A command added to the JSON and not to the doc is invisible: it never reaches the
    bot menu, and nobody knows to type it. This drifted twice before it was noticed.

    The reverse direction — documented but not configured — only holds against a *complete*
    command set. `telegram_support_commands.example.json` is a five-command sample of a file that
    documents twenty-nine, and being a sample is what an example is for; demanding the two match
    would either break every clone or force the example to stop being one.
    """
    import json
    import re

    source = shipped_config("telegram_support_commands.json")
    raw = json.loads(source.read_text(encoding="utf-8"))
    configured = {c["command_text"] for c in raw["telegram_support_commands"]}
    documented = set(re.findall(r"spbot_[a-z0-9_]+", (
        Path("data") / "telegram_support_commands.md").read_text(encoding="utf-8")))

    assert not (configured - documented), (
        f"configured but missing from the .md: {sorted(configured - documented)}"
    )
    if ".example." in source.name:
        return
    assert not (documented - configured), (
        f"in the .md but no longer in the JSON: {sorted(documented - configured)}"
    )


def test_the_botfather_block_lists_every_command():
    """The fenced block at the top is pasted verbatim into /setcommands - a command missing
    there never appears in the bot's menu even when the rest of the doc describes it."""
    import json
    import re

    raw = json.loads(open(shipped_config("telegram_support_commands.json"), encoding="utf-8").read())
    configured = {c["command_text"] for c in raw["telegram_support_commands"]}
    text = open("data/telegram_support_commands.md", encoding="utf-8").read()
    block = text.split("```")[1]
    in_block = {line.split(" - ")[0].strip() for line in block.splitlines() if " - " in line}

    assert not (configured - in_block), f"thieu trong block BotFather: {sorted(configured - in_block)}"
