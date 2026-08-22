#!/bin/bash
# ORACLE_BACKUP_HEALTH (collector_type=docker): a single, opinionated backup/recoverability
# health snapshot for an Oracle database running in the container named $DOCKER_CONTAINER
# (injected by the docker collector from the target's container_name). The docker collector
# ships this script over SSH to the container host (target cmd_access.method=ssh) and runs it
# there; the script `docker exec`s into the DB container to run sqlplus + rman.
#
# This is the "so what" layer competitors (Zabbix/OEM/PRTG) skip: not just "RMAN succeeded"
# but the whole recoverability posture — mode, archivelog, force logging, RMAN config,
# backup ages, FRA, retention, crosscheck (EXPIRED) and obsolete backups — in one glance.
#
# CROSSCHECK is run (allowed, idempotent) so the EXPIRED counts are accurate; expired/obsolete
# pieces are only *reported*, never deleted. Restore validation is a separate low-frequency
# metric (ORACLE_RESTORE_VALIDATION) because RESTORE ... VALIDATE reads every backup.
#
# Emits the standard metric JSON contract, one row per health item:
# [{metric_item, metric_value, metric_unit, status, message}, ...]
set -u

container="${DOCKER_CONTAINER:-}"
rows=()

json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

add_row() { # item value unit status message   (unit "null" => JSON null)
    local item="$1" value="$2" unit="$3" status="$4" msg="$5" uval
    if [ "$unit" = "null" ]; then uval='null'; else uval="\"$(json_escape "$unit")\""; fi
    rows+=("{\"metric_item\":\"$(json_escape "$item")\",\"metric_value\":\"$(json_escape "$value")\",\"metric_unit\":${uval},\"status\":\"${status}\",\"message\":\"$(json_escape "$msg")\"}")
}

emit() { local IFS=,; printf '[%s]\n' "${rows[*]:-}"; }

fail_all() { # message  -> one UNKNOWN row so the attempt is recorded, then exit clean
    add_row "backup_health" "UNKNOWN" "null" "UNKNOWN" "$1"
    emit
    exit 0
}

[ -n "$container" ] || fail_all "DOCKER_CONTAINER is not set (target has no container_name)."

# docker CLI: plain first, fall back to sudo (host user may not be in the docker group).
DOCKER="docker"
$DOCKER info >/dev/null 2>&1 || DOCKER="sudo docker"
$DOCKER inspect "$container" >/dev/null 2>&1 || fail_all "container '${container}' not found or docker unavailable on host."

# --- 1) RMAN: crosscheck (updates EXPIRED status), then read-only config + obsolete report ---
read -r -d '' RMAN_IN <<'RMANEOF'
set echo off;
crosscheck backup;
crosscheck archivelog all;
show retention policy;
show controlfile autobackup;
report obsolete;
exit;
RMANEOF
rman_out="$(printf '%s\n' "$RMAN_IN" | $DOCKER exec -i "$container" bash -lc 'rman target / log /dev/stdout 2>&1' 2>/dev/null)"

# --- 2) SQLPLUS: data-dictionary posture (runs AFTER crosscheck so EXPIRED counts are fresh) ---
read -r -d '' SQL_IN <<'SQLEOF'
set head off feedback off pagesize 0 lines 400 trimspool on serveroutput off echo off
select 'DBROW|'||open_mode||'|'||database_role||'|'||log_mode||'|'||force_logging||'|'||name from v$database;
select 'FRA|'||nvl(to_char(round(space_used/nullif(space_limit,0)*100,1)),'0')||'|'||nvl(to_char(round(space_limit/1024/1024)),'0') from v$recovery_file_dest;
select 'DBBK|'||nvl(to_char(max(completion_time),'YYYY-MM-DD HH24:MI'),'NONE')||'|'||nvl(to_char(round((sysdate-max(completion_time))*24,1)),'-1') from v$backup_set where backup_type in ('D','I');
select 'ARCHBK|'||nvl(to_char(max(s.completion_time),'YYYY-MM-DD HH24:MI'),'NONE')||'|'||nvl(to_char(round((sysdate-max(s.completion_time))*24,1)),'-1') from v$backup_set s where s.backup_type='L';
select 'CTLBK|'||nvl(to_char(max(completion_time),'YYYY-MM-DD HH24:MI'),'NONE')||'|'||nvl(to_char(round((sysdate-max(completion_time))*24,1)),'-1') from v$backup_set where controlfile_included='YES';
select 'EXPBK|'||to_char(count(*)) from v$backup_piece where status='X';
select 'EXPARCH|'||to_char(count(*)) from v$archived_log where status='X';
exit
SQLEOF
sql_out="$(printf '%s\n' "$SQL_IN" | $DOCKER exec -i "$container" bash -lc 'sqlplus -s -L / as sysdba 2>&1' 2>/dev/null)"

get() { printf '%s\n' "$sql_out" | grep -m1 "^$1|" | cut -d'|' -f2-; }

dbrow="$(get DBROW)"
if [ -z "$dbrow" ]; then
    fail_all "sqlplus returned no v\$database row. raw: $(printf '%s' "$sql_out" | tr '\n' ' ' | cut -c1-160)"
fi
IFS='|' read -r open_mode db_role log_mode force_logging db_name <<< "$dbrow"

overall="OK"
bump() { # escalate overall to the worst status seen
    case "$1" in
        CRITICAL) overall="CRITICAL" ;;
        WARNING) [ "$overall" = "CRITICAL" ] || overall="WARNING" ;;
    esac
}

