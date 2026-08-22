#!/bin/bash
# ORACLE_RESTORE_VALIDATION (collector_type=docker): actively proves recoverability by asking
# RMAN to VALIDATE that the backups needed to restore the database are present and not corrupt
# — without touching the live datafiles (RESTORE ... VALIDATE reads backup sets only).
#
# This is DELIBERATELY a separate, low-frequency metric (schedule it nightly/weekly) because it
# reads every backup piece and is far heavier than the ORACLE_BACKUP_HEALTH snapshot. The report
# layer derives "last validation N ago" from this result's collected_at timestamp.
#
# Runs in the container named $DOCKER_CONTAINER (injected by the docker collector). Emits the
# standard metric JSON contract: [{metric_item, metric_value, metric_unit, status, message}].
set -u

container="${DOCKER_CONTAINER:-}"
json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
one_row() { # value unit status message
    printf '[{"metric_item":"restore_validation","metric_value":"%s","metric_unit":%s,"status":"%s","message":"%s"}]\n' \
        "$(json_escape "$1")" "$2" "$3" "$(json_escape "$4")"
}

[ -n "$container" ] || { one_row "UNKNOWN" '"null"' "UNKNOWN" "DOCKER_CONTAINER is not set (target has no container_name)."; exit 0; }

DOCKER="docker"
$DOCKER info >/dev/null 2>&1 || DOCKER="sudo docker"
$DOCKER inspect "$container" >/dev/null 2>&1 || { one_row "UNKNOWN" '"null"' "UNKNOWN" "container '${container}' not found or docker unavailable on host."; exit 0; }

# Encrypted backups must be decrypted to be validated. The backup side writes them with
# `SET ENCRYPTION ON IDENTIFIED BY ... ONLY` (assets/backup/oracle/oracle_rman_database.sh), which
# is password-based and needs no wallet — but RMAN then refuses to read a single piece without the
# same passphrase, failing with ORA-19913 "unable to decrypt backup" / ORA-28365 "Wallet is not
# open" three seconds in, before it has read anything. That looked identical to "the backups are
# unrestorable" and reported CRITICAL on a healthy database for weeks. The passphrase arrives in
# $BACKUP_ENCRYPTION_PASSWORD via the target's metrics.env_secrets (a ref into the encrypted store,
# never a value in config). Unset = unencrypted backups = the previous behaviour.
enc_password="${BACKUP_ENCRYPTION_PASSWORD:-}"
decrypt_line=""
if [ -n "$enc_password" ]; then
    case "$enc_password" in
        *"'"*)
            one_row "UNKNOWN" '"null"' "UNKNOWN" \
                "BACKUP_ENCRYPTION_PASSWORD must not contain a single quote (it is passed to RMAN quoted)."
            exit 0 ;;
    esac
    decrypt_line="SET DECRYPTION IDENTIFIED BY '${enc_password}';"
fi

# Built by concatenation, not a heredoc: the passphrase must not be echoed, and `set echo off`
# above keeps RMAN from printing the command it is running back into the log we parse.
RMAN_IN="set echo off;
${decrypt_line}
restore database validate;
exit;"

start_epoch=$(date +%s)
rman_out="$(printf '%s\n' "$RMAN_IN" | $DOCKER exec -i "$container" bash -lc 'rman target / log /dev/stdout 2>&1' 2>/dev/null)"
elapsed=$(( $(date +%s) - start_epoch ))

# Belt and braces: `set echo off` already stops RMAN echoing its input, but ONE build that
# echoes it anyway would publish the passphrase into a metric message that is stored in the
# runtime store and forwarded to Telegram — irreversible. Scrub it from the captured output
# before anything downstream can read a line of it.
# Bash literal substring replacement, not sed: a passphrase is free to contain regex
# metacharacters, and a scrubber that silently fails to match is worse than none.
if [ -n "$enc_password" ]; then
    rman_out="${rman_out//"$enc_password"/***}"
fi

# RMAN-/ORA- lines are the real failure signal (e.g. RMAN-06023 "no backup or copy of
# datafile N found to restore" = that datafile is NOT recoverable from current backups —
# exactly the gap this metric exists to surface). "Finished restore" confirms a clean run.
errors="$(printf '%s\n' "$rman_out" | grep -ciE 'RMAN-[0-9]|ORA-[0-9]')"
finished="$(printf '%s\n' "$rman_out" | grep -ciE 'Finished restore|validation (succeeded|complete)')"
snippet="$(printf '%s\n' "$rman_out" | grep -iE 'RMAN-[0-9]|ORA-[0-9]|Finished restore|validation' | head -4 | tr '\n' ';' | cut -c1-300)"

if [ "$errors" -gt 0 ]; then
    one_row "FAILED" '"null"' "CRITICAL" "RMAN restore validation FAILED with ${errors} error line(s) in ${elapsed}s - some datafiles are not restorable from current backups. ${snippet}"
elif [ "$finished" -ge 1 ]; then
    one_row "SUCCESS" '"null"' "OK" "RMAN restore validation succeeded in ${elapsed}s - the database is restorable from current backups. ${snippet}"
else
    one_row "UNKNOWN" '"null"' "UNKNOWN" "RMAN restore validation produced no clear result in ${elapsed}s. ${snippet}"
fi
