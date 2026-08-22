#!/bin/bash
json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
emit() {
  [ "$first" -eq 1 ] || printf ','
  first=0
  printf '{"metric_item":"%s","metric_value":"%s","metric_unit":"%s","status":"%s","message":"%s"}' \
    "$(json_escape "$1")" "$(json_escape "$2")" "$(json_escape "$3")" "$4" "$(json_escape "$5")"
}

distro="unknown"
version=""
if [ -r /etc/os-release ]; then
  . /etc/os-release
  distro="${PRETTY_NAME:-$NAME}"
  version="${VERSION_ID:-}"
fi
kernel=$(uname -r)
arch=$(uname -m)
hostname=$(hostname -f 2>/dev/null || hostname)
timezone=$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo "unknown")
uptime_seconds=$(awk '{printf "%d", $1}' /proc/uptime 2>/dev/null || echo 0)
boot_epoch=$(( $(date +%s) - uptime_seconds ))
last_boot=$(date -u -d "@$boot_epoch" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo "unknown")

printf '['
first=1
emit "os_name" "$distro" "text" "OK" "os_family=Linux, edition=$distro, version=$version, build=$kernel, architecture=$arch"
emit "hostname" "$hostname" "text" "OK" "hostname=$hostname"
emit "timezone" "$timezone" "text" "OK" "timezone=$timezone"
emit "last_boot_time" "$last_boot" "timestamp" "OK" "uptime_seconds=$uptime_seconds"
printf ']\n'
