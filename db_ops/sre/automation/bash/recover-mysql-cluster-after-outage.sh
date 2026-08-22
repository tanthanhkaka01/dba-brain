#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
vars_path="${MYSQL_VARS_PATH:-$repo_root/automation/ansible/group_vars/mysql.yml}"

usage() {
  cat <<'EOF'
Usage:
  ./automation/bash/recover-mysql-cluster-after-outage.sh

Optional environment variables:
  MYSQL_CLUSTER_NAME              Override cluster name.
  MYSQL_CLUSTER_ADMIN_USER        Override cluster admin user.
  MYSQL_CLUSTER_ADMIN_PASSWORD    Override cluster admin password.
  MYSQL_VARS_PATH                 Override group vars path.
EOF
}

trim_quotes() {
  local value="$1"
  value="${value%\"}"
  value="${value#\"}"
  value="${value%\'}"
  value="${value#\'}"
  printf '%s' "$value"
}

read_simple_yaml_value() {
  local key="$1"
  local file="$2"
  local raw

  raw="$(awk -F': *' -v wanted="$key" '
    {
      current_key=$1
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", current_key)
      if (current_key == wanted) {
        print substr($0, index($0, ":") + 1)
        exit
      }
    }
  ' "$file")"
  raw="${raw#"${raw%%[![:space:]]*}"}"
  raw="${raw%"${raw##*[![:space:]]}"}"
  trim_quotes "$raw"
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ ! -f "$vars_path" ]]; then
  echo "[db-sre] Group vars not found: $vars_path" >&2
  exit 1
fi

if ! command -v ssh >/dev/null 2>&1; then
  echo "[db-sre] ssh is required on bastion-01." >&2
  exit 1
fi

cluster_name="${MYSQL_CLUSTER_NAME:-$(read_simple_yaml_value mysql_cluster_name "$vars_path")}"
admin_user="${MYSQL_CLUSTER_ADMIN_USER:-$(read_simple_yaml_value mysql_cluster_admin_user "$vars_path")}"
admin_password="${MYSQL_CLUSTER_ADMIN_PASSWORD:-$(read_simple_yaml_value mysql_cluster_admin_password "$vars_path")}"
ansible_user="${MYSQL_SSH_USER:-tuser}"

if [[ -z "$cluster_name" || -z "$admin_user" || -z "$admin_password" ]]; then
  echo "[db-sre] Cluster name, admin user, and admin password must be set." >&2
  exit 1
fi

cluster_nodes=(
  $'mysql-01\t192.168.18.11'
  $'mysql-02\t192.168.18.12'
  $'mysql-03\t192.168.18.13'
)

echo "[db-sre] Checking MySQL service on cluster nodes..."
reachable_nodes=()
for node in "${cluster_nodes[@]}"; do
  IFS=$'\t' read -r host ip <<< "$node"

  if service_state="$(ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=yes "$host" "systemctl is-active mysql" 2>/dev/null)"; then
    if [[ "$service_state" == "active" ]]; then
      echo "[db-sre] $host ($ip): mysql is active"
      reachable_nodes+=("$node")
    else
      echo "[db-sre] $host ($ip): mysql state is '$service_state'" >&2
    fi
  else
    echo "[db-sre] $host ($ip): unreachable over SSH or mysql service check failed" >&2
  fi
done

if [[ "${#reachable_nodes[@]}" -eq 0 ]]; then
  echo "[db-sre] No reachable MySQL nodes with active mysql service. Stop and fix the nodes first." >&2
  exit 1
fi

tmp_js="$(mktemp)"
cleanup() {
  rm -f "$tmp_js"
}
trap cleanup EXIT

cat >"$tmp_js" <<'EOF'
shell.options.useWizards = false;

const clusterName = os.getenv("MYSQL_CLUSTER_NAME");
const adminUser = os.getenv("MYSQL_CLUSTER_ADMIN_USER");
const adminPassword = os.getenv("MYSQL_CLUSTER_ADMIN_PASSWORD");
const targetHost = os.getenv("MYSQL_TARGET_HOST");
const mode = os.getenv("MYSQL_RECOVERY_MODE");
const allHosts = (os.getenv("MYSQL_ALL_HOSTS") || "").split(",").filter(Boolean);

if (!clusterName || !adminUser || !adminPassword || !targetHost || !mode) {
  throw new Error("Required environment variables are missing.");
}

