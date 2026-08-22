#!/bin/bash
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
read_cpu() {
  awk '/^cpu / {print $2+$3+$4+$5+$6+$7+$8, $5}' /proc/stat
}
if [ ! -r /proc/stat ]; then
  row "cpu_usage" "UNKNOWN" "percent" "UNKNOWN" "/proc/stat is not readable."
  exit 0
fi
read total1 idle1 < <(read_cpu)
sleep 1
read total2 idle2 < <(read_cpu)
delta_total=$((total2 - total1))
delta_idle=$((idle2 - idle1))
if [ "$delta_total" -le 0 ]; then
  row "cpu_usage" "UNKNOWN" "percent" "UNKNOWN" "CPU counters did not advance."
  exit 0
fi
usage=$(awk -v total="$delta_total" -v idle="$delta_idle" 'BEGIN { printf "%.2f", ((total-idle)/total)*100 }')
status=$(awk -v v="$usage" 'BEGIN { if (v>=90) print "CRITICAL"; else if (v>=80) print "WARN"; else print "OK" }')

logical=$(grep -c '^processor' /proc/cpuinfo 2>/dev/null || echo 0)
sockets=$(awk -F': ' '/^physical id/ {print $2}' /proc/cpuinfo 2>/dev/null | sort -u | wc -l)
[ "$sockets" -ge 1 ] 2>/dev/null || sockets=1
cores_per_socket=$(awk -F': ' '/^cpu cores/ {print $2; exit}' /proc/cpuinfo 2>/dev/null)
[ -n "$cores_per_socket" ] || cores_per_socket=$logical
cores=$((sockets * cores_per_socket))
model=$(awk -F': ' '/^model name/ {print $2; exit}' /proc/cpuinfo 2>/dev/null)
[ -n "$model" ] || model="unknown"

read load1 load5 load15 rest < /proc/loadavg
load_status=$(awk -v l="$load1" -v c="$logical" 'BEGIN { if (c>0 && l >= 2*c) print "WARN"; else print "OK" }')

printf '['
first=1
emit "cpu_usage" "$usage" "percent" "$status" "Average CPU usage is $usage percent. model=$model, sockets=$sockets, cores=$cores, logical_cpus=$logical"
emit "load_average" "$load1" "processes" "$load_status" "load_1m=$load1, load_5m=$load5, load_15m=$load15, logical_cpus=$logical"
printf ']\n'
