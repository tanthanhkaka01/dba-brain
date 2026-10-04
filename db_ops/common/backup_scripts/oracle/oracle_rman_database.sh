#!/bin/bash
# Oracle RMAN database backup - level 0 or level 1, chosen by whoever calls it.
#
# $BACKUP_LEVEL says which. db_ops sets it from the job's own configuration, so "the weekly
# level 0 on Sunday" is `time_window.weekdays: [7]` on a job pinning BACKUP_LEVEL=0 beside one
# pinning 1 on the other six days. The weekday used to live in this script because TimeWindow had
# no day-of-week dimension; it has one since 2026-09-21, so the literal is gone from here.
#
# Runs on the machine the database is on, shipped over SSH by db_ops.backup_restore.backup;
# with $DOCKER_CONTAINER set it
# `docker exec`s into $DOCKER_CONTAINER, mirroring the metrics docker collector.
#
# Retention is a recovery window: DELETE OBSOLETE removes only what is no longer needed
# to restore to any point inside $RETENTION_DAYS. It is never a blind "delete older than",
# which would happily drop the level 0 that the newer level 1s depend on.
#
# A window under a day: RMAN's recovery window is whole days, so the policy is set to one day -
# the smallest there is - and the window itself ($RETENTION_SECONDS) is applied by RMAN's own
# `DELETE BACKUP COMPLETED BEFORE`, which removes whole backup sets and their catalogue records
# together. Only after a level 0 that succeeded in this run, and never anything from this run:
# the newest level 0 is then always newer than what is removed. Deleting pieces by file age from
# outside RMAN would cut a backup in half - one level 0 is several backup sets finishing seconds
# apart - and leave the catalogue pointing at files that are gone.
#
# Env: DOCKER_CONTAINER (OPTIONAL since 0.21.0 - unset means this host), BACKUP_DIR (required, path inside the container),
#      RETENTION_DAYS (default 14), RETENTION_SECONDS (optional; a window under a day),
#      BACKUP_LEVEL (optional 0|1 override for manual runs),
#      ORACLE_SID (optional; the container login profile's own when unset - set it when the
#      container serves more than one instance, because `rman target /` connects to whichever
#      SID the profile happens to export),
#      ORACLE_OS_USER (optional OS user inside the container; the image default when
#      unset - the PostgreSQL script pins its own because a backup written by the wrong
#      user is unrestorable),
#      RMAN_CONFIGURE (apply|skip, default apply; `skip` leaves the database's own persistent
#      RMAN configuration untouched - see the block that builds the CONFIGURE lines).
# Exit: 0 on success, non-zero on failure (the runner records status from this).
set -u

container="${DOCKER_CONTAINER:-}"
backup_dir="${BACKUP_DIR:-}"
retention_days="${RETENTION_DAYS:-14}"
retention_seconds="${RETENTION_SECONDS:-}"
# When this run began, on this host's clock: the sub-day sweep below counts back from here, so a
# backup that takes longer than the window cannot have its own first sets removed.
run_started="$(date +%s)"
level_override="${BACKUP_LEVEL:-}"
oracle_sid="${ORACLE_SID:-}"
rman_configure="${RMAN_CONFIGURE:-apply}"
oracle_os_user="${ORACLE_OS_USER:-}"

# Empty means "the image's default", which is what both Oracle containers in this estate run as and
# therefore the only behaviour that has ever been exercised. It is a variable rather than a hardcoded
# `-u oracle` for that reason: the PostgreSQL script pins `-u postgres` because getting it wrong there
# writes the backup root-owned and 0700, and PostgreSQL then refuses to start on the restored
# directory - a backup that looks present and is not restorable. Oracle inherits the same shape of
# risk, so the knob exists; picking a default nobody has run would be inventing a fix.
exec_user=""
[ -n "$oracle_os_user" ] && exec_user="-u ${oracle_os_user}"

