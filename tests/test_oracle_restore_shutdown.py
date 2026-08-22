"""How the Oracle drill takes its target down before RMAN DUPLICATE rebuilds it.

`SHUTDOWN IMMEDIATE` is the polite verb and the wrong one here. It waits for the pluggable databases
to close, and on 2026-08-05 that wait never ended: the alert log reached
`alter pluggable database all close immediate` and went silent, the drill's `sqlplus` sat at 0
seconds of CPU for half an hour, and the run only moved again when an operator issued
`shutdown abort` by hand from a second session.

Nothing in the system would have rescued it. The `|| true` on that line handles a shutdown that
*fails*, not one that *hangs*. The entry's `time_window.timeout` marks the `job_runs` row as
TIMEOUT but does not kill the shell running the script. So on the entry's own 02:00 schedule this
is a drill that hangs until morning, every time the target is in that state.

The instance is about to be overwritten by `DUPLICATE ... BACKUP LOCATION` — a restore target is
precisely the one instance whose clean shutdown buys nothing. So the drill uses ABORT, which always
terminates.
"""

from __future__ import annotations

from pathlib import Path
from db_ops.lib.paths import resolve_tool_path

SCRIPT = resolve_tool_path("assets/restore/oracle/oracle_rman_restore.sh")


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_the_target_is_aborted_rather_than_shut_down_gracefully():
    text = _script_text()

    assert 'sql_target "shutdown abort;"' in text


def test_no_unbounded_shutdown_survives_anywhere_in_the_drill():
    """The regression guard: one reintroduced IMMEDIATE puts the nightly drill back to hanging."""
    text = _script_text()

    offenders = [line.strip() for line in text.splitlines()
                 if "shutdown immediate" in line.lower() and not line.strip().startswith("#")]

    assert offenders == [], f"a shutdown that can hang is back: {offenders}"


def test_the_abort_still_precedes_startup_nomount():
    """DUPLICATE builds the controlfile, so it needs the instance in NOMOUNT - the abort is only
    correct as the step before that, not as a way of leaving the target down."""
    text = _script_text()

    assert text.index('sql_target "shutdown abort;"') < text.index('startup nomount;')
