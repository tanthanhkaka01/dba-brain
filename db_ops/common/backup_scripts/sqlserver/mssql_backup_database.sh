#!/bin/bash
# SQL Server backup - one script covering FULL, DIFF and LOG, chosen by $BACKUP_LEVEL.
#
# Unlike Oracle/PostgreSQL, SQL Server names the three levels natively, so the level is a job
# setting rather than something the script derives from the weekday: db_ops schedules a `full`
# job, a `diff` job and a `log` job against the same entry, each with its own time window.
# That is also why retention here is by age alone and safe: a DIFF depends only on the most
# recent FULL, and a LOG chain on the FULL/DIFF before it, so the guard is "never delete a
# backup newer than the oldest one still needed" - see the retention section.
#
# Runs on the container host (shipped over SSH by db_ops.backup_restore.backup) and
# `docker exec`s into $DOCKER_CONTAINER, mirroring the Oracle and PostgreSQL jobs.
#
# Layout, one file per backup:
#   $BACKUP_DIR/<DB>/FULL/<DB>_FULL_<UTC timestamp>.bak
#   $BACKUP_DIR/<DB>/DIFF/<DB>_DIFF_<UTC timestamp>.bak
#   $BACKUP_DIR/<DB>/LOG/<DB>_LOG_<UTC timestamp>.trn
#   $BACKUP_DIR/_cert/<CERT_NAME>.cer + .pvk        the encryption certificate, exported once
#
# ENCRYPTION. With $BACKUP_ENCRYPTION_PASSWORD set, every backup is written
# `WITH ENCRYPTION (ALGORITHM = AES_256, SERVER CERTIFICATE = ...)`. SQL Server needs a
# certificate for that, so the script creates one (and the master key it hangs off) on first
# run and exports it next to the backups - because a backup encrypted with a certificate that
# exists only inside the source instance cannot be restored anywhere else. The exported
# private key is itself protected by the same passphrase. Restoring elsewhere = import that
# .cer/.pvk pair into the target instance first; db_ops.backup_restore.certificate does this
# for the production flow.
#
# Env: DOCKER_CONTAINER (required), BACKUP_DIR (required, path inside the container),
#      BACKUP_LEVEL (required: full|diff|log),
#      MSSQL_USER (default sa), MSSQL_PASSWORD (required, from env_secrets),
#      MSSQL_DATABASES (optional comma list; default = every online user database),
#      BACKUP_ENCRYPTION_PASSWORD (optional, from env_secrets; absent = unencrypted),
#      BACKUP_CERT_NAME (default db_ops_backup_cert), RETENTION_DAYS (default 14).
# Exit: 0 on success, non-zero on failure. Prints RESULT=ok only on a completed run.
set -u

container="${DOCKER_CONTAINER:-}"
backup_dir="${BACKUP_DIR:-}"
level="$(printf '%s' "${BACKUP_LEVEL:-}" | tr '[:upper:]' '[:lower:]')"
mssql_user="${MSSQL_USER:-sa}"
mssql_password="${MSSQL_PASSWORD:-}"
databases_csv="${MSSQL_DATABASES:-}"
enc_password="${BACKUP_ENCRYPTION_PASSWORD:-}"
cert_name="${BACKUP_CERT_NAME:-db_ops_backup_cert}"
retention_days="${RETENTION_DAYS:-14}"

die() { printf 'RESULT=error reason=%s\n' "$1" >&2; exit 1; }

[ -n "$container" ]      || die "DOCKER_CONTAINER is not set."
[ -n "$backup_dir" ]     || die "BACKUP_DIR is not set."
[ -n "$mssql_password" ] || die "MSSQL_PASSWORD is not set (declare it in the job's env_secrets)."
case "$level" in
    full|diff|log) ;;
    *) die "BACKUP_LEVEL must be full, diff or log: '${BACKUP_LEVEL:-}'." ;;
esac
case "$retention_days" in
    ''|*[!0-9]*) die "RETENTION_DAYS must be a whole number of days: '${retention_days}'." ;;
esac

# docker CLI: plain first, fall back to sudo (host user may not be in the docker group).
DOCKER="docker"
$DOCKER info >/dev/null 2>&1 || DOCKER="sudo docker"
$DOCKER inspect "$container" >/dev/null 2>&1 \
    || die "container '${container}' not found or docker unavailable on host."

