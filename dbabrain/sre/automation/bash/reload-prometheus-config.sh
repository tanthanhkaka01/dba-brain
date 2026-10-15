#!/usr/bin/env bash
set -euo pipefail

prometheus_config="${1:-/etc/prometheus/prometheus.yml}"
prometheus_host="${2:-127.0.0.1}"
prometheus_port="${3:-9090}"
wait_seconds="${4:-30}"

echo "[db-sre] Validating Prometheus config: $prometheus_config"
sudo promtool check config "$prometheus_config"

echo "[db-sre] Restarting Prometheus service"
sudo systemctl restart prometheus

echo "[db-sre] Waiting for Prometheus to listen on ${prometheus_host}:${prometheus_port}"
for ((i=1; i<=wait_seconds; i++)); do
  if curl -fsS "http://${prometheus_host}:${prometheus_port}/-/healthy" >/dev/null 2>&1; then
    echo "[db-sre] Prometheus is healthy"
    break
  fi

  if (( i == wait_seconds )); then
    echo "[db-sre] Prometheus did not become ready within ${wait_seconds}s"
    sudo systemctl status prometheus --no-pager || true
    exit 1
  fi

  sleep 1
done

echo "[db-sre] Active targets"
curl -fsS "http://${prometheus_host}:${prometheus_port}/api/v1/targets" \
  | jq '.data.activeTargets[] | {job: .labels.job, instance: .labels.instance, health: .health, lastError: .lastError}'
