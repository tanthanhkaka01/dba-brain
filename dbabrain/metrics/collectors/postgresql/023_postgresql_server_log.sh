#!/bin/bash
# POSTGRES_SERVER_LOG (collector_type=docker): scans the PostgreSQL server log from the container's
# stderr stream for PANIC, FATAL, data corruption, storage exhaustion, out-of-memory, and backend crash
# events.
#
# Why: A corrupt database page, a backend process killed by a signal (forcing the postmaster to
# restart every session), out-of-memory errors, and disk-full write failures are recorded by
# PostgreSQL to its server log and nowhere queryable by SQL DMVs or catalog tables.
# DATABASE_SUSPECT_PAGES detects checksum failures only when data_checksums was enabled at cluster
# initialization; otherwise page corruption is invisible to SQL until a query trips over it.
# Similarly, when a backend crashes with a SIGSEGV/SIGBUS, the postmaster terminates all other backends
# and enters crash recovery, leaving no record in any SQL catalog table.
#
# Two windows: Scans the last 24 hours (PG_LOG_WINDOW_HOURS, default 24h) for historical visibility,
# but gates CRITICAL paging status to events occurring within the recent window
# (PG_LOG_CRITICAL_RECENT_MINUTES, default 120m). An incident that occurred hours ago alerts as
# WARNING so it remains visible on daily dashboards without waking on-call staff repeatedly,
# mirroring the dual-window contract of 017_sqlserver_errorlog_recent_critical.sql.
#
# Two sources, one rule. A container (DOCKER_CONTAINER set) logs to stderr, read with docker logs. A
# host install - no container, run on the host through cmd_access (the metric's host_fallback) -
# writes files, read from PG_LOG_FILE, else PG_LOG_DIRECTORY, else the packagings' own places:
# Debian's /var/log/postgresql, the RPMs' and the source build's data/log. Both are streamed through
# the same grep and graded by the same loop. If logging_collector is on inside a container, docker
# logs holds only early startup lines, which this collector reports as an OK 'not configured' state.
#
# Several clusters on one host are not guessed between: their logs are reported by name and not
# read, until PG_LOG_FILE or PG_LOG_DIRECTORY in the target's collector_env says which is this one.
# A log in a directory only postgres may read (the RPMs' data/log is 0700) is read through sudo -n
# when the login has it; otherwise the row is a WARNING saying so - the log exists and is unseen.
#
# Env: DOCKER_CONTAINER (injected by the docker collector for a container), PG_LOG_FILE,
# PG_LOG_DIRECTORY (a host install, from collector_env), PG_LOG_WINDOW_HOURS (default 24),
# PG_LOG_CRITICAL_RECENT_MINUTES (default 120).
set -u

container="${DOCKER_CONTAINER:-}"
window_hours="${PG_LOG_WINDOW_HOURS:-24}"
critical_recent_minutes="${PG_LOG_CRITICAL_RECENT_MINUTES:-120}"
# Deadlocks are graded by SQL Server's LOCK_DEADLOCK_RECENT rule over the same window: any WARNING,
# this many CRITICAL (1.149's port, 2026-10-16). PostgreSQL keeps their history nowhere but here -
# pg_stat_database.deadlocks is a total since the last reset, which the Workload table differences.
deadlock_critical_count="${PG_LOG_DEADLOCK_CRITICAL:-5}"

case "$deadlock_critical_count" in
    ''|*[!0-9]*) deadlock_critical_count=5 ;;
esac

case "$window_hours" in
    ''|*[!0-9]*) window_hours=24 ;;
esac

case "$critical_recent_minutes" in
    ''|*[!0-9]*) critical_recent_minutes=120 ;;
esac

rows=()

# JSON string escaping matching 009_docker_container_stats.sh.
json_escape() {
    local s="$1" bs='\'
    s="${s//"$bs"/"$bs$bs"}"
    s="${s//\"/"$bs\""}"
    s="${s//$'\t'/"${bs}t"}"
    s="${s//$'\n'/"${bs}n"}"
    s="${s//$'\r'/"${bs}r"}"
    printf '%s' "$s" | tr -d '\000-\010\013\014\016-\037'
}