# sqlcmd moved between tool versions; take whichever the image ships.
sqlcmd_bin=""
for candidate in /opt/mssql-tools18/bin/sqlcmd /opt/mssql-tools/bin/sqlcmd sqlcmd; do
    if $DOCKER exec "$container" test -x "$candidate" 2>/dev/null \
       || $DOCKER exec "$container" sh -c "command -v $candidate" >/dev/null 2>&1; then
        sqlcmd_bin="$candidate"; break
    fi
done
[ -n "$sqlcmd_bin" ] || die "no sqlcmd found in container '${container}'."

# -C trusts the self-signed server certificate the image generates; -b makes sqlcmd exit
# non-zero on a SQL error, which is what turns a failed BACKUP into a failed job.
# stdin is closed on every docker exec: this whole script arrives on the host's `bash -s`
# stdin, and an exec that keeps it open would eat the rest of the script.
run_sql() {
    $DOCKER exec -i "$container" "$sqlcmd_bin" -C -b -S localhost \
        -U "$mssql_user" -P "$mssql_password" -Q "$1" < /dev/null
}
query_sql() {   # single column, no headers, trimmed
    $DOCKER exec -i "$container" "$sqlcmd_bin" -C -b -S localhost \
        -U "$mssql_user" -P "$mssql_password" -h -1 -W -Q "SET NOCOUNT ON; $1" < /dev/null \
        | sed '/^$/d;/^(.*rows affected)$/d'
}

sql_escape() { printf '%s' "$1" | sed "s/'/''/g"; }

run_sql "SELECT 1;" >/dev/null 2>&1 || die "cannot log in to '${container}' as ${mssql_user}."

# --------------------------------------------------------------------------- #
# Encryption material: a master key + certificate, created once and exported so
# the backups can be restored on another instance.
# --------------------------------------------------------------------------- #
encrypt_clause=""
if [ -n "$enc_password" ]; then
    esc_pw="$(sql_escape "$enc_password")"
    esc_cert="$(sql_escape "$cert_name")"
    cert_dir="${backup_dir%/}/_cert"
    $DOCKER exec -i "$container" mkdir -p "$cert_dir" < /dev/null \
        || die "cannot create ${cert_dir} in the container."

    run_sql "
IF NOT EXISTS (SELECT 1 FROM sys.symmetric_keys WHERE name = '##MS_DatabaseMasterKey##')
    CREATE MASTER KEY ENCRYPTION BY PASSWORD = '${esc_pw}';
IF NOT EXISTS (SELECT 1 FROM sys.certificates WHERE name = '${esc_cert}')
    CREATE CERTIFICATE [${cert_name}] WITH SUBJECT = 'db_ops backup encryption';
" >/dev/null || die "could not create the backup master key/certificate."

    # Export once. Without the .cer/.pvk pair beside the backups, an encrypted backup is
    # restorable only on the instance that wrote it — which defeats the point of taking it.
    if ! $DOCKER exec -i "$container" test -f "${cert_dir}/${cert_name}.cer" < /dev/null; then
        run_sql "
BACKUP CERTIFICATE [${cert_name}]
    TO FILE = '${cert_dir}/${cert_name}.cer'
    WITH PRIVATE KEY (
        FILE = '${cert_dir}/${cert_name}.pvk',
        ENCRYPTION BY PASSWORD = '${esc_pw}'
    );
" >/dev/null || die "could not export the backup certificate to ${cert_dir}."
        printf 'exported backup certificate: %s/%s.cer\n' "$cert_dir" "$cert_name"
    fi
    encrypt_clause=", ENCRYPTION (ALGORITHM = AES_256, SERVER CERTIFICATE = [${cert_name}])"
fi

# --------------------------------------------------------------------------- #
# Which databases.
# --------------------------------------------------------------------------- #
if [ -n "$databases_csv" ]; then
    databases="$(printf '%s' "$databases_csv" | tr ',' '\n' | sed 's/^ *//;s/ *$//' | sed '/^$/d')"
else
    # Online user databases only. tempdb is never backed up; model/msdb/master are excluded
    # because restoring them onto a different instance is a different operation entirely.
    # A LOG backup additionally needs FULL recovery — a SIMPLE database has no log chain and
    # BACKUP LOG on it fails, so it is filtered out rather than allowed to fail the job.
    recovery_filter=""
    [ "$level" = "log" ] && recovery_filter="AND recovery_model_desc <> 'SIMPLE'"
    databases="$(query_sql "
