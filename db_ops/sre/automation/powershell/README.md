# PowerShell Automation

This directory stores reusable PowerShell scripts for local VMware automation.

The SRE app, including how these scripts are invoked and what they are for, is documented in
[`docs/10_sre_app.md`](../../../../docs/10_sre_app.md). The VM build runbooks this file used to
link to belonged to the dba-runbooks repository and were not carried over when db_ops became a
standalone tree.

Scripts:

- `00-bootstrap-template-remote-access.ps1`
- `01-clone-vms.ps1`
- `02-set-vm-resources.ps1`
- `03-start-vms.ps1`
- `04-fix-guest-identity.ps1`
- `05-get-vm-info.ps1`
- `06-snapshot-vms.ps1`
- `07-bootstrap-bastion-ansible.ps1`
- `stop-all-running-vms.ps1`
- `list-running-vms.ps1`

Inventory and credentials are defined in [`config.json`](../../../../config.json) at the tool root, but PowerShell scripts do not read that file directly. Run VMware automation through the Python CLI so Python can read config and pass the resolved payload to PowerShell.

`07-bootstrap-bastion-ansible.ps1` now only syncs the repository to `/opt/db-sre/repo` on `bastion-01`. Run `./automation/bash/bootstrap-bastion-ansible.sh <mysql|postgresql>` from inside `bastion-01` for package install and control-node bootstrap.

Examples:

```powershell
python -m db_sre.cli automation run-powershell 00-bootstrap-template-remote-access.ps1 -- -Template all
python -m db_sre.cli automation run-powershell 00-bootstrap-template-remote-access.ps1 -- -Template ubuntu
python -m db_sre.cli automation run-powershell 00-bootstrap-template-remote-access.ps1 -- -Template oraclelinux
python -m db_sre.cli automation run-powershell 00-bootstrap-template-remote-access.ps1 -- -Template windows
python -m db_sre.cli vmware start-vms -- -Group mysql
python -m db_sre.cli vmware start-vms -- -Group postgresql
python -m db_sre.cli vmware start-vms -- -VmName mysql-01
python -m db_sre.cli vmware start-vms -- -VmName mysql-01,mysql-03
python -m db_sre.cli automation run-powershell 07-bootstrap-bastion-ansible.ps1
```