add_row() { # item value unit status message   (unit "null" => JSON null)
    local item="$1" value="$2" unit="$3" status="$4" msg="$5" uval
    if [ "$unit" = "null" ]; then uval='null'; else uval="\"$(json_escape "$unit")\""; fi
    rows+=("{\"metric_item\":\"$(json_escape "$item")\",\"metric_value\":\"$(json_escape "$value")\",\"metric_unit\":${uval},\"status\":\"${status}\",\"message\":\"$(json_escape "$msg")\"}")
}

emit() { local IFS=,; printf '[%s]\n' "${rows[*]:-}"; }

patterns='PANIC:|FATAL:|invalid page in block|could not read block|checksum verification failed|page verification failed|missing chunk number|apparent wraparound|terminating any other active server processes|automatic recovery in progress|unexpected zero page|invalid memory alloc request|from before relfrozenxid|could not access status of transaction|No space left on device|could not write to file|could not extend file|out of memory|was terminated by signal|remaining connection slots are reserved|too many clients already|checkpoints are occurring too frequently|deadlock detected|password authentication failed|no pg_hba.conf entry'
empty_counters="window_hours=${window_hours}, critical_recent=0, critical_window=0, warning_class=0, auth_failures=0, routine_fatal=0, last_line_time=none, latest_class=none, latest_text=none"

if [ -n "$container" ]; then
    source_label="container ${container}"
    DOCKER="docker"
    $DOCKER info >/dev/null 2>&1 || DOCKER="sudo docker"

    # The container is checked first, then its log is streamed through one grep of every pattern
    # below, so bash only ever loops over the lines that matter. Read whole into a variable and
    # looped in bash, a day of a busy log (log_statement = all writes every statement) would outrun
    # the metric's 60-second timeout and hold the log in memory to do it (review of 1.165).
    inspect_err=$($DOCKER inspect --format '{{.State.Status}}' "$container" 2>&1)
    if [ $? -ne 0 ]; then
        err_preview=$(printf '%s' "$inspect_err" | head -n 2 | tr '\r\n\t' '   ' | tr -s ' ')
        add_row "server_log" "0" "lines" "WARNING" "docker inspect failed (container not found or docker unavailable): ${err_preview}"
        emit
        exit 0
    fi

    log_output=$($DOCKER logs --since "${window_hours}h" --timestamps "$container" 2>&1 | grep -E "$patterns")

    # Nothing that matters in the window. A container that logs to a file (logging_collector = on)
    # shows only its startup lines here, so this is also what such a container looks like.
    if [ -z "$log_output" ]; then
        add_row "server_log" "0" "lines" "OK" \
            "no graded line in the window (a container with logging_collector on logs to a file this collector does not read), ${empty_counters}"
        emit
        exit 0
    fi
