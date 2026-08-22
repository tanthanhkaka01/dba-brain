#!/usr/bin/env bash
set -euo pipefail

target_group="${1:-mysql}"
operator_user="${SUDO_USER:-$USER}"
operator_home="$(getent passwd "$operator_user" | cut -d: -f6)"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"

case "$target_group" in
  mysql)
    role_label="MySQL"
    inventory_path="inventory/mysql/hosts.yml"
    ansible_host_group="mysql"
    playbook_path="automation/ansible/playbooks/mysql-cluster.yml"
    target_nodes=("mysql-01:192.168.18.11" "mysql-02:192.168.18.12" "mysql-03:192.168.18.13")
    ;;
  postgresql)
    role_label="PostgreSQL"
    inventory_path="inventory/postgresql/hosts.yml"
    ansible_host_group="postgresql"
    playbook_path="automation/ansible/playbooks/postgresql-ha.yml"
    target_nodes=("pg-01:192.168.18.21" "pg-02:192.168.18.22" "pg-03:192.168.18.23")
    ;;
  sqlserver)
    role_label="SQL Server"
    inventory_path="inventory/sqlserver/hosts.yml"
    ansible_host_group="sqlserver"
    playbook_path="automation/ansible/playbooks/sqlserver-ag.yml"
    target_nodes=("mssql-01:192.168.18.31" "mssql-02:192.168.18.32" "mssql-03:192.168.18.33")
    ;;
  oracle_rac)
    role_label="Oracle RAC"
    inventory_path="inventory/oracle/rac/hosts.yml"
    ansible_host_group="oracle_rac"
    playbook_path="automation/ansible/playbooks/oracle-rac-os-prep.yml"
    target_nodes=("rac01:192.168.18.41" "rac02:192.168.18.42")
    ;;
  oracle_dg)
    role_label="Oracle DataGuard"
    inventory_path="inventory/oracle/dataguard/hosts.yml"
    ansible_host_group="oracle_dg"
    playbook_path="automation/ansible/playbooks/oracle-dg-os-prep.yml"
    target_nodes=("orapri:192.168.18.51" "orastb:192.168.18.52")
    ;;
  monitoring)
    role_label="Monitoring"
    inventory_path="inventory/monitoring/hosts.yml"
    ansible_host_group="monitoring"
    playbook_path="automation/ansible/playbooks/observability.yml"
    target_nodes=("mon-01:192.168.18.4")
    ;;
  *)
    echo "Usage: $0 [mysql|postgresql|sqlserver|oracle_rac|oracle_dg|monitoring]"
    exit 1
    ;;
esac

# Override target_nodes from DB_SRE_TARGET_NODES if injected (test configs use different IPs).
# Format: "name1:ip1 name2:ip2 ..."
if [ -n "${DB_SRE_TARGET_NODES:-}" ]; then
  IFS=' ' read -ra target_nodes <<< "$DB_SRE_TARGET_NODES"
fi

if [[ ! -f "$repo_root/$inventory_path" ]]; then
  echo "[db-sre] Repository root not found from script location: $repo_root"
  exit 1
fi

echo "[db-sre] Running bastion bootstrap for $target_group as $operator_user"

sudo_cmd() {
  sudo "$@"
}

run_as_operator() {
  sudo -u "$operator_user" -H bash -lc "$1"
}

echo "[db-sre] Updating apt metadata..."
sudo_cmd apt-get -o Acquire::http::Pipeline-Depth=0 -o Acquire::http::No-Cache=True update -q

echo "[db-sre] Installing bastion control-node packages..."
sudo_cmd env DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::http::Pipeline-Depth=0 -o Acquire::http::No-Cache=True install -y \
  ansible \
  openssh-client \
  sshpass \
  git \
  rsync \
  python3 \
  python3-apt \
  python3-pip \
  curl \
  jq \
  tmux \
  htop

echo "[db-sre] Ensuring repository workspace ownership..."
sudo_cmd install -d -m 0755 /opt/db-sre
sudo_cmd chown -R "$operator_user:$operator_user" /opt/db-sre

echo "[db-sre] Preparing SSH directory for $operator_user..."
run_as_operator "mkdir -p ~/.ssh && chmod 700 ~/.ssh"

# SSH keys are pre-deployed by 07-bootstrap-bastion-ansible.ps1 (host key) and 08-deploy-host-ssh-key.ps1

