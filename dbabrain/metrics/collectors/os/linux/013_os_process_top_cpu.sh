#!/bin/bash
json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
top_n=${OS_TOP_N:-5}
logical=$(grep -c '^processor' /proc/cpuinfo 2>/dev/null || echo 1)
printf '['
first=1
while read -r pid comm cpu mem rss_kb; do
  [ -n "$pid" ] || continue
  memory_mb=$(awk -v kb="$rss_kb" 'BEGIN { printf "%.2f", kb/1024 }')
  status=$(awk -v v="$cpu" 'BEGIN { if (v>=90) print "WARN"; else print "OK" }')
  [ "$first" -eq 1 ] || printf ','
  first=0
  printf '{"metric_item":"%s","metric_value":"%s","metric_unit":"percent","status":"%s","message":"%s"}' \
    "$(json_escape "$comm")" "$(json_escape "$cpu")" "$status" \
    "$(json_escape "process=$comm, pid=$pid, cpu_percent=$cpu, memory_mb=$memory_mb, memory_percent=$mem, logical_cpus=$logical")"
done < <(ps -eo pid=,comm=,%cpu=,%mem=,rss= --sort=-%cpu 2>/dev/null | head -n "$top_n")
if [ "$first" -eq 1 ]; then
  printf '{"metric_item":"top_cpu","metric_value":"0","metric_unit":"percent","status":"OK","message":"No process is consuming CPU."}'
fi
printf ']\n'