else
    # A host install. Files changed inside the window only: a log rotated away a week ago has
    # nothing this window could grade, and reading it is a busy day's cost for nothing.
    window_minutes=$((window_hours * 60))
    SUDO=""
    unreadable=""
    files=()
    if [ -n "${PG_LOG_FILE:-}" ]; then
        configured="PG_LOG_FILE"
        # Deliberately unquoted: the setting may be a glob (postgresql-*.log).
        for f in ${PG_LOG_FILE}; do
            [ -e "$f" ] && files+=("$f")
        done
    else
        if [ -n "${PG_LOG_DIRECTORY:-}" ]; then
            configured="PG_LOG_DIRECTORY"
            dirs=("${PG_LOG_DIRECTORY}")
        else
            configured=""
            # The packagings' own places. PG_LOG_SEARCH_DIRS replaces the list (a layout of one's
            # own, or a test); it is still a search, and two clusters found in it are not guessed
            # between. Unquoted on purpose: the version directories are globs.
            search="${PG_LOG_SEARCH_DIRS:-/var/log/postgresql /var/lib/pgsql/data/log /var/lib/pgsql/data/pg_log /var/lib/pgsql/*/data/log /var/lib/postgresql/*/main/log /var/lib/postgresql/data/log /usr/local/pgsql/data/log}"
            dirs=()
            for d in $search; do
                dirs+=("$d")
            done
        fi
        for d in "${dirs[@]}"; do
            [ -d "$d" ] || continue
            found=""
            if [ -r "$d" ] && [ -x "$d" ]; then
                found=$(find "$d" -maxdepth 1 -type f \( -name '*.log' -o -name '*.log.1' \) -mmin -"$window_minutes" 2>/dev/null)
            elif sudo -n true 2>/dev/null; then
                # The RPMs keep data/log 0700 postgres; only a login allowed sudo sees into it.
                found=$(sudo -n find "$d" -maxdepth 1 -type f \( -name '*.log' -o -name '*.log.1' \) -mmin -"$window_minutes" 2>/dev/null)
                SUDO="sudo -n"
            else
                unreadable="$d"
            fi
            while IFS= read -r f; do
                [ -n "$f" ] && files+=("$f")
            done <<< "$found"
        done
    fi
    # A file the login cannot open (Debian's are 0640 postgres:adm) is read through sudo -n too.
    if [ -z "$SUDO" ] && [ "${#files[@]}" -gt 0 ]; then
        for f in "${files[@]}"; do
            if [ ! -r "$f" ]; then
                if sudo -n true 2>/dev/null; then SUDO="sudo -n"; else unreadable="$f"; fi
                break
            fi
        done
    fi

    if [ -n "$unreadable" ] && [ -z "$SUDO" ]; then
        add_row "server_log" "0" "lines" "WARNING" \
            "the PostgreSQL log ${unreadable} is not readable by $(id -un 2>/dev/null) and sudo -n is not allowed: the log exists and is unseen - add the login to the log's group or allow it sudo, ${empty_counters}"
        emit
        exit 0
    fi
    if [ "${#files[@]}" -eq 0 ]; then
        add_row "server_log" "0" "lines" "OK" \
            "not configured: DOCKER_CONTAINER is not set (no container) and no PostgreSQL log file changed in the last ${window_hours} h was found ${configured:+at ${configured}}${configured:-in /var/log/postgresql or a data directory log}; a server logging to syslog or journald is not read - set PG_LOG_FILE in the target's collector_env, ${empty_counters}"
        emit
        exit 0
    fi

    # One cluster's log, never a mix: Debian names each cluster's file (postgresql-16-main.log), a
    # data directory is one cluster. Two of either and nothing configured is not this collector's to
    # choose - a PANIC in the other cluster would page as this target's.
    if [ -z "$configured" ]; then
        clusters=$(for f in "${files[@]}"; do
            case "$f" in
                /var/log/postgresql/*) b="${f##*/}"; printf '%s\n' "${b%%.log*}" ;;
                *) printf '%s\n' "${f%/*}" ;;
            esac
        done | sort -u)
        cluster_count=$(printf '%s\n' "$clusters" | grep -c .)
        if [ "$cluster_count" -gt 1 ]; then
            add_row "server_log" "0" "lines" "OK" \
                "not read: logs of ${cluster_count} clusters on this host ($(printf '%s' "$clusters" | tr '\n' ' ')) - set PG_LOG_FILE or PG_LOG_DIRECTORY in the target's collector_env to the one this target writes, ${empty_counters}"
            emit
            exit 0
        fi
    fi

    source_label="host files $(printf '%s ' "${files[@]}")"
    log_output=$($SUDO grep -hE "$patterns" "${files[@]}" 2>/dev/null)
    if [ -z "$log_output" ]; then
        add_row "server_log" "0" "lines" "OK" \
            "no graded line in the window (${source_label% }), ${empty_counters}"
        emit
        exit 0
    fi
fi

now_epoch=$(date -u +%s 2>/dev/null || date +%s 2>/dev/null || echo 0)
recent_cutoff_epoch=$(( now_epoch - critical_recent_minutes * 60 ))
window_cutoff_epoch=$(( now_epoch - window_hours * 3600 ))

# Sets line_time, line_epoch and line_body. docker logs --timestamps prefixes RFC 3339 in UTC; a
# log file starts with log_line_prefix's %m or %t - the default on every packaging - which is the
# server's local time and its zone (+07, UTC). A zone date cannot read falls back to the host's
# local time, which is what log_timezone is unless someone changed it.
parse_line() {
    local line="$1" zone
    line_time=""
    line_epoch=0
    line_body="$line"
    if [[ "$line" =~ ^([0-9]{4}-[0-9]{2}-[0-9]{2})T([0-9]{2}:[0-9]{2}:[0-9]{2})[^[:space:]]*[[:space:]](.*)$ ]]; then
        line_time="${BASH_REMATCH[1]}T${BASH_REMATCH[2]}Z"
        line_body="${BASH_REMATCH[3]}"
        line_epoch=$(date -u -d "$line_time" +%s 2>/dev/null || echo 0)
    elif [[ "$line" =~ ^([0-9]{4}-[0-9]{2}-[0-9]{2})[[:space:]]([0-9]{2}:[0-9]{2}:[0-9]{2})(\.[0-9]+)?[[:space:]]?([A-Za-z]{2,5}|[+-][0-9]{2}(:?[0-9]{2})?)?[[:space:]](.*)$ ]]; then
        zone="${BASH_REMATCH[4]}"
        line_body="${BASH_REMATCH[6]}"
        line_epoch=$(date -u -d "${BASH_REMATCH[1]} ${BASH_REMATCH[2]} ${zone}" +%s 2>/dev/null \
            || date -d "${BASH_REMATCH[1]} ${BASH_REMATCH[2]}" +%s 2>/dev/null || echo 0)
        [ "$line_epoch" -gt 0 ] && line_time=$(date -u -d "@${line_epoch}" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)
    fi
}

