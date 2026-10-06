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
# `docker exec`s into $DOCKER_CONTAINER when one is named, and runs on the host when one is not,
# mirroring the Oracle and PostgreSQL jobs. Windows instances use the .ps1 beside this file.
#
# Layout, one file per backup:
#   $BACKUP_DIR/<DB>/FULL/<DB>_FULL_<YYYYMMDD_HHMMSS>Z.bak   the stamp is UTC, and the Z says so
#   $BACKUP_DIR/<DB>/DIFF/<DB>_DIFF_<YYYYMMDD_HHMMSS>Z.bak
#   $BACKUP_DIR/<DB>/LOG/<DB>_LOG_<YYYYMMDD_HHMMSS>Z.trn
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
# Env: DOCKER_CONTAINER (OPTIONAL since 0.21.0 - unset means this Linux host), BACKUP_DIR (required, path inside the container),
#      BACKUP_LEVEL (required: full|diff|log),
#      MSSQL_USER (default sa), MSSQL_PASSWORD (required, from env_secrets),
#      MSSQL_DATABASES (optional comma list; default = every online user database),
#      BACKUP_ENCRYPTION_PASSWORD (optional, from env_secrets; absent = unencrypted),
#      BACKUP_CERT_NAME (default db_ops_backup_cert), RETENTION_DAYS (default 14),
#      RETENTION_SECONDS (optional; a window under a day, which wins over RETENTION_DAYS).
# Exit: 0 on success, non-zero on failure. Prints RESULT=ok only on a completed run.
set -u

container="${DOCKER_CONTAINER:-}"
backup_dir="${BACKUP_DIR:-}"
level="$(printf '%s' "${BACKUP_LEVEL:-}" | tr '[:upper:]' '[:lower:]')"
mssql_user="${MSSQL_USER:-sa}"
mssql_password="${MSSQL_PASSWORD:-}"
export SQLCMDPASSWORD="$mssql_password"
databases_csv="${MSSQL_DATABASES:-}"
enc_password="${BACKUP_ENCRYPTION_PASSWORD:-}"
cert_name="${BACKUP_CERT_NAME:-db_ops_backup_cert}"
retention_days="${RETENTION_DAYS:-14}"
retention_seconds="${RETENTION_SECONDS:-}"

die() { printf 'RESULT=error reason=%s\n' "$1" >&2; exit 1; }

[ -n "$backup_dir" ]     || die "BACKUP_DIR is not set."
[ -n "$mssql_password" ] || die "MSSQL_PASSWORD is not set (declare it in the job's env_secrets)."
case "$level" in
    full|diff|log) ;;
    *) die "BACKUP_LEVEL must be full, diff or log: '${BACKUP_LEVEL:-}'." ;;
esac
case "$retention_days" in
    ''|*[!0-9]*) die "RETENTION_DAYS must be a whole number of days: '${retention_days}'." ;;
esac
# A window under a day arrives in seconds, because days cannot say it: unset, a two-hour lab
# retention ran on the 14-day default and nothing was ever removed (2026-10-02). `find` counts
# minutes, and the age is the file's own on this host's clock - no time zone enters it.
age_test="-mtime +${retention_days}"
retention_window="${retention_days}d"
if [ -n "$retention_seconds" ]; then
    case "$retention_seconds" in
        *[!0-9]*) die "RETENTION_SECONDS must be a whole number of seconds: '${retention_seconds}'." ;;
    esac
    [ "$retention_seconds" -ge 60 ] \
        || die "RETENTION_SECONDS must be at least 60: '${retention_seconds}'."
    age_test="-mmin +$(( retention_seconds / 60 ))"
    retention_window="${retention_seconds}s"
fi

