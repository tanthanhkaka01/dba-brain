#!/bin/bash
json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
top_n=${OS_TOP_N:-5}
printf '['
first=1
# Group by command name: an application running many worker processes is one line, not N
# lines each below the reporting threshold. rss is KB.
while read -r comm rss_kb mem_pct count; do
  [ -n "$comm" ] || continue
  memory_mb=$(awk -v kb="$rss_kb" 'BEGIN { printf "%.2f", kb/1024 }')
  status=$(awk -v v="$mem_pct" 'BEGIN { if (v>=60) print "WARN"; else print "OK" }')
  [ "$first" -eq 1 ] || printf ','
  first=0
  printf '{"metric_item":"%s","metric_value":"%s","metric_unit":"MB","status":"%s","message":"%s"}' \
    "$(json_escape "$comm")" "$(json_escape "$memory_mb")" "$status" \
    "$(json_escape "process=$comm, memory_mb=$memory_mb, memory_percent=$mem_pct, process_count=$count")"
done < <(ps -eo comm=,rss=,%mem= 2>/dev/null |
  awk '{rss[$1]+=$2; pct[$1]+=$3; n[$1]+=1} END {for (c in rss) printf "%s %d %.2f %d\n", c, rss[c], pct[c], n[c]}' |
  sort -k2 -nr | head -n "$top_n")
if [ "$first" -eq 1 ]; then
  printf '{"metric_item":"top_memory","metric_value":"0","metric_unit":"MB","status":"OK","message":"No process was returned."}'
fi
printf ']\n'
