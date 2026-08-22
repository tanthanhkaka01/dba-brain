#!/bin/bash
# PostgreSQL base backup - one daily job that picks its own level, mirroring the Oracle RMAN job.
#
# Sunday (ISO weekday 7) takes the full baseline; every other day is an incremental against the
# most recent backup (PostgreSQL 17+ `pg_basebackup --incremental`). The weekday lives here
# rather than in the scheduler because db_ops time windows address day-of-month, not day-of-week.
#
# Runs on the container host (shipped over SSH by db_ops.backup_restore.backup) and `docker exec`s
# into $DOCKER_CONTAINER.
#
# Layout, one directory per backup:
#   $BACKUP_DIR/base/<UTC timestamp>_FULL      full baseline
#   $BACKUP_DIR/base/<UTC timestamp>_INCR      incremental, chained to the previous backup
#
# Retention deletes whole *chains*, never single backups: an incremental is worthless without the
# full it descends from, so a chain is dropped only once its newest member is older than
# $RETENTION_DAYS. Deleting by age alone would happily remove the full that the retained
# incrementals still need - a backup set that looks present and cannot be restored.
#
# Env: DOCKER_CONTAINER (required), BACKUP_DIR (required, path inside the container),
#      RETENTION_DAYS (default 14), BACKUP_LEVEL (optional full|incr override for manual runs),
#      PG_USER (default postgres), PG_OS_USER (OS user inside the container, default postgres).
# Exit: 0 on success, non-zero on failure. Prints RESULT=ok only on a completed run.
set -u

container="${DOCKER_CONTAINER:-}"
backup_dir="${BACKUP_DIR:-}"
retention_days="${RETENTION_DAYS:-14}"
level_override="${BACKUP_LEVEL:-}"
pg_user="${PG_USER:-postgres}"
pg_os_user="${PG_OS_USER:-postgres}"

die() { printf 'RESULT=error reason=%s\n' "$1" >&2; exit 1; }

[ -n "$container" ]  || die "DOCKER_CONTAINER is not set."
[ -n "$backup_dir" ] || die "BACKUP_DIR is not set."
case "$retention_days" in
    ''|*[!0-9]*) die "RETENTION_DAYS must be a whole number of days: '${retention_days}'." ;;
esac

# docker CLI: plain first, fall back to sudo (host user may not be in the docker group).
DOCKER="docker"
$DOCKER info >/dev/null 2>&1 || DOCKER="sudo docker"
$DOCKER inspect "$container" >/dev/null 2>&1 \
    || die "container '${container}' not found or docker unavailable on host."

# -u $pg_os_user, not root. `docker exec` on the postgres image lands as root, and a backup
# taken that way is written root-owned with 0700 - unreadable to the postgres user, so
# pg_combinebackup and pg_verifybackup fail and PostgreSQL refuses to start on the restored
# directory. The backup would look present and be unrestorable.
#
# No -i, and stdin closed: this script is fed to `bash -s` over SSH, so a `docker exec -i`
# without its own input would read the rest of this file as the container's stdin and the shell
# would silently run out of script (exit 0, no output, nothing backed up).
in_container() { $DOCKER exec -u "$pg_os_user" "$container" bash -lc "$1" </dev/null; }

base_dir="${backup_dir}/base"
in_container "mkdir -p '${base_dir}'" || die "could not create '${base_dir}' inside '${container}'."

# Refuse to back up a standby: an incremental chain taken from a replica silently diverges from
# the primary's timeline after a failover.
in_recovery="$(in_container "psql -U '${pg_user}' -tAc 'select pg_is_in_recovery()'" 2>/dev/null | tr -d '[:space:]')"
[ "$in_recovery" = "f" ] || die "target is not a primary (pg_is_in_recovery=${in_recovery:-unknown})."

