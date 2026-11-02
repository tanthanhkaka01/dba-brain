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
# memory>=90%=WARNING, CPU share of the CPUs it may use >=80% WARNING / >=90% CRITICAL, a mounted
# volume >=85% WARNING / >=95% CRITICAL; everything else OK/LOGGING.
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

# What 100% of this container's CPU is. `docker stats` counts in CPUs - 319% is 3.19 CPUs busy - so
# a page that printed it had no denominator (2026-10-26: a store container read "CPU 319%"). The
# number is read, never assumed: the container's own limit first (--cpus, a CFS quota, a cpuset),
# else the daemon's count of the host's CPUs. None of them readable leaves cpus empty and no share.
# The basis is one token (limit_cpus, limit_cpu_quota, cpuset_<list with ;>, host_no_limit): the
# message is read back as comma-separated key=value pairs, and a comma would cut the value.
cpu_limits=$(docker inspect "$container" --format '{{.HostConfig.NanoCpus}}|{{.HostConfig.CpuQuota}}|{{.HostConfig.CpuPeriod}}|{{.HostConfig.CpusetCpus}}' 2>/dev/null)
IFS='|' read -r nano_cpus cpu_quota cpu_period cpuset <<< "$cpu_limits"
host_cpus=$(docker info --format '{{.NCPU}}' 2>/dev/null)
cpus=""; cpus_basis=""
if [ "${nano_cpus:-0}" -gt 0 ] 2>/dev/null; then
    cpus=$(awk -v n="$nano_cpus" 'BEGIN { printf "%g", n / 1e9 }'); cpus_basis="limit_cpus"
elif [ "${cpu_quota:-0}" -gt 0 ] 2>/dev/null && [ "${cpu_period:-0}" -gt 0 ] 2>/dev/null; then
    cpus=$(awk -v q="$cpu_quota" -v p="$cpu_period" 'BEGIN { printf "%g", q / p }'); cpus_basis="limit_cpu_quota"
elif [ -n "$cpuset" ]; then
    cpus=$(printf '%s' "$cpuset" | awk -F, '{ n = 0; for (i = 1; i <= NF; i++) { split($i, r, "-"); n += (r[2] == "" ? 1 : r[2] - r[1] + 1) } print n }')
    cpus_basis="cpuset_${cpuset//,/;}"
elif [ "${host_cpus:-0}" -gt 0 ] 2>/dev/null; then
    cpus="$host_cpus"; cpus_basis="host_no_limit"
fi

if [ "$running" = "true" ]; then
    stats=$(docker stats "$container" --no-stream --format '{{.CPUPerc}}|{{.MemPerc}}|{{.MemUsage}}|{{.NetIO}}|{{.BlockIO}}|{{.PIDs}}' 2>/dev/null)
    if [ -n "$stats" ]; then
        IFS='|' read -r cpu memp memu netio blockio pids <<< "$stats"
        cpu_num="${cpu%\%}"
        memp_num="${memp%\%}"
        add_row "${container}:cpu" "$cpu_num" "percent" "OK" "cpu=${cpu}, cpus=${cpus:-unknown}, cpus_basis=${cpus_basis:-not_readable}, host_cpus=${host_cpus:-unknown}"
        if [ -n "$cpus" ]; then
            share=$(awk -v c="${cpu_num:-0}" -v n="$cpus" 'BEGIN { printf "%.2f", c / n }')
            busy=$(awk -v c="${cpu_num:-0}" 'BEGIN { printf "%.2f", c / 100 }')
            share_status=$(awk -v v="$share" 'BEGIN { if (v >= 90) print "CRITICAL"; else if (v >= 80) print "WARNING"; else print "OK" }')
            add_row "${container}:cpu_share" "$share" "percent" "$share_status" \
                "cpu_share_pct=${share}, cpus_busy=${busy}, cpus=${cpus}, cpus_basis=${cpus_basis}, host_cpus=${host_cpus:-unknown}"
        fi
        mem_status="OK"
        if awk "BEGIN{exit !((${memp_num:-0})+0 >= 90)}" 2>/dev/null; then mem_status="WARNING"; fi
        add_row "${container}:memory" "$memp_num" "percent" "$mem_status" "mem_usage=${memu} mem_pct=${memp}"
        add_row "${container}:net_io" "$netio" "io" "LOGGING" "net_io=${netio}"
        add_row "${container}:block_io" "$blockio" "io" "LOGGING" "block_io=${blockio}"
        add_row "${container}:pids" "$pids" "count" "LOGGING" "pids=${pids}"
    fi

    # How full each volume the container writes to is, as the container itself sees it - `df`
    # inside it, on every mount's destination. A database in a container has no host login, so
    # this is the only reading of the disk its data files are on. Mounts on one filesystem are
    # reported once, named by the first destination in sorted order - docker lists mounts in no
    # fixed order, and a name that changed between runs split one volume into two series
    # (2026-10-26). A container with no `df` reports nothing.
    seen_fs="|"
    while IFS= read -r dest; do
        [ -n "$dest" ] || continue
        line=$(docker exec "$container" df -P -B1 "$dest" 2>/dev/null | tail -n 1)
        [ -n "$line" ] || continue
        read -r fs_name fs_total fs_used fs_avail _fs_pct fs_mount <<< "$line"
        case "$fs_total" in ''|*[!0-9]*) continue ;; esac
        [ "$fs_total" -gt 0 ] || continue
        case "$seen_fs" in *"|${fs_name}:${fs_total}|"*) continue ;; esac
        seen_fs="${seen_fs}${fs_name}:${fs_total}|"
        vol=$(awk -v t="$fs_total" -v u="$fs_used" -v a="$fs_avail" 'BEGIN {
            pct = (u + a) > 0 ? u * 100 / (u + a) : 0
            printf "%.2f|%.2f|%.2f|%.2f", pct, t / 1073741824, u / 1073741824, a / 1073741824 }')
        IFS='|' read -r used_pct total_gb used_gb free_gb <<< "$vol"
        vol_status=$(awk -v v="$used_pct" 'BEGIN { if (v >= 95) print "CRITICAL"; else if (v >= 85) print "WARNING"; else print "OK" }')
        add_row "${container}:disk:${dest}" "$used_pct" "percent" "$vol_status" \
            "mount=${dest}, filesystem=${fs_name}, total_gb=${total_gb}, used_gb=${used_gb}, free_gb=${free_gb}, used_pct=${used_pct}"
    done <<< "$(docker inspect "$container" --format '{{range .Mounts}}{{.Destination}}{{println}}{{end}}' 2>/dev/null | sort)"
fi

emit
