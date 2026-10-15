#!/bin/bash
json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
payload="["
first=1
while read -r pid comm cpu mem; do
  [ -n "$pid" ] || continue
  [ "$first" -eq 1 ] || payload="$payload,"
  first=0
  payload="$payload{\"pid\":\"$(json_escape "$pid")\",\"name\":\"$(json_escape "$comm")\",\"cpu_percent\":\"$(json_escape "$cpu")\",\"memory_percent\":\"$(json_escape "$mem")\"}"
done < <(ps -eo pid=,comm=,%cpu=,%mem= --sort=-%cpu 2>/dev/null | head -n 5)
payload="$payload]"
printf '[{"metric_item":"top_processes","metric_value":"%s","metric_unit":"json","status":"OK","message":"Top processes by current CPU percent."}]\n' "$(json_escape "$payload")"
