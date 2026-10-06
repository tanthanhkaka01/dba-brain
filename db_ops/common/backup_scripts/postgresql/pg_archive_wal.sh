#!/bin/bash
# PostgreSQL WAL archiving check + cleanup - the analogue of the Oracle archivelog job.
#
# PostgreSQL archives WAL itself (archive_mode/archive_command), so this job does not copy WAL.
# It does the three things that decide whether that archive is actually worth anything:
#
#   1. Forces a WAL switch, so the segment currently being written is archived instead of
#      sitting on the primary until it happens to fill. This is what bounds data loss to the
#      job interval - without it a quiet database can leave the last change unarchived for hours.
#   2. Fails when the archiver is failing. A broken archive_command leaves the database running
#      happily while recoverability silently rots; this is the check that turns that into an alert.
#   3. Removes WAL the retained base backups no longer need.
#
# Cleanup is driven by the OLDEST retained base backup, not by age alone. WAL older than that
# backup's start is useless; WAL newer than it is required to restore it, however old it looks.
# Deleting by age would leave the oldest base backups present but unrestorable - so
# $RETENTION_DAYS acts as a floor here, never as permission to break a chain.
#
# Env: DOCKER_CONTAINER (OPTIONAL since 0.21.0 - unset means the cluster is on this host), BACKUP_DIR (required, path inside the container),
#      RETENTION_DAYS (default 7), PG_USER (default postgres),
#      PG_OS_USER (OS user inside the container, default postgres).
# Exit: 0 on success, non-zero on failure. Prints RESULT=ok only on a completed run.
set -u

container="${DOCKER_CONTAINER:-}"
backup_dir="${BACKUP_DIR:-}"
retention_days="${RETENTION_DAYS:-7}"
retention_seconds="${RETENTION_SECONDS:-}"
pg_user="${PG_USER:-postgres}"
pg_os_user="${PG_OS_USER:-postgres}"

die() { printf 'RESULT=error reason=%s\n' "$1" >&2; exit 1; }

[ -n "$backup_dir" ] || die "BACKUP_DIR is not set."
case "$retention_days" in
    ''|*[!0-9]*) die "RETENTION_DAYS must be a whole number of days: '${retention_days}'." ;;
esac
# A window under a day arrives in seconds (see pg_basebackup_database.sh). It only matters to the
# fallback below: with a base backup present the cut line is that backup's START WAL, not an age.
age_test="-mtime +${retention_days}"
if [ -n "$retention_seconds" ]; then
    case "$retention_seconds" in
        *[!0-9]*) die "RETENTION_SECONDS must be a whole number of seconds: '${retention_seconds}'." ;;
    esac
    [ "$retention_seconds" -ge 60 ] \
        || die "RETENTION_SECONDS must be at least 60: '${retention_seconds}'."
    age_test="-mmin +$(( retention_seconds / 60 ))"
fi

# $DOCKER_CONTAINER is optional since 0.21.0, exactly as in pg_basebackup_database.sh: set it for a
# cluster inside a container, leave it unset for one on the host. Chosen once, here, so nothing below
# can be written for one of the two by accident.
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

    # -u $pg_os_user and stdin closed - see the notes in pg_basebackup_database.sh.
    run_db() { $DOCKER exec -u "$pg_os_user" "$container" bash -lc "$1" </dev/null; }
    where="container ${container}"
else
    command -v psql >/dev/null 2>&1 \
        || die "psql is not on PATH and DOCKER_CONTAINER is not set, so there is no way to reach a cluster. Set DOCKER_CONTAINER for a containerised cluster, or install the PostgreSQL client tools on this host."
    if [ "$(id -un 2>/dev/null)" = "$pg_os_user" ]; then
        run_db() { bash -lc "$1" </dev/null; }
        where="host as ${pg_os_user}"
    else
        id "$pg_os_user" >/dev/null 2>&1 \
            || die "PG_OS_USER '${pg_os_user}' does not exist on this host."
        command -v su >/dev/null 2>&1 || die "su is not available, so this cannot drop to '${pg_os_user}'."
        run_db() { su -s /bin/bash "$pg_os_user" -c "$1" </dev/null; }
        where="host as ${pg_os_user} via su"
    fi
fi
printf 'reaching the cluster: %s\n' "$where"

psql_1() { run_db "psql -U '${pg_user}' -tAc \"$1\"" 2>/dev/null | tr -d '\r' | tr -d '[:space:]'; }