die() { printf 'RESULT=error reason=%s\n' "$1" >&2; exit 1; }

[ -n "$backup_dir" ] || die "BACKUP_DIR is not set."

case "$retention_days" in
    ''|*[!0-9]*) die "RETENTION_DAYS must be a whole number of days: '${retention_days}'." ;;
esac
case "$retention_seconds" in
    *[!0-9]*) die "RETENTION_SECONDS must be a whole number of seconds: '${retention_seconds}'." ;;
esac

# Empty is the normal case and means "whatever the container's profile exports". Anything else has
# to be an identifier: it is exported into a shell inside the container, so a value with a space or
# a quote in it would run as shell rather than name a database.
case "$oracle_sid" in
    *[!A-Za-z0-9_]*) die "ORACLE_SID must be letters, digits and underscores: '${oracle_sid}'." ;;
esac

# Unquoted on the docker command line - it has to be, because an empty value must expand to no
# argument at all rather than to an empty one - so it cannot be allowed to carry a space.
case "$oracle_os_user" in
    *[!A-Za-z0-9_]*) die "ORACLE_OS_USER must be letters, digits and underscores: '${oracle_os_user}'." ;;
esac

case "$rman_configure" in
    apply|skip) ;;
    *) die "RMAN_CONFIGURE must be apply or skip: '${rman_configure}'." ;;
esac

# The level is the SCHEDULER's decision and arrives in $BACKUP_LEVEL. It used to be made here,
# with `[ "${DB_OPS_WEEKDAY:-$(date +%u)}" = "7" ]`, which read the CONTAINER HOST's clock whenever
# db_ops did not pass $DB_OPS_WEEKDAY - a different day from the one the scheduler used for the
# same job's hour window. See pg_basebackup_database.sh for the night that cost.
#
# Now the weekday is config (`time_window.weekdays`, ISO 1-7), read on the node's own clock. A run
# with no $BACKUP_LEVEL is a manual one and takes level 1; RMAN promotes a level 1 to a level 0 by
# itself when no level 0 exists in the recovery window, so a first-ever run is still a baseline.
if [ -n "$level_override" ]; then
    case "$level_override" in
        0|1) level="$level_override" ;;
        *) die "BACKUP_LEVEL must be 0 or 1: '${level_override}'." ;;
    esac
else
    level=1
fi

# $DOCKER_CONTAINER is optional since 0.21.0: set it for a database inside a container, leave it
# unset for one installed on this host. The choice is made once, here, and everything below goes
# through `run_db` (a shell command) and `run_rman` (the same, with RMAN's script on stdin). Before
# this, `docker exec` was written out at each of the four call sites, so there was nothing to switch.
if [ -n "$container" ]; then
    DOCKER="docker"
    $DOCKER info >/dev/null 2>&1 || DOCKER="sudo docker"
    $DOCKER inspect "$container" >/dev/null 2>&1 \
        || die "container '${container}' not found or docker unavailable on host."
    # Present is not running. A stopped container used to fail further on, as whatever the first
    # exec could not do - "no sqlcmd found (container X); install mssql-tools" on 2026-09-24 -
    # which sends the reader to install tools that are there.
    [ "$($DOCKER inspect -f '{{.State.Running}}' "$container" 2>/dev/null)" = "true" ] \
        || die "container '${container}' is not running - start it (docker start ${container}) and run the backup again."
    run_db()   { $DOCKER exec ${exec_user} "$container" bash -lc "$1" </dev/null; }
    run_rman() { $DOCKER exec -i ${exec_user} "$container" bash -lc "$1"; }
    where="container ${container}"