# $DOCKER_CONTAINER is optional since 0.21.0: set it for an instance inside a container, leave it
# unset for one installed on this Linux host. A NARROWER gap than the PostgreSQL and Oracle scripts
# closed - SQL Server on Windows is backed up by mssql_backup_database.ps1 beside this file, so what
# was unreachable was only SQL Server installed natively on Linux.
#
# `exec_here` wraps one command; every sqlcmd call below goes through it, so a call written for one
# of the two paths cannot quietly work in testing and fail on the other.
if [ -n "$container" ]; then
    # docker CLI: plain first, fall back to sudo (host user may not be in the docker group).
    DOCKER="docker"
    $DOCKER info >/dev/null 2>&1 || DOCKER="sudo docker"
    $DOCKER inspect "$container" >/dev/null 2>&1 \
        || die "container '${container}' not found or docker unavailable on host."
    # Present is not running. A stopped container used to fail further on, as whatever the first
    # exec could not do - "no sqlcmd found (container X); install mssql-tools" on 2026-09-24 -
    # which sends the reader to install tools that are there.
    [ "$($DOCKER inspect -f '{{.State.Running}}' "$container" 2>/dev/null)" = "true" ] \
        || die "container '${container}' is not running - start it (docker start ${container}) and run the backup again."
    # stdin is closed on every docker exec: this whole script arrives on the host's `bash -s`
    # stdin, and an exec that keeps it open would eat the rest of the script.
    exec_here() { $DOCKER exec -i -e SQLCMDPASSWORD "$container" "$@" < /dev/null; }
    # The same, reading the caller's stdin: a batch that carries a secret is fed to sqlcmd this way.
    exec_stdin() { $DOCKER exec -i -e SQLCMDPASSWORD "$container" "$@"; }
    probe_here() { $DOCKER exec "$container" sh -c "$1" >/dev/null 2>&1; }
    # Root where the engine runs, for the one thing its own user cannot do: take over a backup
    # folder someone else made (see "writable by the engine" below).
    exec_as_root() { $DOCKER exec -u 0 -i "$container" "$@" < /dev/null; }
    where="container ${container}"
else
    exec_here() { "$@" < /dev/null; }
    exec_stdin() { "$@"; }
    probe_here() { sh -c "$1" >/dev/null 2>&1; }
    # Never prompts: a host whose sudo wants a password fails here and the folder is named below.
    exec_as_root() { sudo -n "$@" < /dev/null; }
    where="this host"
fi
printf 'reaching the instance: %s\n' "$where"

# sqlcmd moved between tool versions; take whichever is present. The same list either way - the
# mssql-tools paths are where a native Linux install puts them too, not only the image.
sqlcmd_bin=""
for candidate in /opt/mssql-tools18/bin/sqlcmd /opt/mssql-tools/bin/sqlcmd sqlcmd; do
    if probe_here "test -x '$candidate'" || probe_here "command -v $candidate"; then
        sqlcmd_bin="$candidate"; break
    fi
done
[ -n "$sqlcmd_bin" ] || die "no sqlcmd found (${where}); install mssql-tools or set DOCKER_CONTAINER."

# -C trusts the self-signed server certificate the image generates; -b makes sqlcmd exit
# non-zero on a SQL error, which is what turns a failed BACKUP into a failed job.
# stdin is closed on every docker exec: this whole script arrives on the host's `bash -s`
# stdin, and an exec that keeps it open would eat the rest of the script.
# The login's password is never an argument: sqlcmd reads SQLCMDPASSWORD (exported above, handed to
# the container by name with `-e SQLCMDPASSWORD`). As `-P` it was in `ps` on the host and in the
# container for every statement (review 0.25.0, F10.4).
run_sql() {
    exec_here "$sqlcmd_bin" -C -b -S localhost -U "$mssql_user" -Q "$1"
}
# A batch that carries a secret (the encryption password) goes in on stdin, not in -Q.
run_sql_secret() {
    printf '%s\n' "$1" | exec_stdin "$sqlcmd_bin" -C -b -S localhost -U "$mssql_user" -i /dev/stdin
}
query_sql() {   # single column, no headers, trimmed
    exec_here "$sqlcmd_bin" -C -b -S localhost \
        -U "$mssql_user" -h -1 -W -Q "SET NOCOUNT ON; $1" \
        | sed '/^$/d;/^(.*rows affected)$/d'
}

sql_escape() { printf '%s' "$1" | sed "s/'/''/g"; }

run_sql "SELECT 1;" >/dev/null 2>&1 || die "cannot log in to '${container}' as ${mssql_user}."

