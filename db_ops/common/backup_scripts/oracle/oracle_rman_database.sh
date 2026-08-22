#!/bin/bash
# Oracle RMAN database backup - one daily job that picks its own level.
#
# Sunday (ISO weekday 7) takes the level 0 baseline; every other day is a level 1
# incremental against it. The weekday lives here rather than in the scheduler because
# db_ops time windows address day-of-month, not day-of-week: expressing "Sunday" would
# have meant adding a weekday dimension to the TimeWindow shared by all four apps. One
# daily entry that decides its own level keeps the schedule readable and the shared
# scheduling contract untouched.
#
# Runs on the container host (shipped over SSH by db_ops.backup_restore.backup) and
# `docker exec`s into $DOCKER_CONTAINER, mirroring the metrics docker collector.
#
# Retention is a recovery window: DELETE OBSOLETE removes only what is no longer needed
# to restore to any point inside $RETENTION_DAYS. It is never a blind "delete older than",
# which would happily drop the level 0 that the newer level 1s depend on.
#
# Env: DOCKER_CONTAINER (required), BACKUP_DIR (required, path inside the container),
#      RETENTION_DAYS (default 14), BACKUP_LEVEL (optional 0|1 override for manual runs).
# Exit: 0 on success, non-zero on failure (the runner records status from this).
set -u

container="${DOCKER_CONTAINER:-}"
backup_dir="${BACKUP_DIR:-}"
retention_days="${RETENTION_DAYS:-14}"
level_override="${BACKUP_LEVEL:-}"

die() { printf 'RESULT=error reason=%s\n' "$1" >&2; exit 1; }

[ -n "$container" ]  || die "DOCKER_CONTAINER is not set."
[ -n "$backup_dir" ] || die "BACKUP_DIR is not set."

case "$retention_days" in
    ''|*[!0-9]*) die "RETENTION_DAYS must be a whole number of days: '${retention_days}'." ;;
esac

if [ -n "$level_override" ]; then
    case "$level_override" in
        0|1) level="$level_override" ;;
        *) die "BACKUP_LEVEL must be 0 or 1: '${level_override}'." ;;
    esac
elif [ "$(date +%u)" = "7" ]; then
    level=0
else
    level=1
fi

# docker CLI: plain first, fall back to sudo (host user may not be in the docker group).
DOCKER="docker"
$DOCKER info >/dev/null 2>&1 || DOCKER="sudo docker"
$DOCKER inspect "$container" >/dev/null 2>&1 \
    || die "container '${container}' not found or docker unavailable on host."

# No -i, and stdin closed: this script is fed to `bash -s` over SSH, so a `docker exec -i`
# without its own input would read the *rest of this file* as the container's stdin and the
# shell would silently run out of script (exit 0, no output, nothing backed up). The RMAN call
# below may use -i because its stdin is the pipe from printf, not the script.
$DOCKER exec "$container" bash -lc "mkdir -p '${backup_dir}'" </dev/null \
    || die "could not create BACKUP_DIR '${backup_dir}' inside '${container}'."

printf 'backup_level=%s container=%s backup_dir=%s retention_days=%s\n' \
    "$level" "$container" "$backup_dir" "$retention_days"

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

RMAN_IN="$(cat <<RMANEOF
${enc_line}
CONFIGURE RETENTION POLICY TO RECOVERY WINDOW OF ${retention_days} DAYS;
CONFIGURE CONTROLFILE AUTOBACKUP ON;
CONFIGURE CONTROLFILE AUTOBACKUP FORMAT FOR DEVICE TYPE DISK TO '${backup_dir}/autobackup_%F';
CONFIGURE ARCHIVELOG DELETION POLICY TO BACKED UP 1 TIMES TO DISK;
RUN {
  ALLOCATE CHANNEL c1 DEVICE TYPE DISK FORMAT '${backup_dir}/%d_L${level}_%T_%U.bkp';
  BACKUP INCREMENTAL LEVEL ${level} DATABASE TAG 'DBOPS_L${level}';
  BACKUP CURRENT CONTROLFILE TAG 'DBOPS_CTL';
  RELEASE CHANNEL c1;
}
CROSSCHECK BACKUP;
DELETE NOPROMPT EXPIRED BACKUP;
DELETE NOPROMPT OBSOLETE;
EXIT;
RMANEOF
)"

rman_out="$(printf '%s\n' "$RMAN_IN" | $DOCKER exec -i "$container" bash -lc 'rman target / log /dev/stdout 2>&1')"
rman_rc=$?

printf '%s\n' "$rman_out"

# RMAN exits 0 for some partial failures, so the output is checked too: RMAN-03009 is a
# failed command inside RUN{}, RMAN-00569 heads the error stack.
if [ "$rman_rc" -ne 0 ] || printf '%s\n' "$rman_out" | grep -qE 'RMAN-00569|RMAN-03009|ORA-19809|ORA-00257'; then
    printf 'RESULT=error backup_level=%s rman_rc=%s\n' "$level" "$rman_rc" >&2
    exit 1
fi

# Keep the pieces readable to the host's SSH user, for the same reason the PostgreSQL backup
# does: a cross-machine restore reads this directory over SFTP as an ordinary account that
# neither owns the files nor shares their group, and RMAN writes them 0640. Re-applied on every
# run because each run creates new pieces - a one-off chmod stops being true within minutes.
$DOCKER exec "$container" bash -lc "chmod -R a+rX '${backup_dir}'" </dev/null     || printf 'warning: could not relax permissions on %s
' "${backup_dir}" >&2

printf 'RESULT=ok backup_level=%s retention_days=%s\n' "$level" "$retention_days"
