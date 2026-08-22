#!/bin/bash
json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

# Same three-way answer as the Windows variant, and for the same reason: probing 127.0.0.1 alone
# cannot tell "nothing is listening" from "listening, but bound to the address clients use", and
# only one of those is an outage.
#
#   host open                  -> OPEN           OK        clients can reach it
#   host closed, loopback open -> LOOPBACK_ONLY  WARNING   serving, but not where clients knock
#   both closed                -> CLOSED         CRITICAL  nothing is listening
#
# DB_OPS_TARGET_HOST is injected from the inventory (the address the collector itself reached this
# host on); loopback stays the fallback when a target has no address recorded.
probe() {
  timeout 3 bash -c "cat < /dev/null > /dev/tcp/$1/$2" >/dev/null 2>&1
}

# TCP says a socket accepted the connection, not that the listener is serving. `443/https` adds a
# TLS handshake and one request; `443/tls` stops after the handshake. A failure here is WARNING,
# never CRITICAL: TLS and HTTP bring their own ways to fail that have nothing to do with the
# service being down (client certificates, SNI, a proxy in the path, a protocol floor), and this
# metric's CRITICAL has to stay the signal it has always been right about.
probe_health() {
  local target_host="$1" port="$2" scheme="$3"
  if ! command -v openssl >/dev/null 2>&1; then
    printf 'skipped: openssl not installed'
    return 0
  fi
  local handshake
  if ! handshake=$(printf '' | timeout 10 openssl s_client -connect "$target_host:$port" \
      -servername "$target_host" 2>/dev/null); then
    printf 'tls_failed'
    return 1
  fi
  local expiry
  expiry=$(printf '%s' "$handshake" | timeout 5 openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
  local detail="tls_ok"
  [ -n "$expiry" ] && detail="$detail, cert_expires=$expiry"
  if [ "$scheme" != "https" ]; then
    printf '%s' "$detail"
    return 0
  fi
  local status_line code
  status_line=$(printf 'GET / HTTP/1.1\r\nHost: %s\r\nUser-Agent: db_ops-tcp-port-check\r\nConnection: close\r\n\r\n' "$target_host" \
    | timeout 10 openssl s_client -connect "$target_host:$port" -servername "$target_host" -quiet 2>/dev/null \
    | head -n 1)
  code=$(printf '%s' "$status_line" | grep -Eo '^HTTP/[0-9]\.[0-9] [0-9]{3}' | grep -Eo '[0-9]{3}$')
  if [ -z "$code" ]; then
    printf '%s; http_no_response' "$detail"
    return 1
  fi
  # Any HTTP status proves the listener is serving - a 302 to a sign-in page or a 401 is a working
  # front door. Only 5xx says it accepted the request and could not answer it.
  detail="$detail, http_status=$code"
  if [ "$code" -ge 500 ]; then
    printf '%s (server error)' "$detail"
    return 1
  fi
  printf '%s' "$detail"
  return 0
}

ports=${OS_TCP_PORTS:-}
if [ -z "$ports" ]; then
  # No ports configured => nothing to check. Benign OK (not UNKNOWN, which surfaces as WARNING)
  # so an unconfigured target stays quiet. Set metrics.collector_env.OS_TCP_PORTS to enable it.
  printf '[{"metric_item":"tcp_port_status","metric_value":"not_configured","metric_unit":"status","status":"OK","message":"No OS_TCP_PORTS configured; TCP port check skipped for this target."}]\n'
  exit 0
fi
default_host=${DB_OPS_TARGET_HOST:-127.0.0.1}
[ -n "$default_host" ] || default_host=127.0.0.1
IFS=',' read -r -a entries <<< "$ports"
printf '['
first=1
for raw in "${entries[@]}"; do
  entry=$(printf '%s' "$raw" | xargs)
  [ -n "$entry" ] || continue
  # [host:]port[/scheme] - scheme is tcp (default), tls, or https.
  scheme="tcp"
  spec="$entry"
  if printf '%s' "$entry" | grep -Eq '/(tcp|tls|https)$'; then
    scheme=${entry##*/}
    spec=${entry%/*}
  fi
  host="$default_host"
  port="$spec"
  if printf '%s' "$spec" | grep -Eq '^.+:[0-9]+$'; then
    host=${spec%:*}
    port=${spec##*:}
  fi

  on_loopback=0
  if probe "$host" "$port"; then
    value="OPEN"
    status="OK"
  else
    if [ "$host" != "127.0.0.1" ] && probe 127.0.0.1 "$port"; then on_loopback=1; fi
    if [ "$on_loopback" -eq 1 ]; then
      value="LOOPBACK_ONLY"
      status="WARNING"
    else
      value="CLOSED"
      status="CRITICAL"
    fi
  fi

  message="TCP port $host:$port is $value."
  if [ "$value" = "LOOPBACK_ONLY" ]; then
    message="TCP port $port is listening on 127.0.0.1 but not on $host, so nothing outside this host can reach it."
  fi
  if [ "$value" = "OPEN" ] && [ "$scheme" != "tcp" ]; then
    if detail=$(probe_health "$host" "$port" "$scheme"); then
      message="$message probe=$scheme, $detail"
    else
      # The socket is open, so this is not CLOSED - the value stays OPEN and only the status
      # moves. A reader sees "open but not serving", which is the finding.
      message="$message probe=$scheme, $detail"
      status="WARNING"
    fi
  fi

  [ "$first" -eq 1 ] || printf ','
  first=0
  printf '{"metric_item":"%s","metric_value":"%s","metric_unit":"status","status":"%s","message":"%s"}' \
    "$(json_escape "$host:$port")" "$value" "$status" "$(json_escape "$message")"
done
printf ']\n'