SELECT name FROM sys.databases
 WHERE database_id > 4 AND state_desc = 'ONLINE' AND is_read_only = 0
   ${recovery_filter}
 ORDER BY name;")"
fi
[ -n "$databases" ] || { printf 'no database to back up at level=%s\n' "$level"; printf 'RESULT=ok\n'; exit 0; }

# A DIFF with no FULL behind it cannot be restored; SQL Server would silently promote it to a
# full ("base backup not found" only appears at restore time on some paths). Refuse instead.
if [ "$level" = "diff" ]; then
    for db in $databases; do
        has_full="$(query_sql "
SELECT COUNT(*) FROM msdb.dbo.backupset
 WHERE database_name = '$(sql_escape "$db")' AND type = 'D';")"
        case "$has_full" in
            ''|*[!0-9]*) die "cannot read backup history for ${db}." ;;
            0) die "no FULL backup exists for ${db}; a DIFF would have nothing to restore onto. Run the full job first." ;;
        esac
    done
fi

# --------------------------------------------------------------------------- #
# Back up.
# --------------------------------------------------------------------------- #
stamp="$(date -u +%Y%m%d_%H%M%S)"
failed=0
for db in $databases; do
    esc_db="$(sql_escape "$db")"
    case "$level" in
        full) sub=FULL; ext=bak; clause="" ;;
        diff) sub=DIFF; ext=bak; clause=", DIFFERENTIAL" ;;
        log)  sub=LOG;  ext=trn; clause="" ;;
    esac
    target_dir="${backup_dir%/}/${db}/${sub}"
    target_file="${target_dir}/${db}_${sub}_${stamp}.${ext}"
    $DOCKER exec -i "$container" mkdir -p "$target_dir" < /dev/null \
        || { printf 'ERROR mkdir failed: %s\n' "$target_dir" >&2; failed=1; continue; }

    if [ "$level" = "log" ]; then
        statement="BACKUP LOG [${db}] TO DISK = '${target_file}' WITH INIT, CHECKSUM, COMPRESSION${encrypt_clause};"
    else
        statement="BACKUP DATABASE [${db}] TO DISK = '${target_file}' WITH INIT, CHECKSUM, COMPRESSION${clause}${encrypt_clause};"
    fi

    printf -- '-- %s %s -> %s\n' "$db" "$level" "$target_file"
    if run_sql "$statement"; then
        # CHECKSUM on the way in is only half of it: VERIFYONLY re-reads what landed on disk,
        # which is the difference between "the command returned" and "the file is restorable".
        if ! run_sql "RESTORE VERIFYONLY FROM DISK = '${target_file}';" >/dev/null; then
            printf 'ERROR verify failed: %s\n' "$target_file" >&2
            failed=1
        fi
    else
        printf 'ERROR backup failed: %s (%s)\n' "$db" "$level" >&2
        failed=1
    fi
done

# --------------------------------------------------------------------------- #
# Retention: age-based, but never past the newest FULL.
# --------------------------------------------------------------------------- #
# Deleting by age alone can remove the FULL that every retained DIFF/LOG restores onto, leaving
# a backup set that looks present and cannot be used — the same trap the Oracle and PostgreSQL
# jobs guard against with chain-aware retention. Here the rule is simpler: keep everything at
# or newer than the newest FULL, whatever its age, and apply the age cut only below that.
for db in $databases; do
    db_dir="${backup_dir%/}/${db}"
    newest_full="$($DOCKER exec -i "$container" sh -c \
        "ls -1t '${db_dir}/FULL'/*.bak 2>/dev/null | head -1" < /dev/null || true)"
    if [ -z "$newest_full" ]; then
        continue   # nothing to anchor retention to; keep everything
    fi
    $DOCKER exec -i "$container" sh -c "
        find '${db_dir}' -type f \\( -name '*.bak' -o -name '*.trn' \\) \
             -mtime +${retention_days} ! -newer '${newest_full}' -delete 2>/dev/null || true
    " < /dev/null
done

[ "$failed" -eq 0 ] || die "one or more ${level} backups failed."
printf 'RESULT=ok level=%s databases=%s encrypted=%s\n' \
    "$level" "$(printf '%s' "$databases" | tr '\n' ',' | sed 's/,$//')" \
    "$([ -n "$enc_password" ] && echo yes || echo no)"