total_lines=0
critical_window=0
critical_recent=0
warning_class=0
auth_failures=0
routine_fatal=0

latest_class="none"
latest_text="none"
last_line_time="none"

count_panic=0; text_panic=""; time_panic=""
count_corruption=0; text_corruption=""; time_corruption=""
count_storage=0; text_storage=""; time_storage=""
count_out_of_memory=0; text_out_of_memory=""; time_out_of_memory=""
count_process_crash=0; text_process_crash=""; time_process_crash=""
count_connection_exhaustion=0; text_connection_exhaustion=""; time_connection_exhaustion=""
count_fatal=0; text_fatal=""; time_fatal=""
count_checkpoint_pressure=0; text_checkpoint_pressure=""; time_checkpoint_pressure=""
count_deadlock=0; text_deadlock=""; time_deadlock=""

while IFS= read -r line || [ -n "$line" ]; do
    [ -z "$line" ] && continue
    parse_line "$line"
    # A file holds more than the window (one written for days, a .log.1 beside it). docker logs
    # already cut its stream at --since; a file's line is cut here, by its own time.
    if [ "$line_epoch" -gt 0 ] && [ "$line_epoch" -lt "$window_cutoff_epoch" ]; then
        continue
    fi
    total_lines=$((total_lines + 1))

    matched_class=""
    matched_severity=""

    # 1. CRITICAL class patterns (first class that fits). The texts are PostgreSQL's own, checked
    # against what it writes (review of 1.165, 2026-10-16): a checksum failure on a page read is
    # "page verification failed, calculated checksum N but expected M" - "checksum verification
    # failed" is pg_basebackup's - and a TOAST value's missing chunk, a MultiXact past wraparound, and
    # the two lines every crash-restart writes (the postmaster's "terminating any other active
    # server processes", the startup's "automatic recovery in progress") were not read at all.
    if [[ "$line" == *"PANIC:"* ]]; then
        matched_class="panic"
        matched_severity="CRITICAL"
    elif [[ "$line" == *"invalid page in block"* ]] \
      || [[ "$line" == *"could not read block"* ]] \
      || [[ "$line" == *"checksum verification failed"* ]] \
      || [[ "$line" == *"page verification failed"* ]] \
      || [[ "$line" == *"missing chunk number"*"for toast value"* ]] \
      || [[ "$line" == *"has not been created yet -- apparent wraparound"* ]] \
      || [[ "$line" == *"unexpected zero page"* ]] \
      || [[ "$line" == *"invalid memory alloc request"* ]] \
      || [[ "$line" == *"found xmin"*"from before relfrozenxid"* ]] \
      || [[ "$line" =~ found[[:space:]]xmin.*from[[:space:]]before[[:space:]]relfrozenxid ]] \
      || [[ "$line" == *"could not access status of transaction"* ]]; then
        matched_class="corruption"
        matched_severity="CRITICAL"
    elif [[ "$line" == *"No space left on device"* ]] \
      || [[ "$line" == *"could not write to file"* ]] \
      || [[ "$line" == *"could not extend file"* ]]; then
        matched_class="storage"
        matched_severity="CRITICAL"
    elif [[ "$line" == *"out of memory"* ]]; then
        matched_class="out_of_memory"
        matched_severity="CRITICAL"
    elif [[ "$line" == *"server process"*"was terminated by signal"* ]] \
      || [[ "$line" =~ server[[:space:]]process.*was[[:space:]]terminated[[:space:]]by[[:space:]]signal ]] \
      || [[ "$line" == *"terminating any other active server processes"* ]] \
      || [[ "$line" == *"automatic recovery in progress"* ]]; then
        matched_class="process_crash"
        matched_severity="CRITICAL"
    fi

    # 2. WARNING class patterns (checked if not matched by CRITICAL)
    if [ -z "$matched_class" ]; then
        # PostgreSQL's own checkpoint alarm (1.145): logged when checkpoints come closer together
        # than checkpoint_warning (30 s by default) - max_wal_size too small for the write load.
        # The engine's default signal for checkpoint pressure, read here instead of a ratio of
        # counters, which needs two collections subtracted.
        if [[ "$line" == *"checkpoints are occurring too frequently"* ]]; then
            matched_class="checkpoint_pressure"
            matched_severity="WARNING"
        elif [[ "$line" == *"ERROR:"*"deadlock detected"* ]]; then
            # PostgreSQL's "ERROR:  deadlock detected": one statement failed to break a cycle.
            matched_class="deadlock"
            matched_severity="WARNING"
        elif [[ "$line" == *"remaining connection slots are reserved"* ]] \
        || [[ "$line" == *"sorry, too many clients already"* ]]; then
            matched_class="connection_exhaustion"
            matched_severity="WARNING"
        elif [[ "$line" == *"password authentication failed"* ]]; then
            auth_failures=$((auth_failures + 1))
            routine_fatal=$((routine_fatal + 1))
        elif [[ "$line" == *"FATAL:"* ]]; then
            # Routine FATALs are counted but not graded
            if [[ "$line" == *"no pg_hba.conf entry"* ]] \
              || [[ "$line" == *"the database system is starting up"* ]] \
              || [[ "$line" == *"the database system is shutting down"* ]] \
              || [[ "$line" == *"terminating connection due to administrator command"* ]] \
              || [[ "$line" == *"connection to client lost"* ]] \
              || [[ "$line" == *"canceling statement due to"* ]]; then
                routine_fatal=$((routine_fatal + 1))
            else
                matched_class="fatal"
                matched_severity="WARNING"
            fi
        elif [[ "$line" == *"no pg_hba.conf entry"* ]] \
          || [[ "$line" == *"the database system is starting up"* ]] \
          || [[ "$line" == *"the database system is shutting down"* ]] \
          || [[ "$line" == *"terminating connection due to administrator command"* ]] \
          || [[ "$line" == *"connection to client lost"* ]] \
          || [[ "$line" == *"canceling statement due to"* ]]; then
            routine_fatal=$((routine_fatal + 1))
        fi
    fi

    if [ -n "$matched_class" ]; then
        clean_text=$(printf '%s' "$line_body" | tr '\r\n\t' '   ' | tr -s ' ')
        clean_text="${clean_text:0:300}"

        # A line whose time cannot be read is not evidence of now: it grades WARNING, never pages.
        # docker logs always carries a time; a file with a custom log_line_prefix may not.
        is_recent=0
        if [ "$line_epoch" -gt 0 ] && [ "$recent_cutoff_epoch" -gt 0 ]; then
            if [ "$line_epoch" -ge "$recent_cutoff_epoch" ]; then
                is_recent=1
            fi
        fi

        if [ "$matched_severity" = "CRITICAL" ]; then
            critical_window=$((critical_window + 1))
            if [ "$is_recent" -eq 1 ]; then
                critical_recent=$((critical_recent + 1))
            fi
        elif [ "$matched_severity" = "WARNING" ]; then
            warning_class=$((warning_class + 1))
        fi

        latest_class="$matched_class"
        latest_text="$clean_text"
        last_line_time="${line_time:-none}"

        case "$matched_class" in
            panic)
                count_panic=$((count_panic + 1))
                text_panic="$clean_text"
                time_panic="${line_time:-none}"
                ;;
            corruption)
                count_corruption=$((count_corruption + 1))
                text_corruption="$clean_text"
                time_corruption="${line_time:-none}"
                ;;
            storage)
                count_storage=$((count_storage + 1))
                text_storage="$clean_text"
                time_storage="${line_time:-none}"
                ;;
            out_of_memory)
                count_out_of_memory=$((count_out_of_memory + 1))
                text_out_of_memory="$clean_text"
                time_out_of_memory="${line_time:-none}"
                ;;
            process_crash)
                count_process_crash=$((count_process_crash + 1))
                text_process_crash="$clean_text"
                time_process_crash="${line_time:-none}"
                ;;
            connection_exhaustion)
                count_connection_exhaustion=$((count_connection_exhaustion + 1))
                text_connection_exhaustion="$clean_text"
                time_connection_exhaustion="${line_time:-none}"
                ;;
            checkpoint_pressure)
                count_checkpoint_pressure=$((count_checkpoint_pressure + 1))
                text_checkpoint_pressure="$clean_text"
                time_checkpoint_pressure="${line_time:-none}"
                ;;
            deadlock)
                count_deadlock=$((count_deadlock + 1))
                text_deadlock="$clean_text"
                time_deadlock="${line_time:-none}"
                ;;
            fatal)
                count_fatal=$((count_fatal + 1))
                text_fatal="$clean_text"
                time_fatal="${line_time:-none}"
                ;;
        esac
    fi