# --- Database open mode (role-aware: standby is expected MOUNTED / READ ONLY) ---
st="CRITICAL"
case "$db_role" in
    PRIMARY) [ "$open_mode" = "READ WRITE" ] && st="OK" ;;
    *STANDBY*) case "$open_mode" in MOUNTED|READ\ ONLY|READ\ ONLY\ WITH\ APPLY) st="OK" ;; esac ;;
    *) [ "$open_mode" = "READ WRITE" ] && st="OK" ;;
esac
add_row "db_open" "$open_mode" "null" "$st" "database ${db_name} role=${db_role} open_mode=${open_mode}"; bump "$st"

# --- Archivelog mode ---
st="CRITICAL"; [ "$log_mode" = "ARCHIVELOG" ] && st="OK"
add_row "archivelog" "$log_mode" "null" "$st" "log_mode=${log_mode}"; bump "$st"

# --- Force logging ---
st="WARNING"; [ "$force_logging" = "YES" ] && st="OK"
add_row "force_logging" "$force_logging" "null" "$st" "force_logging=${force_logging}"; bump "$st"

# --- FRA usage ---
fra="$(get FRA)"; fra_pct="${fra%%|*}"; fra_limit="${fra#*|}"
st="OK"
if awk "BEGIN{exit !((${fra_pct:-0})+0 >= 90)}"; then st="CRITICAL";
elif awk "BEGIN{exit !((${fra_pct:-0})+0 >= 80)}"; then st="WARNING"; fi
add_row "fra_pct" "${fra_pct:-0}" "percent" "$st" "FRA ${fra_pct:-0}% of ${fra_limit:-0} MB"; bump "$st"

# --- Backup ages (hours). age=-1 means no backup found. ---
age_row() { # item raw_key warn_hours crit_hours label none_status
    local item="$1" key="$2" warn="$3" crit="$4" label="$5" none_status="$6"
    local raw ts age st
    raw="$(get "$key")"; ts="${raw%%|*}"; age="${raw#*|}"
    if [ "$ts" = "NONE" ] || [ "${age}" = "-1" ]; then
        add_row "$item" "NONE" "hours" "$none_status" "${label}: no backup found."; bump "$none_status"; return
    fi
    st="OK"
    if awk "BEGIN{exit !((${age:-0})+0 >= ${crit})}"; then st="CRITICAL";
    elif awk "BEGIN{exit !((${age:-0})+0 >= ${warn})}"; then st="WARNING"; fi
    add_row "$item" "$age" "hours" "$st" "${label}: last at ${ts} (${age}h ago)."; bump "$st"
}
age_row "db_backup_age"          "DBBK"   36 72  "database backup"    "CRITICAL"
age_row "archivelog_backup_age"  "ARCHBK"  6 24  "archivelog backup"  "WARNING"
age_row "controlfile_backup_age" "CTLBK"  36 72  "controlfile backup" "WARNING"

# --- Retention policy + controlfile autobackup (from rman config) ---
retention="$(printf '%s\n' "$rman_out" | grep -m1 -i 'CONFIGURE RETENTION POLICY' | sed 's/;.*$//; s/^[[:space:]]*//; s/CONFIGURE RETENTION POLICY TO //I')"
[ -n "$retention" ] || retention="UNKNOWN"
st="OK"; printf '%s' "$retention" | grep -qi 'NONE' && st="WARNING"
add_row "retention_policy" "$retention" "null" "$st" "RMAN retention policy = ${retention}"; bump "$st"

autobk="$(printf '%s\n' "$rman_out" | grep -m1 -i 'CONFIGURE CONTROLFILE AUTOBACKUP' | grep -qi ' ON' && echo ON || echo OFF)"
st="OK"; [ "$autobk" = "ON" ] || st="WARNING"
add_row "controlfile_autobackup" "$autobk" "null" "$st" "controlfile autobackup = ${autobk}"; bump "$st"

# --- RMAN configured (derived: retention set + autobackup on) ---
st="OK"; { [ "$autobk" != "ON" ] || printf '%s' "$retention" | grep -qi 'NONE'; } && st="WARNING"
add_row "rman_configured" "$([ "$st" = OK ] && echo YES || echo PARTIAL)" "null" "$st" "retention=${retention}, controlfile_autobackup=${autobk}"; bump "$st"

# --- Crosscheck: EXPIRED pieces/archivelogs (counted from v$ after crosscheck ran above) ---
exp_bk="$(get EXPBK)"; exp_arch="$(get EXPARCH)"
exp_total=$(( ${exp_bk:-0} + ${exp_arch:-0} ))
st="OK"; [ "$exp_total" -eq 0 ] || st="WARNING"
add_row "crosscheck_expired" "$exp_total" "count" "$st" "expired backup_pieces=${exp_bk:-0}, expired archivelogs=${exp_arch:-0} (0 = crosscheck clean)"; bump "$st"

# --- Obsolete backups (read-only report; never deleted here) ---
if printf '%s\n' "$rman_out" | grep -qi 'no obsolete backups found'; then
    add_row "obsolete_backups" "0" "count" "OK" "no obsolete backups (per retention policy)."
else
    obs=$(printf '%s\n' "$rman_out" | grep -ciE 'Backup Set|Backup Piece|Archive Log')
    add_row "obsolete_backups" "$obs" "count" "LOGGING" "${obs} obsolete backup object(s) eligible for deletion per retention policy."
fi

# --- Overall verdict ---
add_row "overall" "$overall" "null" "$overall" "Oracle backup health overall = ${overall} for ${db_name} (${db_role})."

emit
