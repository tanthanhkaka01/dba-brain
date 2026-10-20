#!/bin/bash
# OS_CPU_COUNTERS: the CPU this host - and the database process on it - has spent since boot, raw.
#
# OS_CPU_USAGE is one reading of one second, once an hour: right for "is it busy now" and no use
# for "how busy was the last hour", which is what the server page's Workload table asks. These are
# the cumulative totals the kernel keeps, so the report can subtract two collections and get the
# interval exactly (dbabrain.lib.interval_rates). Nothing here is graded.
#
#   host_busy_ms / host_idle_ms - /proc/stat's cpu line summed over every CPU. iowait counts as
#     idle: the CPU was free to run anything, the work was waiting on a disk.
#   db_cpu_ms - the database engine's process tree: its root's own and reaped-children times plus
#     every live descendant's. A PostgreSQL backend that exits hands its time to the postmaster's
#     reaped-children total, so the sum only ever grows while the postmaster lives - the one CPU
#     total PostgreSQL has, since the engine keeps none of its own.
#
# Which process is the database: the container's init when DBABRAIN_DB_CONTAINER names one, else
# the only root process of the engine on the host, else the one whose command line names the
# target's port. Three clusters on one host and no port on any command line is not guessed at:
# db_cpu_ms is left out and db_process says why. /proc/<pid>/stat is world-readable, so none of
# this needs root.
json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
row() {
  printf '[{"metric_item":"%s","metric_value":"%s","metric_unit":"%s","status":"%s","message":"%s"}]\n' \
    "$(json_escape "$1")" "$(json_escape "$2")" "$(json_escape "$3")" "$(json_escape "$4")" "$(json_escape "$5")"
}
emit() {
  [ "$first" -eq 1 ] || printf ','
  first=0
  printf '{"metric_item":"%s","metric_value":"%s","metric_unit":"%s","status":"%s","message":"%s"}' \
    "$(json_escape "$1")" "$(json_escape "$2")" "$(json_escape "$3")" "$4" "$(json_escape "$5")"
}
stamp() { date -d "@$1" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || printf '%s' "$1"; }

if [ ! -r /proc/stat ]; then
  row "host_busy_ms" "UNKNOWN" "ms" "UNKNOWN" "/proc/stat is not readable."
  exit 0
fi
hz=$(getconf CLK_TCK 2>/dev/null || echo 100)
[ -n "$hz" ] && [ "$hz" -gt 0 ] 2>/dev/null || hz=100
btime=$(awk '/^btime / {print $2}' /proc/stat)
read busy_ticks idle_ticks < <(awk '/^cpu / {printf "%.0f %.0f\n", $2+$3+$4+$7+$8+$9, $5+$6}' /proc/stat)
logical=$(grep -c '^processor' /proc/cpuinfo 2>/dev/null || echo 0)
host_busy_ms=$(awk -v t="$busy_ticks" -v hz="$hz" 'BEGIN { printf "%.0f", t * 1000 / hz }')
host_idle_ms=$(awk -v t="$idle_ticks" -v hz="$hz" 'BEGIN { printf "%.0f", t * 1000 / hz }')
since=$(stamp "$btime")

# Every process as "pid ppid comm ticks starttime", ticks = utime + stime + cutime + cstime. The
# command name is parsed between the first ( and the last ), because it may hold spaces.
procs=$(cat /proc/[0-9]*/stat 2>/dev/null | awk '{
  line = $0; i = index(line, "("); j = 0
  for (k = length(line); k > 0; k--) if (substr(line, k, 1) == ")") { j = k; break }
  if (i == 0 || j == 0) next
  pid = substr(line, 1, i - 2); comm = substr(line, i + 1, j - i - 1)
  gsub(/ /, "_", comm)
  n = split(substr(line, j + 2), f, " ")
  if (n < 20) next
  # printf, not print: mawk prints an integer past 2^31 in exponent form, and these pass it.
  printf "%s %s %s %.0f %.0f\n", pid, f[2], comm, f[12] + f[13] + f[14] + f[15], f[20]
}')

case "${DBABRAIN_DB_TYPE:-}" in
  sqlserver) engine="sqlservr" ;;
  postgresql|postgres) engine="postgres" ;;
  *) engine="" ;;
