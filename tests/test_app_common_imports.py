"""An app does not import ``common`` — it calls the ``common`` CLI.

The rule, set on 2026-08-15: ``common`` is the API layer. An app hands it a JSON object, it does
the work, it hands back a JSON object. Importing it instead means the app is reaching *through*
the API into its internals, and then the CLI is not the contract — it is one of two contracts, and
the second one is invisible to everything that checks the first.

Three exemptions, all deliberate, all narrow, and each with its reason attached:

* **``control``** is the deploy tool. It builds the image and the bundle, so it necessarily
  touches every part of the tree at once. Holding it to this rule would mean deploying through a
  CLI it is in the middle of replacing.
* **``common.data_sources``** is the one reader of the ``data/`` folder. Routing it through a
  subprocess would mean a process per config read, and the reason it is *shared* rather than
  copied into each app is exactly that every app asks it the same questions.
* **``metrics``, for the four modules it executes through** — see :data:`EXEMPT_APPS`. That one is
  a measurement, not a judgement, and the measurement is written down there.

Anything that is a **value** rather than an operation is not in ``common`` at all any more — it is
in ``db_ops/lib/`` (pure helpers, imported freely) or ``db_ops/db/`` (row shapes, next to the
store that writes them). That split is what makes this rule achievable: a class cannot come back
from a subprocess, so the modules holding classes had to stop being ``common``'s problem before
``common`` could be import-free.

**``REMAINING`` is a migration baseline, not an allowlist.** Every entry is an app still importing
``common`` for something the CLI should carry. The test fails if the set grows *or* if an entry
becomes stale, so finishing a module means deleting its line here in the same commit and the file
shrinks to nothing. An allowlist that only ever gets appended to is how a rule becomes decoration.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


DB_OPS_ROOT = Path(__file__).resolve().parents[1] / "db_ops"

#: Apps the rule applies to. `control` is exempt — see the module docstring.
APPS = frozenset({
    "jobs", "metrics", "sql_tasks", "reports", "telegram",
    "backup_restore", "sla", "sre", "webhost",
})

#: `common` submodules an app may still import, exempt from the rule.
EXEMPT_MODULES = frozenset({"data_sources"})

#: One app, four modules, and a measurement — not a judgement.
#:
#: `metrics` executes SQL and shell against the whole estate on a ~115-second cycle. Measured on
#: the worker on 2026-08-15 over the last 200 collect passes (`metric_results`, grouped by
#: `collector_type`): **388 executions per pass on average, 1,365 at the peak** — 368 of them SQL
#: (peak 1,177), 24 cmd (peak 209), 13 docker. A whole pass finishes in **19-24 seconds** today.
#:
#: A `common` CLI process costs **117 ms** for `run-sql` and **75 ms** for `run-cmd`, measured in
#: the worker container (bare interpreter 15 ms; the dispatcher import is the rest). One process
#: per execution is therefore **43 s** of interpreter startup on an average pass and **138 s** on
#: the worst one — longer than the interval between passes, so passes would overlap. That is not
#: a slower design, it is a broken one, and no amount of tidying makes it otherwise.
#:
#: The alternative that does work is a **batch command** — one process per *target* per pass,
#: about 20, or 2.3 s — and it is the right API besides. It is not done here because it moves the
#: per-metric timeout, the per-metric error classification and the legacy-Oracle bridge path
#: across a process boundary: a behaviour change to the estate's own monitoring, which wants its
#: guarding test written first. Recorded as work, not as an opinion.
#:
#: Until then `metrics` imports these four directly. **Nothing else about the rule is relaxed**:
#: every other `common` module is still refused to `metrics`, and every other app is still
#: refused these four.
EXEMPT_APPS: dict[str, frozenset[str]] = {
    "metrics": frozenset({"db_connect", "sql_execution", "oracle_bridge", "remote_exec"}),
}

#: The migration baseline: `common` submodule -> the apps still importing it. Measured
#: 2026-08-15 at 2.85.10, immediately after `lib` and the row shapes were split out: 74 import
#: statements across 26 modules. Each entry is work the `common` CLI already does or should do.
#:
#: Closed since: `result_format`, `xlsx_export`, `delimited_import` (with `xlsx_import`) — all
#: four turned out to import nothing but stdlib. They are format converters, not operations, so
#: they moved to `db_ops/lib/` rather than growing a CLI command apiece.
#:
#: Then `backup_policy` and `capacity_forecast`, which were one module doing two jobs: reading a
#: policy file and judging against it. The read went to `data_sources` (the one reader), the
#: judging to `lib` with the document as a required argument — no more default that silently
#: reaches for this repo's data folder.
#:
#: Then `metric_targets_config` and `target_resolve`: both answer "what is configured in the
#: data folder", which is `data_sources`' question, so they became submodules of it rather than
#: two more exemptions to argue separately.
#:
#: Then `report_archive`, split the same way as the two policy modules: `report_base_url` reads
#: reports_config.json and went to `data_sources`; stamping and daily copying take their paths
#: as arguments and went to `lib`.
#:
#: Then `inventory_render` — 545 lines of merging and rendering, of which exactly two functions
#: touch a file and both take the path as an argument. Apps only ever imported the pure half.
#: Down to 51 / 17.
#:
#: Then the first real CLI conversion, and the one the design was already shaped for:
#: `backup_restore` called `restorekey`, `restorestep`, `verifyrestore`, `backupfiles` and
#: `deletefiles` in-process, and every one of them already had a `common` CLI command with the
#: same name. `db_ops/lib/common_cli.py` is now that transport (stdin, no deadline, a failed
#: command raises), and `plan_retention` — arithmetic over the listing, not an operation — went
#: to `lib`.
#:
#: Then the SQL vocabulary: apps were importing `sql_execution`, `oracle_bridge`, `db_connect`
#: and `sql_run` for constants, `normalize_db_type`, the DECLARE prelude and the sqlplus DEFINE
#: rules — none of which connect to anything. They went to `lib/sql_access.py` (what a
#: `sql_access` block means) and `lib/sql_text.py` (SQL text and result limits); connecting and
#: executing stayed in `common` behind the CLI.
#:
#: Then `common/restore/` split along the line its own docstring already drew: `spec`, `pitr`
#: and `plan` — what a restore *is*, decided without touching anything — went to
#: `db_ops/lib/restore/`, because `spec_builder.py` builds and validates a spec long before
#: there is anything to restore to. The SQL Server runner stayed.
#:
#: Then `common.backup`: running a backup went to the `backup-database` CLI, and the app kept
#: the half that was always its own — resolving secrets and building the request
#: (`spec_builder.backup_request_from_job`). A failed backup is a *recorded outcome* here, not
#: an exception, so it goes through `common_cli.run_allowing_failure`; the engine level
#: vocabulary (`backup_level_for`) is needed while deciding what is due and went to `lib`.
#: Down to 36 / 10.
#:
#: Then `table_load`: the Telegram handler already built the exact JSON the
#: `create-table-from-xlsx` command takes, so the conversion was the call itself. The transport
#: was briefly copied into five app folders on the `queue_message.py` precedent and then
#: collapsed into one — `db_ops/lib/common_cli.py` imports nothing from `db_ops`, so nothing
#: forced the copies. Down to 35 / 9.
#:
#: Then `config_admin`, which was five statements doing three different things and needed all
#: three mechanisms at once: the output vocabulary went to `lib/task_output.py` (a value three
#: components share), `resolve_target_from_server_id` went to `data_sources` as
#: `resolve_sql_target_fields` (a read of the data folder, which has one reader), and the two
#: writes — `add_sql_task` and `set_metric_toggle` — became `common.cli add-sql` /
#: `metric-toggle` calls. Those two commands had to start answering in the response envelope
#: first: they printed a bare dict on success and an `ERROR:` line on **stderr** with exit 2 on
#: failure, so a caller with only stdout could not tell a rejected task from a crashed process.
#: `db_ops/sql_tasks/config_admin.py`, a re-export shim over the same engine, was deleted rather
#: than repointed. Down to 30 / 8.
#:
#: Then `sqlserver_instance`, which was already called across the CLI for the two operations that
#: matter — `backup_restore/instance_metadata.py` had been running `sqlserver-export-instance` and
#: `sqlserver-replay-instance` as subprocesses for a while. What was left importing was the
#: *bundle's shape*: the two phase names, the `server/` subdirectory, and ordering the artifact
#: list. Those are read while a config entry is being validated, before anything is connected to,
#: so they went to `lib/instance_bundle.py`; reading the policy file behind them went to
#: `data_sources`. Down to 26 / 7.
#:
#: Then `host_ops` and `sql_run` together, one call site each and neither the same kind of thing.
#: `host_ops` was `cmd_access` *resolution* — pure functions over a config block, read while
#: metrics loads its target list — so it became `lib/cmd_access.py`, the mirror of
#: `lib/sql_access.py`; `_as_bool`/`_as_int` went with it as `lib/coerce.py`. `sql_run` was an
#: unused import in `sql_tasks` and, in `telegram`, `/spbot_sql_to_xlsx` calling `run_sql` in
#: process: that became `common.cli run-sql` through `common_cli.run_ok`, the reader for the 29
#: commands that still answer `{"ok": …}` instead of the envelope. Down to 23 / 5.
#:
#: Then the resolve/builder half of `ssh` and `remote_exec`, which was most of them: of 13
#: statements only 5 were execution. `resolve_ssh_key` (the `data/ssh_keys/` path rule) and
#: `resolve_ssh_password` (a read of the encrypted store) went to `data_sources.ssh_auth`; the
#: four `Ssh*Error` names to `lib/ssh_errors.py`, because `sre/cli.py` was importing a transport
#: for one word; the `Invoke-Command` builders and PowerShell quoting to `lib/powershell.py`,
#: since building a script is not running one. What is left is genuinely opening a session —
#: `open_ssh_client`, `open_session`, `run_script` — and it cannot close until `backup_restore`
#: and `sre` stop assembling their own `ssh`/`sqlcmd`/`robocopy` calls, which is a separate piece
#: of work. Down to 12 / 5, and `metrics` came off every line (see :data:`EXEMPT_APPS`) — which
#: leaves the SQL trio as `sql_tasks` and `telegram` only, both of which run one query per task or
#: per command and cost 117 ms once.
#:
#: Then `telegram` off `db_connect` and `sql_execution`, which needed **`run-sql` to bind
#: parameters first** — it had `commit` and `autocommit` but no `params`, and these commands
#: write to production rows with values that arrive in a chat message. `params` (positional,
#: bound, an object refused) and `prelude` (SQL prepended to every batch, because a T-SQL variable
#: does not survive a `GO`) are the two fields that closed it; `sql_execution` in that app turned
#: out to be an import nobody used. Down to 9 / 5.
#:
#: Then `sql_tasks`, the last holder of the SQL trio, once `run-sql` also gained `capture: "all"`
#: (the runner stores five result sets) and a `connect_timeout_seconds` separate from the
#: statement budget (a task allowed twenty minutes must not wait twenty minutes to learn the host
#: is down). Its `execute_legacy_oracle` went too: it opened nothing, but it decided *which
#: transport* and then reshaped the bridge's answer, and `run-sql` routes `sql_access` itself and
#: answers in one shape whichever transport replied. Nine driver-level `sql_execution` imports
#: went with it — most had already stopped being used. Before converting, both resolvers were run
#: over all 12 configured task targets and agreed on ip, port and login for every one. Down to
#: 5 / 2, and the SQL trio is closed.
REMAINING: dict[str, frozenset[str]] = {
    "remote_exec": frozenset({"backup_restore", "sre"}),
    "ssh": frozenset({"backup_restore"}),
}


def _app_files() -> list[Path]:
    return sorted(
        p for p in DB_OPS_ROOT.rglob("*.py")
        if "__pycache__" not in p.parts
        and len(p.relative_to(DB_OPS_ROOT).parts) > 1
        and p.relative_to(DB_OPS_ROOT).parts[0] in APPS
    )


def _relative(path: Path) -> str:
    return path.relative_to(DB_OPS_ROOT).as_posix()


def _common_submodules(path: Path) -> set[str]:
    """Which ``db_ops.common.<x>`` this file reaches, by either import form."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[:2] == ["db_ops", "common"] and len(parts) > 2:
                    found.add(parts[2])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            parts = node.module.split(".")
            if parts[:2] != ["db_ops", "common"]:
                continue
            if len(parts) > 2:
                found.add(parts[2])
            else:
                # `from db_ops.common import a, b` — the names are the submodules.
                found |= {alias.name for alias in node.names}
    return found - set(EXEMPT_MODULES)


