#!/bin/bash
# Oracle restore drill - rebuild the database from its RMAN backup into a SEPARATE instance.
#
# Runs the same logical phases as the SQL Server restore workflow, so an operator reads one
# sequence whatever the engine is:
#
#   PHASE=restore-key    make the decryption material available on the target
#   PHASE=copy-backup    put the backup pieces where the target instance can read them
#   PHASE=restore-data   RMAN DUPLICATE, then open the restored database
#   PHASE=verify         prove the restored database is actually open and queryable
#
# RMAN DUPLICATE (not RESTORE) is used on purpose: it builds a NEW database from the backup with
# its own DBID and its own file paths, which is exactly a drill - the source keeps running and
# the restored copy cannot be mistaken for it. `DUPLICATE ... BACKUP LOCATION` needs no
# connection to the source instance, so a drill never touches the production primary.
#
# Env: SOURCE_CONTAINER, TARGET_CONTAINER, BACKUP_DIR (path visible inside BOTH containers),
#      RESTORE_ID, ORACLE_SID (default FREE), WALLET_DIR (optional TDE wallet on the host).
# Exit: 0 on success. Prints RESULT=ok only when the restored database is open and queried.
set -u

source_container="${SOURCE_CONTAINER:-}"
target_container="${TARGET_CONTAINER:-}"
backup_dir="${BACKUP_DIR:-}"
restore_id="${RESTORE_ID:-oracle-restore}"
oracle_sid="${ORACLE_SID:-FREE}"
wallet_dir="${WALLET_DIR:-}"

die() { printf 'RESULT=error phase=%s reason=%s\n' "${PHASE:-init}" "$1" >&2; exit 1; }
phase() { PHASE="$1"; printf '\nPHASE=%s %s\n' "$1" "${2:-}"; }

# When this run began, so the verify step can prove the database it is looking at was produced
# BY THIS RUN. See the freshness check at the end.
run_started_epoch="$(date -u +%s)"

[ -n "$backup_dir" ] || die "BACKUP_DIR is not set."

# TARGET_MODE=native means this script already runs on the machine that will host the database
# (a VM with Oracle installed directly), so there is no container to exec into - the engine
# commands run here, as the Oracle OS user. docker mode execs into TARGET_CONTAINER on this host.
target_mode="${TARGET_MODE:-docker}"
oracle_os_user="${ORACLE_OS_USER:-oracle}"

if [ "$target_mode" = "docker" ]; then
    [ -n "$target_container" ] || die "TARGET_CONTAINER is not set."
    DOCKER="docker"
    $DOCKER info >/dev/null 2>&1 || DOCKER="sudo docker"
    $DOCKER inspect "$target_container" >/dev/null 2>&1 \
        || die "target container '$target_container' not found on host."
    # SOURCE_CONTAINER is empty when restoring onto another machine: the source container lives
    # on a different host, so it is neither present here nor something to guard against.
    if [ -n "$source_container" ]; then
        [ "$source_container" != "$target_container" ] \
            || die "target is the source container; a restore must not overwrite what it validates."
        $DOCKER inspect "$source_container" >/dev/null 2>&1 \
            || die "source container '$source_container' not found on host."
    fi
fi

# No -i, stdin closed: this script is fed to `bash -s` over SSH, so a `docker exec -i` without
# its own input would eat the rest of the script.
# The staged backup has to be visible *inside* the target container. Historically that was
# arranged by giving the target a bind mount at the same path — which quietly restricts the
# restore to containers built for the purpose. Any running container can be a target if the
# staged directory is simply copied in, so when BACKUP_DIR is missing inside the container but
# present on the host (where sync_backup_dir put it), copy it across. A container that already
# has the path mounted is left alone, so the mounted setup keeps working unchanged.
# Is backup_dir served by a real mount inside the container? Matching /proc/mounts against
# backup_dir alone was wrong: the bind mount is usually a PARENT ("/opt/oracle/backup" holding
# "/opt/oracle/backup/dbops"), so an exact match never fired, the script concluded there was no
# mount, and every drill died at init. Walk up instead. Stopping before "/" is deliberate - the
# container rootfs is itself a mount, and matching it would call every path live, bringing back
# the stale-copy bug this staging logic exists to prevent.
inside_a_mount() {
    $DOCKER exec "$target_container" sh -c '
        dir="$1"
        while [ "$dir" != "/" ] && [ -n "$dir" ]; do
            grep -q " $dir " /proc/mounts && exit 0
            dir=$(dirname "$dir")
        done
        exit 1
    ' _ "$backup_dir" </dev/null 2>/dev/null
}

