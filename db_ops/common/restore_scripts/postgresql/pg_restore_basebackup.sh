#!/bin/bash
# PostgreSQL restore drill - rebuild the cluster from its base backup chain into a SEPARATE
# container, running the same phases as the SQL Server and Oracle restores:
#
#   PHASE=restore-key    decryption material for the target (none for a plain base backup)
#   PHASE=copy-backup    verify the chain is present AND readable, then combine it into PGDATA
#   PHASE=restore-data   point recovery at the WAL archive and start the restored cluster
#   PHASE=verify         prove the restored cluster is accepting queries and holds real data
#
# The whole chain is reconstructed with pg_combinebackup: an incremental backup is not a usable
# data directory on its own, so restoring one without combining it produces a directory that
# looks complete and refuses to start. Combining is the restore.
#
# The source's backups are mounted read-only, so a drill can never write over the cluster it is
# validating.
#
# Env: SOURCE_CONTAINER, TARGET_CONTAINER, BACKUP_DIR (source backups as seen inside the target),
#      RESTORE_ID, PGDATA (default /var/lib/postgresql/18/docker), PG_USER (default postgres),
#      PG_OS_USER (default postgres), PG_BIN (default /usr/lib/postgresql/18/bin).
# Exit: 0 on success. Prints RESULT=ok only after the restored cluster answers a query.
set -u

source_container="${SOURCE_CONTAINER:-}"
target_container="${TARGET_CONTAINER:-}"
backup_dir="${BACKUP_DIR:-}"
restore_id="${RESTORE_ID:-pg-restore}"
pgdata="${PGDATA:-/var/lib/postgresql/18/docker}"
pg_user="${PG_USER:-postgres}"
pg_os_user="${PG_OS_USER:-postgres}"
pg_bin="${PG_BIN:-/usr/lib/postgresql/18/bin}"

die() { printf 'RESULT=error phase=%s reason=%s\n' "${PHASE:-init}" "$1" >&2; exit 1; }
phase() { PHASE="$1"; printf '\nPHASE=%s %s\n' "$1" "${2:-}"; }

[ -n "$backup_dir" ] || die "BACKUP_DIR is not set."

# native: this script already runs on the machine that will host the cluster (a VM with
# PostgreSQL installed directly), so there is no container to exec into - the engine commands
# run here as the postgres OS user. docker mode execs into TARGET_CONTAINER on this host.
target_mode="${TARGET_MODE:-docker}"

if [ "$target_mode" = "docker" ]; then
    [ -n "$target_container" ] || die "TARGET_CONTAINER is not set."
    DOCKER="docker"
    $DOCKER info >/dev/null 2>&1 || DOCKER="sudo docker"
    $DOCKER inspect "$target_container" >/dev/null 2>&1 \
        || die "target container '$target_container' not found on host."
    # SOURCE_CONTAINER is empty when restoring onto another machine: the source container lives
    # on a different host, so it is neither present here nor something to guard against.
    if [ -n "$source_container" ]; then
        [ "$source_container" != "$target_container" ] \
            || die "target is the source container; a restore must not overwrite what it validates."
        $DOCKER inspect "$source_container" >/dev/null 2>&1 \
            || die "source container '$source_container' not found on host."
    fi
fi

# The staged backup has to be visible *inside* the target container. Historically that was
# arranged by giving the target a bind mount at the same path — which quietly restricts the
# restore to containers built for the purpose. Any running container can be a target if the
# staged directory is simply copied in, so when BACKUP_DIR is missing inside the container but
# present on the host (where sync_backup_dir put it), copy it across. A container that already
# has the path mounted is left alone, so the mounted setup keeps working unchanged.
# Is backup_dir served by a real mount inside the container? Matching /proc/mounts against
# backup_dir alone was wrong: the bind mount is usually a PARENT ("/opt/oracle/backup" holding
# "/opt/oracle/backup/dbops"), so an exact match never fired, the script concluded there was no
# mount, and every drill died at init. Walk up instead. Stopping before "/" is deliberate - the
# container rootfs is itself a mount, and matching it would call every path live, bringing back
# the stale-copy bug this staging logic exists to prevent.
inside_a_mount() {
    $DOCKER exec "$target_container" sh -c '
        dir="$1"
        while [ "$dir" != "/" ] && [ -n "$dir" ]; do
            grep -q " $dir " /proc/mounts && exit 0
            dir=$(dirname "$dir")
        done
        exit 1
    ' _ "$backup_dir" </dev/null 2>/dev/null
}