else
    # `bash -l` in both paths, for the reason the container path needs it: the login profile is what
    # sets ORACLE_HOME and puts rman on the PATH. A host install has the same profile and the same
    # need, so running without -l would fail to find rman on a machine that has it.
    if [ -n "$oracle_os_user" ] && [ "$(id -un 2>/dev/null)" != "$oracle_os_user" ]; then
        id "$oracle_os_user" >/dev/null 2>&1 \
            || die "ORACLE_OS_USER '${oracle_os_user}' does not exist on this host."
        command -v su >/dev/null 2>&1 || die "su is not available, so this cannot drop to '${oracle_os_user}'."
        run_db()   { su -s /bin/bash "$oracle_os_user" -c "bash -lc \"$1\"" </dev/null; }
        run_rman() { su -s /bin/bash "$oracle_os_user" -c "bash -lc \"$1\""; }
        where="host as ${oracle_os_user} via su"
    else
        run_db()   { bash -lc "$1" </dev/null; }
        run_rman() { bash -lc "$1"; }
        where="host as $(id -un 2>/dev/null)"
    fi
    # Checked through the same shell the work runs in: `rman` is usually not on the caller's PATH
    # and is put there by the profile, so testing it from here would refuse a host that is fine.
    run_db "command -v rman >/dev/null 2>&1" \
        || die "rman is not on PATH for this user's login shell and DOCKER_CONTAINER is not set, so there is no way to reach a database. Set DOCKER_CONTAINER for a containerised database, or set ORACLE_OS_USER to an account whose profile provides rman."
fi
printf 'reaching the database: %s\n' "$where"

# No -i, and stdin closed: this script is fed to `bash -s` over SSH, so a `docker exec -i`
# without its own input would read the *rest of this file* as the container's stdin and the
# shell would silently run out of script (exit 0, no output, nothing backed up). The RMAN call
# below may use -i because its stdin is the pipe from printf, not the script.
# Root where the database runs, for the one thing its own user cannot do: take over a backup
# folder someone else made (see "writable by the database user" below). On a host it never
# prompts - a sudo that wants a password fails, and the folder is named.
if [ -n "$container" ]; then
    run_root() { $DOCKER exec -u 0 "$container" bash -lc "$1" </dev/null; }
else
    run_root() { sudo -n bash -lc "$1" </dev/null; }
fi

# Writable by the database user, not only by whoever made the folder above it. A lab's bind mount
# belongs to the SSH user, and on 2026-09-24 every backup into it failed "mkdir: Permission
# denied" until the folder was handed over by hand. Made as root where the database runs and
# given to the user it runs as (numeric ids: the su path cannot carry quotes) - the whole folder,
# because what is checked is the folder this job writes, which may sit one level below it.
if ! run_db "mkdir -p '${backup_dir}' && test -w '${backup_dir}'" 2>/dev/null; then
    engine_uid="$(run_db 'id -u' 2>/dev/null)"; engine_gid="$(run_db 'id -g' 2>/dev/null)"
    [ -n "$engine_uid" ] && run_root "mkdir -p '${backup_dir}' && chown -R ${engine_uid}:${engine_gid} '${backup_dir}'" >/dev/null 2>&1
fi

run_db "mkdir -p '${backup_dir}'" </dev/null \
    || die "could not create BACKUP_DIR '${backup_dir}' (${where})."

printf 'backup_level=%s where=%s backup_dir=%s retention_days=%s\n' \
    "$level" "$where" "$backup_dir" "$retention_days"

# The deletion policy omits APPLIED ON ALL STANDBY on purpose - see the note in
# oracle_rman_archivelog.sh: this lab's standby stopped applying at sequence 14, so that
# clause disabled retention entirely and the archive grew without bound.
# Backup encryption. RMAN password-based encryption (`IDENTIFIED BY ... ONLY`) works on
# Oracle Free — verified on 23.26.2 — and needs no wallet, so the passphrase alone restores
# the set anywhere. Absent $BACKUP_ENCRYPTION_PASSWORD the backups stay unencrypted, which is
# the previous behaviour: turning encryption on silently would produce backups nobody can
# restore once the passphrase is lost.
enc_password="${BACKUP_ENCRYPTION_PASSWORD:-}"
enc_line=""
if [ -n "$enc_password" ]; then
    case "$enc_password" in
        *"'"*) die "BACKUP_ENCRYPTION_PASSWORD must not contain a single quote (it is passed to RMAN quoted)." ;;
    esac
    enc_line="SET ENCRYPTION ON IDENTIFIED BY '${enc_password}' ONLY;"
