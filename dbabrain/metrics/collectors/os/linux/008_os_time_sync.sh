#!/bin/bash
json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
value="UNKNOWN"
status="UNKNOWN"
message="No supported time sync status command was found."
if command -v timedatectl >/dev/null 2>&1; then
  value=$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)
  if [ "$value" = "yes" ]; then status="OK"; elif [ "$value" = "no" ]; then status="WARN"; else status="UNKNOWN"; fi
  message="timedatectl NTPSynchronized is ${value:-UNKNOWN}."
elif command -v chronyc >/dev/null 2>&1; then
  if chronyc tracking >/dev/null 2>&1; then value="synchronized"; status="OK"; message="chronyc tracking succeeded."; else value="unsynchronized"; status="WARN"; message="chronyc tracking failed."; fi
elif command -v ntpq >/dev/null 2>&1; then
  if ntpq -p >/dev/null 2>&1; then value="synchronized"; status="OK"; message="ntpq peers query succeeded."; else value="unsynchronized"; status="WARN"; message="ntpq peers query failed."; fi
fi
printf '[{"metric_item":"time_sync","metric_value":"%s","metric_unit":"status","status":"%s","message":"%s"}]\n' \
  "$(json_escape "$value")" "$status" "$(json_escape "$message")"