stage_into_target() {
    [ "$target_mode" = "docker" ] || return 0
    # A path that is a real mount inside the container IS the host directory - already in
    # sync, nothing to copy.
    # A path served by a mount IS the host directory - live by construction, nothing to copy,
    # and nothing to delete either.
    if inside_a_mount; then
        return 0
    fi
    # A previous run's copy must be replaced, not kept. Skipping the copy because the
    # directory "already exists" silently pins the restore to whatever was staged the first
    # time: pieces deleted at the source stayed here and were still picked up by RMAN hours
    # later, which is exactly how a restore ends up reading a backup nobody can decrypt.
    # Guarded: inside_a_mount already returned above, so this can only be a previous run's
    # copy. Re-checking costs nothing and stops a future edit turning this into an rm -rf of a
    # read-write bind mount - that is, of the source backups themselves.
    if inside_a_mount; then
        die "refusing to delete '${backup_dir}': it is served by a mount inside '${target_container}'."
    fi
    $DOCKER exec -u 0 "$target_container" rm -rf "$backup_dir" </dev/null 2>/dev/null || true
    [ -d "$backup_dir" ] || die "backup dir '${backup_dir}' is not served by a mount inside '${target_container}', and does not exist on this host either - nothing to stage."
    printf 'staging %s into %s (no bind mount on the target)
' "$backup_dir" "$target_container"
    # -u 0: the database user owns its own directories but not /, and the staging path sits
    # outside them. The copied tree is then handed to the database user, which has to *read*
    # the pieces during the restore.
    $DOCKER exec -u 0 "$target_container" mkdir -p "$(dirname "$backup_dir")" </dev/null         || die "cannot create $(dirname "$backup_dir") inside '${target_container}'."
    $DOCKER cp "$backup_dir" "${target_container}:${backup_dir}"         || die "docker cp of '${backup_dir}' into '${target_container}' failed."
    $DOCKER exec -u 0 "$target_container" chmod -R a+rX "$backup_dir" </dev/null         || die "cannot make '${backup_dir}' readable inside '${target_container}'."
}
stage_into_target

in_target() {
    if [ "$target_mode" = "native" ]; then
        su - "$oracle_os_user" -c "$1" </dev/null
    else
        $DOCKER exec "$target_container" bash -lc "$1" </dev/null
    fi
}
sql_target() {
    if [ "$target_mode" = "native" ]; then
        printf '%s\nexit\n' "$1" | su - "$oracle_os_user" -c 'sqlplus -s -L / as sysdba 2>&1'
    else
        printf '%s\nexit\n' "$1" | $DOCKER exec -i "$target_container" bash -lc 'sqlplus -s -L / as sysdba 2>&1'
    fi
}
# DUPLICATE ... BACKUP LOCATION restores INTO this instance, so RMAN must connect to it as the
# AUXILIARY. Connecting as `target` instead fails with RMAN-06174 ("not connected to auxiliary
# database"): `target` names the database being copied FROM, and a backup-based duplicate has no
# source connection at all - the backup pieces are the source.
rman_aux() { printf '%s\nexit\n' "$1" | $DOCKER exec -i "$target_container" bash -lc 'rman auxiliary / log /dev/stdout 2>&1'; }

# --------------------------------------------------------------------------------------------
phase restore-key "wallet/keystore for the target"
# Nothing to import when the backups are not encrypted - but the step is explicit rather than
# skipped silently, because "no key needed" and "key forgotten" look identical in a log otherwise.
if [ -n "$wallet_dir" ]; then
    in_target "mkdir -p /opt/oracle/admin/${oracle_sid}/wallet" \
        || die "could not create the wallet directory in the target."
    $DOCKER cp "$wallet_dir/." "$target_container:/opt/oracle/admin/${oracle_sid}/wallet/" \
        || die "could not copy the TDE wallet into the target."
    printf 'wallet_copied_from=%s\n' "$wallet_dir"
else
    printf 'no WALLET_DIR configured; backups are not encrypted, nothing to import\n'
fi

# --------------------------------------------------------------------------------------------
phase copy-backup "make the backup pieces readable by the target"
# The backup directory is a host bind mount shared with the target container, so there is
# nothing to copy - but it must be verified, not assumed: an empty mount here is the difference
# between a restore drill and a false pass.
pieces="$(in_target "ls -1 '${backup_dir}' 2>/dev/null | wc -l" | tr -d '\r')"
[ "${pieces:-0}" -gt 0 ] || die "no backup pieces visible at ${backup_dir} inside ${target_container}."
newest="$(in_target "ls -1t '${backup_dir}' | head -1" | tr -d '\r')"
printf 'backup_pieces=%s newest=%s\n' "$pieces" "$newest"