def _allowed(app: str, module: str) -> bool:
    return (app in REMAINING.get(module, frozenset())
            or module in EXEMPT_APPS.get(app, frozenset()))


@pytest.mark.parametrize("path", _app_files(), ids=_relative)
def test_an_app_imports_no_common_module_outside_the_baseline(path: Path) -> None:
    app = path.relative_to(DB_OPS_ROOT).parts[0]
    offenders = sorted(
        module for module in _common_submodules(path) if not _allowed(app, module)
    )
    assert not offenders, (
        f"{_relative(path)} imports db_ops.common.{offenders} — an app calls the `common` CLI, "
        "it does not import it. If this is a value rather than an operation it belongs in "
        "db_ops/lib/ (pure) or db_ops/db/ (a row shape); if it is an operation, call it through "
        "`python -m db_ops.common.cli <command> '<json>'`. Do not add it to REMAINING: that list "
        "is a baseline that shrinks."
    )


def test_the_baseline_has_no_stale_entries() -> None:
    """An entry nobody imports any more must go, or the baseline stops measuring anything.

    This is the half that makes the list shrink instead of accumulate: finishing a migration is
    not done until its line is deleted here.
    """
    actual: dict[str, set[str]] = {}
    for path in _app_files():
        app = path.relative_to(DB_OPS_ROOT).parts[0]
        for module in _common_submodules(path):
            actual.setdefault(module, set()).add(app)

    # Only apps that exist in this tree. The public distribution ships seven of the fourteen, so
    # an entry naming one of the other seven is not stale there — it is describing software that
    # was left out, and reading it as a finished migration would delete a line still doing work in
    # the repository this baseline belongs to.
    present = {path.relative_to(DB_OPS_ROOT).parts[0] for path in _app_files()}
    stale = sorted(
        f"{module}: {sorted((apps & present) - actual.get(module, set()))}"
        for module, apps in REMAINING.items()
        if (apps & present) - actual.get(module, set())
    )
    assert not stale, (
        "REMAINING lists apps that no longer import these modules — delete the entries, the "
        f"migration moved on without them: {stale}"
    )


def test_an_exemption_that_stopped_being_used_is_not_an_exemption() -> None:
    """The same rule for :data:`EXEMPT_APPS`, and it matters more here.

    A baseline entry that goes stale merely overstates the work left. A stale *exemption* is a
    permission nobody needs, sitting next to a measurement that argued for it — and the next
    person reads it as still true. `metrics` is exempt for four named modules; if it stops
    importing one of them, that one comes off the list.
    """
    used: dict[str, set[str]] = {}
    for path in _app_files():
        app = path.relative_to(DB_OPS_ROOT).parts[0]
        used.setdefault(app, set()).update(_common_submodules(path))

    stale = sorted(
        f"{app}: {sorted(modules - used.get(app, set()))}"
        for app, modules in EXEMPT_APPS.items()
        if modules - used.get(app, set())
    )
    assert not stale, (
        "EXEMPT_APPS grants exemptions nobody uses any more — delete them, and delete the "
        f"measurement that argued for them if it no longer applies: {stale}"
    )
