#!/bin/bash
json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
services=${OS_SERVICE_NAMES:-sshd,cron,chronyd}

# The same role carries different unit names across distro families, so one OS_SERVICE_NAMES
# list has to work on RHEL and Debian/Ubuntu alike. Time sync is the clearest case: RHEL runs
# chronyd, Ubuntu 24.04 runs systemd-timesyncd, and reporting CRITICAL because the RHEL name
# is absent is a false alarm, not a finding — the host is synchronized, just by another daemon.
candidates_for() {
  case "$1" in
    sshd|ssh)                          printf 'sshd ssh' ;;
    cron|crond)                        printf 'cron crond' ;;
    chronyd|chrony|ntp|ntpd|ntpsec|systemd-timesyncd|timesync)
                                       printf 'chronyd chrony systemd-timesyncd ntpsec ntp ntpd' ;;
    *)                                 printf '%s' "$1" ;;
  esac
}

IFS=',' read -r -a names <<< "$services"
printf '['
first=1
for raw in "${names[@]}"; do
  name=$(printf '%s' "$raw" | xargs)
  [ -n "$name" ] || continue

  value=""
  status=""
  message=""

  if command -v systemctl >/dev/null 2>&1; then
    active_unit=""
    installed_unit=""
    installed_state=""
    for unit in $(candidates_for "$name"); do
      state=$(systemctl is-active "$unit" 2>/dev/null || true)
      if [ "$state" = "active" ]; then
        active_unit="$unit"
        break
      fi
      # `is-active` says "inactive" both for a stopped unit and for one that does not exist;
      # only a unit with a fragment is really installed, and only that is worth alerting on.
      if [ -z "$installed_unit" ] && systemctl cat "$unit" >/dev/null 2>&1; then
        installed_unit="$unit"
        installed_state="${state:-inactive}"
      fi
    done

    if [ -n "$active_unit" ]; then
      value="active"
      status="OK"
      if [ "$active_unit" = "$name" ]; then
        message="Service $name status is active."
      else
        message="Service $name status is active (unit $active_unit)."
      fi
    elif [ -n "$installed_unit" ]; then
      value="$installed_state"
      status="CRITICAL"
      message="Service $name status is $installed_state (unit $installed_unit)."
    else
      # Nothing by that name or any equivalent is installed: a configuration question
      # (wrong name for this distro), not an outage. UNKNOWN is reported as a WARNING.
      value="not-installed"
      status="UNKNOWN"
      message="No unit installed for $name (tried: $(candidates_for "$name" | tr ' ' ',' ))."
    fi
  else
    service "$name" status >/dev/null 2>&1
    rc=$?
    value=$([ "$rc" -eq 0 ] && echo "active" || echo "inactive")
    status=$([ "$rc" -eq 0 ] && echo "OK" || echo "CRITICAL")
    message="Service $name status is $value."
  fi

  [ "$first" -eq 1 ] || printf ','
  first=0
  printf '{"metric_item":"%s","metric_value":"%s","metric_unit":"status","status":"%s","message":"%s"}' \
    "$(json_escape "$name")" "$(json_escape "$value")" "$status" "$(json_escape "$message")"
done
printf ']\n'