# The newest existing backup is what an incremental chains onto.
latest="$(in_container "ls -1d ${base_dir}/*_FULL ${base_dir}/*_INCR 2>/dev/null | sort | tail -1" | tr -d '\r')"

if [ -n "$level_override" ]; then
    case "$level_override" in
        full|FULL|0) level="FULL" ;;
        incr|INCR|1) level="INCR" ;;
        *) die "BACKUP_LEVEL must be full or incr: '${level_override}'." ;;
    esac
elif [ "$(date +%u)" = "7" ]; then
    level="FULL"
else
    level="INCR"
fi

# An incremental with nothing to chain onto is not an error to fail on - it is the first ever
# run (or the first after a retention sweep). Take the baseline instead of failing every day
# until the next Sunday.
if [ "$level" = "INCR" ] && [ -z "$latest" ]; then
    printf 'no prior backup found; taking a FULL baseline instead of an incremental\n'
    level="FULL"
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="${base_dir}/${stamp}_${level}"

printf 'backup_level=%s container=%s target=%s retention_days=%s\n' \
    "$level" "$container" "$target" "$retention_days"

# -X stream: ship the WAL generated during the copy inside the backup, so each backup is
# restorable on its own without reaching into the WAL archive.
# -c fast: checkpoint immediately instead of waiting for the next scheduled one.
if [ "$level" = "FULL" ]; then
    bb_args="-D '${target}' -X stream -c fast --manifest-checksums=CRC32C"
else
    printf 'incremental_parent=%s\n' "$latest"
    bb_args="-D '${target}' -X stream -c fast --manifest-checksums=CRC32C --incremental='${latest}/backup_manifest'"
fi

if ! in_container "pg_basebackup -U '${pg_user}' ${bb_args} 2>&1"; then
    in_container "rm -rf '${target}'" || true
    die "pg_basebackup failed (level=${level}); partial target removed."
fi

in_container "test -f '${target}/backup_manifest'" \
    || die "pg_basebackup left no backup_manifest in '${target}'."

# --- Retention: drop whole chains, newest-member-first ---------------------------------------
# A chain runs from a _FULL up to (not including) the next _FULL. Keep the chain if any member
# is within the window; the directory names sort chronologically, so one pass is enough.
cleanup=$(cat <<CLEANUP
set -u
cutoff=\$(date -u -d "${retention_days} days ago" +%Y%m%dT%H%M%SZ 2>/dev/null) || exit 0
chain=""
newest=""
drop_chain() {
    [ -n "\$chain" ] || return 0
    if [ "\$newest" \< "\$cutoff" ]; then
        for d in \$chain; do
            echo "retention: removing \$d"
            rm -rf "\$d"
        done
    fi
}
for d in \$(ls -1d ${base_dir}/*_FULL ${base_dir}/*_INCR 2>/dev/null | sort); do
    case "\$d" in
        *_FULL) drop_chain; chain="\$d"; newest=\$(basename "\$d" | cut -d_ -f1) ;;
        *_INCR) chain="\$chain \$d"; newest=\$(basename "\$d" | cut -d_ -f1) ;;
    esac
done
drop_chain
CLEANUP
)
in_container "$cleanup" || printf 'warning: retention sweep reported an error\n' >&2

# Make the pieces readable to the host's SSH user. They are written 0600 by the database user,
# which is right for a local backup but blocks the cross-machine restore: the transfer reads the
# source over SFTP as an ordinary account that is neither the owner nor in its group, so a
# freshly written file is unreadable and the copy fails part-way with "Permission denied".
# Group ownership cannot fix it (the group is the database user's own), so this opens read to
# others. That is a deliberate trade: these are lab backups on a private host, and the
# alternative is a transfer that breaks every time the job writes a new file.
in_container "chmod -R a+rX '${backup_dir}'"     || printf 'warning: could not relax permissions on %s
' "${backup_dir}" >&2

size="$(in_container "du -sh '${target}' 2>/dev/null | cut -f1" | tr -d '\r')"
printf 'RESULT=ok backup_level=%s target=%s size=%s retention_days=%s\n' \
    "$level" "$target" "${size:-unknown}" "$retention_days"