autobackup="$(in_target "ls -1 '${backup_dir}'/autobackup_* 2>/dev/null | tail -1" | tr -d '\r')"
[ -n "$autobackup" ] || die "no controlfile autobackup found in ${backup_dir}; DUPLICATE cannot start."
printf 'controlfile_autobackup=%s\n' "$autobackup"

# The recovery chain is reported piece by piece, not assumed. DUPLICATE silently restores to
# whatever point the pieces present allow, so a set missing its incrementals or its archived
# logs still "succeeds" - just at an older point than the operator believes. Counting them here
# makes an incomplete chain visible before the restore rather than after.
n_l0=$(in_target "ls -1 '${backup_dir}'/*_L0_*.bkp 2>/dev/null | wc -l" | tr -d '\r')
n_l1=$(in_target "ls -1 '${backup_dir}'/*_L1_*.bkp 2>/dev/null | wc -l" | tr -d '\r')
n_arch=$(in_target "ls -1 '${backup_dir}'/arch_*.bkp 2>/dev/null | wc -l" | tr -d '\r')
n_ctl=$(in_target "ls -1 '${backup_dir}'/ctl_*.bkp 2>/dev/null | wc -l" | tr -d '\r')
n_spf=$(in_target "ls -1 '${backup_dir}'/spfile_*.bkp 2>/dev/null | wc -l" | tr -d '\r')
printf 'chain full_L0=%s incremental_L1=%s archivelog=%s controlfile=%s spfile=%s\n' \
    "${n_l0:-0}" "${n_l1:-0}" "${n_arch:-0}" "${n_ctl:-0}" "${n_spf:-0}"

# A level 0 is the only piece without which nothing can be rebuilt at all.
[ "${n_l0:-0}" -gt 0 ] \
    || die "no level 0 (full) backup in ${backup_dir}; the incrementals have nothing to build on."
[ "${n_arch:-0}" -gt 0 ] \
    || printf 'warning: no archivelog backups present - the copy can only reach the last datafile backup\n' >&2

# Listable is not readable. The pieces are mounted from the host, so they can carry host
# ownership the database user has no access to (0640 root:root is enough to make RMAN report
# "CONTROLFILE backup not found" while `ls` still shows every file). Read one as the DB user,
# because a permission problem must fail here with a clear reason, not inside RMAN as a
# misleading "not found".
$DOCKER exec -u "${ORACLE_OS_USER:-oracle}" "$target_container" \
    bash -lc "head -c 512 '${autobackup}' >/dev/null" </dev/null \
    || die "backup pieces are not readable by ${ORACLE_OS_USER:-oracle} in ${target_container}: check ownership/permissions on the host backup directory."
printf 'pieces_readable_by=%s\n' "${ORACLE_OS_USER:-oracle}"

# --------------------------------------------------------------------------------------------
phase restore-data "RMAN DUPLICATE into ${target_container}"
# The target must be in NOMOUNT for DUPLICATE to build its controlfile.
#
# ABORT, not IMMEDIATE. The next statement rebuilds this database from the backup set, so there is
# nothing here worth closing cleanly - and IMMEDIATE is not bounded: it waits for the PDBs to close,
# which on 2026-08-05 stopped at `alter pluggable database all close immediate` and never returned.
# The `|| true` below does not help, because it handles a shutdown that FAILS, not one that HANGS:
# the script simply sat there. Nothing else bounds it either - the entry's time_window.timeout marks
# the job_runs row, it does not kill the shell - so the drill hung until an operator noticed, which
# for a 02:00 schedule means the whole night. A restore target is the one instance where a graceful
# shutdown buys nothing at all, so this trades it for a shutdown that always terminates.
printf 'shutting the target instance down to NOMOUNT (abort - it is about to be overwritten)\n'
sql_target "shutdown abort;" >/dev/null 2>&1 || true
startup_out="$(sql_target "startup nomount;")"
printf '%s\n' "$startup_out" | grep -qiE 'ORACLE instance started|instance started' \
    || { printf '%s\n' "$startup_out"; die "target instance would not start NOMOUNT."; }

