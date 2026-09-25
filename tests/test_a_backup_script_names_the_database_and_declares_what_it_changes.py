"""A script sent to a machine must say which database it means, and admit what it leaves behind.

Two assumptions were built into the engine scripts and neither was written down. Both were correct
for the lab that grew them and neither is correct for a database db_ops did not create.

**Which database.** ``rman target /`` connects to whatever ``ORACLE_SID`` the container's login
profile exports, and ``psql``/``pg_basebackup`` connect to whatever port is the image default. One
instance per container makes both right. Two makes them a coin toss that reports success: the backup
completes, against the wrong database, under the right ``backup_id``.

**What is left behind.** The RMAN scripts issue ``CONFIGURE`` statements, and ``CONFIGURE`` in RMAN
is not an option for the run - it is written into the controlfile and governs every later
connection, including the ``DELETE`` in this same script. On the lab database that *is* the
configuration. On an adopted production database it silently replaced somebody else's retention
policy, on the first run, with nothing in the output to say so.

The assertions are about the scripts' text. Their behaviour needs an Oracle instance, a PostgreSQL
17 cluster and a container each; what can be pinned offline is the structure that carries the
decision, which is what actually regressed.
"""

from __future__ import annotations

import re

import pytest

from db_ops.lib.paths import resolve_tool_path

PG = "assets/backup/postgresql/pg_basebackup_database.sh"
ORACLE_DB = "assets/backup/oracle/oracle_rman_database.sh"
ORACLE_ARCH = "assets/backup/oracle/oracle_rman_archivelog.sh"


def read(relpath: str) -> str:
    return resolve_tool_path(relpath).read_text(encoding="utf-8")


def statements(code: str) -> str:
    """The script with its comment lines removed.

    Every negative assertion below has to run against this rather than the whole file. A comment is
    where the reason for a rule is written, so it names the very thing the rule forbids — and the
    first draft of this file passed `"docker exec -e" not in code` right up until the comment
    explaining why `-e` is wrong was added, at which point it failed on its own explanation. The
    inverse is worse and has happened here before: a guard that goes on passing because a comment
    still quotes the literal the code no longer contains.
    """
    return "\n".join(line for line in code.splitlines() if not line.lstrip().startswith("#"))


@pytest.fixture(scope="module")
def pg() -> str:
    return read(PG)


@pytest.fixture(scope="module")
def rman() -> dict[str, str]:
    return {ORACLE_DB: read(ORACLE_DB), ORACLE_ARCH: read(ORACLE_ARCH)}


# --------------------------------------------------------------------------- #
# Which database: the port, and the SID
# --------------------------------------------------------------------------- #
def test_the_postgres_port_travels_as_an_argument_not_as_an_environment_variable(pg: str):
    """`docker exec` does not carry the calling script's environment into the container.

    This is why documenting `PGPORT` would not have been a fix: the variable would reach the host
    script, the container would never see it, and psql would connect to 5432 anyway — succeeding,
    on a host with two clusters, against the wrong one.
    """
    assert 'pg_port="${PG_PORT:-}"' in pg
    assert "-p '${pg_port}'" in pg
    # Not PGPORT: exporting it here would look like a fix and change nothing inside the container.
    assert "PGPORT" not in statements(pg)


def test_every_client_call_in_the_container_uses_the_same_connection_arguments(pg: str):
    """Four calls: pg_is_in_recovery, summarize_wal, the system identifier, and the backup itself.

    They are one variable rather than four argument lists because the failure of threading a port
    into three of four is not an error — it is a check that silently ran against a different
    cluster than the backup did.
    """
    code = statements(pg)
    client_calls = [line for line in code.splitlines()
                    if "run_db " in line and ("psql " in line or "pg_basebackup " in line)]

    assert len(client_calls) == 4, client_calls
    assert all("${pg_conn}" in line for line in client_calls), client_calls
    # Exactly once, in the line that builds pg_conn. A second occurrence is a call site that was
    # written with its own argument list instead of the shared one.
    assert code.count("-U '${pg_user}'") == 1


def test_a_port_that_is_not_a_port_is_refused_before_anything_connects(pg: str):
    assert "PG_PORT must be a port number" in pg


@pytest.mark.parametrize("script", [ORACLE_DB, ORACLE_ARCH])
def test_the_oracle_sid_is_exported_after_the_login_profile_has_run(rman, script):
    """Placement is the whole point, and `docker exec -e` is the wrong one.

    The RMAN call runs under `bash -l` because the profile is what sets ORACLE_HOME and puts `rman`
    on the PATH. A profile that also sets ORACLE_SID runs *after* `-e` and overwrites it. Exporting
    inside the command, after the profile, is the only placement that wins.
    """
    code = statements(rman[script])

    assert "sid_export=\"export ORACLE_SID='${oracle_sid}'; \"" in code
    assert '"${sid_export}rman target / log /dev/stdout 2>&1"' in code
    assert "exec -e" not in code


