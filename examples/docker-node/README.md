# A node in Docker: install the latest, upgrade it, roll it back

A walkthrough, with the two files it needs beside it: [`docker-compose.yml`](./docker-compose.yml)
and [`.env.example`](./.env.example). Every command below was run in this order on 2026-09-28, on a
lab Ubuntu 24.04 VM with Docker Engine 29.8.1 and Compose v5.5.1:
- the install, on the published `latest` (then 0.23.0), and again on 0.24.0;
- the upgrade from 0.23.0 to 0.24.0;
- a rollback to 0.23.0, and the upgrade again.

Each step says **what it proves**. The upgrade is the procedure a production worker has been
upgraded by since 0.19.0, run here on a host of its own.

Placeholders: the tool root is `/opt/dbabrain`, the host is `192.0.2.10` (RFC 5737), the clock is
`Europe/Berlin`. Substitute your own.

**Why the image and not `pip`.** The image carries the system layer a `pip install` cannot:
- Microsoft's ODBC drivers 17 and 18, and `sqlcmd`;
- PowerShell, for Windows targets;
- `openssh-client`, `rsync` and `smbclient`;
- the Docker client;
- the OpenSSL setting that lets SQL Server 2008 R2 connect at all.

On a host that can run the image, this path has fewer ways to go wrong. The pip path is
[`standing-up-a-node.md`](../standing-up-a-node.md). Everything after the install is the same
toolkit, so that guide is also where configuring the node continues (step 7).

---

## 0. What you need

| | |
| --- | --- |
| A Linux host | Docker Engine and the Compose plugin: `docker compose version` answers |
| Disk | about 1.5 GB per image. You keep two while you upgrade: the new one, and the one you roll back to |
| A passphrase | you choose it once. It encrypts the secret store, and **nothing else can decrypt that store** |
| Reach | from this host to your databases, and to `ghcr.io` for the image |
| Port 8080 | the console (`/db_ops/`) and the report pages (`/report_dba/`) |

## 1. Pull the image and look at it

```bash
IMAGE=ghcr.io/tanthanhkaka01/dbabrain
docker pull $IMAGE:latest
docker run --rm $IMAGE:latest db-ops --version
docker run --rm --entrypoint id $IMAGE:latest
docker run --rm $IMAGE:latest --help
```

The image is public, so there is no login. `latest` follows every release and never a release
candidate. `0.24.0` (a version) or `0.24` (a minor) pins it instead. What PyPI calls the newest
is the same number:

```bash
curl -s https://pypi.org/pypi/dbabrain/json | python3 -c "import json,sys;print(json.load(sys.stdin)['info']['version'])"
```

**Proves:** the version you are about to run, and `uid=10001(dbabrain)`. The image does not run as
root, and the next two steps follow from that.

## 2. The tool root: a folder, `init`, the clock

```bash
sudo mkdir -p /opt/dbabrain && sudo chown "$USER": /opt/dbabrain
cd /opt/dbabrain
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" -w /work $IMAGE:latest db-ops init
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" -w /work $IMAGE:latest \
  db-ops db --config config.json timezone --set Europe/Berlin
```

`init` writes the tool root into the folder you mounted:
- `config.json`;
- `data/*.json`: the inventory, the metric catalogue, the schedule, the Telegram settings;
- `assets/`, `secrets/`, `logs/`, `runtime/`, and `AGENTS.md`.

It never overwrites a file that is already there. Running it **as your own user** (`--user`) makes
every file yours, which matters for `config.json`: the service mounts it read-only, so you edit it
on this host.

