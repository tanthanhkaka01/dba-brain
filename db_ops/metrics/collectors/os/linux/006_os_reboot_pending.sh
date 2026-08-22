#!/bin/bash
json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
pending=false
message="No reboot pending indicator was found."
if [ -f /var/run/reboot-required ]; then
  pending=true
  message="/var/run/reboot-required exists."
elif command -v needs-restarting >/dev/null 2>&1; then
  needs-restarting -r >/dev/null 2>&1
  rc=$?
  if [ "$rc" -ne 0 ]; then pending=true; message="needs-restarting reports that reboot is required."; fi
fi
status=$([ "$pending" = true ] && echo "WARN" || echo "OK")
printf '[{"metric_item":"reboot_pending","metric_value":"%s","metric_unit":"boolean","status":"%s","message":"%s"}]\n' \
  "$pending" "$status" "$(json_escape "$message")"
