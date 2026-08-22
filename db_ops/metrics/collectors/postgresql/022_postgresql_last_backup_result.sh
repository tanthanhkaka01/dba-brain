#!/bin/bash
# POSTGRES_BACKUP_LAST_RESULT (collector_type=docker): newest FULL / DIFF / LOG backup for a
# PostgreSQL cluster, one row per type.
#
# Emits the SAME message contract as 022_sqlserver_last_backup_result.sql (`database=`,
# `recovery_model=`, `backup_type=`, `backup_finish_date=`) so that
# db_ops.common.backup_policy.collect_evidence and the inventory report's Backup column read it
# without knowing which engine produced it. Before this existed, all three backup metrics declared
# `"supported": false` for postgresql and the fleet page printed "No metrics" for every PostgreSQL
# server in the estate — while pg_basebackup had been running successfully every day.
#
# Why a docker collector and not SQL: PostgreSQL has no catalog of base backups. A base backup is
# a *directory on disk*, and the only backup fact reachable over SQL is pg_stat_archiver. So this
# reads both — the filesystem for FULL/DIFF, the catalog for LOG — inside the container named
# $DOCKER_CONTAINER, which is the one place both are visible at once.
#
# Layout is the one assets/backup/postgresql/pg_basebackup_database.sh writes:
#   $PG_BACKUP_DIR/base/<UTC stamp>_FULL      full baseline   (Sunday)
#   $PG_BACKUP_DIR/base/<UTC stamp>_INCR      incremental     (other days)
# A directory only counts once it holds a backup_manifest: pg_basebackup creates the target
# directory first and fills it afterwards, so an in-flight (or crashed) backup is a directory that
# looks complete by name alone. Counting it would report a backup that cannot be restored.
#
# Env: DOCKER_CONTAINER (injected by the docker collector), PG_BACKUP_DIR (target
# metrics.collector_env; defaults to the path restore_config.json already uses).
set -u

container="${DOCKER_CONTAINER:-}"
backup_dir="${PG_BACKUP_DIR:-/var/lib/postgresql/backup/dbops}"
rows=()

json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

add_row() { # item value unit status message
    local item="$1" value="$2" unit="$3" status="$4" msg="$5"
    rows+=("{\"metric_item\":\"$(json_escape "$item")\",\"metric_value\":\"$(json_escape "$value")\",\"metric_unit\":\"$(json_escape "$unit")\",\"status\":\"${status}\",\"message\":\"$(json_escape "$msg")\"}")
}

emit() { local IFS=,; printf '[%s]\n' "${rows[*]:-}"; }

fail_all() { # message -> one UNKNOWN row so the attempt is recorded, then exit clean
    add_row "backup_last_result" "UNKNOWN" "" "UNKNOWN" "$1"
    emit
    exit 0
}

[ -n "$container" ] || fail_all "DOCKER_CONTAINER is not set (target has no container_name)."

DOCKER="docker"
$DOCKER info >/dev/null 2>&1 || DOCKER="sudo docker"
$DOCKER inspect "$container" >/dev/null 2>&1 || fail_all "container '${container}' not found or docker unavailable on host."

# `</dev/null`, and no `-i`: the collector ships this script over SSH on stdin, so a `docker exec -i`
# here reads the *rest of the script* as the container command's input and the script silently ends
# mid-way — it produced no output at all, not an error. Same reason and same shape as
# assets/backup/postgresql/pg_basebackup_database.sh.
# `-u postgres`: docker exec on the postgres image lands as root, which peer auth rejects.
in_container() { $DOCKER exec -u "${PG_OS_USER:-postgres}" "$container" bash -lc "$1" </dev/null 2>/dev/null; }

# --- Cluster identity + log posture -----------------------------------------------------------
# archive_mode maps to the SQL Server recovery model whose meaning matches, so one policy file
# covers every engine: archive_mode=on and recovery_model=FULL both mean "point-in-time recovery
# is possible and log backups are required"; off and SIMPLE both mean "restore to the last base
# backup only". data/backup_policy.json keys LOG off recovery_model, so this mapping is what makes
# a stalled archiver an RPO violation instead of silently "not required".
psql_out="$(in_container "psql -U postgres -Atq -F'|' -c \"select current_setting('archive_mode'), coalesce(to_char(last_archived_time,'YYYY-MM-DD HH24:MI:SS'),'NULL'), coalesce(last_archived_wal,'NONE'), failed_count, coalesce(to_char(last_failed_time,'YYYY-MM-DD HH24:MI:SS'),'NULL') from pg_stat_archiver\"")"
[ -n "$psql_out" ] || fail_all "psql returned no pg_stat_archiver row inside '${container}' (is the server up and reachable as postgres?)."