# The backups are AES-encrypted by the backup job when a passphrase is configured, and RMAN
# needs the same passphrase to read them. Without this line the DUPLICATE fails deep inside
# with ORA-19913 ("unable to decrypt backup") rather than saying a key is missing, so the
# passphrase is stated up front and its absence is left explicit in the log.
decrypt_line=""
if [ -n "${BACKUP_ENCRYPTION_PASSWORD:-}" ]; then
    case "$BACKUP_ENCRYPTION_PASSWORD" in
        *"'"*) die "BACKUP_ENCRYPTION_PASSWORD must not contain a single quote (it is passed to RMAN quoted)." ;;
    esac
    decrypt_line="SET DECRYPTION IDENTIFIED BY '${BACKUP_ENCRYPTION_PASSWORD}';"
    printf 'backup decryption: passphrase supplied\n'
else
    printf 'backup decryption: none configured (restoring unencrypted pieces)\n'
fi

# SET DECRYPTION must be issued OUTSIDE a RUN block - inside one, RMAN rejects it with
# RMAN-03032 ("this option of set command needs to be used outside of a run block"). Verified
# on 23.26.2; the tempting fix of wrapping both in RUN{} does not work.
dup_out="$(rman_aux "
${decrypt_line}
DUPLICATE DATABASE TO ${oracle_sid}
  BACKUP LOCATION '${backup_dir}'
  NOFILENAMECHECK;
")"
printf '%s\n' "$dup_out" | tail -40

if printf '%s\n' "$dup_out" | grep -qE 'RMAN-[0-9]{5}|ORA-[0-9]{5}'; then
    # DUPLICATE prints a final success banner; an error stack without it is a real failure.
    printf '%s\n' "$dup_out" | grep -qi 'Finished Duplicate Db' \
        || die "RMAN DUPLICATE failed; see the RMAN output above."
fi

# --------------------------------------------------------------------------------------------
phase verify "the restored database must be open and queryable"
open_mode="$(sql_target "set heading off feedback off
select open_mode||'|'||name||'|'||to_char(dbid) from v\$database;" | tr -d '\r' | grep '|' | head -1)"
printf 'restored=%s\n' "$open_mode"
printf '%s' "$open_mode" | grep -qi 'READ WRITE' \
    || die "restored database is not open READ WRITE (got: ${open_mode:-nothing})."

count="$(sql_target "set heading off feedback off
select 'objects='||count(*) from dba_objects;" | tr -d '\r' | grep 'objects=' | head -1)"
printf 'sanity_query %s\n' "${count:-objects=unknown}"
printf '%s' "$count" | grep -qE 'objects=[1-9]' \
    || die "restored database returned no objects; the restore is not usable."

# --------------------------------------------------------------------------------------------
# FRESHNESS. Everything above is satisfied just as well by *yesterday's* restored database still
# running: open READ WRITE with a full dictionary is what the target looks like between drills.
# So a DUPLICATE that silently did nothing would still have printed RESULT=ok, and the drill
# would have reported success every day while proving nothing about today's backups. RMAN
# DUPLICATE ends in OPEN RESETLOGS, so resetlogs_time is the moment this run created the
# database; requiring it to fall inside this run's own window is what ties the verdict to the work.
#
# The arithmetic is done inside Oracle against SYSDATE, never against the host clock: the
# container's timezone is not the host's, and comparing the two silently drifted the check by
# whole hours. Only the elapsed seconds - a duration, which no timezone changes - crosses over.
elapsed_seconds=$(( $(date -u +%s) - run_started_epoch ))
allowed_minutes=$(( elapsed_seconds / 60 + 15 ))
fresh="$(sql_target "set heading off feedback off
select case when (sysdate - resetlogs_time) * 24 * 60 <= ${allowed_minutes}
            then 'FRESH' else 'STALE' end
    ||'|age_minutes='||to_char(round((sysdate - resetlogs_time) * 24 * 60))
    ||'|allowed_minutes=${allowed_minutes}'
from v\$database;" | tr -d '\r' | grep -E 'FRESH|STALE' | head -1)"
printf 'freshness %s\n' "${fresh:-unknown}"
printf '%s' "$fresh" | grep -q '^FRESH' || die \
    "the target database was NOT created by this run (${fresh:-no resetlogs_time}); RMAN DUPLICATE \
did not take effect and the database open here is a leftover from an earlier restore."

printf '\nRESULT=ok restore_id=%s target=%s %s %s %s\n' \
    "$restore_id" "$target_container" "$open_mode" "$count" "$fresh"
