#!/bin/bash
json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
row() {
  printf '[{"metric_item":"%s","metric_value":"%s","metric_unit":"%s","status":"%s","message":"%s"}]\n' \
    "$(json_escape "$1")" "$(json_escape "$2")" "$(json_escape "$3")" "$(json_escape "$4")" "$(json_escape "$5")"
}
if [ ! -r /proc/meminfo ]; then
  row "memory_usage" "UNKNOWN" "percent" "UNKNOWN" "/proc/meminfo is not readable."
  exit 0
fi
total=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
available=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
if [ -z "$available" ]; then
  free=$(awk '/^MemFree:/ {print $2}' /proc/meminfo)
  buffers=$(awk '/^Buffers:/ {print $2}' /proc/meminfo)
  cached=$(awk '/^Cached:/ {print $2}' /proc/meminfo)
  available=$((free + buffers + cached))
fi
if [ -z "$total" ] || [ "$total" -le 0 ]; then
  row "memory_usage" "UNKNOWN" "percent" "UNKNOWN" "Memory totals are unavailable."
  exit 0
fi
usage=$(awk -v total="$total" -v available="$available" 'BEGIN { printf "%.2f", ((total-available)/total)*100 }')
status=$(awk -v v="$usage" 'BEGIN { if (v>=95) print "CRITICAL"; else if (v>=85) print "WARN"; else print "OK" }')
total_gb=$(awk -v kb="$total" 'BEGIN { printf "%.2f", kb/1024/1024 }')
available_gb=$(awk -v kb="$available" 'BEGIN { printf "%.2f", kb/1024/1024 }')
used_gb=$(awk -v t="$total" -v a="$available" 'BEGIN { printf "%.2f", (t-a)/1024/1024 }')

swap_total=$(awk '/^SwapTotal:/ {print $2}' /proc/meminfo)
swap_free=$(awk '/^SwapFree:/ {print $2}' /proc/meminfo)
[ -n "$swap_total" ] || swap_total=0
[ -n "$swap_free" ] || swap_free=0
swap_total_gb=$(awk -v kb="$swap_total" 'BEGIN { printf "%.2f", kb/1024/1024 }')
swap_used_gb=$(awk -v t="$swap_total" -v f="$swap_free" 'BEGIN { printf "%.2f", (t-f)/1024/1024 }')
if [ "$swap_total" -gt 0 ]; then
  swap_pct=$(awk -v t="$swap_total" -v f="$swap_free" 'BEGIN { printf "%.2f", ((t-f)/t)*100 }')
  swap_status=$(awk -v v="$swap_pct" 'BEGIN { if (v>=90) print "WARN"; else print "OK" }')
else
  swap_pct="0.00"
  swap_status="OK"
fi

emit() {
  [ "$first" -eq 1 ] || printf ','
  first=0
  printf '{"metric_item":"%s","metric_value":"%s","metric_unit":"%s","status":"%s","message":"%s"}' \
    "$(json_escape "$1")" "$(json_escape "$2")" "$(json_escape "$3")" "$4" "$(json_escape "$5")"
}
# The percentage alone cannot be charted against capacity: 60% of 8 GB and 60% of 512 GB are
# different problems. The absolute figure is its own row so it has its own series.
used_mb=$(awk -v t="$total" -v a="$available" 'BEGIN { printf "%d", (t-a)/1024 }')
total_mb=$(awk -v kb="$total" 'BEGIN { printf "%d", kb/1024 }')

printf '['
first=1
emit "memory_usage" "$usage" "percent" "$status" "Memory usage is $usage percent. total_gb=$total_gb, used_gb=$used_gb, available_gb=$available_gb"
emit "memory_used_mb" "$used_mb" "MB" "OK" "Memory used is $used_mb MB of $total_mb MB."
emit "swap_usage" "$swap_pct" "percent" "$swap_status" "Swap usage is $swap_pct percent. swap_total_gb=$swap_total_gb, swap_used_gb=$swap_used_gb"
printf ']\n'