esac
port="${DBABRAIN_DB_PORT:-}"
root=""
db_process="none"
if [ -n "$engine" ]; then
  if [ -n "${DBABRAIN_DB_CONTAINER:-}" ] && command -v docker >/dev/null 2>&1; then
    root=$(docker inspect -f '{{.State.Pid}}' "$DBABRAIN_DB_CONTAINER" 2>/dev/null)
    [ "$root" = "0" ] && root=""
    [ -n "$root" ] && db_process="container $DBABRAIN_DB_CONTAINER init pid $root"
  fi
  if [ -z "$root" ]; then
    roots=$(printf '%s\n' "$procs" | awk -v e="$engine" '
      { comm[$1] = $3; parent[$1] = $2 }
      END { for (p in comm) if (comm[p] == e && comm[parent[p]] != e) print p }')
    count=$(printf '%s\n' "$roots" | grep -c '[0-9]')
    if [ "$count" -gt 1 ] && [ -z "${DBABRAIN_DB_CONTAINER:-}" ]; then
      # A target that is not in a container is not one of the containers' engines. Measured on a
      # host running SQL Server natively beside a containerised one: two sqlservr roots, and the
      # cgroup is what tells them apart - the command lines are identical and name no port.
      native=""
      for candidate in $roots; do
        grep -Eq 'docker|containerd|kubepods|libpod|lxc' "/proc/$candidate/cgroup" 2>/dev/null \
          || native="$native $candidate"
      done
      native_count=$(printf '%s\n' $native | grep -c '[0-9]')
      if [ "$native_count" -eq 1 ]; then
        roots=$(printf '%s\n' $native)
        count=1
      elif [ "$native_count" -eq 0 ]; then
        # Every candidate is containerised and the inventory names no container for this target:
        # the fix is in db_instances.json, and the message says so rather than picking one.
        db_process="none: $count $engine processes on this host and all are in containers - set container_name on the target"
        count=-1
      fi
    fi
    if [ "$count" -eq 1 ]; then
      root=$(printf '%s\n' "$roots" | grep '[0-9]')
      db_process="$engine pid $root"
    elif [ "$count" -gt 1 ] && [ -n "$port" ]; then
      for candidate in $roots; do
        cmdline=$(tr '\0' ' ' < "/proc/$candidate/cmdline" 2>/dev/null)
        if printf '%s' "$cmdline" | grep -Eq -- "(-p|--port|port=)[ =]?$port( |$)"; then
          root=$candidate
          db_process="$engine pid $root matched by port $port"
          break
        fi
      done
      [ -z "$root" ] && db_process="none: $count $engine root processes and none names port $port"
    elif [ "$count" -gt 1 ]; then
      db_process="none: $count $engine root processes and no port to tell them apart"
    elif [ "$count" -eq 0 ]; then
      db_process="none: no $engine process on this host"
    fi
  fi
fi

db_ticks=""
process_since=""
if [ -n "$root" ]; then
  # The tree under the root, every level: a parent's reaped-children time holds what its exited
  # children spent, so leaving a level out would make the total fall when a process exits.
  read db_ticks start_ticks < <(printf '%s\n' "$procs" | awk -v r="$root" '
    { parent[$1] = $2; ticks[$1] = $4; start[$1] = $5 }
    END {
      total = 0
      for (p in parent) {
        q = p; hops = 0
        while (q != "" && q != r && q != "0" && q != "1" && hops < 64) { q = parent[q]; hops++ }
        if (q == r) total += ticks[p]
      }
      if (!(r in ticks)) { print "", ""; exit }
      printf "%.0f %.0f\n", total, start[r]
    }')
  if [ -n "$db_ticks" ]; then
    process_since=$(stamp "$((btime + start_ticks / hz))")
  else
    db_process="none: pid $root is gone"
  fi
fi

tail="counters_since=$since, logical_cpus=$logical, process_since=${process_since:-none}, db_process=$db_process, source=/proc/stat, note=cumulative_since_boot_subtract_two_collections"
first=1
printf '['
emit "host_busy_ms" "$host_busy_ms" "ms" "OK" "value=$host_busy_ms, $tail"
emit "host_idle_ms" "$host_idle_ms" "ms" "OK" "value=$host_idle_ms, $tail"
if [ -n "$db_ticks" ]; then
  db_cpu_ms=$(awk -v t="$db_ticks" -v hz="$hz" 'BEGIN { printf "%.0f", t * 1000 / hz }')
  emit "db_cpu_ms" "$db_cpu_ms" "ms" "OK" "value=$db_cpu_ms, $tail"
fi
printf ']\n'