stage_into_target() {
    [ "$target_mode" = "docker" ] || return 0
    # A path that is a real mount inside the container IS the host directory - already in
    # sync, nothing to copy.
    # A path served by a mount IS the host directory - live by construction, nothing to copy,
    # and nothing to delete either.
    if inside_a_mount; then
        return 0
    fi
    # A previous run's copy must be replaced, not kept. Skipping the copy because the
    # directory "already exists" silently pins the restore to whatever was staged the first
    # time: pieces deleted at the source stayed here and were still picked up by RMAN hours
    # later, which is exactly how a restore ends up reading a backup nobody can decrypt.
    # Guarded: inside_a_mount already returned above, so this can only be a previous run's
    # copy. Re-checking costs nothing and stops a future edit turning this into an rm -rf of a
    # read-write bind mount - that is, of the source backups themselves.
    if inside_a_mount; then
        die "refusing to delete '${backup_dir}': it is served by a mount inside '${target_container}'."
    fi
    $DOCKER exec -u 0 "$target_container" rm -rf "$backup_dir" </dev/null 2>/dev/null || true
    [ -d "$backup_dir" ] || die "backup dir '${backup_dir}' is not served by a mount inside '${target_container}', and does not exist on this host either - nothing to stage."
    printf 'staging %s into %s (no bind mount on the target)
' "$backup_dir" "$target_container"
    # -u 0: the database user owns its own directories but not /, and the staging path sits
    # outside them. The copied tree is then handed to the database user, which has to *read*
    # the pieces during the restore.
    $DOCKER exec -u 0 "$target_container" mkdir -p "$(dirname "$backup_dir")" </dev/null         || die "cannot create $(dirname "$backup_dir") inside '${target_container}'."
    $DOCKER cp "$backup_dir" "${target_container}:${backup_dir}"         || die "docker cp of '${backup_dir}' into '${target_container}' failed."
    $DOCKER exec -u 0 "$target_container" chmod -R a+rX "$backup_dir" </dev/null         || die "cannot make '${backup_dir}' readable inside '${target_container}'."
}
stage_into_target

# -u $pg_os_user and stdin closed: run as the database user so everything written into PGDATA is
# owned by it (PostgreSQL refuses to start on a data directory it does not own), and so a
# `docker exec -i` cannot eat the rest of this script from the shared stdin.
in_t() {
    if [ "$target_mode" = "native" ]; then
        su - "$pg_os_user" -c "$1" </dev/null
    else
        $DOCKER exec -u "$pg_os_user" "$target_container" bash -lc "$1" </dev/null
    fi
}
psql_t() { in_t "psql -U '${pg_user}' -tAc \"$1\"" 2>/dev/null | tr -d '\r'; }

# --------------------------------------------------------------------------------------------
phase restore-key "decryption material for the target"
# pg_basebackup output is not encrypted, so there is nothing to import - stated rather than
# skipped, because "no key needed" and "key forgotten" look identical in a log otherwise.
printf 'base backups are not encrypted; nothing to import\n'