wal_dir="${backup_dir}/wal"
base_dir="${backup_dir}/base"
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
if ! run_db "mkdir -p '${wal_dir}' && test -w '${wal_dir}'" 2>/dev/null; then
    engine_uid="$(run_db 'id -u' 2>/dev/null)"; engine_gid="$(run_db 'id -g' 2>/dev/null)"
    [ -n "$engine_uid" ] && run_root "mkdir -p '${backup_dir}' && chown -R ${engine_uid}:${engine_gid} '${backup_dir}'" >/dev/null 2>&1
fi

run_db "mkdir -p '${wal_dir}'" || die "could not create '${wal_dir}' (${where})."

archive_mode="$(psql_1 "select current_setting('archive_mode')")"
[ "$archive_mode" = "on" ] || [ "$archive_mode" = "always" ] \
    || die "archive_mode is '${archive_mode:-unknown}'; WAL is not being archived."

in_recovery="$(psql_1 "select pg_is_in_recovery()")"
[ "$in_recovery" = "f" ] || die "target is not a primary (pg_is_in_recovery=${in_recovery:-unknown})."

# --- 1. Switch WAL so the current segment gets archived ---------------------------------------
# Only when something has been written since the last switch, so a quiet database does not
# accumulate a forced segment every interval.
before="$(psql_1 "select pg_walfile_name(pg_current_wal_lsn())")"
switched="$(psql_1 "select pg_walfile_name(pg_switch_wal())")"
printf 'wal_before=%s wal_switched=%s\n' "$before" "$switched"

# Give the archiver a moment to pick the segment up before judging it.
sleep 5

# --- 2. Is the archiver healthy? --------------------------------------------------------------
archived="$(psql_1 "select coalesce(last_archived_wal,'')  from pg_stat_archiver")"
failed="$(psql_1   "select coalesce(last_failed_wal,'')    from pg_stat_archiver")"
failed_count="$(psql_1 "select failed_count from pg_stat_archiver")"
printf 'last_archived_wal=%s last_failed_wal=%s failed_count=%s\n' \
    "${archived:-none}" "${failed:-none}" "${failed_count:-0}"

# A failure that is newer than the last success means archiving is broken right now. An older
# failure is history the database has already recovered from.
if [ -n "$failed" ] && { [ -z "$archived" ] || [ "$failed" \> "$archived" ]; }; then
    err="$(psql_1 "select coalesce(to_char(last_failed_time, 'YYYY-MM-DD\"T\"HH24:MI:SSOF'),'') from pg_stat_archiver")"
    since="$(psql_1 "select coalesce(to_char(stats_reset, 'YYYY-MM-DD\"T\"HH24:MI:SSOF'),'') from pg_stat_archiver")"
    command_text="$(psql_1 "select coalesce(current_setting('archive_command', true),'')")"

    # Name the cause, because the three have three different fixes and the counters name none of
    # them: a full disk is the host's, an unwritable directory is the container's uid, and a
    # destination that already exists is the `archive_command` itself. On 2026-09-22 this job
    # reported `last_archived_wal=none failed_count=108` for hours and an operator reading it could
    # not tell which — the real cause was the third, and it does not heal on its own.
    why=""
    if run_db "test -e '${wal_dir}/${failed}'"; then
        size="$(run_db "wc -c < '${wal_dir}/${failed}' 2>/dev/null" | tr -d '[:space:]')"
        why="the destination ${wal_dir}/${failed} ALREADY EXISTS (${size:-unknown} bytes)"
        # 16 MiB is the default segment; anything short is a copy that was cut off mid-write, which
        # is what a disk filling during archiving leaves behind.
        if [ -n "$size" ] && [ "$size" -lt 16777216 ] 2>/dev/null; then
            why="${why} and is SHORT of a 16777216-byte segment, so it was truncated mid-copy"
        fi
        why="${why}. An archive_command of the form 'test ! -f DEST && cp SRC DEST' exits non-zero"
        why="${why} when DEST is present, so the server retries the same segment for ever and never"
        why="${why} advances. Remove or verify that file, and use a command that succeeds when the"
        why="${why} destination is already byte-identical."
    elif ! run_db "test -w '${wal_dir}'"; then
        why="${wal_dir} is not writable by the database user in the archive directory."
    else
        free_kb="$(run_db "df -Pk '${wal_dir}' 2>/dev/null | awk 'NR==2{print \$4}'" | tr -d '[:space:]')"
        why="the destination does not exist and the directory is writable, with ${free_kb:-unknown} KiB free"
        why="${why}; read the server log for what archive_command printed."
    fi

    printf 'archive_command=%s\n' "${command_text:-<unset>}"
    printf 'archiver_stats_since=%s\n' "${since:-unknown}"
    die "WAL archiving is failing: last_failed_wal=${failed} newer than last_archived_wal=${archived:-none} at ${err}. ${why}"
