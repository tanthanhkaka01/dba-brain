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
# Env: DOCKER_CONTAINER (required), BACKUP_DIR (required, path inside the container),
#      RETENTION_DAYS (default 7), PG_USER (default postgres),
#      PG_OS_USER (OS user inside the container, default postgres).
# Exit: 0 on success, non-zero on failure. Prints RESULT=ok only on a completed run.
set -u

container="${DOCKER_CONTAINER:-}"
backup_dir="${BACKUP_DIR:-}"
retention_days="${RETENTION_DAYS:-7}"
pg_user="${PG_USER:-postgres}"
pg_os_user="${PG_OS_USER:-postgres}"

die() { printf 'RESULT=error reason=%s\n' "$1" >&2; exit 1; }

[ -n "$container" ]  || die "DOCKER_CONTAINER is not set."
[ -n "$backup_dir" ] || die "BACKUP_DIR is not set."
case "$retention_days" in
    ''|*[!0-9]*) die "RETENTION_DAYS must be a whole number of days: '${retention_days}'." ;;
esac

DOCKER="docker"
$DOCKER info >/dev/null 2>&1 || DOCKER="sudo docker"
$DOCKER inspect "$container" >/dev/null 2>&1 \
    || die "container '${container}' not found or docker unavailable on host."

# -u $pg_os_user and stdin closed - see the notes in pg_basebackup_database.sh.
in_container() { $DOCKER exec -u "$pg_os_user" "$container" bash -lc "$1" </dev/null; }
psql_1() { in_container "psql -U '${pg_user}' -tAc \"$1\"" 2>/dev/null | tr -d '\r' | tr -d '[:space:]'; }

wal_dir="${backup_dir}/wal"
base_dir="${backup_dir}/base"
in_container "mkdir -p '${wal_dir}'" || die "could not create '${wal_dir}' inside '${container}'."

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
    err="$(psql_1 "select coalesce(last_failed_time::text,'') from pg_stat_archiver")"
    die "WAL archiving is failing: last_failed_wal=${failed} newer than last_archived_wal=${archived:-none} at ${err}."
fi

# --- 3. Remove WAL no retained base backup needs ------------------------------------------------
# The oldest base backup's START WAL is the cut line. pg_archivecleanup deletes everything
# strictly older and nothing that is still required.
oldest_base="$(in_container "ls -1d ${base_dir}/*_FULL 2>/dev/null | sort | head -1" | tr -d '\r')"
if [ -n "$oldest_base" ]; then
    start_wal="$(in_container "grep -m1 -o '[0-9A-F]\{24\}' '${oldest_base}/backup_label' 2>/dev/null" | tr -d '\r')"
    if [ -n "$start_wal" ]; then
        printf 'wal_cleanup_floor=%s from=%s\n' "$start_wal" "$oldest_base"
        in_container "pg_archivecleanup '${wal_dir}' '${start_wal}' 2>&1" \
            || printf 'warning: pg_archivecleanup reported an error\n' >&2
    else
        printf 'skipping WAL cleanup: no START WAL in %s/backup_label\n' "$oldest_base"
    fi
else
    # Nothing to protect yet, but do not let the archive grow without bound while the first
    # base backup has not run: fall back to the age floor.
    printf 'no base backup yet; falling back to age-based WAL cleanup (%s days)\n' "$retention_days"
    in_container "find '${wal_dir}' -type f -mtime +${retention_days} -delete" \
        || printf 'warning: age-based WAL cleanup reported an error\n' >&2
fi

# Make the pieces readable to the host's SSH user. They are written 0600 by the database user,
# which is right for a local backup but blocks the cross-machine restore: the transfer reads the
# source over SFTP as an ordinary account that is neither the owner nor in its group, so a
# freshly written file is unreadable and the copy fails part-way with "Permission denied".
# Group ownership cannot fix it (the group is the database user's own), so this opens read to
# others. That is a deliberate trade: these are lab backups on a private host, and the
# alternative is a transfer that breaks every time the job writes a new file.
in_container "chmod -R a+rX '${backup_dir}'"     || printf 'warning: could not relax permissions on %s
' "${backup_dir}" >&2

wal_count="$(in_container "ls -1 '${wal_dir}' 2>/dev/null | wc -l" | tr -d '\r')"
printf 'RESULT=ok archived_wal=%s wal_files_kept=%s retention_days=%s\n' \
    "${archived:-none}" "${wal_count:-0}" "$retention_days"
