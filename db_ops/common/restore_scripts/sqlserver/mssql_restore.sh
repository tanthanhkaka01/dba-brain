#!/bin/bash
# SQL Server restore drill - rebuild the databases from their FULL/DIFF/LOG chain into a
# SEPARATE container, running the same phases as the Oracle and PostgreSQL restores:
#
#   PHASE=restore-key    import the backup-encryption certificate on the target
#   PHASE=copy-backup    find the newest FULL, and the DIFF/LOG that belong to it
#   PHASE=restore-data   RESTORE ... WITH NORECOVERY / RECOVERY, in chain order
#   PHASE=verify         prove each restored database is ONLINE and answers a query
#
# This is the container-to-container drill, not the production SMB flow: both ends are Linux
# containers, so it reuses the machinery the Oracle/PostgreSQL drills use (host-to-host
# transfer, staging into the target, env_secrets) instead of the SMB share + sqlcmd path in
# restore_database.py. Both remain valid; an entry picks one by declaring a `script` or not.
#
# ENCRYPTION. A backup written `WITH ENCRYPTION (... SERVER CERTIFICATE = ...)` can only be
# read by an instance holding that certificate, so the backup job exports it next to the
# backups (<backup_dir>/_cert/*.cer + .pvk) and this script imports it before restoring. That
# is the whole reason the pair is exported: an encrypted backup restorable only on the
# instance that wrote it is not a backup.
#
# The restore is deliberately destructive on the TARGET only: each database is dropped and
# rebuilt from the chain. The source is never connected to.
#
# Env: TARGET_CONTAINER (required), BACKUP_DIR (required, as seen inside the target),
#      MSSQL_USER (default sa), MSSQL_PASSWORD (required, from env_secrets),
#      BACKUP_ENCRYPTION_PASSWORD (from env_secrets; needed for encrypted backups),
#      BACKUP_CERT_NAME (default db_ops_backup_cert), RESTORE_ID,
#      MSSQL_DATABASES (optional comma list; default = every database found in the backup set).
# Exit: 0 on success. Prints RESULT=ok only after every restored database answers a query.
set -u

target_container="${TARGET_CONTAINER:-}"
backup_dir="${BACKUP_DIR:-}"
restore_id="${RESTORE_ID:-mssql-restore}"
mssql_user="${MSSQL_USER:-sa}"
mssql_password="${MSSQL_PASSWORD:-}"
enc_password="${BACKUP_ENCRYPTION_PASSWORD:-}"
cert_name="${BACKUP_CERT_NAME:-db_ops_backup_cert}"
databases_csv="${MSSQL_DATABASES:-}"
target_mode="${TARGET_MODE:-docker}"

die() { printf 'RESULT=error phase=%s reason=%s\n' "${PHASE:-init}" "$1" >&2; exit 1; }
phase() { PHASE="$1"; printf '\nPHASE=%s %s\n' "$1" "${2:-}"; }

[ -n "$backup_dir" ]     || die "BACKUP_DIR is not set."
[ -n "$mssql_password" ] || die "MSSQL_PASSWORD is not set (declare it in the entry's env_secrets)."
[ "$target_mode" = "docker" ] || die "only TARGET_MODE=docker is supported for SQL Server drills."
[ -n "$target_container" ] || die "TARGET_CONTAINER is not set."

DOCKER="docker"
$DOCKER info >/dev/null 2>&1 || DOCKER="sudo docker"
$DOCKER inspect "$target_container" >/dev/null 2>&1 \
    || die "target container '$target_container' not found on host."

# The staged backup has to be visible inside the target. A path that is a real mount already
# is; anything else is copied in, replacing a previous run's copy rather than reusing it -
# keeping a stale copy silently pins the restore to backups that no longer exist at the source.
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
    # A path served by a mount IS the host directory - live by construction, nothing to copy,
    # and nothing to delete either.
    if inside_a_mount; then
        return 0
    fi
    # Guarded: inside_a_mount already returned above, so this can only be a previous run's
    # copy. Re-checking costs nothing and stops a future edit turning this into an rm -rf of a
    # read-write bind mount - that is, of the source backups themselves.
    if inside_a_mount; then
        die "refusing to delete '${backup_dir}': it is served by a mount inside '${target_container}'."
    fi
    $DOCKER exec -u 0 "$target_container" rm -rf "$backup_dir" </dev/null 2>/dev/null || true
    [ -d "$backup_dir" ] || die "backup dir '${backup_dir}' is not served by a mount inside '${target_container}', and does not exist on this host either - nothing to stage."
    printf 'staging %s into %s\n' "$backup_dir" "$target_container"
    $DOCKER exec -u 0 "$target_container" mkdir -p "$(dirname "$backup_dir")" </dev/null \
        || die "cannot create $(dirname "$backup_dir") inside '${target_container}'."
    $DOCKER cp "$backup_dir" "${target_container}:${backup_dir}" \
        || die "docker cp of '${backup_dir}' into '${target_container}' failed."
    $DOCKER exec -u 0 "$target_container" chown -R mssql: "$backup_dir" </dev/null 2>/dev/null || true
    $DOCKER exec -u 0 "$target_container" chmod -R a+rX "$backup_dir" </dev/null \
        || die "cannot make '${backup_dir}' readable inside '${target_container}'."
}
stage_into_target

