"""`AGENTS.md` is the only documentation a fresh install has. It has to be true.

After `pip install dbabrain` and `db-ops init`, the tool root contains configuration, empty
directories, and one document. There is no `docs/` in the wheel and no README in the root, so this
file *is* the interface for whoever — or whatever — has to work out how to use the toolkit.

Measured on 2026-09-08, at v0.11.0, four things in it were wrong:

* it ended by saying backup/restore validation, SLA checks, scheduled SQL, reports, provisioning
  and the web console were "not in this release" — all six were in `db-ops --help`;
* it used two different names for one command — `encrypt-secret` in the main flow and
  `encrypt-secret-text` in the Telegram section. Both work (the second is a dispatched alias), so
  this one was a readability defect rather than a dead end, which is worth stating precisely: the
  first reading of it was that the alias did not exist, and that was wrong;
* it never mentioned `timezone`, required since 0.10.0, whose absence silently moves every
  hour-bounded schedule to UTC;
* it never mentioned the daemon or `DB_OPS_NODE_ROLE=worker` — so a reader who followed it exactly
  got one manual collection and no running estate.

None of that fails a test, breaks a build, or shows up in a log. Documentation that is merely
stale is invisible until somebody follows it, and by then they have concluded the product does
less than it does. So the claims that can be checked against the code are checked here.
"""

import re
from pathlib import Path

import pytest

from db_ops import scaffold


@pytest.fixture(scope="module")
def guide(tmp_path_factory) -> str:
    """The guide as a real `init` writes it, not the constant it is built from."""
    root = tmp_path_factory.mktemp("tool_root")
    scaffold.initialise(root, app_name="dbabrain")
    return (root / "AGENTS.md").read_text(encoding="utf-8")


def test_every_command_the_guide_tells_you_to_run_exists(guide: str) -> None:
    """A command name in the only shipped document has to be one the toolkit answers to.

    The registry is read rather than listed here. A hand-kept list in a test is the same failure
    the test exists to catch, one level up — and the first version of this test did exactly that,
    guessed two attribute names that do not exist, and reported four real commands as missing.
    """
    from db_ops import cli as top_level

    # `db_ops.cli.APPS` is the app dispatch table; the top-level verbs are the ones the dispatcher
    # matches on argv[0] directly, read from the source so an added command cannot be forgotten.
    source = Path(top_level.__file__).read_text(encoding="utf-8")
    top_level_names = set(re.findall(r'argv\[0\] == "([a-z][a-z0-9-]+)"', source))
    for group in re.findall(r'argv\[0\] in \{([^}]*)\}', source):
        for name in group.split(","):
            cleaned = name.strip().strip('"').strip("'")
            if cleaned:
                top_level_names.add(cleaned)

    known = set(top_level.APPS) | top_level_names
    assert known, "cannot enumerate commands from db_ops.cli — the dispatcher shape changed"

    documented = set(re.findall(r"db-ops ([a-z][a-z0-9-]+)", guide))
    unknown = sorted(name for name in documented if name not in known)
    assert not unknown, (
        f"AGENTS.md tells the reader to run {unknown}, which db-ops does not have. It is the only "
        f"documentation a fresh install ships, so a wrong command name here is a dead end.")


def test_the_guide_does_not_claim_shipped_capabilities_are_missing(guide: str) -> None:
    """It used to end with a list of things that were "not in this release" and were.

    Left alone, that sentence tells every new reader the product is a metrics collector. It is the
    most expensive kind of stale: not wrong about a detail, wrong about the scope.
    """
    from db_ops import cli as top_level

    # Read from the dispatch table, so this cannot go on passing after an app is renamed.
    available = set(top_level.APPS)
    present = sorted(available & {"backup-restore", "sla", "sql-tasks", "reports", "webhost", "sre"})
    assert present, "expected these apps to be registered; the dispatch table changed"

    # Whitespace is collapsed first, and that is the whole trick. The guide is hand-wrapped at 96
    # columns, so the sentence being looked for is split by a newline in the file — the first
    # version of this test matched on the contiguous phrase, passed against the exact text it was
    # written to catch, and would have gone on passing forever.
    flat = " ".join(guide.lower().split())
    for phrase in ("are **not** in this release", "not in this release; they arrive later"):
        assert phrase not in flat, (
            f"AGENTS.md still says capabilities are missing, but {present} are in db-ops --help.")


