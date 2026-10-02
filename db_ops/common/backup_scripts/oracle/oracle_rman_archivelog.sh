#!/bin/bash
# Oracle RMAN archivelog backup - runs on a short interval (every 15 minutes) to keep the
# recovery point close to now and to stop the FRA filling up between database backups.
#
# Runs on the machine the database is on, shipped over SSH by db_ops.backup_restore.backup;
# with $DOCKER_CONTAINER set it
# `docker exec`s into $DOCKER_CONTAINER, mirroring the metrics docker collector.
#
# The control file and SPFILE ride along with every archivelog run, not just with the daily
# database backup. They are tiny and they change far more often than the datafiles do (every
# tablespace/redo/parameter change rewrites them), so pairing them with the 15-minute job keeps
# the newest copy minutes old instead of up to a day old. Autobackup is on as well, but it only
# fires after structural changes and RMAN operations - an explicit backup is the guarantee.
#
# DELETE INPUT is deliberately NOT used: a log is removed only by the retention sweep below,
# after it has been backed up.
#
# The deletion policy does NOT include APPLIED ON ALL STANDBY. It did, which is the correct
# policy while Data Guard works - it stops retention from removing a log the standby still
# needs. But this lab's standby stopped receiving logs at sequence 14 (the redo shipper sidecar
# fails every cycle) while the primary passed 2300, so nothing ever satisfied "applied on all
# standby" and retention could delete nothing at all: the archive grew to 13 GB and kept going.
# A policy that silently disables retention is worse than no policy, so this now deletes what
# has been backed up. Restore APPLIED ON ALL STANDBY once the standby is rebuilt and shipping.
#
# Env: DOCKER_CONTAINER (OPTIONAL since 0.21.0 - unset means this host), BACKUP_DIR (required, path inside the container),
#      RETENTION_DAYS (default 7 - archivelog backups are kept shorter than database backups),
#      ORACLE_SID (optional; the container login profile's own when unset - set it when the
#      container serves more than one instance, because `rman target /` connects to whichever
#      SID the profile happens to export),
#      ORACLE_OS_USER (optional OS user inside the container; the image default when
#      unset - the PostgreSQL script pins its own because a backup written by the wrong
#      user is unrestorable),
#      RMAN_CONFIGURE (apply|skip, default apply; see the block that builds the CONFIGURE lines -
#      `skip` is NOT free here, because the deletes below lean on the deletion policy).
# Exit: 0 on success, non-zero on failure (the runner records status from this).
set -u

container="${DOCKER_CONTAINER:-}"
backup_dir="${BACKUP_DIR:-}"
retention_days="${RETENTION_DAYS:-7}"
retention_seconds="${RETENTION_SECONDS:-}"
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
# How far back an archived log stays on disk once it has been backed up. Days, or - for a window
# under a day - the seconds as a fraction of one: RMAN takes a date expression here. The BACKUPS
# of archivelogs keep the day-based line below: under a day they are removed by the database
# job's sweep after its next level 0 (oracle_rman_database.sh), never ahead of the level 0 that
# makes them unnecessary.
log_age="${retention_days}"
[ -n "$retention_seconds" ] && log_age="${retention_seconds}/86400"

# Empty means "whatever the container profile exports". Anything else is exported into a shell
# inside the container, so it has to be an identifier and nothing else.
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

printf 'container=%s backup_dir=%s retention_days=%s\n' "$container" "$backup_dir" "$retention_days"

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

