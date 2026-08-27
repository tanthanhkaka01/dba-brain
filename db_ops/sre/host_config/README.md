# assets/host — configuration that belongs to a db_ops **host**, not to db_ops

Files here are not read by any db_ops process — nothing applies them for you. They are the tracked
source of truth for settings an operator applies to the machine a worker runs on, so the machine's
state can be reviewed in a diff instead of being remembered.

**Applying them is a step in standing up a worker.** A file tracked here and never copied to the
host protects nothing, which is exactly how 2026-08-26 happened. What db_ops *can* do is notice:
`worker-status` reports a host whose Docker allocation was never pinned.

## `docker-daemon.json` -> `/etc/docker/daemon.json`

Keeps Docker's own address allocation off the ranges this estate routes real databases on.

Docker's default address pool is `172.17.0.0/16 … 172.31.0.0/16`, handed out a `/16` at a time,
and `docker0` itself takes `172.17.0.0/16`. The inventory has production hosts inside that space —
`172.17.100.x`, `172.17.187.x`, `172.18.99.x`. When a Docker network claims one of those ranges,
the **host route table** starts sending that traffic into a local bridge, and the database simply
vanishes from the worker while staying perfectly reachable from everywhere else.

That has happened three times, to the same SQL Server:

| Date | What claimed it | Effect |
| --- | --- | --- |
| 2026-08-05 | the db_ops compose network took `172.18.0.0/16` | a production SQL Server whose address sat inside that same /16 answered `No route to host`; fixed by pinning db_ops to `172.30.240.0/24` |
| 2026-08-14 | `ora11g_lab` (created by `create-db-docker`) took `172.18.0.0/16` | the same SQL Server was unreachable for ~2h — every metric on it failed to connect while the instance was healthy |
| 2026-08-26 | `docker0` took `172.18.0.0/16` on a **newly built worker VM** | the same SQL Server again, ~26h and 415 failed metric rows. The worker had moved to a new host on 2026-08-25 and this file was never applied there |

Each incident taught a different lesson, and all three guards below exist because of one of them.

The second says: pinning a single project fixes nothing, because the collision is decided in the
host route table and *any* project can claim the range.

The third says something sharper — **a guard that lives on a host does not travel with the code.**
The generator's pins moved to the new VM inside the image; this file did not, because applying it
is a human step and nobody knew it was owed. So pinning alone was never going to be enough, and
there is now a third guard that does not depend on anyone remembering:

1. **This file** — `bip` moves `docker0` off `172.17.0.0/16`, and `default-address-pools` confines
   every auto-allocated network to `172.31.0.0/16` in `/24` chunks (256 networks).
2. **The generator** — every compose file `db_ops.sre.create-db-docker` writes pins its own subnet
   in `172.30.0.0/16` (`db_ops/sre/docker_db/models.py::lab_network_subnet`), so a lab is safe even
   on a host where this file was never applied.
3. **The check** — `db_ops.control.cli worker-status` reads the host's actual subnets and compares
   them to [`data/network_reservations.json`](../../../data/network_reservations.json)
   (`db_ops.lib.network_policy`). On a host where guard 1 was never applied it reports
   `UNCONFINED` **before** anything breaks, and once something is inside the range it names the
   network, the address and the instance in one line. This file and that declaration are two
   statements of one decision; `tests/test_network_reservations.py` is what stops them drifting.

Apply it (worker host, needs root; **restarts the Docker daemon**, which bounces every container —
the labs and `db_ops_daemon` all carry `restart: unless-stopped` and come back on their own):

```bash
sudo cp docker-daemon.json /etc/docker/daemon.json
sudo systemctl restart docker
ip -o -4 addr show docker0          # expect 172.30.0.1/24
```

`default-address-pools` and `bip` are **not** re-read by `systemctl reload docker` — a full restart
is required. Existing networks keep the subnet they were created with, so a project that was
already on a bad range has to be recreated (`docker compose down && up`) before the change reaches
it; `docker network ls` + `docker network inspect` is how you check.

Reserved inside `172.30.0.0/16`, do not hand these to a lab:

| Subnet | Owner |
| --- | --- |
| `172.30.0.0/24` | `docker0` (this file's `bip`) |
| `172.30.240.0/24` | the db_ops runtime network (`docker-compose.runtime.yml`) |
| `172.30.1.0/24` – `172.30.200.0/24` | derived lab subnets (`lab_network_subnet`) |