**Set the clock now**, while the whole folder is mounted writable. Every `time_window.from_hour`
is a *local* hour read against it, and `init` ships `UTC`. To change it later, see
[What bites people](#what-bites-people).

**Proves:** `ls` shows the tree, and `grep timezone config.json` shows the zone you chose.

## 3. Give the container the folders it writes

```bash
sudo chown -R 10001:10001 data assets logs runtime
ls -ln
```

No `sudo` on this host? Membership of the `docker` group can do the same through the image. This
form is the one the rehearsal ran:

```bash
docker run --rm --user 0:0 --entrypoint chown -v "$PWD:/work" $IMAGE:latest \
  -R 10001:10001 /work/data /work/assets /work/logs /work/runtime
```

`data/` is written by the console and the bot, `assets/` by registering a SQL task, and `logs/` and
`runtime/` by everything. `config.json`, `secrets/` and `AGENTS.md` stay yours.

**Do this after step 2, not before it.** Once `logs/` and `runtime/` belong to uid 10001, the
image's entrypoint refuses to run as your user in this folder. It names the uid and the fix,
which is how you know this step happened.

**Proves:** `ls -ln` shows `10001` on the four folders and your uid on the rest.

## 4. The compose file

```bash
curl -fsSLO https://raw.githubusercontent.com/tanthanhkaka01/dba-brain/main/examples/docker-node/docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/tanthanhkaka01/dba-brain/main/examples/docker-node/.env.example -o .env
docker compose config -q && echo "compose file: valid"
```

Or copy [`docker-compose.yml`](./docker-compose.yml) and [`.env.example`](./.env.example) from a
checkout; they are the files the rehearsal ran. Each line in them has a reason, written beside it.
The ones that cost something when they were missing:

| Line | Why |
| --- | --- |
| `image: ...:${DBABRAIN_TAG:-latest}` | The version lives in `.env`. Upgrading and rolling back never edit the compose file |
| `hostname: dbabrain` | The store knows the node, and every run it has open, by its host name. Left to Docker, the name is the container id, and it changes on every recreate. The next container then finds its predecessor's open runs belonging to *another host* and waits out their timeouts. Measured in the rehearsal: the two containers started before this line each added a row to the node list; the pinned one kept a single row through three recreates |
| `init: true` | Something that reaps as PID 1. Without it, the commands the bot starts in the background become zombies, and the bot reports a finished command as still running |
| `DB_OPS_NODE_ROLE=worker` | A daemon not told its role is `master`, which schedules nothing and looks healthy and idle |
| `- DB_OPS_SECRET_KEY` | The passphrase is passed through from the shell that runs `docker compose up`. It is never a value in this file or in `.env` |
| `config.json:ro` | The configuration is yours and read-only to the service. `data/` is where it writes |
| `networks: ... subnet` | A network on a /24 you chose. Docker's default pool once handed a stack a /16 holding a real database server, which the host then could not reach (*No route to host*). Pick a /24 none of your monitored machines is in |
| the `docker.sock` lines | Commented out. Only `sre create-db-docker` needs them, and the socket is root on this host |

## 5. Run everything once, before configuring anything

```bash
read -rsp 'passphrase: ' DB_OPS_SECRET_KEY; echo; export DB_OPS_SECRET_KEY
docker compose run --rm dbabrain daemon --once
cat logs/errors.log
```

One pass over the whole shipped schedule, in a throwaway container with the service's own mounts.
Every command ships active, and a node with no inventory, no secret and no token is exactly
what you have now. Every scheduled app must come back clean in this state.

**Proves:** seven apps `status=done`, and `APP-WEBHOST` `skip_service`: the web host is a service,
and `--once` waits for what it starts. `logs/errors.log` holds only its header.

## 6. Start it

```bash
docker compose up -d
docker compose exec dbabrain db-ops db --config config.json sync-config '{}'
```

`sync-config` gives the store its copy of `data/`, which is what the console shows.

After two or three minutes:

```bash
docker compose ps
echo '{}' | docker compose exec -T dbabrain db-ops common self-status -
docker compose exec dbabrain db-ops db --config config.json ops-status '{}'
curl -s -o /dev/null -w 'console %{http_code}\n' http://127.0.0.1:8080/db_ops/login
curl -s -o /dev/null -w 'reports %{http_code}\n' http://127.0.0.1:8080/report_dba/
cat logs/errors.log
docker compose exec -T dbabrain sh -c 'echo "passphrase length: ${#DB_OPS_SECRET_KEY}"'
```

**Proves:**
- `self-status` answers `0.24.0 on dbabrain`, with `runtime` `docker`, `node_role` `worker` and
  your zone.
- On a bare node, `ops-status` says *No Telegram groups are configured, so there is nowhere to
  report to*. That is a state, not a failure.
- The console and the reports answer `200`, and `errors.log` holds only its header.
- The passphrase length is not `0`.

**The passphrase is set when the container is created.** A restart or a reboot keeps it
(`restart: unless-stopped`). Any `docker compose up` that *recreates* the container reads it from
your shell again, so export it first. Without it the new container has none, and the first thing
that needs a secret says *No decryption key provided*. It shows in `docker inspect` to anyone
with Docker access, who can `exec` into the container anyway: Docker access on a host is root
there.

## 7. Configure it

From here the node is configured as in [`standing-up-a-node.md`](../standing-up-a-node.md), from
its step 5. Each `db-ops ...` there runs here as `docker compose exec dbabrain db-ops ...`. Three
things differ in a container.

**A secret goes in on stdin, and `-T` passes stdin through.** `read -s` keeps it off the screen;
`printf` is a shell builtin, so it never reaches a command line or the history:

```bash
read -rsp 'bot token: ' T; echo
printf '{"ref": "TELEGRAM_BOT_TOKEN", "value": "%s"}' "$T" | docker compose exec -T dbabrain db-ops common secret-set -
unset T
```

**The address the pages publish has to be given.** Inside a container, the address the node can
see is on Docker's private network, so `use-base-url --this-node` refuses there and names the
reason. Give this host's own:

```bash
docker compose exec dbabrain db-ops reports --config config.json use-base-url http://192.0.2.10:8080/report_dba/
```

**Edit `data/*.json` through the toolkit**, with its registrars, the console or the bot: `data/`
belongs to uid 10001 now. For a hand edit, use `sudo`, then `sync-config` as in step 6.

## 8. Upgrade

### Before: what runs, what is new, what is running right now

```bash
cd /opt/dbabrain
IMAGE=ghcr.io/tanthanhkaka01/dbabrain
OLD=$(docker compose exec -T dbabrain db-ops --version); echo "$OLD"
curl -s https://pypi.org/pypi/dbabrain/json | python3 -c "import json,sys;print(json.load(sys.stdin)['info']['version'])"
tail -5 logs/backup.log
```

Read the new version's *Upgrading* section before anything else. It is
`docs/releases/v<version>.md` in the repository, and it says whether the store's schema moves and
what to run.

Stopping the container kills whatever it is running. The next start records a cut run as
`stale_running_recovered`, so pick a minute with no backup or restore in progress.

### Back up, keep the running image, pull the new one

```bash
V=0.24.0                                   # the version you are installing
mkdir -p ~/dbabrain_backups
tar czf ~/dbabrain_backups/before_$V.tgz config.json data assets docker-compose.yml .env
docker tag $IMAGE:latest $IMAGE:$OLD       # BEFORE the pull, or the old image loses its name
docker compose pull
docker run --rm $IMAGE:latest db-ops --version
```

On a pinned tag, put the new version in `.env` (`DBABRAIN_TAG=0.24.0`) before `docker compose
pull`. The old tag is already its own name. The rehearsal took this road, because its 0.24.0 was
built on the lab host before the release. The compose mechanics are the same.

**Proves:** `docker image ls $IMAGE` shows `:$OLD`, the one you roll back to, beside the new
image, and the new one prints `$V`.

### Stop, and move `data/` with the new version

```bash
docker compose stop
docker compose run --rm --no-deps -T dbabrain db-ops common upgrade-config '{}'
docker compose run --rm --no-deps -T dbabrain db-ops common upgrade-config '{"dry_run": false}'
docker compose run --rm --no-deps -T dbabrain db-ops db --config config.json init
docker compose run --rm --no-deps -T dbabrain db-ops db --config config.json sync-config '{}'
```

`docker compose run` starts a throwaway container of the **new** image, with the service's own
mounts and no ports. The image's entrypoint is the daemon, so this is how a one-off command runs
while the service is stopped.

What each command does:
- **`upgrade-config`** moves your files to the field names and shapes the new version writes. It
  shows the plan first. Every file it changes is copied first to
  `runtime/config_upgrade/<UTC stamp>/`, and a second run changes nothing.
- **`init`** is idempotent, and applies a schema change if there is one.
- **`sync-config`** brings the store's copy of `data/` level with the files. Otherwise the next
  console save writes the old shape back.

**Why stopped:** some moves replace an app command, and a daemon still running the old one beside
the new would run the same work twice.

**Proves** (0.23.0 to 0.24.0 on a bare root):
- the plan reads `would change 2 record(s)`, with `conflicts` **0**;
- the apply reads `changed 2 record(s); backup in .../runtime/config_upgrade/20260928T013945Z.
  check-objects: 0 violation(s), 0 deprecated; check-references: 0 dangling`;
- a second plan reads `would change 0`;
- `init` reports every schema `ready`.

A conflict leaves that file unwritten and exits 1. A dangling reference is fixed through the
registrar that owns the record (`backup-add` / `restore-add` with `"replace": true`), never by hand.

### Start, and check it

```bash
docker compose up -d
docker compose exec -T dbabrain db-ops --version
docker inspect dbabrain --format '{{.Config.Image}} restarts={{.RestartCount}}'
```

After two or three minutes, run the checks of step 6. **Proves:** the new version, `restarts=0`,
and everything step 6 proved: the same node id (`dbabrain`), your zone, `200`, and an
`errors.log` with nothing new.

## 9. Roll back

```bash
docker compose stop
ls runtime/config_upgrade/                  # the newest stamp is this upgrade's
STAMP=20260928T013945Z                       # yours
docker compose run --rm --no-deps -T --entrypoint sh dbabrain -c "cp runtime/config_upgrade/$STAMP/*.json data/"
sed -i "s/^DBABRAIN_TAG=.*/DBABRAIN_TAG=$OLD/" .env
docker compose run --rm --no-deps -T dbabrain db-ops db --config config.json sync-config '{}'
docker compose up -d
docker compose exec -T dbabrain db-ops --version
```

**Both halves, or neither.** The old image on moved files runs something other than what you had:
an older reader does not know a newer field. Every reader takes the old names, so the new image on
old files is safe; the other way round is not. The copy runs in a container because `data/`
belongs to uid 10001.

**Not free after a schema change.** If the release notes say `SCHEMA_VERSION` moved, the store was
migrated on the new version's first start, and an older build may refuse what it finds. Prefer
rolling forward. 0.23.0 and 0.24.0 share schema 5, which is why this rehearsal could go back and
forth.

Put `DBABRAIN_TAG=latest` back in `.env` before the next upgrade that follows `latest`.

**Proves:** `$OLD` printed, the console `200` again.

---

## What bites people

- **The host name.** Without `hostname:` in the compose file, every recreate is a new node to the
  store, and the runs its predecessor left open wait out their timeouts instead of being closed at
  start.
- **The passphrase not exported** when `docker compose up` recreates the container. The new
  container has none. Check with the `passphrase length` line in step 6.
- **`docker compose pull` moves `latest`.** Tag the running image with its version first, or the
  image you would roll back to is an untagged layer nobody can name.
- **Changing the clock after step 3.** `config.json` is yours, so edit it on this host, then
  `docker compose restart`. `self-status` names the zone the node took, and a name it does not
  know shows as another zone:

  ```bash
  sed -i 's#"timezone": "[^"]*"#"timezone": "UTC"#' config.json
  docker compose restart
  echo '{}' | docker compose exec -T dbabrain db-ops common self-status - | grep -E '"timezone"|"utc_offset"'
  ```

  The step-2 form no longer works here: the entrypoint refuses your uid once `logs/` is 10001's.
- **`-T`** on `docker compose exec` and `run` whenever stdin is piped, or the request never
  arrives.
- **`DB_OPS_NODE_ROLE`**, **`init: true`** and the **network** are in the compose file for the
  reasons beside them. A file of your own that drops one gets back the failure that line was
  written against.
