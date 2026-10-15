#!/bin/bash
# DOCKER_CONTAINER_STATS (collector_type=docker): per-container state + resource usage for the
# container named in $DOCKER_CONTAINER (injected by the docker collector from the target's
# container_name). Runs the docker CLI against whichever daemon the collector reaches it on:
# locally the worker mounts /var/run/docker.sock; for a remote docker host the collector ships
# this script over SSH (target cmd_access.method=ssh) so it runs against that host's daemon.
# Emits the standard metric JSON contract:
# [{metric_item, metric_value, metric_unit, status, message}, ...]. One target = one container,
# so a DB-in-Docker target collects sql + docker. Thresholds: not-running=CRITICAL,
# restarting=WARNING, a restart in the last DOCKER_RESTART_WARN_MINUTES (default 60)=WARNING,
# memory>=90%=WARNING; everything else OK/LOGGING.
set -u

container="${DOCKER_CONTAINER:-}"
# Docker's RestartCount only grows, so "restarts > 0" warned on every pass for months after a single
# restart (review 0.25.0, F3.4). A policy restart resets StartedAt, so a restart is news while the
# current run is younger than this; older ones are history, still named in the message.
restart_warn_minutes="${DOCKER_RESTART_WARN_MINUTES:-60}"
rows=()

# JSON string escaping. The sed version escaped backslash and quote only, so a tab or a newline in
# any docker field made the whole metric "stdout is not valid JSON" (F3.4). A control character with
# no short escape is dropped - it has no meaning in these fields. Every backslash comes from `$bs`:
# how a literal one in a ${s//x/y} replacement is quote-removed differs between bash versions (5.2
# dropped it), and a quoted variable reads the same on 4.4, 5.1 and 5.3.
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

if [ -z "$container" ]; then
    printf '[{"metric_item":"container","metric_value":"UNKNOWN","metric_unit":"status","status":"UNKNOWN","message":"DOCKER_CONTAINER is not set."}]\n'
    exit 0
fi

inspect=$(docker inspect "$container" --format '{{.State.Status}}|{{.State.Running}}|{{.State.Restarting}}|{{.RestartCount}}|{{.State.StartedAt}}' 2>/dev/null)
if [ -z "$inspect" ]; then
    add_row "${container}:status" "not_found" "null" "CRITICAL" "docker inspect failed (container not found, renamed, or docker unavailable)."
    emit
    exit 0
fi

IFS='|' read -r status running restarting restart_count started_at <<< "$inspect"
restart_count="${restart_count:-0}"

if [ "$running" = "true" ] && [ "$restarting" != "true" ]; then
    state_status="OK"
elif [ "$restarting" = "true" ]; then
    state_status="WARNING"
else
    state_status="CRITICAL"
fi
add_row "${container}:status" "$status" "null" "$state_status" \
    "container status=${status} running=${running} restart_count=${restart_count} started_at=${started_at}"

restart_note="container has restarted ${restart_count} time(s)."
case "$restart_warn_minutes" in
    ''|*[!0-9]*)
        restart_note="${restart_note} DOCKER_RESTART_WARN_MINUTES=${restart_warn_minutes} is not a whole number of minutes; 60 used."
        restart_warn_minutes=60 ;;
esac
case "$restart_count" in
    ''|*[!0-9]*|0) restart_status="OK" ;;
    *)
        started_epoch=$(date -d "$started_at" +%s 2>/dev/null || true)
        if [ -z "$started_epoch" ]; then
            # Not knowing when it restarted is not knowing it is history: warn, and say why.
            restart_status="WARNING"
            restart_note="${restart_note} The start time (${started_at}) could not be read, so it may be recent."
        else
            running_minutes=$(( ($(date +%s) - started_epoch) / 60 ))
            if [ "$running_minutes" -lt "$restart_warn_minutes" ]; then
                restart_status="WARNING"
                restart_note="${restart_note} The current run started ${running_minutes} minute(s) ago."
            else
                restart_status="OK"
                restart_note="${restart_note} Running for ${running_minutes} minute(s) since the last start."
            fi
        fi
        ;;
esac
add_row "${container}:restart_count" "$restart_count" "count" "$restart_status" "$restart_note"

if [ "$running" = "true" ]; then
    stats=$(docker stats "$container" --no-stream --format '{{.CPUPerc}}|{{.MemPerc}}|{{.MemUsage}}|{{.NetIO}}|{{.BlockIO}}|{{.PIDs}}' 2>/dev/null)
    if [ -n "$stats" ]; then
        IFS='|' read -r cpu memp memu netio blockio pids <<< "$stats"
        cpu_num="${cpu%\%}"
        memp_num="${memp%\%}"
        add_row "${container}:cpu" "$cpu_num" "percent" "OK" "cpu=${cpu}"
        mem_status="OK"
        if awk "BEGIN{exit !((${memp_num:-0})+0 >= 90)}" 2>/dev/null; then mem_status="WARNING"; fi
        add_row "${container}:memory" "$memp_num" "percent" "$mem_status" "mem_usage=${memu} mem_pct=${memp}"
        add_row "${container}:net_io" "$netio" "io" "LOGGING" "net_io=${netio}"
        add_row "${container}:block_io" "$blockio" "io" "LOGGING" "block_io=${blockio}"
        add_row "${container}:pids" "$pids" "count" "LOGGING" "pids=${pids}"
    fi
fi

emit
