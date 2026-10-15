# Ansible Automation

This directory stores Ansible playbooks, templates, and variable files for the lab.

Current targets:

- install MySQL 8.4 LTS on the three MySQL nodes
- apply a cluster-ready baseline configuration
- bootstrap a three-node MySQL InnoDB Cluster with `mysqlsh`
- install PostgreSQL 16 on the three PostgreSQL nodes
- configure one primary and two streaming replicas with `pg_basebackup`
- install Prometheus, Alertmanager, Grafana, and node exporter on `mon-01`
- provision a baseline Prometheus scrape config and Grafana datasource

Run from a Linux control node or the bastion VM after Ansible is installed:

```bash
cd /path/to/db-sre
ANSIBLE_CONFIG=automation/ansible/ansible.cfg \
ansible-playbook automation/ansible/playbooks/mysql-cluster.yml -k -K
```

```bash
cd /path/to/db-sre
ANSIBLE_CONFIG=automation/ansible/ansible.cfg \
ansible-playbook -i inventory/postgresql/hosts.yml automation/ansible/playbooks/postgresql-ha.yml -k -K
```

Files added for this flow:

- `ansible.cfg`
- `group_vars/mysql.yml`
- `group_vars/postgresql.yml`
- `group_vars/monitoring.yml`
- `playbooks/mysql-cluster.yml`
- `playbooks/postgresql-ha.yml`
- `playbooks/observability.yml`
- `templates/mysql-cluster.cnf.j2`
- `templates/bootstrap-mysql-cluster.js.j2`
- `templates/postgresql-ha.conf.j2`
- `templates/pg_hba.conf.j2`
- `templates/prometheus.yml.j2`
- `templates/observability-alerts.yml.j2`
- `templates/grafana-datasource-prometheus.yml.j2`
