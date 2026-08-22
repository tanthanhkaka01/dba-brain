#!/bin/bash
json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
printf '['
first=1
# df -PT columns: device, fstype, 1K-blocks, used, available, use%, mount point.
while read -r device fstype total_kb used_kb free_kb usedpct mount; do
  pct=${usedpct%\%}
  case "$pct" in ''|*[!0-9]*) continue ;; esac
  if [ "$pct" -ge 95 ]; then status="CRITICAL"; elif [ "$pct" -ge 85 ]; then status="WARN"; else status="OK"; fi
  total_gb=$(awk -v kb="$total_kb" 'BEGIN { printf "%.2f", kb/1024/1024 }')
  used_gb=$(awk -v kb="$used_kb" 'BEGIN { printf "%.2f", kb/1024/1024 }')
  free_gb=$(awk -v kb="$free_kb" 'BEGIN { printf "%.2f", kb/1024/1024 }')
  free_pct=$(awk -v p="$pct" 'BEGIN { printf "%.2f", 100-p }')
  [ "$first" -eq 1 ] || printf ','
  first=0
  printf '{"metric_item":"%s","metric_value":"%s","metric_unit":"percent","status":"%s","message":"%s"}' \
    "$(json_escape "$mount")" "$(json_escape "$pct")" "$status" \
    "$(json_escape "$mount usage is $pct percent. total_gb=$total_gb, used_gb=$used_gb, free_gb=$free_gb, free_percent=$free_pct, filesystem=$fstype, device=$device")"
done < <(df -PT -x tmpfs -x devtmpfs -x overlay -x squashfs 2>/dev/null | tail -n +2)

# How busy the storage is, not just how full — from a 1-second /proc/diskstats delta.
# Fields: 4 = reads completed, 6 = sectors read, 8 = writes completed, 10 = sectors written.
# A sector is 512 bytes here regardless of the physical sector size (the kernel reports it so).
# Physical block devices only: partitions and loop devices would double-count.
if [ -r /proc/diskstats ]; then
  snapshot() {
    awk '$3 ~ /^(sd[a-z]+|nvme[0-9]+n[0-9]+|vd[a-z]+|xvd[a-z]+)$/ {r+=$4; rs+=$6; w+=$8; ws+=$10}
         END {print r+0, w+0, rs+0, ws+0}' /proc/diskstats
  }
  read r1 w1 rs1 ws1 < <(snapshot)
  sleep 1
  read r2 w2 rs2 ws2 < <(snapshot)
  reads=$((r2 - r1))
  writes=$((w2 - w1))
  iops=$((reads + writes))
  read_kbps=$(awk -v s="$((rs2 - rs1))" 'BEGIN { printf "%.2f", (s * 512) / 1024 }')
  write_kbps=$(awk -v s="$((ws2 - ws1))" 'BEGIN { printf "%.2f", (s * 512) / 1024 }')

  [ "$first" -eq 1 ] || printf ','
  first=0
  printf '{"metric_item":"disk_iops","metric_value":"%s","metric_unit":"iops","status":"OK","message":"%s"}' \
    "$iops" "$(json_escape "Disk IO is $iops operations/s (all physical disks). read_iops=$reads, write_iops=$writes")"
  printf ',{"metric_item":"disk_read_kbps","metric_value":"%s","metric_unit":"KB/s","status":"OK","message":"%s"}' \
    "$read_kbps" "$(json_escape "Disk read throughput is $read_kbps KB/s (all physical disks).")"
  printf ',{"metric_item":"disk_write_kbps","metric_value":"%s","metric_unit":"KB/s","status":"OK","message":"%s"}' \
    "$write_kbps" "$(json_escape "Disk write throughput is $write_kbps KB/s (all physical disks).")"
fi
printf ']\n'