def test_the_guide_names_the_setting_that_silently_changes_every_schedule(guide: str) -> None:
    """`timezone` is required, defaults to UTC when absent, and does not fail loudly.

    A reader who never learns it exists gets schedules on a clock they did not choose, and nothing
    in the output says so. That is precisely the kind of thing a first-run guide is for.
    """
    assert "timezone" in guide, "AGENTS.md never mentions the timezone field"
    assert "DB_OPS_NODE_ROLE" in guide or "node_role" in guide, (
        "AGENTS.md never mentions the node role, which decides whether the daemon runs anything")


def test_the_guide_gets_the_reader_to_a_running_estate_not_one_manual_command(guide: str) -> None:
    """Collecting once by hand is not the product. The daemon is."""
    assert "db-ops daemon" in guide, (
        "AGENTS.md never shows how to run the scheduler, so a reader who follows it exactly ends "
        "with one collection and nothing scheduled.")


def test_the_guide_says_where_the_rest_of_the_documentation_is(guide: str) -> None:
    """The wheel ships no `docs/`. If this file does not point somewhere, the reader has reached
    the end of everything the install can tell them."""
    assert "github.com/tanthanhkaka01/dba-brain" in guide, (
        "AGENTS.md does not link to the full documentation, and the package ships none.")

# --------------------------------------------------------------------------------------------- #
# What a reader meets before there is anything to read
# --------------------------------------------------------------------------------------------- #

def test_the_package_name_is_a_command(monkeypatch) -> None:
    """You install `dbabrain`; typing `dbabrain` must do something.

    Measured on a clean install: the only console script was `db-ops`, so the first thing a reader
    typed after `pip install dbabrain` answered "command not found" - in a directory holding
    `.venv` and nothing else, with no README to correct them.
    """
    import tomllib

    pyproject = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml")
                              .read_text(encoding="utf-8"))
    scripts = pyproject["project"]["scripts"]

    assert "dbabrain" in scripts, "the distribution is named dbabrain but installs no such command"
    assert "db-ops" in scripts, "db-ops must stay: it is in every runbook and dated record"
    assert scripts["dbabrain"] == scripts["db-ops"], "both names must reach the same dispatcher"


def test_with_no_tool_root_the_banner_says_what_to_type(tmp_path, monkeypatch, capsys) -> None:
    """Twelve app names answer "what can this do". A new reader is asking "what now".

    None of those apps can run before a tool root exists, so leading with them is twelve dead ends.
    """
    from db_ops import cli

    monkeypatch.chdir(tmp_path)
    assert cli.main([]) == 0
    out = capsys.readouterr().out

    assert "no tool root in this directory" in out
    assert "dbabrain init" in out
    assert "dbabrain guide" in out
    assert "github.com/tanthanhkaka01/dba-brain" in out
    # The app list is the wrong answer here and must not be what leads.
    assert "usage: db-ops <app>" not in out


def test_inside_a_tool_root_the_full_listing_comes_back(tmp_path, monkeypatch, capsys) -> None:
    """The first-run banner is for the first run only. Somebody standing in a configured root is
    asking the other question, and must not be told there is nothing here."""
    from db_ops import cli, scaffold

    scaffold.initialise(tmp_path, app_name="dbabrain")
    monkeypatch.chdir(tmp_path)
    assert cli.main([]) == 0
    out = capsys.readouterr().out

    assert "usage: db-ops <app>" in out
    assert "no tool root in this directory" not in out


def test_the_guide_is_readable_before_anything_is_created(tmp_path, monkeypatch, capsys) -> None:
    """`guide` exists because until `init` runs there is no file to read, and deciding whether to
    run `init` is exactly when a reader wants to know what they are getting."""
    from db_ops import cli

    monkeypatch.chdir(tmp_path)
    assert cli.main(["guide"]) == 0
    out = capsys.readouterr().out

    assert "Running this toolkit for the first time" in out
    assert list(tmp_path.iterdir()) == [], "guide must write nothing"