# The backup folder must be writable by SQL Server itself - uid 10001 in the image - not only by
# whoever made the folder above it. A lab's bind mount belongs to the SSH user, so on 2026-09-24
# every backup into it failed "mkdir: Permission denied" until the folder was handed over by hand.
# So it is made as root where the engine runs and given to the user the engine runs as.
backup_dir_writable() { exec_here mkdir -p "$backup_dir" 2>/dev/null && exec_here test -w "$backup_dir"; }
if ! backup_dir_writable; then
    engine_owner="$(exec_here sh -c 'echo "$(id -u):$(id -g)"' 2>/dev/null)"
    [ -n "$engine_owner" ] && exec_as_root \
        sh -c "mkdir -p '${backup_dir}' && chown -R '${engine_owner}' '${backup_dir}'" >/dev/null 2>&1
fi
backup_dir_writable \
    || die "cannot write to ${backup_dir} as the SQL Server user (${where}) - give that folder to the user the engine runs as."

# --------------------------------------------------------------------------- #
# Encryption material: a master key + certificate, created once and exported so
# the backups can be restored on another instance.
# --------------------------------------------------------------------------- #
encrypt_clause=""
if [ -n "$enc_password" ]; then
    esc_pw="$(sql_escape "$enc_password")"
    esc_cert="$(sql_escape "$cert_name")"
    cert_dir="${backup_dir%/}/_cert"
    exec_here mkdir -p "$cert_dir" \
        || die "cannot create ${cert_dir} (${where})."

    run_sql_secret "
IF NOT EXISTS (SELECT 1 FROM sys.symmetric_keys WHERE name = '##MS_DatabaseMasterKey##')
    CREATE MASTER KEY ENCRYPTION BY PASSWORD = '${esc_pw}';
IF NOT EXISTS (SELECT 1 FROM sys.certificates WHERE name = '${esc_cert}')
    CREATE CERTIFICATE [${cert_name}] WITH SUBJECT = 'db_ops backup encryption';
" >/dev/null || die "could not create the backup master key/certificate."

    # Export once. Without the .cer/.pvk pair beside the backups, an encrypted backup is
    # restorable only on the instance that wrote it — which defeats the point of taking it.
    if ! exec_here test -f "${cert_dir}/${cert_name}.cer"; then
        run_sql_secret "
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
# UTC, and marked: without the Z the name read as local time on a node that runs on +07.
stamp="$(date -u +%Y%m%d_%H%M%SZ)"
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
    exec_here mkdir -p "$target_dir" \
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
    newest_full="$(exec_here sh -c \
        "ls -1t '${db_dir}/FULL'/*.bak 2>/dev/null | head -1" || true)"
    if [ -z "$newest_full" ]; then
        continue   # nothing to anchor retention to; keep everything
    fi
    # -delete before -print: find names a file only once it is gone, so the run says what the
    # window removed - PostgreSQL and Oracle always did, and this said nothing (0.27.0 item 1.91).
    removed="$(exec_here sh -c "
        find '${db_dir}' -type f \\( -name '*.bak' -o -name '*.trn' \\) \
             ${age_test} ! -newer '${newest_full}' -delete -print 2>/dev/null | wc -l
    " || true)"
    printf 'retention: %s removed %s file(s) past the window (%s), below its newest full %s\n' \
        "$db" "$(printf '%s' "${removed:-0}" | tr -d '[:space:]')" "$retention_window" \
        "${newest_full##*/}"
done

# Readable by the SSH user that copies them to another machine, as the Oracle and PostgreSQL jobs
# do after every run - each run writes new pieces, so a one-off chmod stops being true within
# minutes. SQL Server writes them 0660 as its own user; the transfer's `sudo chmod` is silent
# where sudo wants a password, so without this a cross-machine restore could read nothing of a
# chain the engine had just written (the lab drill, 2026-09-24). As the engine user, who owns them.
exec_here chmod -R a+rX "$backup_dir" \
    || printf 'warning: could not relax permissions on %s\n' "$backup_dir" >&2

[ "$failed" -eq 0 ] || die "one or more ${level} backups failed."
printf 'RESULT=ok level=%s databases=%s encrypted=%s\n' \
    "$level" "$(printf '%s' "$databases" | tr '\n' ',' | sed 's/,$//')" \
    "$([ -n "$enc_password" ] && echo yes || echo no)"