sqlcmd_bin=""
for candidate in /opt/mssql-tools18/bin/sqlcmd /opt/mssql-tools/bin/sqlcmd sqlcmd; do
    if $DOCKER exec "$target_container" test -x "$candidate" </dev/null 2>/dev/null; then
        sqlcmd_bin="$candidate"; break
    fi
done
[ -n "$sqlcmd_bin" ] || die "no sqlcmd found in '${target_container}'."

run_sql() {
    $DOCKER exec -i "$target_container" "$sqlcmd_bin" -C -b -S localhost \
        -U "$mssql_user" -P "$mssql_password" -Q "$1" </dev/null
}
query_sql() {
    $DOCKER exec -i "$target_container" "$sqlcmd_bin" -C -b -S localhost \
        -U "$mssql_user" -P "$mssql_password" -h -1 -W -Q "SET NOCOUNT ON; $1" </dev/null \
        | sed '/^$/d;/rows affected/d' | tr -d '\r'
}
sql_escape() { printf '%s' "$1" | sed "s/'/''/g"; }
in_t() { $DOCKER exec "$target_container" bash -lc "$1" </dev/null; }

# pieces <db> <FULL|DIFF|LOG> <bak|trn> -> that database's own pieces at that level, sorted.
#
# Only names the backup job itself writes, <db>_<LEVEL>_<YYYYMMDD>_<HHMMSS>.<ext>, are eligible.
# Everything downstream orders the chain by the stamp in the name, so a file that carries no
# stamp cannot be placed in it - and both ways of getting that wrong were live on the CLOUD
# lab at once. A stray test_db_01_FULL_01.bak dropped into mssql_ha_db/FULL sorted after every
# mssql_ha_db_FULL_2026*.bak, so `sort | tail -1` restored mssql_ha_db from *another database's*
# backup. And test_db_01_LOG_01.trn, whose name has the right prefix but no stamp, fell through
# the stamp-extracting sed unchanged - leaving the literal filename to be compared against
# "20260805_085205", which any letter beats, so a pre-FULL log was always selected and RESTORE
# LOG died with "the log in this backup set terminates at LSN ..., which is too early".
pieces() {
    in_t "ls -1 '${backup_dir}/$1/$2' 2>/dev/null" | tr -d '\r' \
        | grep -E "^$1_$2_[0-9]{8}_[0-9]{6}\.$3\$" \
        | sed "s|^|${backup_dir}/$1/$2/|" | sort
}

run_sql "SELECT 1;" >/dev/null 2>&1 || die "cannot log in to '${target_container}' as ${mssql_user}."

# --------------------------------------------------------------------------------------------
phase restore-key "import the backup-encryption certificate"
cert_dir="${backup_dir%/}/_cert"
if in_t "test -f '${cert_dir}/${cert_name}.cer'"; then
    [ -n "$enc_password" ] || die "the backup set carries an encryption certificate but BACKUP_ENCRYPTION_PASSWORD is not set."
    esc_pw="$(sql_escape "$enc_password")"
    esc_cert="$(sql_escape "$cert_name")"
    run_sql "
IF NOT EXISTS (SELECT 1 FROM sys.symmetric_keys WHERE name = '##MS_DatabaseMasterKey##')
    CREATE MASTER KEY ENCRYPTION BY PASSWORD = '${esc_pw}';
IF EXISTS (SELECT 1 FROM sys.certificates WHERE name = '${esc_cert}')
    DROP CERTIFICATE [${cert_name}];
CREATE CERTIFICATE [${cert_name}]
    FROM FILE = '${cert_dir}/${cert_name}.cer'
    WITH PRIVATE KEY (
        FILE = '${cert_dir}/${cert_name}.pvk',
        DECRYPTION BY PASSWORD = '${esc_pw}'
    );
" >/dev/null || die "could not import the backup certificate; the encrypted backups cannot be read without it."
    printf 'certificate_imported=%s\n' "$cert_name"
else
    printf 'no certificate exported with the backup set; treating the backups as unencrypted\n'
fi

# --------------------------------------------------------------------------------------------
phase copy-backup "find the newest FULL and the DIFF/LOG that belong to it"
if [ -n "$databases_csv" ]; then
    databases="$(printf '%s' "$databases_csv" | tr ',' '\n' | sed 's/^ *//;s/ *$//' | sed '/^$/d')"