@pytest.mark.parametrize("script", [ORACLE_DB, ORACLE_ARCH])
def test_an_unset_sid_leaves_the_profiles_own_choice_alone(rman, script):
    """Every entry in this estate runs one instance per container and wants exactly that. A default
    that forced a SID would break all of them to fix a case none of them has."""
    code = rman[script]

    assert 'oracle_sid="${ORACLE_SID:-}"' in code
    assert '[ -n "$oracle_sid" ] && sid_export=' in code


@pytest.mark.parametrize("script", [ORACLE_DB, ORACLE_ARCH])
def test_a_sid_that_could_be_shell_is_refused(rman, script):
    """It is exported into a shell inside the container, so a value with a space, a semicolon or a
    quote in it would run rather than name anything."""
    assert "ORACLE_SID must be letters, digits and underscores" in rman[script]


@pytest.mark.parametrize("script", [ORACLE_DB, ORACLE_ARCH])
def test_which_database_was_chosen_is_printed(rman, script):
    """db_ops stores this as `stdout_tail`. A backup of the wrong instance is indistinguishable from
    a backup of the right one unless the run says which it connected to."""
    assert "printf 'oracle_sid=%s rman_configure=%s" in rman[script]


# --------------------------------------------------------------------------- #
# What is left behind: CONFIGURE is persistent, and DELETE obeys it
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("script", [ORACLE_DB, ORACLE_ARCH])
def test_the_configuration_as_it_was_is_captured_before_it_is_changed(rman, script):
    """`SHOW ALL` unconditionally, and first. It is the only record of what the policy was, so it is
    also the only way to put it back — and it is what `DELETE NOPROMPT` has to be read against."""
    code = rman[script]

    assert "SHOW ALL;" in code
    assert code.index("SHOW ALL;") < code.index("${configure_lines}")


@pytest.mark.parametrize("script", [ORACLE_DB, ORACLE_ARCH])
def test_a_database_whose_policy_is_not_ours_to_set_can_refuse_the_configure(rman, script):
    code = rman[script]

    assert 'rman_configure="${RMAN_CONFIGURE:-apply}"' in code
    assert 'if [ "$rman_configure" = "apply" ]; then' in code
    assert "RMAN_CONFIGURE must be apply or skip" in code


@pytest.mark.parametrize("script", [ORACLE_DB, ORACLE_ARCH])
def test_skipping_the_configure_says_what_the_deletes_will_then_obey(rman, script):
    """`skip` is not "nothing happens". The DELETE statements still run, against whatever policy the
    database already has, and a run that changed which policy applied without saying so is the fault
    this option exists to avoid — not one to reintroduce from the other side."""
    assert "RMAN_CONFIGURE=skip" in rman[script]


def test_the_archivelog_job_names_the_risk_that_skipping_carries(rman):
    """Here `skip` is genuinely dangerous, which is why the default is `apply` and not `skip`.

    The deletes name an age, but RMAN still consults the ARCHIVELOG DELETION POLICY before removing
    anything. Under the policy this script sets, a log that has not been backed up survives its age.
    Under `TO NONE` — the default on a fresh database — the same statement deletes it.
    """
    code = rman[ORACLE_ARCH]

    assert "has not been backed up" in code
    assert "TO NONE" in code


@pytest.mark.parametrize("script", [ORACLE_DB, ORACLE_ARCH])
def test_no_apostrophe_reaches_rman_inside_a_comment(rman, script):
    """The skip branch is emitted into the RMAN script as a `#` comment. RMAN strips those before
    parsing, and there is still no reason to hand another tool's lexer a quote to be lenient about.
    """
    code = rman[script]
    start = code.index('configure_lines="# RMAN_CONFIGURE=skip')
    end = code.index("\nfi\n", start)

    assert "'" not in code[start:end]


