#!/bin/bash
# Linux counterpart of the Windows event log: journald priority <= 3 (err/crit/alert/emerg).
json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
hours=${OS_EVENTLOG_HOURS:-24}
# Bounded for the same reason as the Windows variant: everything below is derived from the
# count, the newest timestamp and the top 3 units, and pulling the whole window to get them is
# what made the Windows one time out during a 26,000-event storm on 2026-08-09. Here the whole
# journal window also lands in a single shell variable, which is the same unbounded read one
# language over. `-n` bounds it at journald rather than after the fact, and returns the MOST
# RECENT entries, so a truncated read still describes now.
max_events=${OS_EVENTLOG_MAX_EVENTS:-500}

if ! command -v journalctl >/dev/null 2>&1; then
  printf '[{"metric_item":"journal","metric_value":"UNKNOWN","metric_unit":"events","status":"UNKNOWN","message":"journalctl is not available on this host."}]\n'
  exit 0
fi

lines=$(journalctl -p 3 --since "-${hours}h" -n "$max_events" --no-pager -o short-iso 2>/dev/null)
count=$(printf '%s' "$lines" | grep -c . || true)
[ -n "$count" ] || count=0

if [ "$count" -eq 0 ]; then
  printf '[{"metric_item":"journal","metric_value":"0","metric_unit":"events","status":"OK","message":"%s"}]\n' \
    "$(json_escape "No error or higher journal events over the last $hours hours.")"
  exit 0
fi

if [ "$count" -ge 20 ]; then status="WARN"; else status="LOGGING"; fi
if [ "$count" -ge "$max_events" ]; then
  note=", truncated=yes, cap=$max_events, count_is_a_floor=yes (top ranks the newest $max_events only)"
else
  note=", truncated=no"
fi
latest=$(printf '%s\n' "$lines" | tail -n 1 | awk '{print $1}')
top=$(printf '%s\n' "$lines" | awk '{print $4}' | sed 's/\[[0-9]*\]:*$//' | sort | uniq -c | sort -nr | head -n 3 |
  awk '{printf "unit=%s count=%s; ", $2, $1}')
printf '[{"metric_item":"journal","metric_value":"%s","metric_unit":"events","status":"%s","message":"%s"}]\n' \
  "$count" "$status" "$(json_escape "window_hours=$hours, latest=$latest, top=$top$note")"
