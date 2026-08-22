# DB Ops SRE

## Overview

`db_ops.sre` orchestrates the full lifecycle of a VMware Workstation lab: clone and provision VMs, deploy database clusters via Ansible, validate cluster health, and run ad-hoc commands through a bastion host.

**Working directory for all commands:** the repository root (`db_ops`)

---

## Supported Lab Database Versions

This SRE lab is opinionated: the database version, OS template, topology, ports, and verification target are part of the runbook. The active values below come from `data/sre_config.json`, Ansible `group_vars`, and the inventory files under `db_ops/sre/inventory`.

| Stack | Version / package source | Nodes | Topology | Main ports | Verification |
|---|---|---|---|---|---|
| MySQL | MySQL 8.4 LTS via Oracle MySQL APT repo component `mysql-8.4-lts`; installs `mysql-server` and `mysql-shell` | `mysql-01` `198.51.100.11`, `mysql-02` `.12`, `mysql-03` `.13` | 3-node MySQL InnoDB Cluster; first inventory host bootstraps the cluster | 3306 classic SQL, 33061 group replication | `check-mysql-cluster` expects cluster `status=OK`, 1 PRIMARY, 2 SECONDARY |
| PostgreSQL | PostgreSQL 16 from Ubuntu packages; installs `postgresql-16`, `postgresql-client-16`, `postgresql-contrib` | `pg-01` `198.51.100.21`, `pg-02` `.22`, `pg-03` `.23` | 1 primary + 2 physical streaming replicas seeded by `pg_basebackup` | 5432 | `check-postgresql-ha` expects 1 primary not in recovery and 2 streaming replicas |
| SQL Server | SQL Server 2025 Developer via Microsoft Ubuntu 24.04 APT repo; installs `mssql-server`, `mssql-tools18`, `unixodbc-dev` | `mssql-01` `198.51.100.31`, `mssql-02` `.32`, `mssql-03` `.33` | 3-node Always On Availability Group, `CLUSTER_TYPE = NONE`, synchronous commit, manual failover | 1433 SQL, 5022 HADR endpoint | query AG DMV state; expected 1 PRIMARY and 2 SECONDARY replicas |

Shared infrastructure:

- Default Linux DB template for MySQL/PostgreSQL/SQL Server: `tpl-ubuntu-2404`.
- Shared bastion: `bastion-01` at `198.51.100.3`.
- SSH model: Windows host key reaches only bastion; bastion key reaches database nodes.
- Default VM sizing used by this runbook: bastion 2 vCPU / 2 GB RAM, database nodes 2 vCPU / 4 GB RAM.

---

## MySQL + bastion-01: Complete Setup Sequence

This is the end-to-end sequence to bring up bastion-01 and the MySQL InnoDB Cluster from scratch.

### MySQL Lab Definition

| Item | Value |
|---|---|
| Database version | MySQL 8.4 LTS (`mysql_apt_repo_components: mysql-8.4-lts`, plus `mysql-tools`) |
| Packages | `mysql-server`, `mysql-shell` |
| Cluster name | `mysql-lab` |
| Nodes | `mysql-01` `198.51.100.11`, `mysql-02` `198.51.100.12`, `mysql-03` `198.51.100.13` |
| Role model | First inventory host is the bootstrap/primary node; the other two join as replicas/secondaries |
| Ports | `3306` classic SQL, `33061` group replication seed/local address |
| Cluster admin | `clusteradmin` from `sre.database_defaults.mysql` / `group_vars/mysql.yml` |
| Network allowed for replication | `198.51.100.0/24` |
| Idempotency marker | `/var/lib/mysql/.innodb-cluster-ready` on the primary |

The playbook installs MySQL from the Oracle APT repo, deploys `/etc/mysql/conf.d/z99-cluster.cnf`, creates the cluster admin on every node, renders `/tmp/bootstrap-mysql-cluster.js` on the first node, and runs `mysqlsh --js` to create or verify the InnoDB Cluster.

### Prerequisites