else
    # One directory per database, as the backup job writes it. _cert is not a database.
    databases="$(in_t "ls -1 '${backup_dir}' 2>/dev/null" | tr -d '\r' | grep -v '^_cert$' | sed '/^$/d')"
fi
[ -n "$databases" ] || die "no database directories found under ${backup_dir}."
printf 'databases=%s\n' "$(printf '%s' "$databases" | tr '\n' ',' | sed 's/,$//')"

# --------------------------------------------------------------------------------------------
phase restore-data "restore each chain: FULL -> newest DIFF -> the LOGs after it"
restored=0
for db in $databases; do
    full="$(pieces "$db" FULL bak | tail -1)"
    [ -n "$full" ] || die "no FULL backup for ${db} under ${backup_dir}/${db}/FULL."
    # A DIFF is only usable if it was taken after the FULL being restored; same for the LOGs.
    # Sorting by the UTC timestamp in the file name is what makes "after" comparable here.
    full_stamp="$(basename "$full" | sed 's/.*_\([0-9]\{8\}_[0-9]\{6\}\)\.bak$/\1/')"
    diff=""
    for d in $(pieces "$db" DIFF bak); do
        s="$(basename "$d" | sed 's/.*_\([0-9]\{8\}_[0-9]\{6\}\)\.bak$/\1/')"
        [ "$s" \> "$full_stamp" ] && diff="$d"
    done
    logs=""
    base_stamp="${full_stamp}"
    [ -n "$diff" ] && base_stamp="$(basename "$diff" | sed 's/.*_\([0-9]\{8\}_[0-9]\{6\}\)\.bak$/\1/')"
    for l in $(pieces "$db" LOG trn); do
        s="$(basename "$l" | sed 's/.*_\([0-9]\{8\}_[0-9]\{6\}\)\.trn$/\1/')"
        [ "$s" \> "$base_stamp" ] && logs="$logs $l"
    done
    printf -- '-- %s: full=%s diff=%s logs=%s\n' "$db" "$(basename "$full")" \
        "$(if [ -n "$diff" ]; then basename "$diff"; else echo none; fi)" "$(printf '%s' "$logs" | wc -w)"

    esc_db="$(sql_escape "$db")"
    # MOVE is not needed: the target is the same image and the same data directory layout, and
    # REPLACE lets the restore overwrite a database of the same name from a different instance.
    # SET SINGLE_USER only for a database that is actually up. A drill that failed part way
    # through leaves its database in RESTORING (state 1), where ALTER DATABASE is rejected
    # outright - "ALTER DATABASE is not permitted while a database is in the Restoring state" -
    # and with sqlcmd -b that error alone failed the whole run, so the *next* drill could never
    # clear the wreckage of the previous one. DROP DATABASE needs no such preparation: there
    # are no sessions to evict on a database nobody can connect to.
    run_sql "
IF DB_ID('${esc_db}') IS NOT NULL
BEGIN
    IF (SELECT state FROM sys.databases WHERE name = '${esc_db}') = 0
        ALTER DATABASE [${db}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
    DROP DATABASE [${db}];
END
RESTORE DATABASE [${db}] FROM DISK = '${full}' WITH REPLACE, NORECOVERY, STATS = 25;
" || die "RESTORE DATABASE (full) failed for ${db}."

    if [ -n "$diff" ]; then
        run_sql "RESTORE DATABASE [${db}] FROM DISK = '${diff}' WITH NORECOVERY, STATS = 25;" \
            || die "RESTORE DATABASE (differential) failed for ${db}."
    fi
    for l in $logs; do
        run_sql "RESTORE LOG [${db}] FROM DISK = '${l}' WITH NORECOVERY;" \
            || die "RESTORE LOG failed for ${db} at $(basename "$l")."
    done
    run_sql "RESTORE DATABASE [${db}] WITH RECOVERY;" \
        || die "bringing ${db} online (WITH RECOVERY) failed."
    restored=$((restored + 1))
done

# --------------------------------------------------------------------------------------------
phase verify "every restored database must be ONLINE and answer a query"
for db in $databases; do
    state="$(query_sql "SELECT state_desc FROM sys.databases WHERE name = '$(sql_escape "$db")';")"
    [ "$state" = "ONLINE" ] || die "${db} is ${state:-missing} after the restore, not ONLINE."
    tables="$(query_sql "SELECT COUNT(*) FROM [${db}].sys.tables;")"
    printf 'db=%s state=%s user_tables=%s\n' "$db" "$state" "${tables:-0}"
done

printf '\nRESULT=ok restore_id=%s target=%s databases=%s\n' \
    "$restore_id" "$target_container" "$restored"