# --------------------------------------------------------------------------------------------
phase copy-backup "verify the chain, then combine it into PGDATA"
base_src="${backup_dir}/base"
full="$(in_t "ls -1d ${base_src}/*_FULL 2>/dev/null | sort | tail -1" | tr -d '\r')"
[ -n "$full" ] || die "no _FULL base backup found under ${base_src}."

# Every incremental taken after that full belongs to its chain and must be combined with it.
chain="$full"
for d in $(in_t "ls -1d ${base_src}/*_INCR 2>/dev/null | sort" | tr -d '\r'); do
    [ "$(basename "$d")" \> "$(basename "$full")" ] && chain="$chain $d"
done
printf 'chain=%s\n' "$chain"

# Listable is not readable: the backups come from another container's volume, so ownership can
# leave them unreadable while `ls` still shows them - a failure that surfaces later as a
# confusing "invalid data directory" instead of a permission error.
in_t "head -c 64 '${full}/backup_manifest' >/dev/null" \
    || die "backup pieces are not readable by ${pg_os_user} in ${target_container}: check ownership on the source backup volume."
printf 'pieces_readable_by=%s\n' "$pg_os_user"

# The cluster stays up while the chain is combined below; it is stopped only for the swap
# itself, so the target is down for seconds rather than for the minutes pg_combinebackup takes.

# Staging goes beside the volume root, not beside PGDATA: the version directory that contains
# PGDATA is root-owned, so the database user cannot create anything in it. Same filesystem as
# PGDATA, so moving the result in later is a rename rather than a copy.
staging="${RESTORE_STAGING:-/var/lib/postgresql/dbops_restore_staging}"
in_t "rm -rf '${staging}'" || die "could not clear the staging directory."
if ! in_t "${pg_bin}/pg_combinebackup ${chain} -o '${staging}' 2>&1"; then
    die "pg_combinebackup failed; the chain does not reconstruct."
fi
in_t "test -f '${staging}/PG_VERSION' && test -d '${staging}/base'" \
    || die "combined output is not a data directory (no PG_VERSION/base)."
combined_size="$(in_t "du -sh '${staging}' | cut -f1" | tr -d '\r')"
printf 'combined=%s size=%s\n' "$staging" "$combined_size"

# --------------------------------------------------------------------------------------------
phase restore-data "recover from the WAL archive and start the cluster"
# PGDATA's *contents* are replaced, not PGDATA itself. Its parent directory is root-owned, so
# the database user can neither delete nor recreate PGDATA - but it owns PGDATA, so it can empty
# it and move the combined cluster in. dotglob catches the dot-files (PG_VERSION is not one, but
# a restored cluster carries others) that a bare * would silently leave behind.
# Stopping the database means stopping the *container* when the database is its main process:
# `pg_ctl stop` there kills PID 1, the supervisor restarts the container, and its entrypoint
# starts postgres again on a data directory this script is half way through replacing. Observed
# directly on this lab: RestartCount climbed and a later step failed with "Container is
# restarting, wait until the container is running". An explicit `docker stop` is NOT undone by
# restart=unless-stopped, so the container is taken down deliberately, the swap happens on the
# volume from the host, and it is brought back up. A `native` target keeps using pg_ctl - there
# is no supervisor to fight there.
if [ "$target_mode" = "docker" ]; then
    volume_root="$($DOCKER inspect "$target_container" --format \
        '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql"}}{{.Source}}{{end}}{{end}}' 2>/dev/null | tr -d '\r')"
    [ -n "$volume_root" ] || die "cannot find the /var/lib/postgresql mount of '${target_container}'."
    host_pgdata="${volume_root}$(printf '%s' "$pgdata" | sed 's#^/var/lib/postgresql##')"
    host_staging="${volume_root}$(printf '%s' "$staging" | sed 's#^/var/lib/postgresql##')"
    printf 'host_pgdata=%s\n' "$host_pgdata"

    SUDO=""
    [ -w "$host_pgdata" ] || SUDO="sudo"

    printf 'stopping container %s for the swap\n' "$target_container"
    $DOCKER stop "$target_container" >/dev/null || die "could not stop '${target_container}'."

    # Same volume, so moving the combined cluster in is a rename rather than a copy.
    $SUDO find "$host_pgdata" -mindepth 1 -delete \
        || die "could not empty the target data directory on the host."
    $SUDO sh -c "mv '${host_staging}'/* '${host_pgdata}'/ 2>/dev/null; \
                 mv '${host_staging}'/.[!.]* '${host_pgdata}'/ 2>/dev/null; :" \
        || die "could not move the combined cluster into the data directory."
    $SUDO rmdir "$host_staging" 2>/dev/null || true

    # recovery.signal + restore_command replay the archived WAL on top of the base backup.
    # Without them the cluster starts at the backup's own consistency point and silently loses
    # every change made after it. Written from the host because the container is down.
    wal_src="${backup_dir}/wal"
    $SUDO sh -c "cat > '${host_pgdata}/postgresql.auto.conf' <<CONF
restore_command = 'cp ${wal_src}/%f %p'
recovery_target_timeline = 'latest'
CONF" || die "could not write the recovery configuration."
    $SUDO touch "${host_pgdata}/recovery.signal" || die "could not create recovery.signal."
    # PostgreSQL refuses to start on a data directory it does not own.
    owner="$($DOCKER run --rm -v "${volume_root}:/v" busybox stat -c '%u:%g' /v 2>/dev/null || echo '')"
    [ -n "$owner" ] && $SUDO chown -R "$owner" "$host_pgdata" 2>/dev/null || true
    $SUDO chmod 700 "$host_pgdata" || die "could not set permissions on the data directory."

    printf 'starting container %s\n' "$target_container"
    $DOCKER start "$target_container" >/dev/null || die "could not start '${target_container}'."
    # Do not assume restarting the container starts PostgreSQL. That holds for a container whose
    # command IS postgres (an HA node), but a drill target is often parked - built from the
    # postgres image with `sleep infinity` as its command so it holds the volume without serving.
    # Restarting one of those brings back the sleep and nothing else, and the wait below then
    # times out reporting "the cluster did not answer", which names the symptom and hides the
    # cause. So: wait briefly, and if no postmaster appeared, start one explicitly.
    wait_for_cluster() {
        _n=0
        while [ "$_n" -lt "$1" ]; do
            [ "$(psql_t "select 1")" = "1" ] && return 0
            _n=$((_n + 1))
            sleep 5
        done
        return 1
    }
    if ! wait_for_cluster 6; then
        if in_t "test -f '${pgdata}/postmaster.pid'"; then
            printf 'postmaster is up but not answering yet; waiting
'
        else
            printf 'no postmaster after the restart; starting one (target command does not serve)
'
            in_t "${pg_bin}/pg_ctl -D '${pgdata}' -l '${pgdata}/dbops_restore.log' -w -t 60 start" || printf 'pg_ctl start returned non-zero; falling through to the wait
'
        fi
        if ! wait_for_cluster 48; then
            printf -- '-- last lines of the cluster log --
'
            in_t "tail -30 '${pgdata}/dbops_restore.log' 2>/dev/null" || true
            die "the restored cluster did not answer after the container restarted."
        fi
    fi
else
    in_t "${pg_bin}/pg_ctl -D '${pgdata}' -m fast -w stop" >/dev/null 2>&1 || true
    in_t "shopt -s dotglob nullglob; rm -rf '${pgdata}'/*" \
        || die "could not empty the target data directory."
    in_t "shopt -s dotglob nullglob; mv '${staging}'/* '${pgdata}'/" \
        || die "could not move the combined cluster into the data directory."
    in_t "rmdir '${staging}' 2>/dev/null; chmod 700 '${pgdata}'" \
        || die "could not set permissions on the data directory."
    wal_src="${backup_dir}/wal"
    in_t "cat > '${pgdata}/postgresql.auto.conf' <<'CONF'
restore_command = 'cp ${wal_src}/%f %p'
recovery_target_timeline = 'latest'
CONF" || die "could not write the recovery configuration."
    in_t "touch '${pgdata}/recovery.signal'" || die "could not create recovery.signal."
    in_t "${pg_bin}/pg_ctl -D '${pgdata}' -l '${pgdata}/restore.log' -w -t 300 start" \
        || die "the restored cluster did not start."
fi

# Recovery finishes and the cluster leaves read-only mode on its own; wait rather than assume.
for _ in $(seq 1 60); do
    [ "$(psql_t "select pg_is_in_recovery()")" = "f" ] && break
    sleep 5
done
in_recovery="$(psql_t "select pg_is_in_recovery()")"
printf 'in_recovery=%s\n' "${in_recovery:-unknown}"

# --------------------------------------------------------------------------------------------
phase verify "the restored cluster must answer queries and hold data"
version="$(psql_t "select current_setting('server_version')")"
[ -n "$version" ] || die "the restored cluster is not answering queries."

databases="$(psql_t "select count(*) from pg_database where not datistemplate")"
relations="$(psql_t "select count(*) from pg_class")"
printf 'server_version=%s databases=%s relations=%s\n' "$version" "${databases:-0}" "${relations:-0}"

case "${relations:-0}" in ''|0) die "restored cluster reports no relations; the restore is not usable." ;; esac
[ "${in_recovery}" = "f" ] || die "restored cluster is still in recovery; it never opened for read/write."

# --------------------------------------------------------------------------------------------
# FRESHNESS. The three checks above pass on a cluster this script never touched: an empty
# PostgreSQL answers queries, is not in recovery, and reports 415 relations from its system
# catalog alone. On this lab the source cluster holds no user tables, so "relations=415
# databases=1" was printed by every successful drill AND would have been printed by a restore
# that silently did nothing - the drill could not tell the two apart, and reported success daily
# either way.
#
# The control file settles it: pg_controldata's latest checkpoint is written by the recovery this
# run performed, so it cannot predate the backup this run selected. Comparing it against the
# newest piece in the chain (whose directory name is a UTC stamp) proves the running cluster came
# out of that backup rather than being whatever was already mounted.
control_field() { # <pgdata> <exact pg_controldata label>   -> the value, trimmed
    in_t "${pg_bin}/pg_controldata -D '$1'" 2>/dev/null \
        | sed -n "s/^$2: *//p" | head -1 | tr -d '\r' | sed 's/[[:space:]]*$//'
}