done <<< "$log_output"

graded_count=$((critical_window + warning_class))

if [ "$critical_recent" -gt 0 ] || [ "$count_deadlock" -ge "$deadlock_critical_count" ]; then
    overall_status="CRITICAL"
elif [ "$critical_window" -gt 0 ] || [ "$warning_class" -gt 0 ]; then
    overall_status="WARNING"
else
    overall_status="OK"
fi

summary_msg="source=${source_label% }, window_hours=${window_hours}, critical_recent=${critical_recent}, critical_window=${critical_window}, warning_class=${warning_class}, auth_failures=${auth_failures}, routine_fatal=${routine_fatal}, deadlocks=${count_deadlock}, last_line_time=${last_line_time}, latest_class=${latest_class}, latest_text=${latest_text}"

# Summary row carries the overall grade
add_row "server_log" "$graded_count" "lines" "$overall_status" "$summary_msg"

# Up to 5 detail rows for classes seen (LOGGING status so they do not duplicate alerts)
detail_count=0
for c in panic corruption storage out_of_memory process_crash deadlock connection_exhaustion checkpoint_pressure fatal; do
    [ "$detail_count" -ge 5 ] && break
    case "$c" in
        panic)
            if [ "$count_panic" -gt 0 ]; then
                add_row "server_log :: panic" "$count_panic" "lines" "LOGGING" "$text_panic"
                detail_count=$((detail_count + 1))
            fi
            ;;
        corruption)
            if [ "$count_corruption" -gt 0 ]; then
                add_row "server_log :: corruption" "$count_corruption" "lines" "LOGGING" "$text_corruption"
                detail_count=$((detail_count + 1))
            fi
            ;;
        storage)
            if [ "$count_storage" -gt 0 ]; then
                add_row "server_log :: storage" "$count_storage" "lines" "LOGGING" "$text_storage"
                detail_count=$((detail_count + 1))
            fi
            ;;
        out_of_memory)
            if [ "$count_out_of_memory" -gt 0 ]; then
                add_row "server_log :: out_of_memory" "$count_out_of_memory" "lines" "LOGGING" "$text_out_of_memory"
                detail_count=$((detail_count + 1))
            fi
            ;;
        process_crash)
            if [ "$count_process_crash" -gt 0 ]; then
                add_row "server_log :: process_crash" "$count_process_crash" "lines" "LOGGING" "$text_process_crash"
                detail_count=$((detail_count + 1))
            fi
            ;;
        deadlock)
            if [ "$count_deadlock" -gt 0 ]; then
                add_row "server_log :: deadlock" "$count_deadlock" "lines" "LOGGING" "$text_deadlock"
                detail_count=$((detail_count + 1))
            fi
            ;;
        connection_exhaustion)
            if [ "$count_connection_exhaustion" -gt 0 ]; then
                add_row "server_log :: connection_exhaustion" "$count_connection_exhaustion" "lines" "LOGGING" "$text_connection_exhaustion"
                detail_count=$((detail_count + 1))
            fi
            ;;
        checkpoint_pressure)
            if [ "$count_checkpoint_pressure" -gt 0 ]; then
                add_row "server_log :: checkpoint_pressure" "$count_checkpoint_pressure" "lines" "LOGGING" "$text_checkpoint_pressure"
                detail_count=$((detail_count + 1))
            fi
            ;;
        fatal)
            if [ "$count_fatal" -gt 0 ]; then
                add_row "server_log :: fatal" "$count_fatal" "lines" "LOGGING" "$text_fatal"
                detail_count=$((detail_count + 1))
            fi
            ;;
    esac
done

emit
