# 11. Control App & Docker Deployment

The **control** app (`db_ops/control/`) holds the master-side operations that drive a
master-worker deployment: build and deploy the worker image, bump the version, and orchestrate the
worker. It follows the same layout as every other app — a self-contained package with a CLI
(`python -m db_ops.control.cli`).

> **You do not need this app to run the toolkit.** A single-machine install runs the scheduler
> locally and never builds an image. Control exists for the deployment where configuration is
> edited on one machine — the **master** — and the scheduled apps run on another, close to the
> databases — the **worker**. Which role a process plays is declared per node by
> `DB_OPS_NODE_ROLE`, so a configuration file copied between machines can never mislabel one.

> ### ⚠ Scope decision — control is a *control plane*, not a report producer
>
> **Target architecture:** `db_ops.control` is **only** the cluster control/CI plane —
> things that *operate* the master and worker nodes:
>
> - **deploy** (build image → copy bundle → start/restart the worker daemon),
> - **build / CI-CD** (`build-image`, version bump, image tagging),
> - **test / smoke-check** of a node, and other master⇄worker orchestration.
>
> **Report generation does NOT belong here.** Producing the inventory health overlay, the
> inventory summary, or the styled HTML/Markdown inventory report is the **reports app's**
> job (`db_ops.reports.cli`), which already builds them locally on the worker from the
> metrics already collected into the runtime store — see [06_reports_app.md](06_reports_app.md). The worker-side,
> Store-local `inventory-workflow` (with `--beauty 1` for the HTML + Markdown report) is the
> supported path.
>
> **Current state (not yet refactored):** the control app *still* carries
> `inventory-health` / `inventory-summary` / `inventory-workflow` (the master→worker SSH
> orchestration documented in the table below). These are **legacy / to be removed** — kept
> working for now, to be cleared once the reports-app workflow fully replaces them. **Do not
> add new report/rendering features to the control app**; add them to the reports app.

Previously the deploy/version scripts were standalone, under a scripts tree that no longer exists; they
were rewritten into this app so the master operations live in the same package as the rest of
db_ops. Like the other apps it is decoupled: it invokes the reports app's
`build-inventory-health` command over SSH rather than importing it.