- VMware Workstation template exists and has been bootstrapped (see [Template Bootstrap](#template-bootstrap-one-time)).
- Windows SSH key pair exists at the path configured in `sre_config.json â†’ sre.credentials.ssh_identity_file`.

---

### Step 1 — Clone VMs

```powershell
python -m db_ops.sre.cli run-powershell 01-clone-vms -- -Group shared
python -m db_ops.sre.cli run-powershell 01-clone-vms -- -Group mysql
```

Clones bastion-01 (shared) and mysql-01/02/03 from the template snapshot. VMs are off after this step.

---

### Step 2 — Set CPU and Memory

```powershell
python -m db_ops.sre.cli run-powershell 02-set-vm-resources -- -Group shared -CpuCount 2 -MemoryGB 2
python -m db_ops.sre.cli run-powershell 02-set-vm-resources -- -Group mysql  -CpuCount 2 -MemoryGB 4
```

VMs must be off. Adjust CPU/RAM to match your host capacity.

---

### Step 3 — Start VMs

```powershell
python -m db_ops.sre.cli run-powershell 03-start-vms -- -Group shared
python -m db_ops.sre.cli run-powershell 03-start-vms -- -Group mysql
```

---

### Step 4 — Fix Guest Identity

```powershell
python -m db_ops.sre.cli run-powershell 04-fix-guest-identity -- -Group shared
python -m db_ops.sre.cli run-powershell 04-fix-guest-identity -- -Group mysql
```

Sets the correct hostname, regenerates machine-id, and assigns the static IP defined in `sre_config.json â†’ sre.inventory`. VMs reboot automatically after this step.

---

### Step 5 — Deploy Windows Host SSH Key to Bastion

```powershell
python -m db_ops.sre.cli run-powershell 08-deploy-host-ssh-key -- -Group shared
```

Pushes the Windows host public key into `~/.ssh/authorized_keys` on **bastion-01 only** (and other shared VMs like mon-01 if present). MySQL nodes are **not** targeted here.

**Why only bastion?**
- All SSH-based commands (`run-bastion-script`, `run-bastion-playbook`, `check-*`) use bastion as the entry point.
- `check-mysql-cluster` and `check-postgresql-ha` route through bastion via SSH ProxyJump — the Windows host never connects directly to MySQL/PostgreSQL nodes.
- bastion's own key is deployed to MySQL nodes in Step 6 — that is the only key MySQL nodes need.

`BatchMode=yes` is set on all SSH calls (no interactive password prompt), so the Windows host needs key-based auth on bastion. This is the only node that requires the host key.

---

### Step 6a — Sync Repo and Deploy Bastion Key (VMware Tools)

```powershell
python -m db_ops.sre.cli run-powershell 07-bootstrap-bastion-ansible -- -TargetGroup mysql
```

Runs via VMware Tools (no SSH from host required yet). Does three things:
1. Packages the local repo and copies it to bastion-01 at `/opt/db-sre/repo`.
2. Generates an ed25519 SSH key pair on bastion-01 (`~/.ssh/id_ed25519`).
3. Appends bastion-01's public key to `~/.ssh/authorized_keys` on every MySQL node — so Ansible can reach them from bastion.

---

### Step 6b — Install Ansible on Bastion

```powershell
python -m db_ops.sre.cli run-bastion-script bootstrap-bastion-ansible -- mysql
```

SSHes from Windows host to bastion-01 and runs `automation/bash/bootstrap-bastion-ansible.sh mysql`. This script:
1. Installs `ansible`, `openssh-client`, `python3`, and other control-node packages via apt.
2. Writes `~/.ssh/config` with hostnameâ†’IP mappings for mysql-01/02/03.
3. Seeds `~/.ssh/known_hosts` via `ssh-keyscan`.
4. **Configures NOPASSWD sudo** on each MySQL node (SSHes from bastion â†’ mysql-0x and runs `sudo bash`). Required for Ansible `become: yes`.
5. Validates passwordless SSH: `ssh mysql-01 hostname`, etc.
6. Runs `ansible -m ping` to confirm Ansible connectivity.

The `guest_password` from `sre_config.json` is injected automatically by the Python CLI as `GUEST_BECOME_PASS` env var — the script uses it once to bootstrap NOPASSWD sudo on MySQL nodes, then never needs it again.

Requires: Step 5 (host key on bastion) and Step 6a (repo on bastion, bastion key on MySQL nodes).

---

### Step 7 — Deploy MySQL InnoDB Cluster

```powershell
python -m db_ops.sre.cli run-bastion-playbook automation/ansible/playbooks/mysql-cluster.yml `
    -i inventory/mysql/hosts.yml
```

SSHes to bastion-01 and runs `ansible-playbook` from `/opt/db-sre/repo`. Requires Step 5 (host key on bastion) and Step 6 (repo synced, bastion key on MySQL nodes).

---

### Step 8 — Verify

```powershell
python -m db_ops.sre.cli check-shared-vms --vm-name bastion-01
python -m db_ops.sre.cli check-mysql-cluster
```

`check-shared-vms` verifies hostname, IP, and systemd state via SSH.
`check-mysql-cluster` runs `mysqlsh` on the primary and requires: `status=OK`, 1 PRIMARY, 2 SECONDARY.

---

## PostgreSQL HA: Complete Setup Sequence

### PostgreSQL Lab Definition

| Item | Value |
|---|---|
| Database version | PostgreSQL 16 (`postgresql_major_version: 16`) |
| Packages | `postgresql-16`, `postgresql-client-16`, `postgresql-contrib` |
| Cluster name | `postgresql-lab` |
| Nodes | `pg-01` `198.51.100.21` primary, `pg-02` `198.51.100.22` replica, `pg-03` `198.51.100.23` replica |
| Port | `5432` |
| Data directory | `/var/lib/postgresql/16/main` |
| Config directory | `/etc/postgresql/16/main` |
| PostgreSQL bin directory | `/usr/lib/postgresql/16/bin` |
| Admin user | `postgres` |
| Replication user | `replicator` |
| Client network | `198.51.100.0/24` via `scram-sha-256` |
| Replication network | `198.51.100.0/24` via `scram-sha-256` |

### PostgreSQL HA Architecture

Current architecture: **native PostgreSQL physical streaming replication only**.

It does **not** currently use:

- Patroni
- etcd / Consul / ZooKeeper distributed consensus
- HAProxy / PgBouncer / VIP listener
- Pacemaker / Corosync
- repmgr
- automatic leader election
- automatic application connection failover

`pg-01` is the fixed primary chosen from the first host in `inventory/postgresql/hosts.yml`; `pg-02` and `pg-03` are seeded from `pg-01` with `pg_basebackup -R`, which writes standby connection metadata for automatic replica startup.

What this means operationally:

- The cluster has replicated standby copies for HA/DR lab validation.
- If `pg-01` fails, PostgreSQL will not automatically promote `pg-02` or `pg-03`.
- Failover is manual until a failover manager is added.
- Applications must connect directly to the active primary IP, or an external routing layer must be added later.
- The current check validates replication health, not automatic failover capability.

Recommended future HA target: **Patroni + etcd + HAProxy/PgBouncer or VIP** if the lab needs production-like automatic failover. In that model, etcd stores cluster state, Patroni manages leader election and promotion, and HAProxy/PgBouncer/VIP gives clients a stable endpoint.

Important behavior:

- `wal_level = replica`, `max_wal_senders = 10`, `max_replication_slots = 10`, `hot_standby = on`, and `wal_log_hints = on` are deployed through `conf.d/99-db-sre-ha.conf`.
- `listen_addresses = '*'` and `port = 5432` are set in the same managed config file.
- Password encryption is `scram-sha-256`.
- Replicas store `/var/lib/postgresql/.pgpass` for reconnecting to the primary as `replicator`.
- Replicas are reseeded only when `postgresql_force_reseed: true` or when the marker `/var/lib/postgresql/16/main/.db-sre-replica-ready` is missing.
- Synchronous replication is currently disabled (`postgresql_synchronous_replication: false`), so this is asynchronous streaming replication unless that variable is changed.
- The built-in health check should show `pg-01` as primary (`pg_is_in_recovery() = false`) and both replicas streaming from it.

End-to-end sequence to bring up the PostgreSQL HA cluster (pg-01/02/03). bastion-01 must already be up and the Windows host key must already be on it (Steps 1–5 of the MySQL sequence, or just shared VMs if MySQL was deployed first).

> If MySQL is already running, skip the shared-VM steps — bastion-01 is reused as-is.

### Step 1 — Clone VMs

```powershell
python -m db_ops.sre.cli run-powershell 01-clone-vms -- -Group postgresql
```

### Step 2 — Set CPU and Memory

```powershell
python -m db_ops.sre.cli run-powershell 02-set-vm-resources -- -Group postgresql -CpuCount 2 -MemoryGB 4
```

### Step 3 — Start VMs

```powershell
python -m db_ops.sre.cli run-powershell 03-start-vms -- -Group postgresql
```

### Step 4 — Fix Guest Identity

```powershell
python -m db_ops.sre.cli run-powershell 04-fix-guest-identity -- -Group postgresql
```

Assigns static IPs (198.51.100.21–23), sets hostnames pg-01/02/03, reboots.

### Step 5 — Sync Repo and Deploy Bastion Key

```powershell
python -m db_ops.sre.cli run-powershell 07-bootstrap-bastion-ansible -- -TargetGroup postgresql
```

Re-syncs the repo to bastion-01 and deploys bastion's ed25519 public key to pg-01/02/03.

### Step 6 — Bootstrap Ansible on Bastion

```powershell
python -m db_ops.sre.cli run-bastion-script bootstrap-bastion-ansible -- postgresql
```

Installs Ansible (if not present), writes SSH config for pg-01/02/03, seeds known_hosts, configures NOPASSWD sudo on each PostgreSQL node, and validates `ansible -m ping`.

### Step 7 — Deploy PostgreSQL HA

```powershell
python -m db_ops.sre.cli run-bastion-playbook automation/ansible/playbooks/postgresql-ha.yml `
    -i inventory/postgresql/hosts.yml
```

### Step 8 — Verify

```powershell
python -m db_ops.sre.cli check-postgresql-ha
```

Requires: 1 primary (not in recovery) + 2 streaming replicas.

The playbook stages are:

1. **Install and configure all PostgreSQL nodes** - installs PostgreSQL 16 packages, deploys `99-db-sre-ha.conf` and `pg_hba.conf`, starts the service, sets the `postgres` password on the primary, and creates or updates the `replicator` role.
2. **Seed replicas from the primary** - stops each replica when reseed is needed, clears its data directory, runs `pg_basebackup` from `pg-01`, writes the replica-ready marker, and starts PostgreSQL again.

Useful manual checks through the CLI:

```powershell
# Primary should return f
python -m db_ops.sre.cli ssh 198.51.100.21 -- "sudo -u postgres psql -tAc \"SELECT pg_is_in_recovery();\""

# Primary should show both replicas as streaming
python -m db_ops.sre.cli ssh 198.51.100.21 -- "sudo -u postgres psql -x -c \"SELECT application_name, client_addr, state, sync_state, replay_lag FROM pg_stat_replication;\""

# Each replica should return t
python -m db_ops.sre.cli ssh 198.51.100.22 -- "sudo -u postgres psql -tAc \"SELECT pg_is_in_recovery();\""
python -m db_ops.sre.cli ssh 198.51.100.23 -- "sudo -u postgres psql -tAc \"SELECT pg_is_in_recovery();\""
```

---

## SQL Server 2025 AG: Complete Setup Sequence

End-to-end sequence to bring up a 3-node SQL Server 2025 Always On Availability Group (mssql-01/02/03, CLUSTER_TYPE = NONE). bastion-01 must already be up with the Windows host key deployed.

### SQL Server Lab Definition

| Item | Value |
|---|---|
| Database version | SQL Server 2025 |
| Edition / PID | Developer (`mssql_pid: developer`) |
| Package source | Microsoft Ubuntu 24.04 repo `https://packages.microsoft.com/ubuntu/24.04/mssql-server-2025` |
| Packages | `mssql-server`, `mssql-tools18`, `unixodbc-dev` |
| Nodes | `mssql-01` `198.51.100.31`, `mssql-02` `198.51.100.32`, `mssql-03` `198.51.100.33` |
| Port | `1433` |
| HADR endpoint | `Hadr_endpoint` on TCP `5022` |
| Availability Group | `AG_SQL2025_LAB` |
| AG mode | `CLUSTER_TYPE = NONE`, `DB_FAILOVER = ON`, synchronous commit, manual failover, automatic seeding |
| SQL Agent | Enabled in `mssql.conf` |
| sqlcmd | `/opt/mssql-tools18/bin/sqlcmd -C` because sqlcmd 18 requires certificate trust handling |

This is a Linux SQL Server Always On lab without Windows Server Failover Cluster or Pacemaker. The playbook creates certificate-based database mirroring endpoints, creates the AG on the first inventory host, and joins the two secondary replicas.

### Step 1 — Clone VMs

```powershell
python -m db_ops.sre.cli run-powershell 01-clone-vms -- -Group sqlserver
```

### Step 2 — Set CPU and Memory

```powershell
python -m db_ops.sre.cli run-powershell 02-set-vm-resources -- -Group sqlserver -CpuCount 2 -MemoryGB 4
```

SQL Server requires at least 2 GB RAM per node. 4 GB is recommended.

### Step 3 — Start VMs

```powershell
python -m db_ops.sre.cli run-powershell 03-start-vms -- -Group sqlserver
```

### Step 4 — Fix Guest Identity

```powershell
python -m db_ops.sre.cli run-powershell 04-fix-guest-identity -- -Group sqlserver
```

Assigns static IPs (198.51.100.31–33), sets hostnames mssql-01/02/03, reboots.

### Step 5 — Sync Repo and Deploy Bastion Key

```powershell
python -m db_ops.sre.cli run-powershell 07-bootstrap-bastion-ansible -- -TargetGroup sqlserver
```

Re-syncs the repo to bastion-01 and deploys bastion's ed25519 public key to mssql-01/02/03.

### Step 6 — Bootstrap Ansible on Bastion

```powershell
python -m db_ops.sre.cli run-bastion-script bootstrap-bastion-ansible -- sqlserver
```

Installs Ansible, writes SSH config for mssql-01/02/03, seeds known_hosts, configures NOPASSWD sudo on each SQL Server node, and validates `ansible -m ping`.

### Step 7 — Deploy SQL Server 2025 AG

```powershell
python -m db_ops.sre.cli run-bastion-playbook automation/ansible/playbooks/sqlserver-ag.yml `
    -i inventory/sqlserver/hosts.yml
```

The playbook runs three plays in sequence:
1. **Install SQL Server** — Adds Microsoft APT repo, installs `mssql-server` + `mssql-tools18`, runs `mssql-conf setup` (SA password + EULA), deploys `mssql.conf` (HA + SQL Agent enabled), starts the service.
2. **Certificates and endpoints** — Creates a master key and DBM certificate on the primary, distributes the cert files to secondaries, creates and starts the `Hadr_endpoint` (port 5022) on all nodes.
3. **Bootstrap AG** — Creates the `AG_SQL2025_LAB` Availability Group with `CLUSTER_TYPE = NONE` (no Windows/Pacemaker cluster required), joins secondaries, writes an idempotency marker.

### Step 8 — Verify

```powershell
python -m db_ops.sre.cli ssh 198.51.100.31 -- "SQLCMDPASSWORD='ChangeMe_SA_123!' /opt/mssql-tools18/bin/sqlcmd -C -S localhost -U SA -Q \"SELECT ag.name, ar.replica_server_name, ars.role_desc FROM sys.availability_groups ag JOIN sys.availability_replicas ar ON ag.group_id=ar.group_id LEFT JOIN sys.dm_hadr_availability_replica_states ars ON ar.replica_id=ars.replica_id;\""
```

Expected output: mssql-01 = PRIMARY, mssql-02 = SECONDARY, mssql-03 = SECONDARY.

---

## Oracle RAC 26ai: Complete Setup Sequence

End-to-end sequence to bring up a 2-node Oracle 23ai RAC on Oracle Linux R9U6 (rac01/rac02). bastion-01 must already be up with the Windows host key deployed.

> Oracle RAC VMs are cloned from the Oracle Linux R9U6 template (`tpl-oraclelinux-r9u6`), not the Ubuntu template. Ensure the Oracle Linux template is bootstrapped first (`00-bootstrap-template-remote-access -- -Template oraclelinux`).
>
> Set `oracle.grid_installer_path` and `oracle.db_installer_path` in `sre_config.json` before Step 9.

### Step 1 — Clone VMs

```powershell
python -m db_ops.sre.cli run-powershell 01-clone-vms -- -Group oracle_rac
```

Clones rac01 and rac02 from the Oracle Linux template snapshot.

### Step 2 — Set CPU and Memory

```powershell
python -m db_ops.sre.cli run-powershell 02-set-vm-resources -- -Group oracle_rac -CpuCount 4 -MemoryGB 12
```

Oracle 26ai RAC requires significant resources: 12 GB SGA + 4 GB PGA per node. 4 vCPU / 12 GB RAM per node is the recommended minimum for a 32 GB lab host (26 GB total for both nodes).

### Step 3 — Start VMs

```powershell
python -m db_ops.sre.cli run-powershell 03-start-vms -- -Group oracle_rac
```

### Step 4 — Fix Guest Identity

```powershell
python -m db_ops.sre.cli run-powershell 04-fix-guest-identity -- -Group oracle_rac
```

Assigns static IPs (198.51.100.41–42), sets hostnames rac01/rac02, reboots.

### Step 5 — Stop VMs and Add Shared ASM Disks

Oracle ASM requires multi-writer shared VMDKs that must be attached while VMs are off.

```powershell
python -m db_ops.sre.cli run-powershell stop-vms -- -Group oracle_rac
python -m db_ops.sre.cli run-powershell add-oracle-shared-disks
```

Creates and attaches 5 shared VMDKs in `D:\VMs\oracle_rac_shared\`:

| Disk | Size | Use |
|---|---|---|
| ocr01/02/03 | 10 GB Ã— 3 | OCR — Oracle Cluster Registry (ASM diskgroup) |
| data01 | 50 GB | DATA diskgroup |
| fra01 | 30 GB | FRA diskgroup |

The disks are attached to SCSI controller 1 as `independent-persistent` (not snapshotted). Default sizes can be overridden: `add-oracle-shared-disks -- -OcrDiskGb 15 -DataDiskGb 80`. Use `-Force` to recreate existing disks.

### Step 6 — Start VMs

```powershell
python -m db_ops.sre.cli run-powershell 03-start-vms -- -Group oracle_rac
```

Verify the 5 shared disks appear in the guest as `/dev/sdb` through `/dev/sdf` before running Ansible.

### Step 7 — Sync Repo and Deploy Bastion Key

```powershell
python -m db_ops.sre.cli run-powershell 07-bootstrap-bastion-ansible -- -TargetGroup oracle_rac
```

Re-syncs the repo to bastion-01 and deploys bastion's ed25519 public key to rac01/rac02.

### Step 8 — Bootstrap Ansible on Bastion

```powershell
python -m db_ops.sre.cli run-bastion-script bootstrap-bastion-ansible -- oracle_rac
```

Installs Ansible, writes SSH config for rac01/rac02, seeds known_hosts, configures NOPASSWD sudo on each RAC node, and validates `ansible -m ping`. The next hint printed by the script will reference `oracle-rac-os-prep.yml` as the next playbook.

### Step 9 — OS Preparation

```powershell
python -m db_ops.sre.cli run-bastion-playbook automation/ansible/playbooks/oracle-rac-os-prep.yml `
    -i inventory/oracle/rac/hosts.yml
```

Runs on both nodes. Installs Oracle prerequisite packages (binutils, libaio, ksh, unzip, etc.), creates OS groups (`oinstall`, `dba`, `asmadmin`, …), creates `grid` and `oracle` users, sets kernel parameters (`shmmax`, `sem`, `hugepages`), configures `/etc/hosts` with public/VIP/SCAN/private entries, labels ASM disks via udev rules, and prepares the Oracle directory tree under `/u01/`.

**Must run before staging installers** — `09-stage-oracle-installer` sets `chown grid:oinstall` on the Grid ZIP, which requires the `grid` user and `oinstall` group to exist.

### Step 10 — Stage Oracle Installer ZIPs

Before running this step, set the installer paths in `sre_config.json`:

```json
"oracle": {
  "grid_installer_path": "D:/softwares/LINUX.X64_2326100_grid_home.zip",
  "db_installer_path":   "D:/softwares/LINUX.X64_2326100_db_home.zip",
  "install_stage": "/u01/app/stage"
}
```

Then run:

```powershell
python -m db_ops.sre.cli run-powershell stage-oracle-installer
```

Transfer path: **Windows host â†’ bastion-01 â†’ rac01, rac02**

1. SCPs both ZIPs to bastion `/tmp/oracle-installer-stage/`.
2. From bastion, SCPs each ZIP to `/u01/app/stage/` on rac01 and rac02.
3. Sets ownership: `grid:oinstall` for the Grid ZIP, `oracle:oinstall` for the DB ZIP.
4. Cleans up the bastion temp directory.

### Step 10b — Install Grid Infrastructure

```powershell
python -m db_ops.sre.cli run-bastion-playbook automation/ansible/playbooks/oracle-rac-grid.yml `
    -i inventory/oracle/rac/hosts.yml
```

Unzips the Grid Home ZIP, runs `gridSetup.sh` in silent mode using a response file, runs `root.sh` on both nodes (sequentially: rac01 first, then rac02), and starts the GI stack. OCR and VOTE disks are formatted into the OCR diskgroup, DATA and FRA diskgroups are created.

This play takes 20–40 minutes depending on storage speed.

### Step 10c — Install Oracle Database

```powershell
python -m db_ops.sre.cli run-bastion-playbook automation/ansible/playbooks/oracle-rac-db.yml `
    -i inventory/oracle/rac/hosts.yml
```

Installs the Oracle DB Home, creates the RAC database (`pridb`) via `dbca` in silent mode with the PDB `pridbpdb1`. Both nodes join as RAC instances.

### Step 11 — Verify

```powershell
# Check ASM and CRS from rac01
python -m db_ops.sre.cli ssh 198.51.100.41 -- "sudo -u grid /u01/app/23c/grid/bin/crsctl stat res -t"

# Check RAC instances
python -m db_ops.sre.cli ssh 198.51.100.41 -- "sudo -u oracle /u01/app/product/db23c/bin/srvctl status database -d pridb"
```

Expected: GI stack online, pridb running on both rac01 and rac02.

---

## Oracle DataGuard 26ai: Complete Setup Sequence

End-to-end sequence to bring up a 2-node Oracle 23ai DataGuard configuration (orapri / orastb) on Oracle Linux R9U6. bastion-01 must already be up with the Windows host key deployed.

> DG uses only the Oracle DB Home installer (no Grid Infrastructure). Only `oracle.db_installer_path` needs to be set in `sre_config.json`.
>
> DG nodes use local filesystem storage — no shared VMDKs are required.

### Step 1 — Clone VMs

```powershell
python -m db_ops.sre.cli run-powershell 01-clone-vms -- -Group oracle_dg
```

Clones orapri and orastb from the Oracle Linux template snapshot.

### Step 2 — Set CPU and Memory

```powershell
python -m db_ops.sre.cli run-powershell 02-set-vm-resources -- -Group oracle_dg -CpuCount 2 -MemoryGB 8
```

DG is lighter than RAC: SGA 4 GB + PGA 1 GB per node. 2 vCPU / 8 GB RAM is sufficient for a lab.

### Step 3 — Start VMs

```powershell
python -m db_ops.sre.cli run-powershell 03-start-vms -- -Group oracle_dg
```

### Step 4 — Fix Guest Identity

```powershell
python -m db_ops.sre.cli run-powershell 04-fix-guest-identity -- -Group oracle_dg
```

Assigns static IPs (198.51.100.51 â†’ orapri, 198.51.100.52 â†’ orastb), sets hostnames, reboots.

### Step 5 — Sync Repo and Deploy Bastion Key

```powershell
python -m db_ops.sre.cli run-powershell 07-bootstrap-bastion-ansible -- -TargetGroup oracle_dg
```

Re-syncs the repo to bastion-01 and deploys bastion's ed25519 public key to orapri/orastb.

### Step 6 — Bootstrap Ansible on Bastion

```powershell
python -m db_ops.sre.cli run-bastion-script bootstrap-bastion-ansible -- oracle_dg
```

Installs Ansible (if not present), writes SSH config for orapri/orastb, seeds known_hosts, configures NOPASSWD sudo, and validates `ansible -m ping`.

### Step 7 — OS Preparation

```powershell
python -m db_ops.sre.cli run-bastion-playbook automation/ansible/playbooks/oracle-dg-os-prep.yml `
    -i inventory/oracle/dataguard/hosts.yml
```

Runs on both nodes. Installs Oracle prerequisite packages, creates OS groups (`oinstall`, `dba`, `dgdba`, …), creates `oracle` user, sets kernel parameters, creates Oracle directory tree (`/u01/app/oracle/`, `/u01/app/product/db23c/`, `/u01/app/stage/`, redo log and FRA dirs), deploys `.setenv.sh` env script, disables transparent hugepages, disables firewalld and sets SELinux to permissive.

### Step 8 — Stage DB Installer

Set the DB installer path in `sre_config.json` if not done already:

```json
"oracle": {
  "grid_installer_path": null,
  "db_installer_path": "D:/softwares/LINUX.X64_2326100_db_home.zip",
  "install_stage": "/u01/app/stage"
}
```

Then run:

```powershell
python -m db_ops.sre.cli run-powershell stage-oracle-installer -- -Group oracle_dg
```

Transfer path: **Windows host â†’ bastion-01 â†’ orapri, orastb**

Only the DB Home ZIP is transferred. Ownership is set to `oracle:oinstall`. The Grid installer is not required for DataGuard.

### Step 9 — Install Oracle DB and Create Primary Database

```powershell
python -m db_ops.sre.cli run-bastion-playbook automation/ansible/playbooks/oracle-dg-primary.yml `
    -i inventory/oracle/dataguard/hosts.yml
```

Two plays run sequentially:

1. **Install Oracle DB software on all nodes** — Unzips DB Home on both orapri and orastb, runs `runInstaller` in silent software-only mode, runs `root.sh`, deploys `listener.ora` and `tnsnames.ora`, starts the listener on both nodes.
2. **Create primary database** — Runs `dbca` in silent mode on orapri only to create `dgpri` (SID), enables archivelog mode, enables `dg_broker_start=TRUE`.

This play takes 20–40 minutes. An idempotency marker (`.db-sre-dg-primary-ready`) is written to skip DB creation on re-runs.

### Step 10 — Create Standby and Configure DG Broker

```powershell
python -m db_ops.sre.cli run-bastion-playbook automation/ansible/playbooks/oracle-dg-standby.yml `
    -i inventory/oracle/dataguard/hosts.yml
```

Runs from orastb (standby) perspective:
1. Creates standby directories and password file on orastb.
2. Creates a minimal pfile and starts orastb in `NOMOUNT` mode.
3. Runs `RMAN DUPLICATE TARGET DATABASE FOR STANDBY FROM ACTIVE DATABASE` — copies the entire primary database over the network to orastb.
4. Starts Managed Recovery Process (MRP) on orastb: `ALTER DATABASE RECOVER MANAGED STANDBY DATABASE DISCONNECT FROM SESSION`.
5. Enables `dg_broker_start=TRUE` on orastb.
6. Configures the DG broker via `DGMGRL` on orapri: creates configuration `dg-lab`, adds `dg_standby` as physical standby, enables the configuration, sets protection mode (`MaxAvailability`) and log transport (`SYNC`).

An idempotency marker (`.db-sre-dg-ready`) is written to skip re-configuration on re-runs. To force reseed the standby: set `oracle_standby_force_reseed: true` in `group_vars/oracle_dg.yml`.

### Step 11 — Verify

```powershell
# Check DG broker status from primary
python -m db_ops.sre.cli ssh 198.51.100.51 -- "sudo -u oracle ORACLE_HOME=/u01/app/product/db23c /u01/app/product/db23c/bin/dgmgrl / 'show configuration'"

# Check apply lag on standby
python -m db_ops.sre.cli ssh 198.51.100.52 -- "sudo -u oracle ORACLE_HOME=/u01/app/product/db23c /u01/app/product/db23c/bin/sqlplus -S / as sysdba <<'EOF'
SELECT DEST_ID, STATUS, TARGET, ARCHIVER, SCHEDULE, DESTINATION FROM V\$ARCHIVE_DEST WHERE TARGET='STANDBY';
EOF"
```

Expected: `show configuration` returns `SUCCESS`, configuration protection mode `MaxAvailability`, both databases status `SUCCESS`.

---

## setup-sequence — Run a complete cluster setup with one command

`setup-sequence` calls individual SRE sub-commands as child Python processes in sequence. It never invokes PowerShell or SSH directly — each step is a subprocess of `python -m db_ops.sre.cli`.

```powershell
python -m db_ops.sre.cli --config data/sre_config.json setup-sequence <db_type> [options]
```

### Supported db_type values

| db_type | Steps | Equivalent manual sequence |
|---|---|---|
| `shared` | 5 | Provision bastion-01 / mon-01 / log-01 only |
| `mysql` | 14 | MySQL + bastion-01: Complete Setup Sequence |
| `postgresql` | 14 | PostgreSQL HA: Complete Setup Sequence |
| `mssql` | 14 | SQL Server 2025 AG: Complete Setup Sequence |
| `oracle-rac` | 18 | Oracle RAC 26ai: Complete Setup Sequence |
| `oracle-dg` | 17 | Oracle DataGuard 26ai: Complete Setup Sequence |

### Options

| Flag | Description |
|---|---|
| `--dry-run` | Print each step's command without executing |
| `--from-step N` | Skip steps before N and resume from step N (1-based) |
| `--continue-on-error` | Continue to the next step even if a step fails |

### Examples

```powershell
# Provision a full cluster from scratch
python -m db_ops.sre.cli --config data/sre_config.json setup-sequence mssql
python -m db_ops.sre.cli --config data/sre_config.json setup-sequence mysql
python -m db_ops.sre.cli --config data/sre_config.json setup-sequence postgresql
python -m db_ops.sre.cli --config data/sre_config.json setup-sequence oracle-rac
python -m db_ops.sre.cli --config data/sre_config.json setup-sequence oracle-dg

# Preview all commands without executing
python -m db_ops.sre.cli --config data/sre_config.json setup-sequence mssql --dry-run

# Resume from step 5 after a previous failure
python -m db_ops.sre.cli --config data/sre_config.json setup-sequence oracle-rac --from-step 5

# Continue past failures instead of aborting
python -m db_ops.sre.cli --config data/sre_config.json setup-sequence oracle-dg --continue-on-error
```

### Steps per db_type

**shared** — 5 steps:

| Step | Sub-command | Args |
|---|---|---|
| 1 | `run-powershell 01-clone-vms` | `-Group shared` |
| 2 | `run-powershell 02-set-vm-resources` | `-Group shared -CpuCount 2 -MemoryGB 2` |
| 3 | `run-powershell 03-start-vms` | `-Group shared` |
| 4 | `run-powershell 04-fix-guest-identity` | `-Group shared` |
| 5 | `run-powershell 08-deploy-host-ssh-key` | `-Group shared` |

**mysql / postgresql / mssql** — 14 steps, same structure; only the `-Group` and final playbook differ:

| Step | Sub-command | Args | ~Time |
|---|---|---|---|
| 1 | `run-powershell 01-clone-vms` | `-Group shared` | 25s |
| 2 | `run-powershell 02-set-vm-resources` | `-Group shared -CpuCount 2 -MemoryGB 2` | 1s |
| 3 | `run-powershell 03-start-vms` | `-Group shared` | 50s |
| 4 | `run-powershell 04-fix-guest-identity` | `-Group shared` | 3m |
| 5 | `run-powershell stop-vms` | `-VmName mon-01` | 7s |
| 6 | `run-powershell stop-vms` | `-VmName log-01` | 7s |
| 7 | `run-powershell 01-clone-vms` | `-Group <db_group>` | 20s |
| 8 | `run-powershell 02-set-vm-resources` | `-Group <db_group> -CpuCount 2 -MemoryGB 4` | 1s |
| 9 | `run-powershell 03-start-vms` | `-Group <db_group>` | 50s |
| 10 | `run-powershell 04-fix-guest-identity` | `-Group <db_group>` | 2–3m |
| 11 | `run-powershell 07-bootstrap-bastion-ansible` | `-TargetGroup <db_group>` | 10s |
| 12 | `run-powershell 08-deploy-host-ssh-key` | `-VmName bastion-01` | 5s |
| 13 | `run-bastion-script bootstrap-bastion-ansible` | `<db_group>` | 2–3m |
| 14 | `run-bastion-playbook <playbook>.yml` | `-i inventory/<db_group>/hosts.yml` | mysql ~12m / pg ~12m / mssql ~6m |

Group â†’ playbook: `mysql` â†’ `mysql-cluster.yml`, `postgresql` â†’ `postgresql-ha.yml`, `mssql` (group `sqlserver`) â†’ `sqlserver-ag.yml`.

Actual total time: **mysql/postgresql ~25–30m** · **mssql ~15–20m** from scratch.

**Note on `01-clone-vms`:** Skips if the VM directory already exists (no overwrite). To re-clone from scratch: delete the VM directory first, or run with `-Force`. If the VM is deleted in VMware but the directory remains â†’ clone is skipped but `02-set-vm-resources` will fail (VM still running).

**oracle-rac** — 18 steps:

| Step | Sub-command | Args | ~Time |
|---|---|---|---|
| 1 | `run-powershell 01-clone-vms` | `-Group shared` | 25s |
| 2 | `run-powershell 02-set-vm-resources` | `-Group shared -CpuCount 2 -MemoryGB 2` | 1s |
| 3 | `run-powershell 03-start-vms` | `-Group shared` | 50s |
| 4 | `run-powershell 04-fix-guest-identity` | `-Group shared` | 3m |
| 5 | `run-powershell stop-vms` | `-VmName mon-01` | 7s |
| 6 | `run-powershell stop-vms` | `-VmName log-01` | 7s |
| 7 | `run-powershell 01-clone-vms` | `-Group oracle_rac` | 20s |
| 8 | `run-powershell 02-set-vm-resources` | `-Group oracle_rac -CpuCount 4 -MemoryGB 12` | 1s |
| 9 | `run-powershell 08-add-oracle-shared-disks` | *(shared disks must be attached before power on)* | 1m |
| 10 | `run-powershell 03-start-vms` | `-Group oracle_rac` | 1m |
| 11 | `run-powershell 04-fix-guest-identity` | `-Group oracle_rac` | 3m |
| 12 | `run-powershell 07-bootstrap-bastion-ansible` | `-TargetGroup oracle_rac` | 10s |
| 13 | `run-powershell 08-deploy-host-ssh-key` | `-VmName bastion-01` | 5s |
| 14 | `run-bastion-script bootstrap-bastion-ansible` | `oracle_rac` | 3m |
| 15 | `run-bastion-playbook oracle-rac-os-prep.yml` | `-i inventory/oracle/rac/hosts.yml` | 20m |
| 16 | `run-powershell 09-stage-oracle-installer` | `-Group oracle_rac` | 10–20m |
| 17 | `run-bastion-playbook oracle-rac-grid.yml` | `-i inventory/oracle/rac/hosts.yml` | 40m |
| 18 | `run-bastion-playbook oracle-rac-db.yml` | `-i inventory/oracle/rac/hosts.yml` | 30m |

Actual total time: **oracle-rac ~90–120m** from scratch (mostly grid + db install).

**oracle-dg** — 17 steps:

| Step | Sub-command | Args | ~Time |
|---|---|---|---|
| 1 | `run-powershell 01-clone-vms` | `-Group shared` | 25s |
| 2 | `run-powershell 02-set-vm-resources` | `-Group shared -CpuCount 2 -MemoryGB 2` | 1s |
| 3 | `run-powershell 03-start-vms` | `-Group shared` | 50s |
| 4 | `run-powershell 04-fix-guest-identity` | `-Group shared` | 3m |
| 5 | `run-powershell stop-vms` | `-VmName mon-01` | 7s |
| 6 | `run-powershell stop-vms` | `-VmName log-01` | 7s |
| 7 | `run-powershell 01-clone-vms` | `-Group oracle_dg` | 20s |
| 8 | `run-powershell 02-set-vm-resources` | `-Group oracle_dg -CpuCount 2 -MemoryGB 8` | 1s |
| 9 | `run-powershell 03-start-vms` | `-Group oracle_dg` | 50s |
| 10 | `run-powershell 04-fix-guest-identity` | `-Group oracle_dg` | 2–3m |
| 11 | `run-powershell 07-bootstrap-bastion-ansible` | `-TargetGroup oracle_dg` | 10s |
| 12 | `run-powershell 08-deploy-host-ssh-key` | `-VmName bastion-01` | 5s |
| 13 | `run-bastion-script bootstrap-bastion-ansible` | `oracle_dg` | 3m |
| 14 | `run-bastion-playbook oracle-dg-os-prep.yml` | `-i inventory/oracle/dataguard/hosts.yml` | 20m |
| 15 | `run-powershell 09-stage-oracle-installer` | `-Group oracle_dg` | 10–20m |
| 16 | `run-bastion-playbook oracle-dg-primary.yml` | `-i inventory/oracle/dataguard/hosts.yml` | 30m |
| 17 | `run-bastion-playbook oracle-dg-standby.yml` | `-i inventory/oracle/dataguard/hosts.yml` | 20m |

Actual total time: **oracle-dg ~75–100m** from scratch.

> **Note:** `setup-sequence` provisions from scratch: shared VMs first (steps 1–4), stops mon-01/log-01 (steps 5–6), then DB VMs (steps 7+). If VMs already exist, use manual commands in "Deploy to Pre-Existing VMs" or `--from-step N` to skip completed steps.

---

## Deploy to Pre-Existing VMs (No VMware Access on DB Nodes)

Use this flow when the DB VMs already exist and are reachable on the network but you do **not** have VMware Tools access to them — physical hosts, non-VMware hypervisor, cloud instances, or security policy prohibits vmrun on those VMs.

**Rule:** vmrun is only ever used for **bastion-01** (start + fix identity + deploy Windows host key). All DB nodes are reached exclusively via SSH from bastion using key-based auth bootstrapped with `sshpass`.

---

### vmrun Usage Audit — SQL Server 2025 AG Orchestrator

`deploy_sqlserver_ag.py` makes the following SRE CLI calls. Every `run-powershell` invocation names `-VmName bastion-01` only. The 3 SQL nodes are never passed to any vmrun command.

| Step | SRE CLI call | VMware target | Touches SQL nodes? |
|---|---|---|---|
| start-bastion | `run-powershell 03-start-vms -- -VmName bastion-01` | bastion-01 | No |
| fix-bastion-identity | `run-powershell 04-fix-guest-identity -- -VmName bastion-01` | bastion-01 | No |
| deploy-host-key-bastion | `run-powershell 08-deploy-host-ssh-key -- -VmName bastion-01` | bastion-01 | No |
| repo-sync-bastion | `scp` to bastion IP + `ssh bastion-IP` extract | None (SCP/SSH only) | No |
| bastion-key-to-sql-nodes | `ssh bastion-IP` â†’ `sshpass ssh <sql-ip>` | None (SSH only) | SSH+password only |
| bootstrap-bastion-ansible | `run-bastion-script bootstrap-bastion-ansible -- sqlserver` | None (SSH only) | Via Ansible key-auth |
| playbook-sqlserver-ag | `run-bastion-playbook sqlserver-ag.yml` | None (SSH only) | Via Ansible key-auth |
| verify-always-on | `ssh 198.51.100.31` (ProxyJump via bastion) | None (SSH only) | Via SSH ProxyJump |

---

### SQL Server 2025 AG — Pre-Existing VMs

**Orchestrator:** `python db_ops/sre/data_folder/deploy_sqlserver_ag.py`

#### Input JSON

Create `db_ops/sre/data_folder/<date>_install_sql_server.json`:

```json
{
  "cluster_name": "sql2025-ag-lab",
  "engine": "sqlserver",
  "version": "2025",
  "deployment_type": "always_on",
  "ssh": {
    "sudo_user": "dba_user",
    "sudo_password": "123456"
  },
  "nodes": [
    {"name": "sql-01", "host": "198.51.100.31", "role": "primary",   "is_primary": true},
    {"name": "sql-02", "host": "198.51.100.32", "role": "secondary", "is_primary": false},
    {"name": "sql-03", "host": "198.51.100.33", "role": "secondary", "is_primary": false}
  ],
  "availability_group": {
    "name": "AG_SQL2025_LAB",
    "primary_node": "198.51.100.31",
    "replicas": ["198.51.100.31", "198.51.100.32", "198.51.100.33"]
  }
}
```

#### Run

```powershell
# from the repository root
$env:PYTHONIOENCODING = "utf-8"
.venv\Scripts\python.exe db_ops/sre/data_folder/deploy_sqlserver_ag.py
```

Results written to `db_ops/sre/data_folder/<date>_result_install_sql_server.json`.

#### Pipeline steps (in order)

| Step | Command | vmrun? |
|---|---|---|
| ensure-ssh-key | Verifies/generates `~/.ssh/db_sre_id_ed25519` on Windows host | No |
| start-bastion | `run-powershell 03-start-vms -- -VmName bastion-01` (waits 30s) | bastion-01 only |
| fix-bastion-identity | `run-powershell 04-fix-guest-identity -- -VmName bastion-01` (waits 40s) | bastion-01 only |
| deploy-host-key-bastion | `run-powershell 08-deploy-host-ssh-key -- -VmName bastion-01` | bastion-01 only |
| repo-sync-bastion | Tarballs `db_ops/sre/`, SCPs to bastion `/opt/db-sre/repo/`, fixes CRLF | No |
| bastion-key-to-sql-nodes | SSHâ†’bastion â†’ `sshpass` pushes bastion pubkey to each SQL node | No |
| bootstrap-bastion-ansible | `run-bastion-script bootstrap-bastion-ansible -- sqlserver` | No |
| playbook-sqlserver-ag | `run-bastion-playbook sqlserver-ag.yml -i inventory/sqlserver/hosts.yml` | No |
| verify-always-on | `ssh 198.51.100.31` â†’ `sqlcmd -Q "SELECT ... FROM sys.availability_groups ..."` (non-critical) | No |

---

### MySQL InnoDB Cluster — Pre-Existing VMs

Update `db_ops/sre/inventory/mysql/hosts.yml` to match your node IPs and names, then run:

```powershell
# 1. Bastion only — vmrun
python -m db_ops.sre.cli run-powershell 03-start-vms -- -VmName bastion-01
python -m db_ops.sre.cli run-powershell 04-fix-guest-identity -- -VmName bastion-01
# Wait ~40s for bastion reboot
python -m db_ops.sre.cli run-powershell 08-deploy-host-ssh-key -- -VmName bastion-01

# 2. Install sshpass + generate bastion SSH key (SSH only — no vmrun)
python -m db_ops.sre.cli ssh 198.51.100.3 -- "echo '123456' | sudo -S apt-get install -y -q sshpass 2>&1 | tail -3; [ ! -f ~/.ssh/id_ed25519 ] && ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519 -q || true"

# 3. Push bastion pubkey to MySQL nodes via sshpass (no vmrun)
python -m db_ops.sre.cli ssh 198.51.100.3 -- "PUBKEY=\$(cat ~/.ssh/id_ed25519.pub); for IP in 198.51.100.11 198.51.100.12 198.51.100.13; do sshpass -p '123456' ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 dba_user@\$IP \"mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && grep -qxF \\\"\$PUBKEY\\\" ~/.ssh/authorized_keys || echo \\\"\$PUBKEY\\\" >> ~/.ssh/authorized_keys && echo \$IP: key deployed\"; done"

# 4. Bootstrap Ansible + run playbook (SSH only)
python -m db_ops.sre.cli run-bastion-script bootstrap-bastion-ansible -- mysql
python -m db_ops.sre.cli run-bastion-playbook automation/ansible/playbooks/mysql-cluster.yml `
    -i inventory/mysql/hosts.yml

# 5. Verify
python -m db_ops.sre.cli check-mysql-cluster
```

---

### PostgreSQL HA — Pre-Existing VMs

Update `db_ops/sre/inventory/postgresql/hosts.yml` to match your node IPs and names, then run:

```powershell
# 1. Bastion only — vmrun
python -m db_ops.sre.cli run-powershell 03-start-vms -- -VmName bastion-01
python -m db_ops.sre.cli run-powershell 04-fix-guest-identity -- -VmName bastion-01
# Wait ~40s for bastion reboot
python -m db_ops.sre.cli run-powershell 08-deploy-host-ssh-key -- -VmName bastion-01

# 2. Install sshpass + generate bastion SSH key (SSH only — no vmrun)
python -m db_ops.sre.cli ssh 198.51.100.3 -- "echo '123456' | sudo -S apt-get install -y -q sshpass 2>&1 | tail -3; [ ! -f ~/.ssh/id_ed25519 ] && ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519 -q || true"

# 3. Push bastion pubkey to PostgreSQL nodes via sshpass (no vmrun)
python -m db_ops.sre.cli ssh 198.51.100.3 -- "PUBKEY=\$(cat ~/.ssh/id_ed25519.pub); for IP in 198.51.100.21 198.51.100.22 198.51.100.23; do sshpass -p '123456' ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 dba_user@\$IP \"mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && grep -qxF \\\"\$PUBKEY\\\" ~/.ssh/authorized_keys || echo \\\"\$PUBKEY\\\" >> ~/.ssh/authorized_keys && echo \$IP: key deployed\"; done"

# 4. Bootstrap Ansible + run playbook (SSH only)
python -m db_ops.sre.cli run-bastion-script bootstrap-bastion-ansible -- postgresql
python -m db_ops.sre.cli run-bastion-playbook automation/ansible/playbooks/postgresql-ha.yml `
    -i inventory/postgresql/hosts.yml

# 5. Verify
python -m db_ops.sre.cli check-postgresql-ha
```

---

### Oracle RAC — Pre-Existing VMs

For pre-existing Oracle Linux nodes with shared ASM disks already attached. No `add-oracle-shared-disks` step needed. Set installer paths in `sre_config.json` before step 7.

```powershell
# 1. Bastion only — vmrun
python -m db_ops.sre.cli run-powershell 03-start-vms -- -VmName bastion-01
python -m db_ops.sre.cli run-powershell 04-fix-guest-identity -- -VmName bastion-01
# Wait ~40s for bastion reboot
python -m db_ops.sre.cli run-powershell 08-deploy-host-ssh-key -- -VmName bastion-01

# 2. Install sshpass + generate bastion SSH key (SSH only — no vmrun)
python -m db_ops.sre.cli ssh 198.51.100.3 -- "echo '123456' | sudo -S apt-get install -y -q sshpass 2>&1 | tail -3; [ ! -f ~/.ssh/id_ed25519 ] && ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519 -q || true"

# 3. Push bastion pubkey to RAC nodes via sshpass (Oracle Linux — no vmrun)
python -m db_ops.sre.cli ssh 198.51.100.3 -- "PUBKEY=\$(cat ~/.ssh/id_ed25519.pub); for IP in 198.51.100.41 198.51.100.42; do sshpass -p '123456' ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 dba_user@\$IP \"mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && grep -qxF \\\"\$PUBKEY\\\" ~/.ssh/authorized_keys || echo \\\"\$PUBKEY\\\" >> ~/.ssh/authorized_keys && echo \$IP: key deployed\"; done"

# 4. Bootstrap Ansible (SSH only)
python -m db_ops.sre.cli run-bastion-script bootstrap-bastion-ansible -- oracle_rac

# 5. OS prep (SSH only)
python -m db_ops.sre.cli run-bastion-playbook automation/ansible/playbooks/oracle-rac-os-prep.yml `
    -i inventory/oracle/rac/hosts.yml

# 6. Stage Oracle installers: Windows â†’ bastion SCP â†’ RAC nodes SCP (no vmrun)
python -m db_ops.sre.cli run-powershell stage-oracle-installer -- -Group oracle_rac

# 7. Install Grid Infrastructure (SSH only, 20–40 min)
python -m db_ops.sre.cli run-bastion-playbook automation/ansible/playbooks/oracle-rac-grid.yml `
    -i inventory/oracle/rac/hosts.yml

# 8. Install Oracle Database (SSH only, 20–40 min)
python -m db_ops.sre.cli run-bastion-playbook automation/ansible/playbooks/oracle-rac-db.yml `
    -i inventory/oracle/rac/hosts.yml

# 9. Verify
python -m db_ops.sre.cli ssh 198.51.100.41 -- "sudo -u grid /u01/app/23c/grid/bin/crsctl stat res -t"
python -m db_ops.sre.cli ssh 198.51.100.41 -- "sudo -u oracle /u01/app/product/db23c/bin/srvctl status database -d pridb"
```

---

### Oracle DataGuard — Pre-Existing VMs

For pre-existing Oracle Linux nodes. DB installer only — no Grid, no shared disks.

```powershell
# 1. Bastion only — vmrun
python -m db_ops.sre.cli run-powershell 03-start-vms -- -VmName bastion-01
python -m db_ops.sre.cli run-powershell 04-fix-guest-identity -- -VmName bastion-01
# Wait ~40s for bastion reboot
python -m db_ops.sre.cli run-powershell 08-deploy-host-ssh-key -- -VmName bastion-01

# 2. Install sshpass + generate bastion SSH key (SSH only — no vmrun)
python -m db_ops.sre.cli ssh 198.51.100.3 -- "echo '123456' | sudo -S apt-get install -y -q sshpass 2>&1 | tail -3; [ ! -f ~/.ssh/id_ed25519 ] && ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519 -q || true"

# 3. Push bastion pubkey to DG nodes via sshpass (Oracle Linux — no vmrun)
python -m db_ops.sre.cli ssh 198.51.100.3 -- "PUBKEY=\$(cat ~/.ssh/id_ed25519.pub); for IP in 198.51.100.51 198.51.100.52; do sshpass -p '123456' ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 dba_user@\$IP \"mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && grep -qxF \\\"\$PUBKEY\\\" ~/.ssh/authorized_keys || echo \\\"\$PUBKEY\\\" >> ~/.ssh/authorized_keys && echo \$IP: key deployed\"; done"

# 4. Bootstrap Ansible (SSH only)
python -m db_ops.sre.cli run-bastion-script bootstrap-bastion-ansible -- oracle_dg

# 5. OS prep (SSH only)
python -m db_ops.sre.cli run-bastion-playbook automation/ansible/playbooks/oracle-dg-os-prep.yml `
    -i inventory/oracle/dataguard/hosts.yml

# 6. Stage DB installer: Windows â†’ bastion SCP â†’ DG nodes SCP (no vmrun)
python -m db_ops.sre.cli run-powershell stage-oracle-installer -- -Group oracle_dg

# 7. Install Oracle DB + create primary database (SSH only, 20–40 min)
python -m db_ops.sre.cli run-bastion-playbook automation/ansible/playbooks/oracle-dg-primary.yml `
    -i inventory/oracle/dataguard/hosts.yml

# 8. Create standby + configure DG broker (SSH only, 20–40 min)
python -m db_ops.sre.cli run-bastion-playbook automation/ansible/playbooks/oracle-dg-standby.yml `
    -i inventory/oracle/dataguard/hosts.yml

# 9. Verify
python -m db_ops.sre.cli ssh 198.51.100.51 -- "sudo -u oracle ORACLE_HOME=/u01/app/product/db23c /u01/app/product/db23c/bin/dgmgrl / 'show configuration'"
```

---

## Stop VMs

```powershell
# Stop a specific group (graceful soft stop â†’ hard stop if timeout)
python -m db_ops.sre.cli run-powershell stop-vms -- -Group mysql
python -m db_ops.sre.cli run-powershell stop-vms -- -Group postgresql
python -m db_ops.sre.cli run-powershell stop-vms -- -Group sqlserver
python -m db_ops.sre.cli run-powershell stop-vms -- -Group oracle_rac
python -m db_ops.sre.cli run-powershell stop-vms -- -Group oracle_dg
python -m db_ops.sre.cli run-powershell stop-vms -- -Group shared
python -m db_ops.sre.cli run-powershell stop-vms -- -Group all

# Stop specific VMs by name
python -m db_ops.sre.cli run-powershell stop-vms -- -VmName mssql-01,mssql-02

# Stop all currently running VMs (no grace period)
python -m db_ops.sre.cli run-powershell stop-all-running-vms
```

`stop-vms` sends a soft (ACPI) shutdown first, waits up to 120 seconds, then force-stops any remaining VMs. Add `-SkipHardStop` to skip the force step.

---

## E2E Test Runner

`tests/sre_e2e_runner.py` runs the full provisioning â†’ verify â†’ teardown sequence for each cluster group. Each group is a self-contained test case executed by the Python runner — no manual step-by-step needed.

**Working directory:** the repository root (`db_ops`)

### Usage

```powershell
# Run all groups
python tests/sre_e2e_runner.py

# Run specific groups
python tests/sre_e2e_runner.py --groups shared mysql
python tests/sre_e2e_runner.py --groups oracle_rac oracle_dg

# Dry-run: print commands without executing
python tests/sre_e2e_runner.py --groups all --dry-run

# Skip teardown after test (keep VMs for debugging)
python tests/sre_e2e_runner.py --groups mysql --no-cleanup

# Custom config (default: a test config beside data/sre_config.json)
python tests/sre_e2e_runner.py --config data/my_config.json --groups mysql
```

### Config

| File | Purpose |
|---|---|
| a test-only config | Separate VM root and address range from `data/sre_config.json`, so a test run cannot touch the real lab |
| `data/sre_config.json` | Production lab config (untouched by the test runner) |

Templates directory junction (create once):
```powershell
cmd /c mklink /J D:\VMs_test\templates D:\VMs\templates
```

### IP ranges (test config)

| Group | VMs | IPs |
|---|---|---|
| shared | bastion-01 | .61 |
| mysql | mysql-01/02/03 | .71–.73 |
| postgresql | pg-01/02/03 | .81–.83 |
| sqlserver | mssql-01/02/03 | .91–.93 |
| oracle_rac | rac01, rac02 | .95–.96 |
| oracle_dg | orapri, orastb | .97–.98 |

### Steps per test case

**shared** — provisions bastion-01 only; mon-01/log-01 skipped to conserve RAM. Must run before any DB group.

| Step | Command |
|---|---|
| clone-vms | `01-clone-vms -VmName bastion-01` |
| set-resources | `02-set-vm-resources -VmName bastion-01 -CpuCount 2 -MemoryGB 2` |
| start-vms | `03-start-vms -VmName bastion-01` |
| fix-identity | `04-fix-guest-identity -VmName bastion-01` |
| deploy-key | `08-deploy-host-ssh-key -VmName bastion-01` |

Teardown: `stop-vms -VmName bastion-01` (runs after all DB groups).

---

**mysql**

| Step | Command |
|---|---|
| clone-vms | `01-clone-vms -Group mysql` |
| set-resources | `02-set-vm-resources -Group mysql -CpuCount 2 -MemoryGB 4` |
| start-vms | `03-start-vms -Group mysql` |
| fix-identity | `04-fix-guest-identity -Group mysql` |
| bootstrap-bastion | `07-bootstrap-bastion-ansible -TargetGroup mysql` |
| bootstrap-ansible | `run-bastion-script bootstrap-bastion-ansible mysql` |
| playbook:mysql-cluster | `run-bastion-playbook automation/ansible/playbooks/mysql-cluster.yml` |
| verify:check-mysql-cluster | `check-mysql-cluster` (non-critical) |
| stop | `stop-vms -Group mysql` (cleanup) |

---

**mssql**

| Step | Command |
|---|---|
| clone-vms | `01-clone-vms -Group sqlserver` |
| set-resources | `02-set-vm-resources -Group sqlserver -CpuCount 4 -MemoryGB 8` |
| start-vms | `03-start-vms -Group sqlserver` |
| fix-identity | `04-fix-guest-identity -Group sqlserver` |
| bootstrap-bastion | `07-bootstrap-bastion-ansible -TargetGroup sqlserver` |
| bootstrap-ansible | `run-bastion-script bootstrap-bastion-ansible sqlserver` |
| playbook:sqlserver-ag | `run-bastion-playbook automation/ansible/playbooks/sqlserver-ag.yml` (90 min) |
| stop | `stop-vms -Group sqlserver` (cleanup) |

---

**postgresql**

| Step | Command |
|---|---|
| clone-vms | `01-clone-vms -Group postgresql` |
| set-resources | `02-set-vm-resources -Group postgresql -CpuCount 2 -MemoryGB 4` |
| start-vms | `03-start-vms -Group postgresql` |
| fix-identity | `04-fix-guest-identity -Group postgresql` |
| bootstrap-bastion | `07-bootstrap-bastion-ansible -TargetGroup postgresql` |
| bootstrap-ansible | `run-bastion-script bootstrap-bastion-ansible postgresql` |
| playbook:postgresql-ha | `run-bastion-playbook automation/ansible/playbooks/postgresql-ha.yml` |
| verify:check-postgresql-ha | `check-postgresql-ha` (non-critical) |
| stop | `stop-vms -Group postgresql` (cleanup) |

---

**oracle_rac** — 2-phase due to shared ASM disks. Oracle Linux template required.

| Phase | Step | Command |
|---|---|---|
| 1 | clone-vms | `01-clone-vms -Group oracle_rac -Template tpl-oraclelinux-r9u6` |
| 1 | set-resources | `02-set-vm-resources -Group oracle_rac -CpuCount 4 -MemoryGB 12` |
| 1 | add-shared-disks | `08-add-oracle-shared-disks` |
| 1 | start-vms | `03-start-vms -Group oracle_rac` |
| — | IP discovery | ARP scan â†’ MACâ†’IP map for fix-identity |
| 2 | fix-identity | `04-fix-guest-identity -Group oracle_rac -CurrentIpMapJson ...` |
| 2 | bootstrap-bastion | `07-bootstrap-bastion-ansible -TargetGroup oracle_rac` |
| 2 | bootstrap-ansible | `run-bastion-script bootstrap-bastion-ansible oracle_rac` |
| 2 | playbook:oracle-rac-os-prep | `run-bastion-playbook automation/ansible/playbooks/oracle-rac-os-prep.yml` |
| 2 | stage-installer | `09-stage-oracle-installer -Group oracle_rac` |
| 2 | playbook:oracle-rac-grid | `run-bastion-playbook oracle-rac-grid.yml` (20–40 min) |
| 2 | playbook:oracle-rac-db | `run-bastion-playbook oracle-rac-db.yml` (20–40 min) |
| cleanup | stop + delete | `stop-vms`, `delete-vms`, removes `D:\VMs_test\oracle_rac_shared\` |

---

**dataguard** — 2-phase pattern, no shared disks. DB installer only.

| Phase | Step | Command |
|---|---|---|
| 1 | clone-vms | `01-clone-vms -Group oracle_dg -Template tpl-oraclelinux-r9u6` |
| 1 | set-resources | `02-set-vm-resources -Group oracle_dg -CpuCount 4 -MemoryGB 16` |
| 1 | start-vms | `03-start-vms -Group oracle_dg` |
| — | IP discovery | ARP scan â†’ MACâ†’IP map |
| 2 | fix-identity | `04-fix-guest-identity -Group oracle_dg -CurrentIpMapJson ...` |
| 2 | bootstrap-bastion | `07-bootstrap-bastion-ansible -TargetGroup oracle_dg` |
| 2 | bootstrap-ansible | `run-bastion-script bootstrap-bastion-ansible oracle_dg` |
| 2 | playbook:oracle-dg-os-prep | `run-bastion-playbook automation/ansible/playbooks/oracle-dg-os-prep.yml` |
| 2 | stage-installer | `09-stage-oracle-installer -Group oracle_dg` |
| 2 | playbook:oracle-dg-primary | `run-bastion-playbook oracle-dg-primary.yml` (20–40 min) |
| 2 | playbook:oracle-dg-standby | `run-bastion-playbook oracle-dg-standby.yml` (20–40 min) |
| cleanup | stop + delete | `stop-vms`, `delete-vms` |

### Timeouts

| Category | Value | Steps |
|---|---|---|
| Short | 5 min | clone, start, stop, delete |
| Medium | 30 min | fix-identity, bootstrap, simple playbooks |
| Long | 90 min | Oracle Grid, DB, DataGuard RMAN duplicate |

Critical step failure aborts the group immediately and triggers cleanup. Non-critical steps (verify checks) report failure but do not abort.

### Output

```
================================================================
  E2E TEST SUMMARY
================================================================

  [PASS] SHARED           5 steps   12s
  [PASS] MYSQL            8 steps  1823s
  [FAIL] POSTGRESQL       6 steps   640s
    XX  playbook:postgresql-ha             600.0s  rc=1
```

Exit code `0` = all groups passed, `1` = at least one group failed.

---

## Template Bootstrap (One-Time)

Before cloning, the template VM must have SSH and open-vm-tools configured.

```powershell
# Bootstrap all templates (ubuntu + oraclelinux + windows)
python -m db_ops.sre.cli run-powershell 00-bootstrap-template-remote-access

# Or a specific template
python -m db_ops.sre.cli run-powershell 00-bootstrap-template-remote-access -- -Template ubuntu
python -m db_ops.sre.cli run-powershell 00-bootstrap-template-remote-access -- -Template oraclelinux
```

After bootstrap, take a snapshot of the template so clones start from a clean state:

```powershell
python -m db_ops.sre.cli run-powershell 06-snapshot-vms -- -Group shared   # if template is in shared
```

---

## CLI Reference

### Read-Only

```powershell
python -m db_ops.sre.cli list-workflows        # registered workflows
python -m db_ops.sre.cli inventory-assets      # nodes from config (no SSH)
python -m db_ops.sre.cli vmware-list           # available PowerShell wrapper scripts
```

### VMware / PowerShell

All `run-powershell` commands pass the full SRE config to the script via `-DbSrePayloadJsonBase64`. Arguments after `--` are forwarded as-is to the PowerShell script.

```powershell
python -m db_ops.sre.cli run-powershell 00-bootstrap-template-remote-access
python -m db_ops.sre.cli run-powershell 00-bootstrap-template-remote-access -- -Template ubuntu

python -m db_ops.sre.cli run-powershell 01-clone-vms
python -m db_ops.sre.cli run-powershell 01-clone-vms -- -Group mysql
python -m db_ops.sre.cli run-powershell 01-clone-vms -- -VmName mysql-02

python -m db_ops.sre.cli run-powershell 02-set-vm-resources -- -Group mysql -CpuCount 2 -MemoryGB 4
python -m db_ops.sre.cli run-powershell 02-set-vm-resources -- -VmName mysql-01 -CpuCount 4 -MemoryGB 8

python -m db_ops.sre.cli run-powershell 03-start-vms
python -m db_ops.sre.cli run-powershell 03-start-vms -- -Group shared
python -m db_ops.sre.cli run-powershell 03-start-vms -- -VmName bastion-01

python -m db_ops.sre.cli run-powershell 04-fix-guest-identity
python -m db_ops.sre.cli run-powershell 04-fix-guest-identity -- -Group mysql
python -m db_ops.sre.cli run-powershell 04-fix-guest-identity -- -VmName mysql-01 -SkipReboot

python -m db_ops.sre.cli run-powershell 05-get-vm-info

python -m db_ops.sre.cli run-powershell 06-snapshot-vms

python -m db_ops.sre.cli run-powershell 07-bootstrap-bastion-ansible -- -TargetGroup mysql
python -m db_ops.sre.cli run-powershell 07-bootstrap-bastion-ansible -- -TargetGroup postgresql
python -m db_ops.sre.cli run-powershell 07-bootstrap-bastion-ansible -- -TargetGroup sqlserver
python -m db_ops.sre.cli run-powershell 07-bootstrap-bastion-ansible -- -TargetGroup oracle_rac
python -m db_ops.sre.cli run-powershell 07-bootstrap-bastion-ansible -- -TargetGroup oracle_dg

# Deploy to shared VMs only (bastion-01, mon-01, ...) — NOT database nodes
python -m db_ops.sre.cli run-powershell 08-deploy-host-ssh-key -- -Group shared
python -m db_ops.sre.cli run-powershell 08-deploy-host-ssh-key -- -VmName bastion-01

# Oracle RAC shared ASM disks (VMs must be stopped)
python -m db_ops.sre.cli run-powershell add-oracle-shared-disks
python -m db_ops.sre.cli run-powershell add-oracle-shared-disks -- -OcrDiskGb 15 -DataDiskGb 80 -FraDiskGb 50
python -m db_ops.sre.cli run-powershell add-oracle-shared-disks -- -Force

# Oracle installer staging (Windows â†’ bastion â†’ nodes via SCP)
# RAC: requires oracle.grid_installer_path + oracle.db_installer_path
# DG:  requires oracle.db_installer_path only
python -m db_ops.sre.cli run-powershell stage-oracle-installer -- -Group oracle_rac
python -m db_ops.sre.cli run-powershell stage-oracle-installer -- -Group oracle_dg

# Stop VMs by group (soft â†’ hard)
python -m db_ops.sre.cli run-powershell stop-vms -- -Group mysql
python -m db_ops.sre.cli run-powershell stop-vms -- -Group postgresql
python -m db_ops.sre.cli run-powershell stop-vms -- -Group sqlserver
python -m db_ops.sre.cli run-powershell stop-vms -- -Group oracle_rac
python -m db_ops.sre.cli run-powershell stop-vms -- -Group oracle_dg
python -m db_ops.sre.cli run-powershell stop-vms -- -Group shared
python -m db_ops.sre.cli run-powershell stop-vms -- -Group all
python -m db_ops.sre.cli run-powershell stop-vms -- -VmName mssql-01,mssql-02

python -m db_ops.sre.cli run-powershell list-running-vms
python -m db_ops.sre.cli run-powershell stop-all-running-vms
```

Add `--dry-run` to any command to print the resolved PowerShell invocation without executing:

```powershell
python -m db_ops.sre.cli run-powershell 01-clone-vms --dry-run
```

### Bastion SSH Commands

Requires: Step 5 (host key deployed) and Step 6 (repo synced on bastion).

```powershell
# Run Ansible playbook from bastion
python -m db_ops.sre.cli run-bastion-playbook automation/ansible/playbooks/mysql-cluster.yml `
    -i inventory/mysql/hosts.yml
python -m db_ops.sre.cli run-bastion-playbook automation/ansible/playbooks/postgresql-ha.yml `
    -i inventory/postgresql/hosts.yml
python -m db_ops.sre.cli run-bastion-playbook automation/ansible/playbooks/sqlserver-ag.yml `
    -i inventory/sqlserver/hosts.yml
python -m db_ops.sre.cli run-bastion-playbook automation/ansible/playbooks/observability.yml `
    -i inventory/monitoring/hosts.yml

# Run a bash script on bastion
python -m db_ops.sre.cli run-bastion-script bootstrap-bastion-ansible -- mysql
python -m db_ops.sre.cli run-bastion-script bootstrap-bastion-ansible -- postgresql
python -m db_ops.sre.cli run-bastion-script bootstrap-bastion-ansible -- sqlserver
python -m db_ops.sre.cli run-bastion-script bootstrap-bastion-ansible -- oracle_rac
python -m db_ops.sre.cli run-bastion-script bootstrap-bastion-ansible -- oracle_dg
python -m db_ops.sre.cli run-bastion-script recover-mysql-cluster-after-outage.sh

# Oracle RAC Ansible playbooks
python -m db_ops.sre.cli run-bastion-playbook automation/ansible/playbooks/oracle-rac-os-prep.yml `
    -i inventory/oracle/rac/hosts.yml
python -m db_ops.sre.cli run-bastion-playbook automation/ansible/playbooks/oracle-rac-grid.yml `
    -i inventory/oracle/rac/hosts.yml
python -m db_ops.sre.cli run-bastion-playbook automation/ansible/playbooks/oracle-rac-db.yml `
    -i inventory/oracle/rac/hosts.yml

# Oracle DataGuard Ansible playbooks
python -m db_ops.sre.cli run-bastion-playbook automation/ansible/playbooks/oracle-dg-os-prep.yml `
    -i inventory/oracle/dataguard/hosts.yml
python -m db_ops.sre.cli run-bastion-playbook automation/ansible/playbooks/oracle-dg-primary.yml `
    -i inventory/oracle/dataguard/hosts.yml
python -m db_ops.sre.cli run-bastion-playbook automation/ansible/playbooks/oracle-dg-standby.yml `
    -i inventory/oracle/dataguard/hosts.yml

# Ansible ad-hoc
python -m db_ops.sre.cli run-bastion-ansible mysql -m ping
python -m db_ops.sre.cli run-bastion-ansible mysql -m shell -a "systemctl is-active mysql"
python -m db_ops.sre.cli run-bastion-ansible sqlserver -m ping
python -m db_ops.sre.cli run-bastion-ansible sqlserver -m shell -a "systemctl is-active mssql-server"
```

### setup-sequence

```powershell
# Provision a full cluster end-to-end
python -m db_ops.sre.cli --config data/sre_config.json setup-sequence mysql
python -m db_ops.sre.cli --config data/sre_config.json setup-sequence postgresql
python -m db_ops.sre.cli --config data/sre_config.json setup-sequence mssql
python -m db_ops.sre.cli --config data/sre_config.json setup-sequence oracle-rac
python -m db_ops.sre.cli --config data/sre_config.json setup-sequence oracle-dg

# Print each step's command without executing
python -m db_ops.sre.cli --config data/sre_config.json setup-sequence mssql --dry-run

# Resume from step N (skip earlier steps)
python -m db_ops.sre.cli --config data/sre_config.json setup-sequence oracle-rac --from-step 5

# Continue past step failures
python -m db_ops.sre.cli --config data/sre_config.json setup-sequence oracle-dg --continue-on-error
```

### Health Checks

```powershell
# Shared VMs: hostname, IP, systemd state
python -m db_ops.sre.cli check-shared-vms
python -m db_ops.sre.cli check-shared-vms --vm-name bastion-01
python -m db_ops.sre.cli check-shared-vms --vm-name bastion-01 mon-01

# MySQL: 1 PRIMARY + 2 SECONDARY via mysqlsh
python -m db_ops.sre.cli check-mysql-cluster

# PostgreSQL: 1 primary + 2 streaming replicas via psql
python -m db_ops.sre.cli check-postgresql-ha

# Dry-run: print SSH commands without executing
python -m db_ops.sre.cli check-mysql-cluster --dry-run
python -m db_ops.sre.cli check-shared-vms --dry-run
```

### Direct SSH

```powershell
python -m db_ops.sre.cli ssh 198.51.100.3  -- uptime
python -m db_ops.sre.cli ssh 198.51.100.11 -- systemctl status mysql
```

### Lab Database Docker Instances

`create-db-docker` provisions a single or HA-lab database **container** (distinct from the VMware cluster flows above), writes its compose + `.env` under `/opt/db_ops/containers/<name>/`, brings it up with `docker compose`, waits for health, and registers the connection in `data/docker_db_connections.json`. It can run three ways:

1. **inside the worker container** from the master via the control app's `worker-run` — the original path, still fully supported and **unchanged**. Example:
   `python -m db_ops.control.cli worker-run --key-base64 "<KEY>" -- python -m db_ops.sre.cli create-db-docker --name ora_lab --engine oracle --version 23.26.2 --mode ha-lab`.
   Here docker runs on the worker host and the post-start step (Oracle Data Guard / SQL Server AG) executes **synchronously inside the container** using the local filesystem — the `--remote-host` machinery below is not involved. All four engines and their HA labs work this way. (See `docs/11_control_app.md`.)
2. **locally** on any Docker-equipped host (run the CLI directly, no `--remote-host`);
3. **directly against a remote Ubuntu host over SSH** with `--remote-host <ip> --remote-user <user>` — the CLI runs on the master (no intermediate hop, no worker involved): instance files are written over SFTP and every `docker`/`docker compose`/health command executes on that machine. The connection entry is registered in the **master's** `data/docker_db_connections.json` with the remote IP as host.

   **What you need for a one-command remote install** — nothing pre-seeded:
   - `--remote-host <ip> --remote-user <user>` and SSH auth, in one of three forms: **`--remote-key <path>`** (an SSH private key file — how key-auth VMs such as Oracle Cloud connect; no password), **`--remote-password '<pass>'`** (inline value), or **`--remote-password-ref <REF>`** (a ref in the encrypted secret store). A key-auth VM with passwordless sudo needs no password at all.
   - the DB password with `--password-text '<db-pass>'` (stored under `<NAME>_PASSWORD` in the master secret store, which needs `--key-base64 "<KEY>"`); or set an env var and pass `--password-env`.
   - on the VM: **docker + the compose plugin installed**, and the containers dir writable by the SSH user (one-time: `sudo mkdir -p /opt/db_ops/containers && sudo chown <user>: /opt/db_ops/containers`) — **or pass `--install-docker`** to have the CLI do all of that over SSH for a bare Ubuntu VM (installs docker via `get.docker.com`, falls back to distro packages, adds the SSH user to the docker group, creates the dir; needs the SSH user to have sudo). `--install-docker` is idempotent: a host that already has Docker is left as-is.

   Long post-start steps (the Oracle Data Guard RMAN duplicate, ~3–4 min) run **detached on the remote host and are polled**, so a blip in the control SSH connection does not interrupt them — the one command runs to completion on its own.

   **Every generated lab pins its own network subnet (2026-08-14).** The compose file carries

   ```yaml
   networks:
     default:
       ipam:
         config:
           - subnet: 172.30.<n>.0/24
   ```

   where `<n>` is derived from `--name` (`db_ops/sre/docker_db/models.py::lab_network_subnet`), or
   whatever `--network-subnet` says. It is never left to Docker, whose default pool spans
   `172.17.0.0/16`–`172.31.0.0/16` — private ranges that plenty of networks already route real
   databases on. An auto-allocated lab bridge has twice taken a production instance off the map:
   on 2026-08-14 `ora11g_lab` was created, Docker gave it `172.18.0.0/16`, and the host route sent
   a monitored instance inside that range into the new bridge instead of the LAN — every metric on
   a healthy SQL Server failed to connect for two hours. Two lab names can hash to the same `/24`; that is a loud `docker compose up` error
   ("Pool overlaps…"), settled with `--network-subnet 172.30.42.0/24`. The host-side half of the
   guard is `db_ops/sre/host_config/docker-daemon.json`, which also moves `docker0` off `172.17.0.0/16`.

   **`oracle` vs `oracle-xe` — two engines, not one with different tags (2026-08-14).**
   `--engine oracle` is Oracle Database **Free** (`gvenzl/oracle-free`, 23ai/26ai);
   `--engine oracle-xe` is Oracle Database **Express Edition** (`gvenzl/oracle-xe`), which is
   the only way to get **11g R2** in a container — tag `11` is 11.2.0.2. They are separate
   entries because almost nothing about them matches:

   | | `oracle` | `oracle-xe` |
   | --- | --- | --- |
   | Image | `gvenzl/oracle-free` | `gvenzl/oracle-xe` |
   | Service to connect to | `FREEPDB1` (pluggable) | **`XE`** — non-CDB, there is no PDB |
   | `--mode ha-lab` | Data Guard 1+1 | **not supported** (Data Guard is an Enterprise feature; XE is the most cut-down edition) |
   | Limits | Free edition limits | 11 GB user data, 1 GB SGA+PGA, 1 CPU |

   Sharing one entry would put a service name that does not exist into every connection string
   the registry writes, and the failure would arrive at first login rather than at provisioning.
   On 18c/21c XE the pluggable database is `XEPDB1`, so `--version 21` is *not* interchangeable
   with `11` in a connect string — `11` is what this engine is documented for.

   **There is no 32-bit option.** Oracle never shipped a Linux x86 (32-bit) build of 11.2 XE (the
   32-bit Express Edition was 10.2), and the image manifest is `architecture: amd64`. A 32-bit
   11g R2 container would mean building an image from an Oracle installer you supply yourself;
   it is not something this command can grow.

   The registry records `engine: "oracle-xe"`, and `db_connect.normalize_db_type` maps it to
   `oracle` — the distinction matters when creating the container, never when connecting to it.

   **Password characters the engine cannot carry.** Oracle sets the password with
   `ALTER USER SYS IDENTIFIED BY "<pw>"` through SQL\*Plus, which expands `&name` as a
   **substitution variable** — a password containing `&` is silently replaced by whatever
   text follows in the image's own script, so the database comes up *healthy* with a password
   nobody knows and every later login fails `ORA-01017`. On an ha-lab that surfaces only at
   the Data Guard `connect target`, after the whole database has been copied. `"` breaks the
   same statement's quoting. Both are refused up front by `provisioner.validate_password()`
   (`ENGINE_META[...].forbidden_password_chars`), before the password reaches the `.env` file
   and before a container exists. Letters, digits and `_ - . # ! %` are safe.

   **How long the provisioner waits** — two separate budgets, both engine facts rather than
   caller decisions (`ENGINE_META` in `sre/docker_db/models.py`):

   | | health (first start) | post-start step | + pull allowance | total |
   | --- | --- | --- | --- | --- |
   | postgres / mysql / mssql | 180 s | 900 s | 600 s | 1680 s |
   | **oracle** | **1500 s** | **1500 s** | 600 s | **3600 s** |

   Oracle is the outlier because its first start on an empty volume *creates the database* —
   and an ha-lab creates two at once on one host. The 180 s that is plenty for "has postgres
   opened its port" timed both Data Guard nodes out before either finished initialising, so
   `setup_dataguard.sh` was never reached. The health wait is a **ceiling, not a delay**: the
   poll returns the moment every node reports healthy, so a generous value costs nothing on a
   healthy run. `--health-timeout <seconds>` still overrides it for a slower host, or to fail
   fast.

   **The totals are not free-floating.** The tightest caller is the Telegram
   `spbot_create_db_docker` command, whose poller **SIGKILLs** the process at its own
   `timeout_seconds` (60 min). Every engine's budget must finish inside that with room for
   the image pull, or the run is killed mid-step and the operator gets a blunt "timed out"
   instead of the provisioner's message naming the failed step and how to resume. The two
   numbers live in `models.py` (`CALLER_BUDGET_SECONDS`) and in the command config; a test
   (`tests/test_docker_db_oracle_remote.py`) fails if they drift apart. Raising one means
   raising the other.

```powershell
# PostgreSQL single instance
python -m db_ops.sre.cli create-db-docker `
    --name pg_lab_01 --engine postgres --version 16 --mode single `
    --host-port 5433 --password-env POSTGRES_PASSWORD

# MySQL single instance
python -m db_ops.sre.cli create-db-docker `
    --name mysql_lab_01 --engine mysql --version 8.4 --mode single `
    --host-port 3307 --password-env MYSQL_ROOT_PASSWORD

# SQL Server single instance. --host-port and --password-env may be omitted: the port defaults
# to the engine's own (1433) and the secret ref to <NAME>_PASSWORD.
python -m db_ops.sre.cli create-db-docker `
    --name mssql_lab_01 --engine mssql --version 2022-latest --mode single

# PostgreSQL HA lab: 1 primary + N standbys via native streaming replication
# (official postgres image; standbys seeded with pg_basebackup); each node on its own port
python -m db_ops.sre.cli create-db-docker `
    --name pg_ha_lab_01 --engine postgres --version 18 --mode ha-lab `
    --replicas 2 --host-port 5433 --password-env POSTGRES_PASSWORD

# SQL Server Always On lab: 3 nodes, AG with CLUSTER_TYPE = NONE (see the note below).
# Also stores the password in the encrypted secret store under MSSQL_AG_LAB_PASSWORD.
python -m db_ops.sre.cli create-db-docker `
    --name mssql_ag_lab --engine mssql --version 2022-latest --mode ha-lab `
    --replicas 2 --host-port 15433 --password-text '<StrongPassw0rd!>'

# Oracle Database Free 26ai single instance (gvenzl/oracle-free image; the tag is the major
# version — 26 = 26ai, 23 = 23ai; there is no '26ai' tag). For 11g R2 see `oracle-xe` below.
python -m db_ops.sre.cli create-db-docker `
    --name ora_lab_01 --engine oracle --version 23.26.2 --mode single

# Oracle Data Guard lab: 1 primary + 1 physical standby (replicas fixed at 1)
python -m db_ops.sre.cli create-db-docker `
    --name ora_dg_lab --engine oracle --version 23.26.2 --mode ha-lab --host-port 15210 `
    --password-text '<StrongPassw0rd!>' --key-base64 "<KEY>"

# Oracle 11g R2 — engine `oracle-xe`, NOT a tag of `oracle`. See the note below.
python -m db_ops.sre.cli create-db-docker `
    --name ora11g_lab --engine oracle-xe --version 11 --mode single --host-port 1521 `
    --password-text '<StrongPassw0rd!>' --key-base64 "<KEY>"

# Provision DIRECTLY on a remote Ubuntu host from the master (no worker hop), nothing
# pre-seeded — just ip + SSH user + SSH password. docker runs on that machine.
# Oracle Data Guard (1 primary + 1 standby) on the VM in a single command:
python -m db_ops.sre.cli create-db-docker `
    --name ora_dg_lab --engine oracle --version 23.26.2 --mode ha-lab --host-port 15210 `
    --remote-host 198.51.100.146 --remote-user dba_user --remote-password '<SSH-PASSWORD>' `
    --password-text '<DB-PASSWORD>' --key-base64 "<KEY>"

# ... or single instance, same idea:
python -m db_ops.sre.cli create-db-docker `
    --name ora_lab_01 --engine oracle --version 23.26.2 --mode single `
    --remote-host 198.51.100.146 --remote-user dba_user --remote-password '<SSH-PASSWORD>' `
    --password-text '<DB-PASSWORD>' --key-base64 "<KEY>"

# Key-auth VM (e.g. Oracle Cloud), bare Ubuntu — install docker over SSH then deploy, one command:
python -m db_ops.sre.cli create-db-docker `
    --name pg_cloud --engine postgres --version 18 --mode single --host-port 5433 `
    --remote-host 203.0.113.188 --remote-user ubuntu --remote-key C:\path\to\oracle-cloud.key `
    --install-docker --password-text '<DB-PASSWORD>' --key-base64 "<KEY>"

# Preview everything (compose, .env, connection entry, commands) — changes nothing
python -m db_ops.sre.cli create-db-docker --name pg_lab_01 --engine postgres `
    --version 16 --mode single --host-port 5433 --password-env POSTGRES_PASSWORD --dry-run

# Register a connection only (no container created)
python -m db_ops.sre.cli register-db-connection `
    --name pg_lab_01 --engine postgres --host 198.51.100.129 --port 5433 `
    --database postgres --username postgres --password-env POSTGRES_PASSWORD
```

Options for `create-db-docker`:

| Flag | Description |
|---|---|
| `--name` | Instance name — letters, numbers, `_`, `-` only. |
| `--engine` | `postgres` \| `mysql` \| `mssql` \| `oracle`. |
| `--version` | Image tag, checked against the registry **before** anything is created. postgres: `18`/`17`/`16`. mysql: `8.4`/`8.0`. mssql: `2022-latest`, `2025-latest`, or a full tag such as `2025-CU6-ubuntu-24.04` — **there is no bare-year tag: `2025` does not exist** ([tag list](https://mcr.microsoft.com/v2/mssql/server/tags/list)). oracle: `26`/`26-slim` (26ai), `23`/`23-slim` (23ai) — gvenzl/oracle-free tags, **no `ai` suffix**. |
| `--mode` | `single` \| `ha-lab`. **`ha-lab` is each engine's own replication, not one product** — see below. `oracle` ha-lab is Data Guard, fixed at 1 primary + 1 standby. |
| `--replicas` | Standby count for `ha-lab` (default 2); rejected with `--mode single`. |
| `--host-port` | Host port for the (primary) instance; HA standbys take the next ports. Default: the engine's own port (postgres 5432, mysql 3306, mssql 1433). |
| `--password-env` | Env var / secret ref holding the password. **Never hardcoded**; it lands only in the instance `.env`. Default: `<NAME>_PASSWORD`, so each instance has its own ref. |
| `--password-text` | The password value. Stored under `--password-env` in the encrypted secret store, then used to provision. Visible in the process list while it runs. |
| `--password-text-env` | Name of an env var holding the password instead — how the Telegram command passes it, so nothing sensitive reaches argv. |
| `--worker-host` | DB host/IP recorded in the connection entry and connection hint. |
| `--containers-dir` | Base dir for instance folders (default `/opt/db_ops/containers`). **A backup bind mount must not live under this default** — it is inside the `control deploy` tree; see [Backups and the container must be kept separate](#backups-and-the-container-must-be-kept-separate). |
| `--no-register` | Create the instance but skip the connection registration. |
| `--force` | Clean-recreate an existing instance: `docker compose down -v` (removes its containers **and data volumes**) before rebuilding. Required to change an instance's engine version. |
| `--dry-run` | Print generated compose/`.env`/connection/commands without changing anything. |
| `--key` / `--key-base64` | Passphrase to resolve the password from the secret store when the env var is unset. |
| `--remote-host` / `--remote-user` / `--remote-port` | Provision on that Ubuntu host over SSH instead of locally (CLI on the master, docker on the remote machine). |
| `--remote-password-ref` / `--remote-password` | SSH password: a secret-store ref decrypted with `--key`/`--key-base64` (preferred), or the literal value. |

The password is resolved from the `--password-env` environment variable first, then the encrypted secret store (ref == the env-var name) if a key is available. `--password-text` / `--password-text-env` write it into that store first, so the ref exists on the next run too. Validation rules: safe `--name`; `--engine`/`--mode` whitelists; `--replicas` only with `ha-lab`; host port(s) must be free; the instance folder must not exist unless `--force`.

**What `ha-lab` actually builds** — it is *not* one HA product, it is each engine's own replication:

| Engine | `ha-lab` = | Failover |
|---|---|---|
| `postgres` | Physical **streaming replication**: 1 primary + N standbys, seeded with `pg_basebackup`, replication role provisioned by an initdb hook. | Manual (promote a standby). |
| `mysql` | **Asynchronous** primary/replica (bitnami image, `MYSQL_REPLICATION_MODE`). | Manual. |
| `mssql` | An **Always On availability group** with `CLUSTER_TYPE = NONE`: `MSSQL_ENABLE_HADR=1` on each node, one shared certificate, mirroring endpoints on 5022, `SEEDING_MODE = AUTOMATIC`, and a database (`<name>_db`) added to the group. Built by `setup/setup_ag.sh`, which the provisioner runs once the nodes are healthy — compose cannot express it and the image has no init hook. | **Manual, and there is no listener.** A container lab has no WSFC or Pacemaker, so this is a read-scale AG: `ALTER AVAILABILITY GROUP [ag_<name>] FAILOVER` on the target replica while the primary is alive, `FORCE_FAILOVER_ALLOW_DATA_LOSS` once it is gone. Clients connect to a replica by port. Enough to exercise replication, seeding, AG DMVs and monitoring — **not** automatic HA. |
| `oracle` | **Data Guard, exactly 1 primary + 1 physical standby** (`--replicas` fixed at 1). Both nodes first start as normal databases; `setup/setup_dataguard.sh` (run by the provisioner once both are healthy) enables ARCHIVELOG/FORCE LOGGING + standby redo logs on the primary, copies the SYS password file, rebuilds the standby as a physical standby via `RMAN DUPLICATE ... FOR STANDBY FROM ACTIVE DATABASE` (aux pfile generated from the standby's own spfile — a minimal pfile dies with ORA-00443; aux reached via a static listener SID entry + `DGSB_AUX` alias present on **both** nodes) and starts managed recovery (MRP). **Redo transport: Oracle Free blocks every live mode with ORA-00439 (ASYNC, SYNC and FAL gap fetch — verified on 23.26.2), so redo moves Standard-Edition style**: the `<name>-shipper` sidecar (docker:cli + socket) forces a log switch and copies+registers each new archived log every 2 minutes; MRP applies them. RPO ≈ that interval. The standby stays **MOUNTED** — its container healthcheck reports *unhealthy* from then on, which is expected. After a standby restart, re-start managed recovery by hand (the setup output prints the command). | **Failover only** (no live transport → no clean switchover): on the standby `RECOVER MANAGED STANDBY DATABASE CANCEL;` then `ALTER DATABASE ACTIVATE STANDBY DATABASE;` — data current to the last shipped log. |

SQL Server needs ~2 GB of RAM per node: a 3-node AG wants ~6 GB free on the worker before it will start.

**Data storage.** Each instance's data lives in Docker **named volumes** (`<name>[-<node>]_data`), not host bind mounts — a bind mount inherits the host uid and breaks the DB image's own user. The compose file + `.env` live on the worker under `/opt/db_ops/containers/<name>/`. To run **two separate clusters**, give each a distinct `--name` and non-overlapping ports; reusing a name with `--force` **overwrites** (and wipes) the existing instance rather than creating a second one.

> HA-lab mode is only an HA *simulation* — all containers run on one worker, so a worker failure stops the whole cluster. The generated compose and the command summary both print this warning.

---

## move-db-docker — Move a lab instance to another host, with its data

`create-db-docker` builds an instance from a template. `move-db-docker` relocates one that
already exists — its image, its compose file and `.env`, the contents of its named volumes, and
(with `--commit-container`) the container's own filesystem — onto a second host, starts it there,
waits for health, and repoints the connection entry.

Re-provisioning is not moving. By the time anyone wants to move a lab database it is not
disposable: `ora11g_lab` carries a restored 11g R2 estate, and provisioning it again on the new
host produces an **empty database with the same name**.

```powershell
# What would move, without touching anything
python -m db_ops.sre.cli move-db-docker --name ora11g_lab `
    --from-target ACME-192-0-2-249-HOST `
    --to-target   ACME-192-0-2-11-MSSQL25-1433 `
    --dry-run --key-base64 "<KEY>"

# The move itself: container filesystem included, source stopped once the destination is healthy
python -m db_ops.sre.cli move-db-docker --name ora11g_lab `
    --from-target ACME-192-0-2-249-HOST `
    --to-target   ACME-192-0-2-11-MSSQL25-1433 `
    --commit-container --stop-source --key-base64 "<KEY>"
```

Both hosts are named as db_ops **targets** — a `server_id` or ip from `db_instances.json` — not
as host/user/password: a machine that already runs db_ops containers is already in the inventory
with a credential the secret store resolves, and retyping it is how two spellings of one host end
up in two runbooks. Resolution is `common.host_ops.resolve_host`, the same one `run-cmd` uses.

### A named volume is not proof of where the data is

`gvenzl/oracle-xe:11` mounts `/opt/oracle/oradata` and keeps XE 11.2's datafiles at
`/u01/app/oracle/oradata`. Those are not the same place: `ora11g_lab`'s declared volume was
**empty** and its 15.2 GB database lived in the container's writable layer. A move that ships the
image and the volumes therefore shipped nothing — the destination started the stock image, came
up **healthy**, and answered as an empty database with the right name, the right port and the
right password. Nothing about the result said it was wrong.

So the mover checks instead of trusting: if every declared volume is empty while the container
has written more than 256 MB into its own filesystem, the move is **refused** and names
`--commit-container`, which `docker commit`s the stopped container into an image so the data
travels as an image layer. The moved instance then gets a `docker-compose.override.yml` pinning
that image — an override rather than an edit, so the compose file that travelled stays the one
the operator wrote, and `docker compose up` in that directory still does the right thing.

> **Do not delete `docker-compose.override.yml`** on a committed move. Without it compose starts
> the stock image again — the empty-but-healthy database described above.

### What it does, in order

1. **Reads the source from Docker, not from the compose file** — containers, images, volumes,
   published ports, pinned subnet, compose service names, writable-layer size. The file describes
   intent; the daemon describes what is running. Ports come from `HostConfig.PortBindings`, which
   survives the container being stopped (`NetworkSettings.Ports` does not, and the container is
   stopped when the answer is needed).
2. **Every guard, before a byte is packed** — docker + compose usable on the destination, the
   data-travels check above, the instance dir free (or `--force`), the published ports free, and
   the pinned subnet not overlapping one the destination's Docker already routes. Each of these
   otherwise fails *after* the transfer, and a lab bundle is gigabytes.
3. **Exports into one staging directory on the source** (`/tmp/db_ops_move/<name>`): the image(s)
   as `docker save | gzip -1`, the instance directory, and each volume as a tar. The containers
   are stopped for that with `docker stop -t 180` — Docker's default of 10 s SIGKILLs Oracle
   mid-checkpoint, and a datafile copied in that state restores to a database that opens and is
   wrong — and started again immediately, even if the packing failed.
4. **Relays each artifact host-to-host** with `common.cli relay-file`: streamed through the
   master without touching its disk, one sha256 across the whole trip.
5. **Imports on the destination**, in an order that matters: `docker load`, extract the instance
   dir, `docker compose create` — *this* is what creates the named volumes **with compose's own
   labels**; creating them with `docker volume create` leaves them unlabelled and compose then
   refuses the stack with "volume already exists but was not created by Docker Compose", after
   the data is already inside — restore the volumes, `docker compose up -d`, wait for health.
6. **Only then** repoints `data/docker_db_connections.json` (host, worker_host, compose_path —
   nothing else) and, with `--stop-source`, stops the source containers. Stopped, never removed:
   they and their volumes are the only other copy until a person has looked at the new host. The
   summary prints the `docker start` that undoes it.

### Root comes from Docker, not from sudo

The instance directory is root-owned (`.env` is 0600 — it holds the database password) and a
volume's contents belong to the engine's uid (54321 for the Oracle images), so the SSH user can
read neither. Rather than require sudo on both hosts, every privileged read and write runs in a
throwaway `docker run --rm --user 0 --entrypoint /bin/sh` container using **the instance's own
image** — already present on the source, just loaded on the destination, so nothing is pulled.
No new privilege is granted either: the SSH user is in the `docker` group, which is what lets it
manage the instance at all.

On the destination the extracted instance directory is then **chowned to the SSH user**. That is
not tidiness: compose reads `.env` before every verb, and root-owned 0600 makes every compose
command fail with `open …/.env: permission denied` — including the `compose create` two steps
later. It is the same ownership `create-db-docker --remote-host` produces, so a moved instance
ends up indistinguishable from a provisioned one.

Options for `move-db-docker`:

| Flag | Description |
|---|---|
| `--name` | Instance name — the directory under the containers dir, which is also the compose project name. The destination must use the same name: volumes are `<project>_<volume>`, so a different one restores the data into volumes nothing mounts. |
| `--from-target` / `--to-target` | Source and destination hosts, as `server_id` or ip from `db_instances.json`. |
| `--containers-dir` / `--to-containers-dir` | Instance folders on the source / destination (default `/opt/db_ops/containers`, and the same on both). |
| `--stage-dir` | Where the bundle is written on both hosts (default `/tmp/db_ops_move`). Removed afterwards unless `--keep-stage`. |
| `--commit-container` | Move the container's filesystem too, via `docker commit`. Required whenever the engine writes outside its declared volumes; the mover refuses and names this flag rather than shipping an empty database. |
| `--no-volumes` | Image and compose files only — the destination initialises an **empty** database. Say it deliberately. |
| `--stop-source` | A move rather than a clone: stop the source containers once the destination is healthy. Files and volumes are kept. |
| `--force` | Replace an instance of the same name on the destination: `docker compose down -v` (**destroys its volumes**) and removes the directory first. The teardown runs *before* the port check, so a retry of the same move is not refused on the ports of the stack it is about to remove. |
| `--engine` | Engine, for the health probe. Default: whatever `data/docker_db_connections.json` records for the instance. |
| `--health-timeout` | Seconds to wait for the moved stack. Default: the engine's own ceiling. |
| `--no-register` | Leave the connection entry pointing at the old host. |
| `--dry-run` | Read the source and print the plan (including the writable-layer size and whether it travels). Touches nothing. |
| `--key` / `--key-base64` | Passphrase that decrypts both hosts' SSH credentials. |

If the destination never becomes healthy the command fails with the containers' own last log
lines and **the source is left exactly as it was** — running, and never stopped. Fix the
destination and re-run with `--force`.

---

## Configuration

**File:** `data/sre_config.json`

Override: `--config <path>` or `DB_OPS_SRE_CONFIG` env var.

### `sre.vmware`

| Key | Purpose |
|---|---|
| `vmrun_path` | Absolute path to `vmrun.exe` |
| `vm_root` | Root directory where VM folders are created |
| `template_name` | Default template VM folder name (e.g. `tpl-ubuntu-2404`). Used for shared/mysql/postgresql/sqlserver groups. |
| `template_snapshot_name` | Snapshot to clone from (e.g. `base-os`) |
| `net_interface` | Guest network interface (e.g. `ens33`) |
| `cidr` | Subnet prefix length (e.g. `24`) |
| `gateway` | Default gateway IP |
| `dns_servers` | Array of DNS IPs |

### `sre.credentials`

| Key | Purpose |
|---|---|
| `guest_user` | SSH user on all lab VMs (also used for `vmrun -gu`) |
| `guest_password` | Guest password (used by `vmrun -gp` and `sudo`) |
| `ssh_identity_file` | Path to Windows host private key (e.g. `~/.ssh/id_ed25519`) |

### `sre.inventory`

```json
"inventory": {
  "groups": {
    "shared": [
      {"name": "bastion-01", "ip": "198.51.100.3", "role": "bastion"}
    ],
    "mysql": [
      {"name": "mysql-01", "ip": "198.51.100.11", "role": "mysql"},
      {"name": "mysql-02", "ip": "198.51.100.12", "role": "mysql"},
      {"name": "mysql-03", "ip": "198.51.100.13", "role": "mysql"}
    ]
  }
}
```

The bastion host is the first node in `shared` with `name == "bastion-01"` or `role == "bastion"`.

### `sre.oracle`

| Key | Purpose |
|---|---|
| `rac_template_name` | VM template for oracle_rac clones (e.g. `tpl-oraclelinux-r9u6`). Injected automatically into the `01-clone-vms` step of `setup-sequence oracle-rac`. |
| `dg_template_name` | VM template for oracle_dg clones (e.g. `tpl-oraclelinux-r9u6`). Injected automatically into the `01-clone-vms` step of `setup-sequence oracle-dg`. |
| `grid_installer_path` | Absolute Windows path to the Grid Infrastructure ZIP (oracle_rac only). |
| `db_installer_path` | Absolute Windows path to the Oracle DB Home ZIP. |
| `install_stage` | Target directory on RAC/DG nodes for staged installers (default `/u01/app/stage`). |

Oracle and DataGuard sequences clone from `rac_template_name`/`dg_template_name` rather than the shared `vmware.template_name`. If either key is absent or empty, the shared template is used as fallback.

### `sre.database_defaults.mysql`

```json
"mysql": {
  "cluster_name": "mysql-lab",
  "cluster_admin_user": "clusteradmin",
  "cluster_admin_password": "...",
  "classic_port": 3306
}
```

### `sre.database_defaults.postgresql`

```json
"postgresql": {
  "cluster_name": "postgresql-lab",
  "version": 16,
  "port": 5432,
  "superuser": "postgres",
  "superuser_password": "...",
  "replication_user": "replicator",
  "replication_password": "..."
}
```

Runtime deployment details are currently controlled by `automation/ansible/group_vars/postgresql.yml`. Keep `sre_config.json` and the Ansible group vars aligned if the PostgreSQL major version, port, users, or passwords change.

### `sre.database_defaults.sqlserver`

```json
"sqlserver": {
  "port": 1433,
  "instance_name": "MSSQLSERVER",
  "sa_user": "sa",
  "sa_password": "...",
  "ha_mode": "always_on_ag",
  "ag_name": "AG_SQL2025_LAB",
  "ag_listener_port": 1433
}
```

Runtime deployment details are currently controlled by `automation/ansible/group_vars/sqlserver.yml`. The active Ansible defaults are SQL Server `2025`, Developer edition, endpoint port `5022`, and AG name `AG_SQL2025_LAB`.

---

## Backups and the container must be kept separate

Every lab instance this app provisions is a **Docker container**, so its backups belong on a host
path that has nothing to do with the container. Three separations, none optional:

1. **Not inside the container** — `--force` runs `docker compose down -v` by design, so a backup
   on the container's own filesystem is destroyed by the normal recreate path.
2. **Not under the engine's data volume** — the postgres image declares
   `VOLUME /var/lib/postgresql`, so a backup mounted at `/var/lib/postgresql/backup` lives inside
   the data volume. If that bind mount ever goes missing the path still works, and backups land in
   the database's own volume to be deleted with it.
3. **Not under `/opt/db_ops`** — `--containers-dir` defaults to `/opt/db_ops/containers`, inside
   the tree `control deploy` manages. The deploy re-owns that tree to the SSH user, which takes
   write access away from the database user inside the container (PostgreSQL is uid 999, the
   deploy user is uid 1000).

Rule 3 failed on 2026-07-31 and took out WAL archiving on `pg_ha_01-primary`: the database stayed
healthy and serving, `archive_command` failed on every segment, and nothing reported it except
`failed_count` in `pg_stat_archiver` climbing into the thousands. `control deploy` now skips
`containers/`, and that instance was relocated to `/opt/db_backups/pg_ha_01` → `/opt/pgbackup`.

When provisioning a new instance, give its backup its own host path and pass `--containers-dir`
if the instance directory itself should live outside the deploy root too.

Full rationale, the current layout table, the audit one-liner, and the relocation procedure:
[`docs/08_backup_restore_app.md`](./08_backup_restore_app.md#dockerized-engines-backups-and-the-container-must-be-kept-separate).

## Architecture

```
db_ops.sre.cli
  â”‚
  â”œâ”€â”€ run-powershell <script>
  â”‚     powershell -File <ps1> -DbSrePayloadJsonBase64 <config> [args]
  â”‚     â†’ VMware Workstation via vmrun.exe (no SSH required)
  â”‚     â†’ Output streamed line-by-line to terminal
  â”‚
  â”œâ”€â”€ run-bastion-playbook / run-bastion-script / run-bastion-ansible
  â”‚     SSH Windows â†’ bastion-01 â†’ ansible-playbook / bash / ansible
  â”‚
  â”œâ”€â”€ check-shared-vms
  â”‚     SSH Windows â†’ shared node IP (bastion, mon, log)
  â”‚
  â”œâ”€â”€ check-mysql-cluster / check-postgresql-ha
  â”‚     SSH Windows â†’ bastion-01 (ProxyJump) â†’ primary node â†’ mysqlsh / psql
  â”‚     Windows host never connects directly to MySQL/PostgreSQL nodes
  â”‚
  â””â”€â”€ ssh <host>
        SSH Windows â†’ bastion-01 (ProxyJump) â†’ target host
```

### SSH Access Model

```
Windows host â”€â”€(key)â”€â”€â–º bastion-01 â”€â”€(bastion key)â”€â”€â–º mysql-01/02/03
                                   â”œâ”€â”€(bastion key)â”€â”€â–º pg-01/02/03
                                   â”œâ”€â”€(bastion key)â”€â”€â–º mssql-01/02/03
                                   â”œâ”€â”€(bastion key)â”€â”€â–º rac01/rac02
                                   â””â”€â”€(bastion key)â”€â”€â–º orapri/orastb
```

- **Windows host key** is deployed only to bastion-01 (`08-deploy-host-ssh-key -Group shared`).
- **bastion key** is deployed to all database nodes (`07-bootstrap-bastion-ansible -TargetGroup <group>`).
- For any SSH target that is not bastion, the Python CLI routes via bastion as a hop — bastion runs the inner `ssh` to the final node using its own key.
- Database nodes (MySQL, PostgreSQL, SQL Server, Oracle RAC) never need the Windows host key.
- For Oracle RAC installer staging, files are transferred Windows â†’ bastion via SCP, then bastion â†’ RAC nodes via SCP (not vmrun). This is because Oracle Linux VMs are treated as pre-existing infrastructure.

### Why VMware Tools for Steps 1–6

`vmrun runScriptInGuest` (VMware Tools daemon) is used instead of SSH for early provisioning because:
- VMs have no static IP yet after clone.
- No host key has been deployed yet.

Once Step 5 is complete (host key on bastion), all SSH-based commands work.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Permission denied (publickey,password)` connecting to bastion | Host SSH key not on bastion-01 | Run Step 5: `08-deploy-host-ssh-key -Group shared` |
| `Permission denied` on MySQL/PostgreSQL node | bastion key not deployed to that node | Run Step 6: `07-bootstrap-bastion-ansible -TargetGroup mysql` |
| `VMware Tools did not respond` | open-vm-tools not installed or VM still booting | Open VM console: `sudo apt install -y open-vm-tools` |
| `Script not found` | Wrong `bash_dir` or `powershell_dir` in config | Verify `sre.automation` paths in `sre_config.json` |
| `fix-guest-identity` fails | Wrong password or missing sudo access | Check guest log printed after the command |
| MySQL cluster check: `cluster is not healthy` | Cluster not yet deployed or a node is down | Run Step 7 first; then `check-mysql-cluster` |
| `Missing sudo password` on Ansible tasks | NOPASSWD sudo not configured on database nodes | Re-run `run-bastion-script bootstrap-bastion-ansible -- <group>` |
| SQL Server: `mssql-server` start-limit-hit | `mssql-conf setup` and systemd restart collided | `ssh` into the node and run `sudo systemctl reset-failed mssql-server && sudo systemctl start mssql-server`; the playbook adds `reset-failed` automatically on next run |
| SQL Server: `Login failed for user 'SA'` | Missing `-C` flag (sqlcmd18 requires cert trust) | Already fixed in `group_vars/sqlserver.yml` (`mssql_sqlcmd_path: "/opt/mssql-tools18/bin/sqlcmd -C"`) |
| `tar: Cannot connect to C: resolve failed` | Git Bash's `tar` in PATH, not Windows System32 tar | Already fixed in `07-bootstrap-bastion-ansible.ps1` (uses `$env:SystemRoot\System32\tar.exe`) |
| `bastion-01 not found in inventory` | Missing entry in `sre.inventory.groups.shared` | Add `{"name": "bastion-01", "role": "bastion", "ip": "..."}` |
| `[db_ops.config] source=shared_fallback` | `data/sre_config.json` not found | Create it or set `DB_OPS_SRE_CONFIG` |
| `stage-oracle-installer`: `Grid installer not found` | Path in `oracle.grid_installer_path` is wrong or null | Set the full path in `sre_config.json` (e.g. `D:/softwares/LINUX.X64_2326100_grid_home.zip`) |
| `stage-oracle-installer`: `SCP of Grid installer to bastion failed` | bastion SSH key not deployed or bastion not reachable | Run Step 5 (host SSH key) and Step 7 (bootstrap-bastion) first |
| `stage-oracle-installer`: `chown: invalid group: grid:oinstall` | Oracle users not yet created on RAC nodes | Run `oracle-rac-os-prep.yml` first, then re-run `stage-oracle-installer` |
| `add-oracle-shared-disks`: `VM must be powered off` | RAC VMs still running | Run `stop-vms -Group oracle_rac` first |
| `add-oracle-shared-disks`: `vmware-vdiskmanager.exe not found` | VMware Workstation not installed at default path | Check `sre.vmware.vmrun_path` — vdiskmanager is in the same directory as vmrun |
| Grid install fails: `INS-32025 The chosen installation conflicts` | Grid home already partially installed | Set `oracle_grid_force_reinstall: true` in `group_vars/oracle_rac.yml` and re-run `oracle-rac-grid.yml` |
| ASM disks not visible (`/dev/RAC_*` missing) | udev rules not applied or shared disks not attached | Check `lsblk` on RAC node; re-run `oracle-rac-os-prep.yml`; verify Step 5 shared disk attachment |
| DG: RMAN duplicate fails: `ORA-12543 TNS:destination host unreachable` | tnsnames.ora on orastb can't reach orapri | Verify `/etc/hosts` on both nodes has correct entries; re-run `oracle-dg-os-prep.yml` |
| DG: RMAN duplicate fails: `ORA-01017 invalid username/password` | sys password mismatch between primary and standby | Verify `oracle_sys_password` in `group_vars/oracle_dg.yml`; re-run with `oracle_standby_force_reseed: true` |
| DG: `show configuration` returns `ORA-16532 DG broker config does not exist` | Broker not yet configured | Re-run `oracle-dg-standby.yml` with `oracle_dg_force_reconfigure: true` |
| DG: `stage-oracle-installer -Group oracle_dg` error: `grid_installer_path is not set` | Grid path null triggers validation | This is expected — DG does not need grid. The `-Group oracle_dg` flag skips grid validation automatically (check script version) |

---

## Logging

| File | Content |
|---|---|
| `logs/sre.log` | Structured events: `DATE\|LEVEL\|APP\|HOST\|FUNCTION\|TEXT` |
| `logs/sre_runtime.log` | All stdout/stderr output |
| `logs/errors.log` | ERROR-level events across all db_ops components |