fi

# --- 3. Remove WAL no retained base backup needs ------------------------------------------------
# The oldest base backup's START WAL is the cut line. pg_archivecleanup deletes everything
# strictly older and nothing that is still required.
oldest_base="$(run_db "ls -1d ${base_dir}/*_FULL 2>/dev/null | sort | head -1" | tr -d '\r')"
if [ -n "$oldest_base" ]; then
    start_wal="$(run_db "grep -m1 -o '[0-9A-F]\{24\}' '${oldest_base}/backup_label' 2>/dev/null" | tr -d '\r')"
    if [ -n "$start_wal" ]; then
        printf 'wal_cleanup_floor=%s from=%s\n' "$start_wal" "$oldest_base"
        # The backup-history file each base backup leaves (<segment>.<offset>.backup) is removed by
        # the same cut - without it they stayed for ever: 129 on the lab after three days, and the
        # count below read as WAL growing when only they were (0.27.0 item 1.90). pg_archivecleanup
        # removes them itself from PostgreSQL 17 (-b); before that, the same name-ordered cut by
        # hand, compared without the timeline as the tool compares.
        history_before="$(run_db "ls -1 '${wal_dir}' 2>/dev/null | grep -c '\.backup\$'" | tr -d '[:space:]')"
        if run_db "pg_archivecleanup --help 2>&1 | grep -q -- --clean-backup-history"; then
            run_db "pg_archivecleanup -b '${wal_dir}' '${start_wal}' 2>&1" \
                || printf 'warning: pg_archivecleanup reported an error\n' >&2
        else
            run_db "pg_archivecleanup '${wal_dir}' '${start_wal}' 2>&1" \
                || printf 'warning: pg_archivecleanup reported an error\n' >&2
            run_db "cd '${wal_dir}' && ls -1 | grep '\.backup\$' | awk -v cut='${start_wal}' 'substr(\$0, 9, 16) < substr(cut, 9, 16)' | xargs -r rm -f --" \
                || printf 'warning: removing old backup-history files reported an error\n' >&2
        fi
        history_after="$(run_db "ls -1 '${wal_dir}' 2>/dev/null | grep -c '\.backup\$'" | tr -d '[:space:]')"
        printf 'backup_history_removed=%s backup_history_kept=%s\n' \
            "$(( ${history_before:-0} - ${history_after:-0} ))" "${history_after:-0}"
    else
        printf 'skipping WAL cleanup: no START WAL in %s/backup_label\n' "$oldest_base"
    fi
else
    # Nothing to protect yet, but do not let the archive grow without bound while the first
    # base backup has not run: fall back to the age floor.
    printf 'no base backup yet; falling back to age-based WAL cleanup (%s days)\n' "$retention_days"
    run_db "find '${wal_dir}' -type f ${age_test} -delete" \
        || printf 'warning: age-based WAL cleanup reported an error\n' >&2
fi

# Make the pieces readable to the host's SSH user. They are written 0600 by the database user,
# which is right for a local backup but blocks the cross-machine restore: the transfer reads the
# source over SFTP as an ordinary account that is neither the owner nor in its group, so a
# freshly written file is unreadable and the copy fails part-way with "Permission denied".
# Group ownership cannot fix it (the group is the database user's own), so this opens read to
# others. That is a deliberate trade: these are lab backups on a private host, and the
# alternative is a transfer that breaks every time the job writes a new file.
run_db "chmod -R a+rX '${backup_dir}'"     || printf 'warning: could not relax permissions on %s
' "${backup_dir}" >&2

wal_count="$(run_db "ls -1 '${wal_dir}' 2>/dev/null | wc -l" | tr -d '\r')"
printf 'RESULT=ok archived_wal=%s wal_files_kept=%s retention_days=%s\n' \
    "${archived:-none}" "${wal_count:-0}" "$retention_days"