fi

# These four are PERSISTENT database settings, not options for this run: RMAN stores them in the
# controlfile and they outlive the backup, apply to every other tool that connects, and change what
# `DELETE NOPROMPT OBSOLETE` below deletes. On the lab database db_ops owns that is the point - the
# retention window is the job's configuration. On a database adopted into db_ops that somebody else
# administers it is not: a job would quietly overwrite their retention policy and their archivelog
# deletion policy on its first run, and nothing in the output would say it had happened.
#
# So: `SHOW ALL` first, unconditionally, which puts the values as they were into stdout_tail where
# `DELETE OBSOLETE` can be read against them and where a wrong one can be put back. And
# RMAN_CONFIGURE=skip for a database whose policy is not db_ops's to set. The default stays `apply`
# because that is what every entry in this estate already relies on, and changing a default
# silently is the same class of fault as setting the policy silently.
if [ "$rman_configure" = "apply" ]; then
    configure_lines="CONFIGURE RETENTION POLICY TO RECOVERY WINDOW OF ${retention_days} DAYS;
CONFIGURE CONTROLFILE AUTOBACKUP ON;
CONFIGURE CONTROLFILE AUTOBACKUP FORMAT FOR DEVICE TYPE DISK TO '${backup_dir}/autobackup_%F';
CONFIGURE ARCHIVELOG DELETION POLICY TO BACKED UP 1 TIMES TO DISK;"
else
    # Not "nothing": DELETE NOPROMPT OBSOLETE still runs, and it obeys whatever policy the database
    # already has. Saying so in the log is the difference between a deliberate choice and a
    # retention sweep nobody can account for.
    # No apostrophe in the text: it is emitted into the RMAN script as a comment, and there is no
    # reason to hand a quote to another tool lexer to be lenient about.
    configure_lines="# RMAN_CONFIGURE=skip: the database keeps the persistent configuration it has,
# and DELETE NOPROMPT OBSOLETE below acts on THAT policy, not on RETENTION_DAYS=${retention_days}."
fi

# The backup ends with its own archived logs. A level 0 or 1 taken with the database open is fuzzy:
# to open it, a restore applies the redo written WHILE it was taken, and that redo sat in the online
# log until the next archivelog job - up to its interval later. Until then the backup could not be
# used: a DUPLICATE recovers through the newest archived-log backup and stops, so on the 0.26.0 lab
# (2026-10-04) a level 0 checkpointed at SCN 2899140, the newest log backup ending at 2898894, made
# the restore fall back to the level 0 before (RMAN-06023 while the copy held only the new one).
# So the current log is archived and every log not yet backed up goes into this backup's
# directory, under the archivelog job's own name, before the controlfile that records them.
RMAN_IN="$(cat <<RMANEOF
${enc_line}
SHOW ALL;
${configure_lines}
RUN {
  ALLOCATE CHANNEL c1 DEVICE TYPE DISK FORMAT '${backup_dir}/%d_L${level}_%T_%U.bkp';
  BACKUP INCREMENTAL LEVEL ${level} DATABASE TAG 'DBOPS_L${level}';
  SQL 'ALTER SYSTEM ARCHIVE LOG CURRENT';
  BACKUP ARCHIVELOG ALL NOT BACKED UP 1 TIMES TAG 'DBOPS_ARCH' FORMAT '${backup_dir}/arch_%d_%T_%U.bkp';
  BACKUP CURRENT CONTROLFILE TAG 'DBOPS_CTL';
  RELEASE CHANNEL c1;
}
CROSSCHECK BACKUP;
DELETE NOPROMPT EXPIRED BACKUP;
DELETE NOPROMPT OBSOLETE;
EXIT;
RMANEOF
)"