# 1) Identity. A base backup is a byte copy of the source cluster, so it carries the source's
# system identifier - and initdb mints a new one. Comparing the running cluster against the
# BACKUP's own control file (the chain's FULL directory is a PGDATA layout, so pg_controldata
# reads it directly) proves the cluster serving queries came out of this backup. No access to the
# source host is needed, which matters because the two database hosts cannot reach each other.
chain_full="$(printf '%s\n' $chain | head -1)"
chain_newest="$(basename "$(printf '%s\n' $chain | tail -1)")"
backup_sysid="$(control_field "$chain_full" "Database system identifier")"
target_sysid="$(control_field "$pgdata" "Database system identifier")"
printf 'system_identifier backup=%s target=%s\n' "${backup_sysid:-unknown}" "${target_sysid:-unknown}"
[ -n "$backup_sysid" ] && [ -n "$target_sysid" ] || die \
    "could not read the system identifier (backup='${backup_sysid:-unreadable}' \
target='${target_sysid:-unreadable}'); identity is unproven, so this run is not a pass."
[ "$backup_sysid" = "$target_sysid" ] || die \
    "the running cluster is NOT the one in this backup (backup system identifier ${backup_sysid}, \
running ${target_sysid}); the restore did not take effect and the drill is validating a different cluster."

