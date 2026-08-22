#!/bin/bash
json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

# Prefer interfaces backed by a device (physical/virtio NICs, not bridges or veth). A
# container has none of those, so fall back to every non-loopback interface that is up
# rather than reporting the host as having no network at all.
interfaces=""
for path in /sys/class/net/*; do
  iface=$(basename "$path")
  [ "$iface" = "lo" ] && continue
  [ -d "$path/device" ] || continue
  interfaces="$interfaces $iface"
done
if [ -z "$interfaces" ]; then
  for path in /sys/class/net/*; do
    iface=$(basename "$path")
    [ "$iface" = "lo" ] && continue
    interfaces="$interfaces $iface"
  done
fi

# tx_bytes/rx_bytes are cumulative since boot: a lifetime byte count says nothing about what the
# link is doing now. One second apart turns them into throughput.
declare -A tx0 rx0
for iface in $interfaces; do
  tx0[$iface]=$(cat "/sys/class/net/$iface/statistics/tx_bytes" 2>/dev/null || echo 0)
  rx0[$iface]=$(cat "/sys/class/net/$iface/statistics/rx_bytes" 2>/dev/null || echo 0)
done
sleep 1

printf '['
first=1
for iface in $interfaces; do
  path="/sys/class/net/$iface"
  operstate=$(cat "$path/operstate" 2>/dev/null || echo "unknown")
  [ "$operstate" = "up" ] || [ "$operstate" = "unknown" ] || continue
  speed=$(cat "$path/speed" 2>/dev/null || echo "")
  case "$speed" in ''|*[!0-9-]*) speed=0 ;; esac
  ip=$(ip -4 -o addr show dev "$iface" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | tr '\n' ' ' | sed 's/ *$//')
  sent=$(cat "$path/statistics/tx_bytes" 2>/dev/null || echo 0)
  received=$(cat "$path/statistics/rx_bytes" 2>/dev/null || echo 0)
  send_mbps=$(awk -v a="$sent" -v b="${tx0[$iface]:-0}" 'BEGIN { d=(a-b)*8/1000000; if (d<0) d=0; printf "%.2f", d }')
  recv_mbps=$(awk -v a="$received" -v b="${rx0[$iface]:-0}" 'BEGIN { d=(a-b)*8/1000000; if (d<0) d=0; printf "%.2f", d }')
  tx_err=$(cat "$path/statistics/tx_errors" 2>/dev/null || echo 0)
  rx_err=$(cat "$path/statistics/rx_errors" 2>/dev/null || echo 0)
  tx_drop=$(cat "$path/statistics/tx_dropped" 2>/dev/null || echo 0)
  rx_drop=$(cat "$path/statistics/rx_dropped" 2>/dev/null || echo 0)
  errors=$((tx_err + rx_err))
  dropped=$((tx_drop + rx_drop))
  if [ "$errors" -gt 0 ]; then status="WARN"; else status="OK"; fi
  [ "$first" -eq 1 ] || printf ','
  first=0
  printf '{"metric_item":"%s","metric_value":"%s","metric_unit":"text","status":"%s","message":"%s"}' \
    "$(json_escape "$iface")" "$(json_escape "$ip")" "$status" \
    "$(json_escape "link=$operstate, speed_mbps=$speed, bytes_sent=$sent, bytes_received=$received, errors=$errors, dropped=$dropped")"
  # Throughput gets its own rows so each is a chartable series (the row above carries an IP).
  printf ',{"metric_item":"%s","metric_value":"%s","metric_unit":"Mbps","status":"OK","message":"%s"}' \
    "$(json_escape "$iface send")" "$send_mbps" \
    "$(json_escape "$iface is sending $send_mbps Mbps (link speed $speed Mbps).")"
  printf ',{"metric_item":"%s","metric_value":"%s","metric_unit":"Mbps","status":"OK","message":"%s"}' \
    "$(json_escape "$iface receive")" "$recv_mbps" \
    "$(json_escape "$iface is receiving $recv_mbps Mbps (link speed $speed Mbps).")"
done
if [ "$first" -eq 1 ]; then
  printf '{"metric_item":"network","metric_value":"UNKNOWN","metric_unit":"text","status":"UNKNOWN","message":"No physical network interface is up."}'
fi
printf ']\n'