# ORACLE_SID is exported inside the container, not passed through `docker exec -e`: the RMAN call
# runs under `bash -l`, whose profile is what sets ORACLE_HOME and the PATH that finds `rman`, and a
# profile that also sets ORACLE_SID would overwrite an -e. Exporting after the profile has run is
# the only placement that wins, and an unset ORACLE_SID leaves the profile's choice alone.
sid_export=""
[ -n "$oracle_sid" ] && sid_export="export ORACLE_SID='${oracle_sid}'; "
printf 'oracle_sid=%s rman_configure=%s\n' "${oracle_sid:-<profile default>}" "$rman_configure"

rman_out="$(printf '%s\n' "$RMAN_IN" | run_rman "${sid_export}rman target / log /dev/stdout 2>&1")"
rman_rc=$?

printf '%s\n' "$rman_out"

# RMAN exits 0 for some partial failures, so the output is checked too: RMAN-03009 is a
# failed command inside RUN{}, RMAN-00569 heads the error stack.
if [ "$rman_rc" -ne 0 ] || printf '%s\n' "$rman_out" | grep -qE 'RMAN-00569|RMAN-03009|ORA-19809|ORA-00257'; then
    printf 'RESULT=error backup_level=%s rman_rc=%s\n' "$level" "$rman_rc" >&2
    exit 1
fi

# --- A window under a day ---------------------------------------------------------------------
# Reached only when the backup above succeeded. After a level 0: every backup set completed before
# (the start of this run - the window) goes, through RMAN. After a level 1 nothing is removed -
# the level 0 it depends on may be older than the window, and it is the next level 0 that makes
# the older ones unnecessary.
#
# The cutoff is SYSDATE - RETENTION_SECONDS/86400 - the configured window, never a literal - less
# the time this run has taken: counted from the run's start, so a level 0 that takes longer than
# the window cannot have its own first backup sets removed.
if [ -n "$retention_seconds" ] && [ "$level" = "0" ]; then
    run_elapsed=$(( $(date +%s) - run_started ))
    sweep_in="$(cat <<RMANEOF
DELETE NOPROMPT BACKUP COMPLETED BEFORE 'SYSDATE-${retention_seconds}/86400-${run_elapsed}/86400';
EXIT;
RMANEOF
)"
    printf 'retention_sweep=SYSDATE-%s/86400-%s/86400 (the window, then this run so far)\n' \
        "$retention_seconds" "$run_elapsed"
    sweep_out="$(printf '%s\n' "$sweep_in" | run_rman "${sid_export}rman target / log /dev/stdout 2>&1")" \
        || printf 'warning: the retention sweep exited non-zero\n' >&2
    printf '%s\n' "$sweep_out"
    # Never the backup's verdict: the backup is taken and good, and a sweep that could not run is
    # a warning the next level 0 gets to try again.
    printf '%s\n' "$sweep_out" | grep -qE 'RMAN-00569' \
        && printf 'warning: the retention sweep reported an RMAN error\n' >&2
fi

# Keep the pieces readable to the host's SSH user, for the same reason the PostgreSQL backup
# does: a cross-machine restore reads this directory over SFTP as an ordinary account that
# neither owns the files nor shares their group, and RMAN writes them 0640. Re-applied on every
# run because each run creates new pieces - a one-off chmod stops being true within minutes.
run_db "chmod -R a+rX '${backup_dir}'" </dev/null     || printf 'warning: could not relax permissions on %s
' "${backup_dir}" >&2

printf 'RESULT=ok backup_level=%s retention_days=%s retention_seconds=%s\n' \
    "$level" "$retention_days" "${retention_seconds:-<unset>}"