# 2) Freshness. Identity alone still passes on last week's restore of the same source, so the
# control file's checkpoint - written by the recovery this run performed - must not predate the
# backup this run selected. The directory name is the backup's UTC start stamp.
# The stamp is ISO 8601 *basic* (20260803T185512Z), which GNU date refuses outright — it wants
# the extended form. Reshape it rather than parse it, so this does not depend on date's
# willingness to guess a format.
chain_stamp="${chain_newest%%_*}"
chain_iso="${chain_stamp:0:4}-${chain_stamp:4:2}-${chain_stamp:6:2} ${chain_stamp:9:2}:${chain_stamp:11:2}:${chain_stamp:13:2} UTC"
chain_epoch="$(date -u -d "${chain_iso}" +%s 2>/dev/null || echo 0)"
checkpoint_text="$(control_field "$pgdata" "Time of latest checkpoint")"
checkpoint_epoch="$(date -u -d "${checkpoint_text}" +%s 2>/dev/null || echo 0)"
printf 'chain_newest=%s checkpoint=%s\n' "$chain_newest" "${checkpoint_text:-unknown}"
[ "$chain_epoch" -gt 0 ] && [ "$checkpoint_epoch" -gt 0 ] || die \
    "could not read a comparable checkpoint time (chain=${chain_newest} \
checkpoint='${checkpoint_text:-unreadable}'); freshness is unproven, so this run is not a pass."
[ "$checkpoint_epoch" -ge "$chain_epoch" ] || die \
    "the running cluster predates the backup this run restored (checkpoint ${checkpoint_text} < \
backup ${chain_newest}); the data directory in use was NOT produced by this restore."
printf 'freshness=FRESH identity_matches_and_checkpoint_is_at_or_after_backup\n'

printf '\nRESULT=ok restore_id=%s target=%s version=%s databases=%s relations=%s system_identifier=%s checkpoint=%s\n' \
    "$restore_id" "$target_container" "$version" "$databases" "$relations" "$target_sysid" "$checkpoint_text"