@pytest.mark.parametrize("script", [ORACLE_DB, ORACLE_ARCH])
def test_the_os_user_inside_the_container_is_expressible_and_defaults_to_nothing(rman, script):
    """The PostgreSQL script pins `-u postgres` and its comment says what getting it wrong costs: a
    backup written root-owned and 0700, which pg_verifybackup cannot read and PostgreSQL will not
    start on. Oracle inherited the shape of that risk with no way to say anything about it.

    The default is empty — the image's own user, which is the only behaviour either Oracle container
    in this estate has ever run. Picking a default nobody has exercised would be inventing a fix.
    """
    code = statements(rman[script])

    assert 'oracle_os_user="${ORACLE_OS_USER:-}"' in code
    assert 'exec_user=""' in code
    assert '[ -n "$oracle_os_user" ] && exec_user="-u ${oracle_os_user}"' in code


@pytest.mark.parametrize("script", [ORACLE_DB, ORACLE_ARCH])
def test_every_docker_exec_in_the_oracle_scripts_goes_through_the_same_user(rman, script):
    """Three calls per script: the mkdir, the RMAN pipe, and the chmod that follows. A backup taken
    as one user and chmod'd as another is the permission bug this is meant to make expressible."""
    code = statements(rman[script])
    execs = [line for line in code.splitlines() if "$DOCKER exec" in line]

    assert execs, code
    # One exception, by name: `run_root`, which only makes the backup folder and hands it to the
    # engine's user when that user cannot (a lab's bind mount belongs to the SSH user). It writes
    # no backup, so it cannot take one as another user.
    as_root = [line for line in execs if line.lstrip().startswith("run_root()")]
    assert len(as_root) == 1, as_root
    assert all("${exec_user}" in line for line in execs if line not in as_root), execs
    calls = [line for line in code.splitlines() if "run_root " in line and "run_root()" not in line]
    assert calls and all("chown -R ${engine_uid}:${engine_gid}" in line for line in calls), calls


@pytest.mark.parametrize("script", [ORACLE_DB, ORACLE_ARCH])
def test_an_os_user_that_could_carry_a_space_is_refused(rman, script):
    """It is unquoted on the docker command line, and it has to be: an empty value must expand to no
    argument rather than to an empty one. That makes validating it the only defence."""
    assert "ORACLE_OS_USER must be letters, digits and underscores" in rman[script]


# --------------------------------------------------------------------------- #
# §1.3a: a container is one way to reach the engine, not the only way
# --------------------------------------------------------------------------- #
MSSQL = "assets/backup/sqlserver/mssql_backup_database.sh"
EVERY_SH = [PG, ORACLE_DB, ORACLE_ARCH, MSSQL]


@pytest.mark.parametrize("script", EVERY_SH)
def test_the_container_is_no_longer_required(script):
    """Until 0.21.0 every one of these began by refusing to run without `DOCKER_CONTAINER`, so an
    engine installed directly on a host could not be backed up at all — and the refusal read as a
    configuration mistake rather than a missing capability. All four targets in this estate are
    containers, so nothing was visibly broken, which is how it stayed undocumented for so long.
    """
    code = statements(read(script))

    assert '|| die "DOCKER_CONTAINER is not set."' not in code
    assert 'if [ -n "$container" ]; then' in code


@pytest.mark.parametrize("script", EVERY_SH)
def test_both_paths_go_through_one_wrapper(script):
    """The property that keeps the two from drifting. A call written as `docker exec` at its own
    site works in every test anyone runs here and fails on a host install — and the reverse. The
    wrapper is defined twice, once per branch, and called everywhere.

    This is not hypothetical: the first version of the SQL Server change converted the two sqlcmd
    callers and left five `docker exec` sites for certificates and file work, which would have run
    `docker exec -i "" ...` on a host. The count below is what caught it.
    """
    code = statements(read(script))
    wrapper = "exec_here" if script == MSSQL else "run_db"

    direct = [line for line in code.splitlines() if "$DOCKER exec" in line]

    # Every remaining `docker exec` must be a wrapper DEFINITION, never a call site. Oracle defines
    # two (a shell command and the RMAN pipe, which needs stdin open) and SQL Server two (a command
    # and a silent probe), so the count is not the property - being a definition is.
    assert all(re.search(r"\w+\(\)\s*\{", line) for line in direct), direct
    assert code.count(f"{wrapper} ") >= 2


@pytest.mark.parametrize("script", EVERY_SH)
def test_the_run_says_which_way_it_reached_the_engine(script):
    """`stdout_tail` has to answer "where did this actually run" — a backup taken against the host
    when the operator meant the container is not distinguishable from the right one otherwise."""
    code = statements(read(script))

    assert 'where="' in code
    assert "printf 'reaching the" in code


