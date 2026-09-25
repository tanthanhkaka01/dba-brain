"""A night that produces no backup is worse than a night that produces the wrong level.

On five consecutive nights from 2026-09-19 the PostgreSQL job failed with *"WAL summaries are
required on timeline 1 from 21/2E000028 … but the summaries for that timeline and LSN range are
incomplete"*, and left ``base/`` without a single new directory. The baseline it was chaining onto
had been taken four days before ``summarize_wal`` was turned on, so no retry could ever have
worked — the summaries for that LSN range were never written and the WAL is long recycled.

The script already knew that an incremental with **nothing** to chain onto should take a baseline
instead. It did not know that a baseline which exists but cannot be used is the same situation.

These are assertions about the script's text rather than its behaviour, because the behaviour needs
a PostgreSQL server, a container and a WAL history to reproduce. What they pin is the *structure*
that carries the decision: which failure is recovered from, which is still fatal, and the one thing
that must never happen — a directory the script makes itself so the run looks successful.
"""

from __future__ import annotations

import pytest

from db_ops.lib.paths import resolve_tool_path

SCRIPT = "assets/backup/postgresql/pg_basebackup_database.sh"


@pytest.fixture(scope="module")
def script() -> str:
    return resolve_tool_path(SCRIPT).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def code(script: str) -> str:
    """The script with its comments removed, so a rule cannot be satisfied by a comment quoting it."""
    return "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))


def test_an_incremental_the_server_refuses_for_want_of_summaries_takes_a_baseline(code: str):
    """The recovery, and it is narrow: only that refusal, and only from an incremental."""
    assert 'INCR:*"WAL summaries"*' in code
    assert "take_backup FULL" in code


def test_any_other_failure_is_still_fatal(code: str):
    """Answering a full disk by writing a 21 GB baseline is not a recovery. Everything that is not
    the summaries refusal still ends the run with RESULT=error."""
    assert 'die "pg_basebackup failed (level=${level}); partial target removed."' in code
    # And a baseline that itself fails is fatal too, rather than looping.
    assert "level=FULL, taken after the incremental was refused" in code


def test_it_asks_before_it_tries_when_summaries_are_switched_off_entirely(code: str):
    """`summarize_wal = off` makes an incremental certain to be refused. Asking first turns a
    guaranteed failure — and the checkpoint it forces, and the warning it leaves in the server log
    — into a baseline taken on purpose."""
    assert "show summarize_wal" in code
    assert '[ "$summarize" != "on" ]' in code


def test_the_first_ever_run_still_takes_a_baseline(code: str):
    """The rule that was already there and still has to be: an incremental with nothing to chain
    onto is the first run, or the first after a retention sweep, not an error."""
    assert '[ "$level" = "INCR" ] && [ -z "$latest" ]' in code


def test_the_script_never_creates_the_backup_directory_itself(code: str):
    """The one thing that must not happen. `pg_basebackup` creates its target and removes it when
    the server refuses; a directory the script made to fill the gap would be a backup that does not
    restore, which is worse than an error. Only parents are ever created here: `base/`, and since
    2026-09-24 the backup folder itself when another user made it and it has to be taken over (as
    root, then handed to the engine). This asserted exactly one `mkdir` until that change added two,
    and the count, not the rule, is what went stale."""
    made = [line for line in code.splitlines() if "mkdir" in line]

    assert made, "the script must create base/ itself"
    for line in made:
        assert "${base_dir}" in line or "${backup_dir}" in line, line
        assert "$target" not in line and "${target}" not in line, line


def test_the_failed_target_is_removed_on_every_path_that_fails(code: str):
    """A partial directory left behind becomes `latest`, and the next night would chain onto it.

    Two calls can fail — the scheduled level, and the baseline taken after a refusal — so there are
    two removals. If a third call is ever added, this count is what says it needs its own.
    """
    assert code.count("if ! take_backup") == 2
    assert code.count("rm -rf '${target}'") == 2


def test_pg_basebackup_output_is_printed_as_well_as_classified(code: str):
    """db_ops stores this as `stdout_tail`, and the server's own message is the only thing that
    says why a backup did not happen. Capturing it to classify it must not swallow it."""
    assert 'bb_output="$(run_db' in code
    assert "printf '%s\\n' \"$bb_output\"" in code


# --------------------------------------------------------------------------- #
# The second way a parent can be unusable: it belongs to a different cluster
# --------------------------------------------------------------------------- #
def test_the_parents_cluster_is_checked_before_the_incremental_is_asked_for(code: str):
    """`latest` is only the newest directory in `base/` — nothing said it was the same database.

    A cluster that has been restored from elsewhere, re-initdb'd or replaced carries a new system
    identifier, and the server refuses a manifest from the old one. That refusal is permanent in the
    strongest sense: no retry, and no amount of waiting, changes which cluster the parent came from.
    Before this check the refusal reached the generic failure path and the job answered "error"
    every night with `base/` untouched — the five-night shape this module is named after, from a
    different cause.
    """
    assert "parent_sysid=" in code
    assert "System-Identifier" in code
    assert "pg_control_system()" in code
    # Before the attempt, not after it: the point is to avoid the forced checkpoint and the
    # "aborting backup" line that a refused pg_basebackup leaves in the server log.
    assert code.index("parent_sysid=") < code.index("take_backup() {")


def test_a_parent_from_another_cluster_takes_a_baseline_rather_than_failing(code: str):
    mismatch = [line for line in code.splitlines() if "the chain is dead" in line]

    assert len(mismatch) == 1, mismatch
    # All three numbers in the message. "the chain is dead" with no identifiers tells its reader
    # nothing they can check against pg_controldata.
    assert mismatch[0].count("%s") == 3


def test_a_manifest_too_old_to_carry_an_identifier_also_takes_a_baseline(code: str):
    """PostgreSQL only writes System-Identifier into the manifest from 17, the release that added
    --incremental. A parent without one predates it, so it cannot be chained to either — and an
    absent identifier must not be read as a matching one."""
    assert "carries no System-Identifier" in code
    assert 'if [ -z "$parent_sysid" ]; then' in code


def test_an_unreadable_live_identifier_does_not_force_a_baseline(code: str):
    """The asymmetry is deliberate. A missing identifier on the PARENT is proof the chain is
    unusable; a missing one from the live cluster is only a failed query, and treating it as a
    mismatch would turn one flaky psql call into a full copy every night."""
    assert 'elif [ -n "$live_sysid" ] && [ "$parent_sysid" != "$live_sysid" ]; then' in code


def test_the_servers_own_refusal_is_still_recovered_from_as_a_second_line(code: str):
    """The pre-flight reads the parent's manifest; the server compares against pg_control. If they
    ever disagree — a manifest edited, a parent replaced between the check and the attempt — the
    refusal must still land a baseline rather than a night with no backup."""
    assert 'INCR:*"WAL summaries"*|INCR:*"system identifier"*)' in code
