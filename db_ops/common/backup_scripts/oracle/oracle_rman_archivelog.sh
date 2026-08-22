#!/bin/bash
# Oracle RMAN archivelog backup - runs on a short interval (every 15 minutes) to keep the
# recovery point close to now and to stop the FRA filling up between database backups.
#
# Runs on the container host (shipped over SSH by db_ops.backup_restore.backup) and
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
# Env: DOCKER_CONTAINER (required), BACKUP_DIR (required, path inside the container),
#      RETENTION_DAYS (default 7 - archivelog backups are kept shorter than database backups).
# Exit: 0 on success, non-zero on failure (the runner records status from this).
set -u

container="${DOCKER_CONTAINER:-}"
backup_dir="${BACKUP_DIR:-}"
retention_days="${RETENTION_DAYS:-7}"

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

# No -i, and stdin closed: this script is fed to `bash -s` over SSH, so a `docker exec -i`
# without its own input would read the *rest of this file* as the container's stdin and the
# shell would silently run out of script (exit 0, no output, nothing backed up). The RMAN call
# below may use -i because its stdin is the pipe from printf, not the script.
$DOCKER exec "$container" bash -lc "mkdir -p '${backup_dir}'" </dev/null \
    || die "could not create BACKUP_DIR '${backup_dir}' inside '${container}'."

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

RMAN_IN="$(cat <<RMANEOF
${enc_line}
CONFIGURE CONTROLFILE AUTOBACKUP ON;
CONFIGURE ARCHIVELOG DELETION POLICY TO BACKED UP 1 TIMES TO DISK;
RUN {
  ALLOCATE CHANNEL c1 DEVICE TYPE DISK FORMAT '${backup_dir}/arch_%d_%T_%U.bkp';
  BACKUP ARCHIVELOG ALL NOT BACKED UP 1 TIMES TAG 'DBOPS_ARCH';
  BACKUP CURRENT CONTROLFILE TAG 'DBOPS_CTL' FORMAT '${backup_dir}/ctl_%d_%T_%U.bkp';
  BACKUP SPFILE TAG 'DBOPS_SPFILE' FORMAT '${backup_dir}/spfile_%d_%T_%U.bkp';
  RELEASE CHANNEL c1;
}
CROSSCHECK ARCHIVELOG ALL;
DELETE NOPROMPT EXPIRED ARCHIVELOG ALL;
DELETE NOPROMPT ARCHIVELOG ALL COMPLETED BEFORE 'SYSDATE-${retention_days}';
DELETE NOPROMPT BACKUP OF ARCHIVELOG ALL COMPLETED BEFORE 'SYSDATE-${retention_days}';
EXIT;
RMANEOF
)"

rman_out="$(printf '%s\n' "$RMAN_IN" | $DOCKER exec -i "$container" bash -lc 'rman target / log /dev/stdout 2>&1')"
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
$DOCKER exec "$container" bash -lc "chmod -R a+rX '${backup_dir}'" </dev/null     || printf 'warning: could not relax permissions on %s
' "${backup_dir}" >&2

printf 'RESULT=ok retention_days=%s\n' "$retention_days"