IFS='|' read -r archive_mode last_archived_time last_archived_wal failed_count last_failed_time <<< "$psql_out"

recovery_model="SIMPLE"
[ "$archive_mode" = "on" ] || [ "$archive_mode" = "always" ] && recovery_model="FULL"

# The cluster, not a database: a PostgreSQL base backup covers every database in the cluster at
# once, so per-database rows would claim a granularity the backup does not have. The name is the
# container's, which is what db_instances.json calls the instance.
database="$container"

# --- FULL / DIFF: newest completed base backup of each kind ------------------------------------
# One `find` per kind, newest first by directory name (the stamp is UTC and sorts lexically).
newest_backup() { # suffix -> "<stamp dir name>" or ""
    in_container "ls -1d '${backup_dir}/base/'*_$1 2>/dev/null | sort | tail -1" | tr -d '\r'
}

emit_dir_backup() { # backup_type suffix
    local backup_type="$1" suffix="$2" dir stamp finish age
    dir="$(newest_backup "$suffix")"
    if [ -n "$dir" ] && ! in_container "test -f '${dir}/backup_manifest'"; then
        # Named like a backup but never finished. Fall back to the previous one of this kind
        # rather than reporting either the partial or nothing at all.
        dir="$(in_container "ls -1d '${backup_dir}/base/'*_${suffix} 2>/dev/null | sort | head -n -1 | tail -1" | tr -d '\r')"
        if [ -n "$dir" ] && ! in_container "test -f '${dir}/backup_manifest'"; then dir=""; fi
    fi
    if [ -z "$dir" ]; then
        add_row "${database} / ${backup_type}" "-1" "hours_since_last_backup" "OK" \
            "database=${database}, recovery_model=${recovery_model}, backup_type=${backup_type}, backup_finish_date=NULL"
        return
    fi
    # The directory name is the backup's UTC start stamp (YYYYMMDDTHHMMSSZ); its mtime is when
    # pg_basebackup finished writing it, which is the finish time the policy wants.
    finish="$(in_container "date -u -d @\$(stat -c %Y '${dir}') '+%Y-%m-%d %H:%M:%S'")"
    age="$(in_container "echo \$(( ( \$(date -u +%s) - \$(stat -c %Y '${dir}') ) / 3600 ))")"
    stamp="$(basename "$dir")"
    add_row "${database} / ${backup_type}" "${age:--1}" "hours_since_last_backup" "OK" \
        "database=${database}, recovery_model=${recovery_model}, backup_type=${backup_type}, backup_finish_date=${finish:-NULL}, backup_set=${stamp}"
}

emit_dir_backup "FULL" "FULL"
emit_dir_backup "DIFF" "INCR"

# --- LOG: WAL archiving ------------------------------------------------------------------------
# pg_stat_archiver's last_archived_time is the closest thing PostgreSQL has to "last log backup".
# Note what it does NOT prove: on an idle cluster no WAL is generated, so nothing is archived and
# this ages even though the archiver is healthy and there is no data to lose. The policy's LOG
# thresholds judge it; failed_count is reported alongside so a genuinely broken archiver (which
# ages the same way) is distinguishable in the message.
if [ "$recovery_model" = "SIMPLE" ]; then
    add_row "${database} / LOG" "-1" "hours_since_last_backup" "OK" \
        "database=${database}, recovery_model=${recovery_model}, backup_type=LOG, backup_finish_date=NULL, archive_mode=${archive_mode}"
elif [ "$last_archived_time" = "NULL" ]; then
    add_row "${database} / LOG" "-1" "hours_since_last_backup" "OK" \
        "database=${database}, recovery_model=${recovery_model}, backup_type=LOG, backup_finish_date=NULL, archive_mode=${archive_mode}, archived_count=0"
else
    log_age="$(in_container "echo \$(( ( \$(date -u +%s) - \$(date -u -d '${last_archived_time}' +%s) ) / 3600 ))")"
    add_row "${database} / LOG" "${log_age:--1}" "hours_since_last_backup" "OK" \
        "database=${database}, recovery_model=${recovery_model}, backup_type=LOG, backup_finish_date=${last_archived_time}, archive_mode=${archive_mode}, last_archived_wal=${last_archived_wal}, failed_count=${failed_count}, last_failed_time=${last_failed_time}"
fi

emit