ssh_config_file="$(mktemp)"
{
  for node in "${target_nodes[@]}"; do
    host_name="${node%%:*}"
    host_ip="${node##*:}"
    printf 'Host %s\n' "$host_name"
    printf '  HostName %s\n' "$host_ip"
    printf '  User %s\n\n' "$operator_user"
  done
} > "$ssh_config_file"
sudo_cmd install -o "$operator_user" -g "$operator_user" -m 600 "$ssh_config_file" "$operator_home/.ssh/config"
rm -f "$ssh_config_file"

echo "[db-sre] Seeding known_hosts..."
run_as_operator "touch ~/.ssh/known_hosts && chmod 600 ~/.ssh/known_hosts"
for node in "${target_nodes[@]}"; do
  host_ip="${node##*:}"
  run_as_operator "ssh-keygen -R '$host_ip' >/dev/null 2>&1 || true"
  run_as_operator "ssh-keyscan -H '$host_ip' >> ~/.ssh/known_hosts 2>/dev/null"
done

if [ -n "${GUEST_BECOME_PASS:-}" ]; then
  echo "[db-sre] Configuring NOPASSWD sudo on $role_label nodes..."
  for node in "${target_nodes[@]}"; do
    host_name="${node%%:*}"
    tmp_script="$(mktemp)"
    cat > "$tmp_script" << SUDO_EOF
printf '%s\n' '${GUEST_BECOME_PASS}' | sudo -S bash -c 'echo "${operator_user} ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/${operator_user}-nopasswd && chmod 440 /etc/sudoers.d/${operator_user}-nopasswd'
echo "NOPASSWD sudo configured on \$(hostname)"
SUDO_EOF
    chmod 600 "$tmp_script"
    sudo -u "$operator_user" -H ssh -o BatchMode=yes -o ConnectTimeout=10 "$host_name" 'bash -s' < "$tmp_script" \
      && echo "[db-sre] $host_name: NOPASSWD sudo OK" \
      || echo "[db-sre] $host_name: NOPASSWD sudo may already be set or failed (continuing)"
    rm -f "$tmp_script"
  done
else
  echo "[db-sre] GUEST_BECOME_PASS not set — skipping NOPASSWD sudo configuration"
  echo "[db-sre] If Ansible reports 'Missing sudo password', re-run this script via the Python CLI (it injects the password automatically)"
fi

echo "[db-sre] Validating passwordless SSH to $role_label nodes..."
for node in "${target_nodes[@]}"; do
  host_name="${node%%:*}"
  run_as_operator "ssh -o BatchMode=yes -o ConnectTimeout=10 $host_name 'hostname'"
done

echo "[db-sre] Validating ansible inventory and connectivity..."
run_as_operator "cd '$repo_root' && ANSIBLE_CONFIG=automation/ansible/ansible.cfg ansible --version | head -1"

# When target nodes were injected from config (e.g. test env), patch ansible_host values in hosts.yml
# so that the inventory used here AND by subsequent playbook runs has the correct IPs.
_ansible_inventory="$inventory_path"
if [ -n "${DB_SRE_TARGET_NODES:-}" ]; then
  _tmp_inv="$(mktemp /tmp/db-sre-hosts-XXXX.yml)"
  {
    echo "all:"
    echo "  children:"
    echo "    $ansible_host_group:"
    echo "      vars:"
    printf '        ansible_user: %s\n' "$operator_user"
    echo "      hosts:"
    for _node in "${target_nodes[@]}"; do
      printf '        %s:\n          ansible_host: %s\n' "${_node%%:*}" "${_node##*:}"
    done
  } > "$_tmp_inv"
  # Overwrite the static hosts.yml in the repo so subsequent playbook steps also use correct IPs
  cp "$_tmp_inv" "$repo_root/$inventory_path"
  _ansible_inventory="$_tmp_inv"
fi

run_as_operator "cd '$repo_root' && ANSIBLE_CONFIG=automation/ansible/ansible.cfg ansible-inventory -i '$_ansible_inventory' --list >/dev/null"
run_as_operator "cd '$repo_root' && ANSIBLE_CONFIG=automation/ansible/ansible.cfg ansible -i '$_ansible_inventory' '$ansible_host_group' -m ping"
[ "${_ansible_inventory}" != "$inventory_path" ] && rm -f "$_ansible_inventory" || true

cat <<EOF
[db-sre] Bastion Ansible control node bootstrap completed.
[db-sre] Repo path: $repo_root
[db-sre] Operator user: $operator_user
[db-sre] Next step:
  python -m db_ops.sre.cli run-bastion-playbook $playbook_path -i $inventory_path
EOF