The control app is the **high-level interface**; the Docker deployment runbook in
[Part B](#part-b--docker-deployment-manual-mechanism) below documents the underlying
mechanism that `build-image` / `copy` / `start-daemon` / `deploy` automate. Use the control
app for day-to-day deploys; reach for the manual runbook when debugging or running a step by
hand.

---

## Part A — Control App (master-side operations)

### Cluster awareness

The worker host is read from `config.json`'s `worker` array, so most commands need only
`--user` (and a password prompt). Override with `--host`. The daemon runs only the
`app_commands.json` entries whose `node_role` matches its node — see
[03_app_command_daemon.md](03_app_command_daemon.md).

**`node_role` values in `app_commands.json`:** `master`, `worker`, or `all`.

- A **master** node runs entries with `node_role` = `master` or `all`.
- A **worker** node runs entries with `node_role` = `worker` or `all`.
- `all` (legacy aliases `both`/`any`/empty) runs on every node.

**How a node knows its own role — `DB_OPS_NODE_ROLE` only.** Each node *declares* its role at
run time via the `DB_OPS_NODE_ROLE` env, the same mechanism on both sides:

```bash
# worker (the deploy/start-daemon default)
docker compose run -e DB_OPS_NODE_ROLE=worker -d --name db_ops_daemon db_ops daemon <key>
# master, run the identical way with the role flipped
docker compose run -e DB_OPS_NODE_ROLE=master -d --name db_ops_daemon db_ops daemon <key>
```

There is **no host autodetection** and **no role baked into `config.json`** — a config copied
between nodes can never mislabel one. With the env unset the role defaults to `master`, which
is why running the daemon natively on the PC needs no env. The `master`/`worker` arrays in
`config.json` are cluster-definition metadata (e.g. where the worker host lives for the
control app) — they do **not** decide this node's role. Each daemon tick logs the resolved
`node_role` and the count of active commands it will run.

### Commands

| Command | What it does |
| --- | --- |
| `bump-version [--part patch\|minor\|major] [--set X.Y.Z] [--dry-run]` | Bump `db_ops/__init__.py` `__version__`. |
| `build-image [--platform] [--no-cache] [--skip-build]` | Build `db_ops:<version>` + `db_ops:latest` and assemble the deploy bundle locally. |
| `copy [--host --user --password --remote-dir]` | SFTP the bundle to the worker (overwrites config/data/assets/image; keeps `logs/` and `runtime/db_ops.sqlite`). The bundle also carries the canonical `architecture/database-inventory.json` (repo copy, where static blocks like `sqlserver_resources`/`deployment` are edited) into the worker's `runtime/reports/database-inventory.json` — the worker-side canonical the reports app merges health into. Health blocks are rebuilt from the runtime store on the next `inventory-workflow` run, so the overwrite loses nothing durable. After the upload it also **moves aside any top-level directory under `data/` or `assets/` that the bundle no longer carries** — see [Directories the bundle owns](#directories-the-bundle-owns). |
| `start-daemon [... --key-base64/--key --container --node-role]` | `docker load`, replace the container, start the daemon with `DB_OPS_NODE_ROLE` (default `worker`), set `restart=unless-stopped`, verify the version, then **prune the images this deploy superseded** (see below). |
| `deploy [... all of the above ...] [--merge]` | `build-image` → `copy` → `start-daemon` in one shot. **Master → worker**: the master's `data/` and `assets/` overwrite the worker's, so anything registered through the bot since the last deploy is deleted. `--merge` prepends **merge worker secrets** → **merge worker config** → pull `*.sql`, which pulls what the bot created on the worker (SQL tasks/targets, Telegram groups/users, new secret refs) into the master first; a secret ref that differs on both sides then aborts the deploy before anything is built. See [Syncing worker-side config](#syncing-worker-side-config-back-to-the-master). |
| `worker-status [--host --user --key... --container --json --no-metrics]` | Read-only health check: is the daemon container up?, which db_ops version it runs, and — via the in-container `python -m db_ops.jobs.status` — every app command on that node (active?, last run time/status, due now?, last error) plus metric freshness per target. If the deployed image predates the status module the command **says so and exits** — it does not fall back to an inline query. The old fallback hard-coded a SQLite path, so against a PostgreSQL node it reported "no data" for a healthy worker; see the note at `db_ops/control/worker_status.py:17`. |
| `worker-run [--host --user --key... --container] -- <command...>` | Run an **arbitrary command inside the worker container** from the master. The command after `--` is passed through verbatim (each token shell-quoted), so any `python -m db_ops.<app>.cli ...` can be triggered on the worker without hard-coding. Exit code + stdout/stderr are returned. |
| `worker-create-db-docker [--host --user --key... --container] --name --engine --version --mode --replicas --host-port --password-env [--worker-host --containers-dir --no-register --force --dry-run --pull-config]` | Convenience wrapper: runs `sre.cli create-db-docker` **inside the worker container** via `worker-run` (provisions a lab DB container, see `docs/10_sre_app.md`), then, with `--pull-config`, pulls the updated `data/` config back to the master. The `--key`/`--key-base64` is forwarded to the in-container command so it can resolve the DB password from the secret store. |
| `worker-pull-data-config [--host --user --key... --from-worker-path --to-master-path --files --all-json --include-secrets --merge-secrets --plaintext-secret-path --overwrite --dry-run]` | Copy updated `data/` config files from the worker back to the master over SFTP (the worker's `data/` is bind-mounted on the host at `<remote-dir>/data`). Defaults to just `docker_db_connections.json`; `--all-json` widens to every `*.json` (still excluding the encrypted secret store unless `--include-secrets`). Existing master files are skipped unless `--overwrite`; `--dry-run` prints the plan. |

**The secret store is not an ordinary file.** `--include-secrets` copies it like any other, which is last-writer-wins: a ref the master added *after* the last deploy exists only on the master, and the worker's file would silently delete it. Use **`--merge-secrets`** whenever the worker created a secret (the Telegram `spbot_create_db_docker` command does): it decrypts and unions the worker encrypted store, the master encrypted store, and the master plaintext source (`secrets/secret_text.json`). Both master stores are synchronized to that union. If one ref holds different values in any participating store, it reports a conflict and writes nothing rather than guessing which password is current. It needs `--key`/`--key-base64`, implies `--include-secrets`, and accepts `--plaintext-secret-path` when the plaintext source is not at its repository default. The plaintext file remains local and gitignored; it is never copied to the worker.

```powershell
# after the bot created a lab database on the worker
python -m db_ops.control.cli worker-pull-data-config `
    --key-base64 "<base64-passphrase>" --all-json --merge-secrets --overwrite
```
| `inventory-health [--host --user --password --days --date --container ...]` | **(legacy — moving to reports app)** Trigger the reports app's `build-inventory-health` inside the worker container, copy the dated overlay into `runtime/reports/`, and merge its health blocks into `architecture/database-inventory.json` (servers without metrics — e.g. lab VMs — are left untouched). |
| `inventory-summary [--inventory --output-dir --date]` | **(legacy — moving to reports app)** Render `<YYYYMMDD_HHMMSS>_database-inventory-summary.md` into `runtime/reports/` from the canonical inventory JSON (full inventory + merged health; lab VMs and credential fields excluded). |
| `inventory-workflow [--host --user --password --days --date ...]` | **(legacy — moving to reports app)** Run `inventory-health` then `inventory-summary` in one shot, sharing one `YYYYMMDD_HHMMSS` stamp. `--user` defaults to the worker's `user` in `config.json`. Superseded by the worker-side `db_ops.reports.cli inventory-workflow [--beauty 1]` (store-local, no SSH). |

### `start-daemon` prunes what it replaced

Every deploy ends in `docker load`, which adds `db_ops:<version>` and repoints `db_ops:latest`.
Nothing removed the version it superseded, so the pile grew once per deploy. Measured on the worker
on 2026-08-18: **383 `db_ops` tags and 526 dangling images**, on a host whose root volume had
already been extended once from 293 GB to 589 GB. Deploy frequency is what made this a *daily*
cost rather than a one-off — 2.85.32 through 2.85.49 is seventeen versions in two days — because
each build changes the layer holding the project source, so every version carries its own delta
even where the base layers are shared.

`_prune_old_images` now runs as step 5, after the daemon is up and verified:

- `db_ops:latest` is **never** removed — it is what the running container was created from.
- The newest `KEEP_IMAGE_VERSIONS` (5) version tags stay, so rolling back a bad release is a
  `docker run` away and needs no rebuild. Ordering is `sort -V`, not `sort`: a lexical sort puts
  `2.85.10` before `2.85.9` and would keep an arbitrary set rather than the newest.
- Removing a *tag* is not removing an image. `docker image prune -f` afterwards collects only the
  layers no tag references at all.
- Best-effort (`check=False`). A deploy that worked must not be reported as failed because the
  tidying afterwards did not.

### Authentication — only `--key-base64`, no `--password`

`--user` defaults to `config.json` `worker[0].user`, and the SSH **password is resolved from
the secret store**: `worker[0].password_ref` (e.g. `REMOTE_192_0_2_249_TUSER`) is decrypted
from `data/encrypted_secret_text.json` with the `--key-base64`/`--key` you already pass. So an
SSH command needs neither `--user` nor `--password` — just the key. Resolution order:
explicit `--password` > secret store (`password_ref` + key) > interactive prompt. (Key/agent
SSH auth is not configured for the worker, so the password path is what authenticates.)

### Typical flows

```powershell
# Deploy a new build to the worker (host/user/password all from config + secret store)
python -m db_ops.control.cli bump-version --part patch
python -m db_ops.control.cli deploy --key-base64 "<base64-passphrase>"

# check worker status
python -m db_ops.control.cli worker-status --key-base64 "<base64-passphrase>"

# run any command inside the worker container from the master (command after `--`, verbatim)
python -m db_ops.control.cli worker-run `
    --key-base64 "<base64-passphrase>" `
    -- `
    python -m db_ops.backup_restore.cli restore-workflow `
    --config config.json `
    --restore-id ACME_TO_MSSQL2025_DOCKER `
    --point-in-time "2026-06-08 12:00:00 +07:00" `
    --key-base64 "<base64-passphrase>"

# provision a lab DB container on the worker, then pull the updated config back
python -m db_ops.control.cli worker-create-db-docker `
    --key-base64 "<base64-passphrase>" `
    --name pg_lab_01 --engine postgres --version 16 --mode single `
    --host-port 5433 --password-env POSTGRES_PASSWORD --pull-config

# or do it in two explicit steps (equivalent):
python -m db_ops.control.cli worker-run `
    --key-base64 "<base64-passphrase>" `
    -- `
    python -m db_ops.sre.cli create-db-docker `
    --name pg_lab_01 --engine postgres --version 16 --mode single `
    --host-port 5433 --password-env POSTGRES_PASSWORD
python -m db_ops.control.cli worker-pull-data-config `
    --key-base64 "<base64-passphrase>" --overwrite
```

> In-container provisioning drives the host Docker daemon: the runtime compose mounts `/var/run/docker.sock` and passes `/opt/db_ops/containers` through at the same absolute path, and the worker image needs a Docker client + compose plugin (`apt: docker.io docker-compose-v2`). Preview safely first with `--dry-run`.

### Notes

- Secrets travel **encrypted** in the bundle (`data/encrypted_secret_text.json`); the
  passphrase is passed to the daemon as `--key_base64` and never bundled. The same key also
  decrypts the worker SSH password for the control commands above.
- `inventory-health` never copies the live store — it runs the extraction query
  inside the container and transfers only the small dated overlay.
- The host key is auto-accepted (`AutoAddPolicy`); intended for trusted hosts.

---

## The config-drift gate

A deploy copies the master's `data/` over the worker's. That was safe while files were the only
way to change config. It stopped being safe when the **web console** could edit a record: the
console writes to the runtime **store**, which master and worker share, and to the *worker's*
files. The master's copy is untouched — so the next deploy from that master ships the old values
straight back over the change, with a success message.

Not hypothetical. On 2026-08-21 an operator set
`APP-REPORTS-INVENTORY-WORKFLOW.repeat_interval` to 3600 in the console; the master's
`app_commands.json` still said 7200, and a deploy at that moment would have reverted it.

So `deploy` and `build-image` check first, before anything is staged, and stop if the store and
`data/` disagree about what an app would read:

```
------------------------------------------------------------------------------
  CONFIG DRIFT - the runtime store and this master's data/ disagree
------------------------------------------------------------------------------
    app_commands.json
        content differs: app_commands[APP-REPORTS-INVENTORY-WORKFLOW] changed

  adopt : rebuild this master's data/ from the store, then deploy that
  keep  : deploy this master's data/ as it is, and re-sync the store from it
  abort : change nothing and stop
```

| Answer | What it does | When it is right |
| --- | --- | --- |
| `adopt` | Rebuilds `data/` from the store (`export-config`), then deploys that. | Someone changed config in the console and it should stick. |
| `keep` | Deploys `data/` as it is, **and re-syncs the store from it**. | The master's file is the intended one; the console's value was a mistake. |
| `abort` | Nothing changes. | You want to look first. |

`keep` deliberately writes the store too. Leaving it holding the other value would bring the same
prompt back on the next deploy, and the third time nobody reads it. Nothing is lost either way —
the replaced value stays in `config_item_revisions`.

**Formatting-only drift never stops a deploy.** A hand-formatted file that has since been
normalised differs byte for byte and not in meaning; halting for that trains people to answer the
prompt without reading it.

Unattended runs declare the answer instead of being asked:

```powershell
python -m db_ops.control.cli deploy --key-base64 <K> --on-config-drift adopt
python -m db_ops.control.cli deploy --key-base64 <K> --on-config-drift keep
```

With **no terminal and no `--on-config-drift`** the deploy aborts with exit code 3. Guessing which
side is right is the one thing this gate must not do — both alternatives destroy somebody's change.

### This is not what `--merge` does

`--merge` unions worker-added *records* into the master and **the master wins on a shared key**
(see below). A console edit is a change to a record the master already has, so `--merge` keeps the
master's old value and the deploy then overwrites the worker's file with it. `--merge` rescues
things the worker **added**; the drift gate is what rescues things it **changed**.


## Part B — Docker Deployment (manual mechanism)

End-to-end runbook: encrypt secrets, build the Docker image on your Windows
machine, ship it to an Ubuntu server, start the daemon with your key, and check
logs/errors. This is what the control app's `deploy` automates; follow it top to
bottom when running steps by hand.

### B0. Mental model — what "build" produces, and where things live

**The image contains only the engine:**

```
db_ops:<version>  (Docker image; also tagged db_ops:latest)
├── Ubuntu 24.04 base
├── msodbcsql17 + msodbcsql18 + unixODBC + mssql-tools18  (SQL Server drivers; 17 for legacy SQL 2008 R2)
├── PowerShell 7 (pwsh), openssh-client, rsync, smbclient
├── Python venv + requirements.txt           (incl. cryptography)
└── db_ops/ package source                    ← your application code
```

The version comes from `db_ops/__init__.py` (`__version__`); the build tags the
image `db_ops:<version>` and `db_ops:latest`. Bump `__version__` on every change.

**The image does NOT contain your config or secrets.** Those are *bind-mounted*
at run time:

```
config.json, data/*.json, assets/   ← stay on the host, mounted into the container
```

Secrets (DB passwords, Telegram bot token) live **encrypted** inside
`data/encrypted_secret_text.json`, which rides along in the mounted `data/`
folder. The decryption **passphrase is never stored** in the image, the compose
file, or any env file — you pass it on the command line with `--key` each time
you start the daemon.

So a deployment is three things:

| Artifact | What it is | Rebuild to change? |
| --- | --- | --- |
| `db_ops_image.tar` | the engine (code + drivers + runtime) | yes — rebuild on code/dependency change |
| deploy bundle (`config.json`, `data/`, `assets/`) | your environment, incl. the **encrypted** secret file | no — edit and restart |
| `--key` passphrase | decrypts the secret file at runtime | never stored; supplied each start |

> Does `build` bundle the JSON files into the image? **No.** `build` bundles
> **code + dependencies** into the image tar. `config.json`, `data/`, and `assets/`
> are copied alongside as plain files and mounted. (`.dockerignore` excludes
> `config.json`, `config.*.json`, `data/`, and `assets/` from the image.)

### Directories the bundle owns

The upload writes files over files. It has no opinion about a directory that *used to be* shipped
and is not any more, so one simply stays — and the deploy prints success either way.

On 2026-08-22 that cost a deploy. The built-in SQL had just moved into the package, so the bundle
stopped carrying `assets/metrics`; the worker's copy from the previous layout survived; and the
asset lookup prefers the operator's tree over the package's. The image was correct and the worker
went on running the **old** queries. The fix at the time was a person renaming four directories
over SSH.

So the bundle now declares what it decides the shape of. `copy` compares, after the upload:

| | |
| --- | --- |
| **The bundle owns** | the top level of `data/` and `assets/` |
| **The worker owns** | everything else under the remote directory — `logs/`, `runtime/`, `containers/`, and the quarantine below |

A top-level directory under `data/` or `assets/` that the bundle does not carry is **moved aside**
into `<remote-dir>/.superseded/<timestamp>/`, and the run names each one. Two limits are
deliberate:

- **Top level only.** A whole directory disappearing is a structural change made on the master.
  What happens *inside* `assets/tasks/` is the bot writing SQL on the worker, which the deploy is
  meant to mirror back (`--merge`, `--include-sql`) rather than remove.
- **Moved, not deleted.** This runs against a live worker, driven by a diff with a bundle built
  locally moments earlier. Moving is enough to fix the defect — the lookup stops finding the
  directory — and one bad build does not become data loss. A bundle carrying no such directory at
  all supersedes nothing, for the same reason: that is a failed build, not a retirement.

`.superseded/` is not one of the compose mounts and nothing reads it. Delete old entries whenever.

### B1. Prerequisites

**On Windows (build machine):**

- Docker Desktop for Windows, running (WSL2 backend). Verify:
  ```powershell
  docker version
  docker info
  ```

**On Ubuntu (target server):**

- Docker Engine + compose plugin:
  ```bash
  sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2
  sudo systemctl enable --now docker
  sudo usermod -aG docker $USER   # then log out/in so 'docker' works without sudo
  docker version
  ```

Both machines should be **amd64** (the image is built `--platform linux/amd64`).

### B2. Encrypt secrets first (on Windows)

The daemon decrypts `data/encrypted_secret_text.json`; that file must exist
before you build/package. Generate it from the plaintext source with your chosen
passphrase:

```powershell
# from the repository root
.venv\Scripts\python.exe -m db_ops.control.cli encrypt-secret-text --key-base64 "<base64-passphrase>"
```

- Reads plaintext `secrets\secret_text.json` (ignored, never committed/shipped).
- Writes encrypted `data\encrypted_secret_text.json` (safe to ship — ciphertext only).
- Re-run this whenever a secret value changes.

Remember the passphrase — you pass the same value to the daemon at runtime. It is
not stored anywhere; if you lose it, re-encrypt with a new one.

> **Shell-special characters.** A passphrase with `#`, `$`, `%`, `!`, spaces, etc.
> is mangled by bash/PowerShell quoting. Use the `--key_base64` form everywhere
> instead: base64-encode the passphrase once and pass that. Both the encrypt
> script and every CLI/daemon accept `--key_base64`.
>
> ```powershell
> # base64 of the passphrase (PowerShell) — use your own, do not commit it:
> [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('<your-passphrase>'))
> # -> <base64-string>
> python -m db_ops.control.cli encrypt-secret-text --key-base64 "<base64-string>"
> ```
>
> The examples below use `--key_base64 "<base64-passphrase>"`. Plain `--key
> "<passphrase>"` works too when the passphrase has no special characters.

### B3. Build + package on Windows

From the repository root:

```powershell
.\docker\build_and_package.ps1
```

It will:

1. `docker build --platform linux/amd64 -t db_ops:<version> -t db_ops:latest .` (version read from `db_ops/__init__.py`, also stamped as the `org.opencontainers.image.version` label)
2. `docker save` both tags to a tar,
3. assemble a self-contained bundle at `deploy\db_ops_deploy\` (repo root, gitignored):

```
deploy\db_ops_deploy\
├── db_ops_image.tar          (~190 MB; ~750 MB once loaded/uncompressed)
├── docker-compose.yml        (runtime version, no build step)
├── config.json
├── data\                     (your *.json configs + encrypted_secret_text.json)
├── assets\                   (metric SQL + OS/docker scripts, SQL tasks, telegram SQL)
├── logs\                     (empty)
└── runtime\                  (empty; db_ops.sqlite is created here on first run)
```

The script aborts if `data\encrypted_secret_text.json` is missing (step B2 not
done). Note: the **encrypted** secret file is included; the **passphrase** is not.

> The control app's `build-image` performs this same build + bundle step.

#### Manual equivalent (no script)

```powershell
# from the repository root
docker build --platform linux/amd64 -t db_ops:latest .
docker save db_ops:latest -o db_ops_image.tar
# then copy db_ops_image.tar + docker-compose.runtime.yml (as docker-compose.yml)
#   + config.json + data\ + sql\ to a folder
```

### B4. Copy the bundle to Ubuntu

First create the target directory **owned by your user**, so the copy needs no
sudo (`/opt` is root-owned by default). Replace `user`/`ubuntu-host` accordingly:

```bash
# on the Ubuntu host (or: ssh user@ubuntu-host '<cmd>')
sudo mkdir -p /opt/db_ops && sudo chown "$USER:$USER" /opt/db_ops
```

Then copy the bundle contents from Windows. (The control app's `copy` automates this
via SFTP.)

- **SSH key auth** — OpenSSH `scp` works:
  ```powershell
  scp -r .\deploy\db_ops_deploy\*  user@ubuntu-host:/opt/db_ops/
  ```

- **Password auth on Windows** — OpenSSH `scp` cannot take a password
  non-interactively. Use PuTTY's `pscp` with `-pw` (install PuTTY, or use
  `winget install PuTTY.PuTTY`):
  ```powershell
  & "C:\Program Files\PuTTY\pscp.exe" -pw "<password>" -r `
      .\deploy\db_ops_deploy\*  user@ubuntu-host:/opt/db_ops/
  ```
  On the first connection accept the host key (pscp prompts once; PuTTY caches it).

Result on Ubuntu: `/opt/db_ops/` holds the compose file, image tar, configs, and
sql. The encrypted secret file is already inside `data/`. **No separate secrets
copy is needed** — and the passphrase is never sent in a file.

### B5. Load the image and start the daemon (with your key)

```bash
cd /opt/db_ops

# 1. import the image built on Windows
docker load -i db_ops_image.tar
docker image ls | grep db_ops          # should show db_ops:latest

# 2. sanity-check config + key before going live (dry-run, no daemon).
#    This decrypts secrets, so a wrong key fails here immediately.
docker compose run --rm db_ops \
  python -m db_ops.metrics.cli --config config.json collect --dry-run --key_base64 "<base64-passphrase>"

# 3. start the daemon, passing the passphrase at runtime + the worker node role
docker compose run -d --name db_ops_daemon -e DB_OPS_NODE_ROLE=worker db_ops daemon --key_base64 "<base64-passphrase>"

# 4. confirm it is running
docker ps | grep db_ops_daemon
```

The passphrase is supplied only on this command line — it is not written to the
compose file, an env file, or the image. The daemon exports it in-memory
(`DB_OPS_SECRET_KEY`) so the app commands it spawns inherit it and decrypt
on demand. (`--key_base64` is used so a passphrase with `#$%` etc. survives shell
quoting; plain `--key "<passphrase>"` works for simple passphrases.)

`DB_OPS_NODE_ROLE=worker` makes the daemon run only `app_commands.json` entries with
`node_role` of `worker` or `all`. The control app's `start-daemon` sets this for you
(default `worker`, override with `--node-role`).

> **Restart behavior.** Because the key is never persisted, a stopped container
> cannot auto-restart with the key. Re-run the step-3 command to restart. (If you
> accept the key living in Docker's container metadata — visible via
> `docker inspect` — you may instead use
> `docker run -d --restart unless-stopped -e DB_OPS_NODE_ROLE=worker <mounts> db_ops:latest daemon --key_base64 "<base64-passphrase>"`
> so it survives reboots; the strict "nothing stored" option is the `compose run`
> command above. The control app's `start-daemon` uses `restart=unless-stopped`.)

### B6. Check status, logs, and errors

**Container-level:**

```bash
docker ps                                       # Up / Exited?
docker logs -f db_ops_daemon                     # live stdout/stderr of the daemon
docker logs --tail=100 db_ops_daemon             # last 100 lines
docker inspect --format '{{.State.Status}} {{.State.ExitCode}}' db_ops_daemon
```

**Application logs** (written to the mounted `logs/` folder — readable directly
on the host, no need to enter the container):

```bash
ls -la /opt/db_ops/logs/
tail -f /opt/db_ops/logs/errors.log
tail -f /opt/db_ops/logs/metrics.log
tail -f /opt/db_ops/logs/restore_workflow.log
tail -f /opt/db_ops/logs/telegram.log
```

Confirm the node role the daemon resolved — each tick logs `node_role` and the
count of active commands it will run (`app.daemon.tick`). On the worker this should
read `worker`.

**Runtime history** (the runtime store; the paths below apply when the backend is `sqlite`):

```bash
ls -la /opt/db_ops/runtime/db_ops.sqlite
```

**Get a shell inside the container** to debug interactively:

```bash
docker compose run --rm db_ops shell
# then, inside (one-off commands that read secrets need the key):
python -m db_ops.backup_restore.cli restore-latest --config config.json --dry-run --key_base64 "<base64-passphrase>"
pwsh -v          # confirm PowerShell 7 is present
odbcinst -q -d   # confirm ODBC drivers
```

### B7. One-off commands

Any documented `python -m db_ops.<app>` command runs through the same image.
Commands that read secrets (most do) need the key:

```bash
docker compose run --rm db_ops \
  python -m db_ops.metrics.cli --config config.json collect --dry-run --key_base64 "<base64-passphrase>"

docker compose run --rm db_ops \
  python -m db_ops.reports.cli --config config.json run-scheduled --summary-limit 150
```

### B8. Updating later

**Changed config only** (threshold, target, routing): edit the file under
`/opt/db_ops/{config.json,data/...}`, then stop and re-run the daemon:

```bash
docker rm -f db_ops_daemon
docker compose run -d --name db_ops_daemon -e DB_OPS_NODE_ROLE=worker db_ops daemon --key_base64 "<base64-passphrase>"
```

**Changed a secret value**: re-run the encrypt step on Windows, re-copy
`data/encrypted_secret_text.json` to `/opt/db_ops/data/`, then restart the daemon
(same commands).

**Changed code/dependencies**: rebuild on Windows, re-copy the tar, then on Ubuntu:

```bash
docker load -i db_ops_image.tar
docker rm -f db_ops_daemon
docker compose run -d --name db_ops_daemon -e DB_OPS_NODE_ROLE=worker db_ops daemon --key_base64 "<base64-passphrase>"
```

> From the master PC, all three updates are a single `python -m db_ops.control.cli deploy --user <user>`.

### B9. Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Jobs error `Failed to decrypt secret text: wrong key or corrupted file` | wrong `--key` | restart with the exact passphrase used to encrypt |
| Jobs error `No decryption key provided` | daemon started without a key | restart with `daemon --key_base64 "<base64-passphrase>"` |
| `password ref not found ...` for every target | `data/encrypted_secret_text.json` missing/empty | re-run the encrypt step and re-copy the file |
| Worker runs no commands (tick shows `active_commands=0`) | all entries tagged `node_role: master`, or node resolved as `master` | tag worker entries `worker`/`all`; confirm `DB_OPS_NODE_ROLE=worker` is set on the container |
| `docker build` fails: apt `403 Forbidden` or `Hash Sum mismatch` | a transparent HTTP proxy on the build network corrupts apt traffic | the Dockerfile defaults to an HTTPS mirror to bypass it; for a different network override `docker build --build-arg UBUNTU_MIRROR=http://archive.ubuntu.com/ubuntu ...` |
| `exec format error` on `docker load`/run | image arch ≠ host arch | rebuild with `--platform linux/amd64` |
| `Config file not found: config.json` | config not mounted / wrong cwd | confirm `./config.json` exists next to the compose file |
| `Can't open lib 'ODBC Driver 18 for SQL Server'` | ran host Python, not the image | use the container: `docker compose run --rm db_ops odbcinst -q -d` |
| `working_dir not found: tools/db_ops` | layout mismatch | use the provided compose; the image expects `/app/tools/db_ops` |
| Legacy SQL Server (e.g. 2008 R2) fails with `SSL routines::unsupported protocol` | the server only offers TLS 1.0, which OpenSSL 3 blocks by default | the image enables legacy TLS (openssl.cnf `MinProtocol=TLSv1.0`, `CipherString=DEFAULT@SECLEVEL=0`); set that target's `sqlserver_driver` to `ODBC Driver 17 for SQL Server` in `db_instances.json`. Note `pymssql`/FreeTDS does **not** honor this OpenSSL setting, so for TLS-1.0-only hosts use an ODBC driver, not pymssql |
| Permission denied writing `logs/`/`runtime/` | host dir owned by root | `sudo chown -R $USER:$USER /opt/db_ops/logs /opt/db_ops/runtime` |

### B10. Reference

#### Image contents

| Component | Why |
| --- | --- |
| `msodbcsql17` + `msodbcsql18` + `unixodbc` + `mssql-tools18` | SQL Server access via `pyodbc` / `sqlcmd`; driver 17 is retained for legacy SQL Server 2008 R2 hosts that driver 18's TLS/cert defaults reject. |
| `powershell` (`pwsh`) | Windows-target remote operations (WinRM/`Invoke-Command`) from Linux. |
| `openssh-client`, `rsync`, `smbclient` | Linux-target copy/exec and SMB copy paths. |
| Python venv at `/opt/venv` + `requirements.txt` | App runtime (incl. `cryptography` for secret decryption). |
| `db_ops/` under `/app/tools/db_ops` | App source. `tools/db_ops` is kept inside the image as a fixed layout, not as a repo path. |

#### Layout mirroring

The container places the package at `/app/tools/db_ops/db_ops`. `tools/db_ops` is a
**logical alias for the tool root**, not a folder in this repository (which is standalone,
with `db_ops/` at its root). Keeping the alias fixed is what lets the same
`app_commands.json` work on the Windows master and in the container:

- the daemon's `REPO_ROOT=/app` and `TOOL_ROOT=/app/tools/db_ops`;
- `app_commands.json` `working_dir: "tools/db_ops"` → `/app/tools/db_ops` in the image, and the
  repository root on a local checkout (resolved in `db_ops/jobs/daemon.py`);
- secrets read from `/app/tools/db_ops/data/encrypted_secret_text.json` (mounted).

#### Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `DB_OPS_CONFIG` | `config.json` | Config path handed to the daemon. |
| `DB_OPS_DAEMON_DELAY` | `2` | Daemon scan delay (seconds). |
| `DB_OPS_NODE_ROLE` | `master` | This node's role (`master`/`worker`) — the *only* thing that decides it. Set `worker` on the deployed container (`start-daemon`/`deploy` do this by default); set `master` to run a master daemon the same way. Unset → defaults to `master`. No host autodetection; the shared `config.json` never sets the role. |
| `DB_OPS_SECRET_KEY` | _(unset)_ | Decryption passphrase. Normally supplied via `--key`; the daemon exports it for child app commands. Set it directly only for a non-daemon one-off. |
| `DB_OPS_POWERSHELL` | _(auto)_ | Force a PowerShell executable; otherwise `pwsh` is auto-detected. |

#### Cross-platform PowerShell

Windows-target operations resolve the PowerShell binary at runtime via
`db_ops/lib/shell.py`: `DB_OPS_POWERSHELL` → `pwsh` (installed) →
`powershell.exe` on a native Windows host. This lets the previously Windows-only
code paths run from the Ubuntu container.

#### Linux source reads, and the remaining Windows-target limit

When db_ops itself runs on Linux (this container) and restores to a **Linux** SQL
Server target, the full path works end-to-end and needs no Windows machine: it
reads the Windows SMB backup **source** via `smbclient` (auth file, recursive
`mget`, with the backup mtime recovered from the file name so the log chain
filters correctly), stages the files locally, and sftp's them to the target. A
fresh target's **Database Master Key is created automatically** before the
backup-encryption certificate is imported. This path is validated against a
containerized SQL Server 2025 target.

Still limited: restoring **to a Windows target from a Linux host**. SQL execution
and certificate import work via `pwsh` + WinRM, but the backup **copy onto** a
Windows SMB share (`vm_import_unc`) still uses the Windows-native `robocopy` /
`cmdkey` / UNC write path in
`db_ops/backup_restore/{copy_backup,delete_backup,preflight}.py`. From Linux,
prefer Linux restore targets; validate any Windows-target copy/restore against a
real share before relying on it.

### B11. Alternative: build directly on Ubuntu (no image transfer)

If you would rather skip the `save`/`load` step, copy the **source** and build on
the server:

```bash
# copy the repository (code + Dockerfile + config + data + sql) to /opt/db_ops-src
cd /opt/db_ops-src
docker compose build                                   # uses docker-compose.yml (with build:)
docker compose run -d --name db_ops_daemon -e DB_OPS_NODE_ROLE=worker db_ops daemon --key_base64 "<base64-passphrase>"
docker logs -f db_ops_daemon
```

`data/encrypted_secret_text.json` must already exist (run the encrypt step first).
This is simpler operationally (one machine builds and runs) but downloads the
base image and packages on the server.

## Syncing worker-side config back to the master

The worker may edit config at runtime (e.g. the Telegram `/spbot_add_sql` command appends to
`sql_commands.json` / `sql_targets.json` and writes a new `assets/tasks/...` script). Because deploys
push the master's `data/` and `assets/` to the worker, those runtime edits must reach the master or a
later deploy would overwrite them.

### `deploy` merges them for you

**`deploy` runs the merge itself, before it builds the bundle.** It folds the worker's runtime
changes into the master's copy and mirrors the worker's `*.sql` scripts back, so the bundle it
then ships carries both sides. The worker changes config in **two different ways**, and each
needs its own rule.

#### Files the worker *appends records to* — union by key

| File | A record is identified by | What adds one |
| --- | --- | --- |
| `sql_commands.json` | `sql_id` | `/spbot_add_sql` |
| `sql_targets.json` | `sql_id` + `target_no` | `/spbot_add_sql` |
| `telegram_groups.json` | `group_id` | the update poller, on every new chat |
| `telegram_users.json` | `user_id` | the update poller, on every new user |
| `docker_db_connections.json` | `id` | `sre create-db-docker` |
| `app_commands.json` | `app_command_id` | nothing today — unioned so a future writer cannot silently lose entries |

**The master wins a shared key; worker-only records are added.** A record on both sides has
usually been *edited* on the master (a schedule retuned, a group's `allow_command` raised from
the `0` the bot writes for every chat it discovers) — taking the worker's copy would revert that
silently. A record only the worker has can only be an addition, so it is kept. Fields are not
merged *within* a shared record here: two people editing the same target between deploys is a
conflict, and interleaving their fields would produce a row neither of them wrote.

#### The secret store — merged, and a conflict aborts the deploy

`/spbot_create_db_docker` registers a new database password **on the worker**. A deploy then
re-encrypts the master's plaintext source over the top and ships it, so the ref disappears — the
same loss as an un-merged SQL task, one layer down and harder to spot, because what breaks later
is a connection that used to work.

`deploy` therefore merges `encrypted_secret_text.json` first. Two things make the order matter:

- It runs **after** `_refresh_encrypted_secret_store` (which the CLI does before calling `deploy`).
  That refresh **replaces** the master's encrypted store with exactly the plaintext content, so a
  merge done before it would be thrown away.
- It writes **both** master stores — the encrypted one *and* the plaintext source — or the next
  deploy's refresh would drop the merged refs again.

A ref present on both sides with **different values is a conflict**: `SecretMergeConflict` is
raised, nothing is written, and the deploy stops before anything is built or shipped. Guessing
which side is current is how a production credential gets replaced by a lab password. Align the
two values, then re-run.

With no `--key`/`--key-base64` the step is **skipped with a notice** rather than failing: both
stores are encrypted, so there is nothing to compare — but the notice says a worker-only ref will
be lost by that deploy.

#### `db_instances.json` — the worker *edits* records, so the merge is per field

`/spbot_metric_toggle` changes an existing target rather than adding one. A union by `server_id`
would keep the master's record whole and throw the toggle away: the file would look merged while
the metric an operator switched off overnight quietly came back on. So the master's record is the
base and the worker's value is overlaid at **exactly the paths the toggle writes**:

```text
metrics.enabled
metrics.disabled_collector_types
metrics.metric_overrides
report_policy.disabled_metric_codes
```

Everything else in the record — `ip`, `port`, `default_credential_name`, `cmd_access`,
`service_name`, … — stays the master's. A worker running a stale copy must not be able to push an
old address back over an inventory correction.

The paths are **leaves on purpose**. `metrics` also holds `collector_env` and `severity_map`,
which the toggle never touches; overlaying the whole `metrics` object would revert a master edit
to those. A path the worker does not have at all is left alone, so a re-deploy does not resurrect
a removed toggle as `null`. A `server_id` only the worker has (a lab database
`create-db-docker` registered) is added whole.

#### The worker's files have to be readable first — `reclaim worker files`

`data/` and `assets/` are bind mounts shared between the container and its host, the container runs
as **root**, and the master reads the worker over SFTP as `dba_user`. So anything the container writes
there lands owned by root, and from that moment the master cannot open it. The merge caught the
permission error with the same `except IOError` as a missing file, printed
`MISSING db_instances.json (not on worker)`, and the copy step overwrote the operator's change with
the master's copy — written, real, and gone at the next deploy with nothing in the output saying so.

The deploy's **first** step is now `reclaim_worker_files`: `chown -R <ssh_user>` over `data/` and
`assets/`, the whole tree. Not a list of files — enumerating the writers that can cause this means
remembering every future one, in every app. `copy_bundle` already ran this exact `chown`; it ran it
*after* the merge, which is precisely too late.

```text
=== reclaim worker files ===
  OK       data/ and assets/ are readable by dba_user again
```

Two rules that command must keep:

- **`chown` only, never `chmod`.** Ownership is the only thing the merge needs — the owner can read
  its own `0600` file — so there is no reason to touch modes, and every reason not to: `data/` holds
  `encrypted_secret_text.json` and `ssh_keys/*.key`, and a step that rewrites permissions across
  that tree is one bad default away from publishing a private key. (Their modes on the worker are
  **not** currently tight, which was measured rather than assumed. That is a
  separate problem, and widening them further is not the fix.)
- **Never `containers/`.** The lab DB containers bind-mount their data out of there and it belongs
  to the database users inside them (postgres is uid 999). See the note in `copy_bundle`.

Two backstops behind it: `config_admin._atomic_write` preserves the original mode and owner so the
drift does not start, and a file that exists but cannot be read **aborts** the deploy
(`WorkerConfigUnreadable`) instead of being treated as absent. Absent means there is nothing to
merge; unreadable means there may be everything to merge and the next step destroys it.

**Deleting an override on the master does not delete it on the worker**, and that follows from the
same rule: the worker owns `metrics.metric_overrides`, so the merge reads its copy back and the
edit is undone on both sides. On 2026-08-05 two `DATABASE_CHECKDB` overrides were removed from
the master, deployed, and were back in `data/db_instances.json` before the image finished
building. Clear an override where it was written — `metric-toggle --state on` on the worker, or
`/spbot_metric_toggle` — and then on the master, so the next merge finds nothing to restore.
`--state on` removes the entry rather than setting `enabled: true`, precisely so there is nothing
left for the merge to carry.

Each file keeps the indent it already uses — 1 space for `db_instances.json`, 2 for the Telegram
files, 4 for the SQL ones — so a two-line merge does not land as a whole-file reformat.

### The merge is opt-in — `--merge`

**Since 2026-08-11 a deploy is master → worker by default**, and everything in this section runs
only when `--merge` is passed. `--no-merge-worker` is kept as a no-op so existing runbook lines do
not break.

Both directions destroy work, which is why the choice is now explicit and `deploy` prints which
one it is about to run:

| Without `--merge` (default) | With `--merge` |
| --- | --- |
| Anything registered or toggled through the bot since the last deploy is **deleted** | The master **loses** at `db_instances.json` `metrics.enabled`, `disabled_collector_types`, `metric_overrides`, and `report_policy.disabled_metric_codes` |

> The old default exists because of a real loss. On 2026-07-31 an operator added a SQL task through
> `/spbot_add_sql`; a deploy minutes later shipped the master's `sql_targets.json`, which did not
> have it, and the task was gone. It was rebuilt only because `sql_runs.run_key` happened to
> record the resolved server/service/instance/credential.
>
> The new default exists because of the other half of that trade. The worker owning
> `metric_overrides` made the master **un-editable** there: a severity map written on the master
> and deployed was replaced by the worker's copy — over the master's own file, before the build —
> so the edit vanished from both sides with nothing said. Downgrading six standing alerts on
> 2026-08-11 hit it, and the workaround (write it on the worker, then merge it back) is not
> something anyone should have to know.

So: **pass `--merge` when the bot has registered something since the last deploy**, and leave it
off when you are shipping a deliberate master-side change.

### Pulling explicitly

`worker-pull-data-config` still does it on demand (master-initiated SFTP; no reverse credentials
on the worker) — useful to sync without deploying, or to take the worker's copy of a file
wholesale rather than merging it:

```bash
# Pull the worker-owned config files (overwrite) AND mirror new *.sql scripts back:
python -m db_ops.control.cli worker-pull-data-config --key-base64 <KEY> \
  --writeback-config --include-sql
```

- `--writeback-config` — preset that pulls `sql_commands.json`, `sql_targets.json`, and
  `app_commands.json` with `--overwrite` (the files the worker mutates at runtime).
- `--include-sql` — mirrors the worker's `assets/` tree (`*.sql`) into the master's `assets/`, creating
  subfolders as needed (only `*.sql` files are fetched; existing files are skipped unless
  overwriting).
- `--all-json` / `--files` / `--dry-run` / `--overwrite` continue to work as before; the default
  (no flags) still pulls only the docker-db connection registry, so existing behaviour is
  unchanged.

Since `deploy` merges the files above automatically, running this by hand is now only needed to
sync without deploying, or to overwrite a master file with the worker's copy outright. The
`assets/` mount is read-write on the worker (`docker-compose.runtime.yml`) specifically so
runtime-added task scripts persist and can be synced.
