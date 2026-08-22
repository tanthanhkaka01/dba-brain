#!/bin/bash
json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
if [ -r /proc/uptime ]; then
  uptime_seconds=$(awk '{printf "%d", $1}' /proc/uptime)
  status="OK"
  message="System uptime is $uptime_seconds seconds."
else
  uptime_seconds="UNKNOWN"
  status="UNKNOWN"
  message="/proc/uptime is not readable."
fi
printf '[{"metric_item":"uptime","metric_value":"%s","metric_unit":"seconds","status":"%s","message":"%s"}]\n' \
  "$(json_escape "$uptime_seconds")" "$status" "$(json_escape "$message")"