@pytest.mark.parametrize("script", [PG, ORACLE_DB, ORACLE_ARCH])
def test_the_native_path_still_runs_as_the_database_user(script):
    """The `-u` on the container path exists because a backup written by the wrong user is owned
    root and 0700, which the restore cannot read. Dropping to the host path must not drop that: the
    ordinary case is already being that user, and `su` covers being root."""
    code = statements(read(script))

    assert 'id -un' in code
    assert "su -s /bin/bash" in code


@pytest.mark.parametrize("script", EVERY_SH)
def test_a_host_with_no_client_tools_is_refused_with_both_ways_out(script):
    """The failure an operator actually meets on a host install. Naming only the missing binary
    leaves them guessing whether db_ops supports this at all."""
    code = statements(read(script))

    assert "DOCKER_CONTAINER is not set, so there is no way to reach" in code or \
           "set DOCKER_CONTAINER" in code


def test_the_oracle_native_path_keeps_the_login_shell():
    """`bash -l` is not decoration on either path: the profile is what sets ORACLE_HOME and puts
    `rman` on the PATH. A host install has the same profile and the same need, so a native path
    without `-l` would fail to find rman on a machine that has it — and the check for rman has to
    run through that same shell for the same reason."""
    code = statements(read(ORACLE_DB))

    assert 'bash -lc' in code
    assert 'run_db "command -v rman' in code


# --------------------------------------------------------------------------- #
# The chained pair must share one entry, and the listing says so when it does not
# --------------------------------------------------------------------------- #
def test_the_listing_warns_when_a_postgres_incremental_has_no_baseline_beside_it():
    """Why `database_full` and `database` stay in ONE entry, and why splitting them is worse than
    the asymmetry it would tidy away.

    `pg_basebackup --incremental` chains onto the newest backup in **this entry's** `backup_dir/base/`.
    Across two entries, `backup_dir` becomes a copy-paste invariant, and getting it wrong raises
    nothing: `latest` comes back empty, the script takes a FULL baseline instead — by the rule that
    exists so a first run is not an error — reports success, and does that for ever. No incremental
    is ever taken again and every run says `done`.

    So the pair is not split, and the listing an operator reads before running one says so when
    somebody splits it by hand.
    """
    from db_ops.backup_restore.cli import _format_backup_list

    class Job:
        def __init__(self, backup_id, job, db_type="postgresql"):
            self.backup_id = backup_id
            self.job = job
            self.db_type = db_type
            self.active = True
            self.env = {}
            self.env_secrets = {}
            self.server_id = "HOST-1"
            self.time_window = type("W", (), {
                "from_hour": 1, "to_hour": 5, "repeat_interval": 3600, "weekdays": None})()

    split = _format_backup_list([Job("PG_INCR_ONLY", "database")])
    together = _format_backup_list([Job("PG_PAIR", "database"), Job("PG_PAIR", "database_full")])

    assert "PG_INCR_ONLY: an incremental with no database_full job beside it" in split
    assert "take a FULL instead, and report success" in split
    # The correct shape says nothing at all: a warning on every healthy entry is a warning nobody
    # reads, which is the same fault as the silence it replaces.
    assert "!" not in together.replace("!", "", 0) or "no database_full" not in together


def test_the_warning_groups_by_entry_rather_than_by_job():
    """The first draft read each listing element as an entry. The listing is already flattened one
    element per JOB, so it reported every incremental in the estate — including the correct ones."""
    from db_ops.backup_restore.cli import _format_backup_list

    class Job:
        def __init__(self, backup_id, job):
            self.backup_id, self.job, self.db_type = backup_id, job, "postgresql"
            self.active = True
            self.env = {}
            self.env_secrets = {}
            self.server_id = "HOST-1"
            self.time_window = type("W", (), {
                "from_hour": 1, "to_hour": 5, "repeat_interval": 3600, "weekdays": None})()

    text = _format_backup_list([Job("PAIR", "database_full"), Job("PAIR", "database")])

    assert "no database_full" not in text


def test_a_non_postgres_incremental_is_not_warned_about():
    """SQL Server's diff does not need its full in the same directory — it needs it in the same
    database history — so full/diff/log as three separate entries is correct there and is what this
    estate already does for six of its SQL Server registrations."""
    from db_ops.backup_restore.cli import _format_backup_list

    class Job:
        def __init__(self):
            self.backup_id, self.job, self.db_type = "MSSQL_DIFF", "diff", "sqlserver"
            self.active = True
            self.env = {}
            self.env_secrets = {}
            self.server_id = "HOST-1"
            self.time_window = type("W", (), {
                "from_hour": 1, "to_hour": 5, "repeat_interval": 3600, "weekdays": None})()

    assert "no database_full" not in _format_backup_list([Job()])