function instanceUri(host) {
  return {
    host: host,
    port: 3306,
    user: adminUser,
    password: adminPassword
  };
}

function topologyEntry(status, host) {
  const topology = status.defaultReplicaSet && status.defaultReplicaSet.topology
    ? status.defaultReplicaSet.topology
    : {};
  return topology[`${host}:3306`] || null;
}

shell.connect(instanceUri(targetHost));

if (mode === "dry-run") {
  const result = dba.rebootClusterFromCompleteOutage(clusterName, {dryRun: true});
  print(JSON.stringify(result, null, 2));
} else if (mode === "recover") {
  const cluster = dba.rebootClusterFromCompleteOutage(clusterName);

  allHosts.forEach((host) => {
    const status = cluster.status();
    const entry = topologyEntry(status, host);

    if (entry && entry.status === "ONLINE") {
      print(`[db-sre] ${host}:3306 already ONLINE`);
      return;
    }

    try {
      cluster.rejoinInstance(`${adminUser}@${host}:3306`);
      print(`[db-sre] Rejoined ${host}:3306`);
    } catch (error) {
      print(`[db-sre] Rejoin skipped for ${host}:3306 -> ${error.message}`);
    }
  });

  print(JSON.stringify(cluster.status(), null, 2));
} else {
  throw new Error(`Unsupported MYSQL_RECOVERY_MODE: ${mode}`);
}
EOF

run_remote_mysqlsh() {
  local target_ip="$1"
  local target_host="$2"
  local mode="$3"
  local remote_js
  local rc

  remote_js="$(ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=yes "$target_host" "mktemp /tmp/db-sre-mysql-recover-XXXXXX.js")"

  cat "$tmp_js" | ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=yes "$target_host" "cat > '$remote_js'"

  if [[ "${4:-}" == "quiet" ]]; then
    if ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=yes \
      "$target_host" \
      "env MYSQL_CLUSTER_NAME='${cluster_name}' MYSQL_CLUSTER_ADMIN_USER='${admin_user}' MYSQL_CLUSTER_ADMIN_PASSWORD='${admin_password}' MYSQL_TARGET_HOST='${target_ip}' MYSQL_ALL_HOSTS='${all_hosts_csv}' MYSQL_RECOVERY_MODE='${mode}' mysqlsh --js --file '$remote_js'" \
      >/dev/null; then
      rc=0
    else
      rc=$?
    fi
  else
    if ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=yes \
      "$target_host" \
      "env MYSQL_CLUSTER_NAME='${cluster_name}' MYSQL_CLUSTER_ADMIN_USER='${admin_user}' MYSQL_CLUSTER_ADMIN_PASSWORD='${admin_password}' MYSQL_TARGET_HOST='${target_ip}' MYSQL_ALL_HOSTS='${all_hosts_csv}' MYSQL_RECOVERY_MODE='${mode}' mysqlsh --js --file '$remote_js'"; then
      rc=0
    else
      rc=$?
    fi
  fi

  ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=yes "$target_host" "rm -f '$remote_js'" >/dev/null 2>&1 || true
  return "$rc"
}

all_hosts_csv=""
for node in "${cluster_nodes[@]}"; do
  IFS=$'\t' read -r _ ip <<< "$node"
  if [[ -n "$all_hosts_csv" ]]; then
    all_hosts_csv+=","
  fi
  all_hosts_csv+="$ip"
done

echo "[db-sre] Trying dry-run recovery to find a valid seed node..."
selected_seed=""
for node in "${reachable_nodes[@]}"; do
  IFS=$'\t' read -r host ip <<< "$node"
  echo "[db-sre] Dry-run on $host ($ip)"

  if run_remote_mysqlsh "$ip" "$host" "dry-run" "quiet"; then
    selected_seed="$node"
    echo "[db-sre] Selected seed node: $host ($ip)"
    break
  fi
done

if [[ -z "$selected_seed" ]]; then
  echo "[db-sre] Dry-run recovery failed on every reachable node. Stop and inspect GTID/cluster metadata manually." >&2
  exit 1
fi

echo "[db-sre] Rebooting InnoDB Cluster '$cluster_name' from complete outage..."
IFS=$'\t' read -r selected_seed_host selected_seed_ip <<< "$selected_seed"
run_remote_mysqlsh "$selected_seed_ip" "$selected_seed_host" "recover"