# Both of these are PERSISTENT: RMAN keeps them in the controlfile, and they apply to every tool
# that connects afterwards, not just to this run. `SHOW ALL` first so the values as they were are in
# the output, and RMAN_CONFIGURE=skip for a database whose configuration is somebody else to set.
#
# `skip` carries a real risk here that it does not carry in the database job, and it is the whole
# reason the default is `apply`: the DELETE statements below name an age, but RMAN still refuses to
# remove a log that the ARCHIVELOG DELETION POLICY says is not yet safe to remove. With the policy
# this script sets, `BACKED UP 1 TIMES TO DISK`, an un-backed-up log survives its age. Under a
# permissive policy - `TO NONE` is the default on a fresh database - the same DELETE removes logs
# that were never backed up, and the recovery window has a hole in it that nothing reports.
if [ "$rman_configure" = "apply" ]; then
    configure_lines="CONFIGURE CONTROLFILE AUTOBACKUP ON;
CONFIGURE ARCHIVELOG DELETION POLICY TO BACKED UP 1 TIMES TO DISK;"
else
    configure_lines="# RMAN_CONFIGURE=skip: the deletes below obey the deletion policy this database
# already has, which may allow removing an archivelog that has not been backed up."
fi

RMAN_IN="$(cat <<RMANEOF
${enc_line}
SHOW ALL;
${configure_lines}
RUN {
  ALLOCATE CHANNEL c1 DEVICE TYPE DISK FORMAT '${backup_dir}/arch_%d_%T_%U.bkp';
  BACKUP ARCHIVELOG ALL NOT BACKED UP 1 TIMES TAG 'DBOPS_ARCH';
  BACKUP CURRENT CONTROLFILE TAG 'DBOPS_CTL' FORMAT '${backup_dir}/ctl_%d_%T_%U.bkp';
  BACKUP SPFILE TAG 'DBOPS_SPFILE' FORMAT '${backup_dir}/spfile_%d_%T_%U.bkp';
  RELEASE CHANNEL c1;
}
CROSSCHECK ARCHIVELOG ALL;
DELETE NOPROMPT EXPIRED ARCHIVELOG ALL;
DELETE NOPROMPT ARCHIVELOG ALL COMPLETED BEFORE 'SYSDATE-${log_age}';
DELETE NOPROMPT BACKUP OF ARCHIVELOG ALL COMPLETED BEFORE 'SYSDATE-${retention_days}';
EXIT;
RMANEOF
)"

# Exported inside the container rather than passed with `docker exec -e`: the call runs under
# `bash -l`, and a login profile that sets ORACLE_SID would overwrite an -e. After the profile is
# the only placement that wins, and unset leaves the profile's own choice alone.
sid_export=""
[ -n "$oracle_sid" ] && sid_export="export ORACLE_SID='${oracle_sid}'; "
printf 'oracle_sid=%s rman_configure=%s\n' "${oracle_sid:-<profile default>}" "$rman_configure"

rman_out="$(printf '%s\n' "$RMAN_IN" | run_rman "${sid_export}rman target / log /dev/stdout 2>&1")"
rman_rc=$?

printf '%s\n' "$rman_out"

# "no archived logs to backup" is a normal quiet interval, not a failure.
if printf '%s\n' "$rman_out" | grep -qi 'no archived logs? *to backup\|no archived log found'; then
    printf 'RESULT=ok archivelogs=none retention_days=%s\n' "$retention_days"
    exit 0
fi

if [ "$rman_rc" -ne 0 ] || printf '%s\n' "$rman_out" | grep -qE 'RMAN-00569|RMAN-03009|ORA-19809|ORA-00257'; then
    printf 'RESULT=error rman_rc=%s\n' "$rman_rc" >&2
    exit 1
fi

# Keep the pieces readable to the host's SSH user, for the same reason the PostgreSQL backup
# does: a cross-machine restore reads this directory over SFTP as an ordinary account that
# neither owns the files nor shares their group, and RMAN writes them 0640. Re-applied on every
# run because each run creates new pieces - a one-off chmod stops being true within minutes.
run_db "chmod -R a+rX '${backup_dir}'" </dev/null     || printf 'warning: could not relax permissions on %s
' "${backup_dir}" >&2

printf 'RESULT=ok retention_days=%s\n' "$retention_days"
