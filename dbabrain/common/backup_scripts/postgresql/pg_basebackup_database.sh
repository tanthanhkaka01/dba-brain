#!/bin/bash
# PostgreSQL base backup - FULL or incremental, the level chosen by whoever calls it.
#
# $BACKUP_LEVEL says which: `full` takes a baseline, `incr` chains onto the most recent backup
# (PostgreSQL 17+ `pg_basebackup --incremental`). dbabrain sets it from the job's own configuration,
# so "the weekly full on Sunday" is `time_window.weekdays: [7]` on a job pinning BACKUP_LEVEL=full
# beside one pinning `incr` on the other six days - it is no longer a weekday literal in here.
#
# Runs on the machine the cluster is on, shipped over SSH by dbabrain.backup_restore.backup. With
# $DOCKER_CONTAINER set it `docker exec`s into that container; without it, it runs the same commands
# on the host as $PG_OS_USER. Every database command goes through one function either way, so the
# two paths cannot drift: `run_db` is defined once, twice.
#
# Until 0.21.0 the container was mandatory, and an engine installed directly on a host could not be
# backed up at all - the failure being `DOCKER_CONTAINER is not set`, which reads like a
# configuration mistake rather than a missing capability.
#
# Layout, one directory per backup:
#   $BACKUP_DIR/base/<UTC timestamp>_FULL      full baseline
#   $BACKUP_DIR/base/<UTC timestamp>_INCR      incremental, chained to the previous backup
#
# Each directory holds the backup in TAR format, compressed ON THE SERVER (0.37.0, the operator
# 2026-11-10: *switch to tar, compressed*): base.tar.zst, pg_wal.tar (the WAL streamed during the
# backup), one <oid>.tar.zst per extra tablespace, and backup_manifest beside them. It used to be the
# plain format - the whole data directory, byte for byte, uncompressed: the store's 65 GB cluster
# was 5,904 files and 65 GB a night, where its data compresses five to thirteen times. The plain
# format cannot be compressed at all. A server older than 15 has no server-side compression and
# still gets the plain format (said in the output); a restore reads both.
#
# Retention deletes whole *chains*, never single backups: an incremental is worthless without the
# full it descends from, so a chain is dropped only once its newest member is older than
# $RETENTION_DAYS. Deleting by age alone would happily remove the full that the retained
# incrementals still need - a backup set that looks present and cannot be restored.
#
# Env: DOCKER_CONTAINER (OPTIONAL since 0.21.0 - set it to reach a cluster inside a container,
#      leave it unset for one installed on the host itself), BACKUP_DIR (required; inside the
#      container when DOCKER_CONTAINER is set, on the host otherwise),
#      RETENTION_DAYS (default 14), RETENTION_SECONDS (optional; a window under a day, which
#      wins over RETENTION_DAYS), BACKUP_LEVEL (optional full|incr override for manual runs),
#      PG_USER (default postgres), PG_OS_USER (OS user inside the container, default postgres),
#      PG_PORT (optional; the image's default when unset - set it when the container runs the
#      cluster on a non-default port, or runs more than one).
# Exit: 0 on success, non-zero on failure. Prints RESULT=ok only on a completed run.
# Receipt (0.34.0): one `FILE` line for the backup directory - the sha256 of its backup_manifest,
# which holds a checksum of every file, and pg_verifybackup's verdict on it.
set -u

container="${DOCKER_CONTAINER:-}"
backup_dir="${BACKUP_DIR:-}"
retention_days="${RETENTION_DAYS:-14}"
retention_seconds="${RETENTION_SECONDS:-}"
level_override="${BACKUP_LEVEL:-}"
pg_user="${PG_USER:-postgres}"
pg_os_user="${PG_OS_USER:-postgres}"
pg_port="${PG_PORT:-}"

# Every client call inside the container goes through this, so the port cannot be threaded into
# three of the four and forgotten in the fourth.
#
# It is an ARGUMENT, not an exported variable, because `docker exec` does not carry this script's
# environment into the container: a PGPORT set on the job would reach the host and never reach
# psql, which would then connect to 5432 and either fail or - on a host running two clusters -
# quietly back up the wrong one. Empty means "the image's default", which is what every entry in
# this estate wants.
pg_conn="-U '${pg_user}'"
[ -n "$pg_port" ] && pg_conn="${pg_conn} -p '${pg_port}'"

die() { printf 'RESULT=error reason=%s\n' "$1" >&2; exit 1; }

# Where a stop finds this run (0.34.0, 1.238): this shell's pid, under the run's tag, removed on exit.
# backup-stop ends the tree under it - killing the engine session alone left the script to start the
# next database a second later (a production SQL Server, 2026-10-29).
if [ -n "${DBABRAIN_RUN_TAG:-}" ]; then
    run_file="${TMPDIR:-/tmp}/dbabrain_runs/${DBABRAIN_RUN_TAG}.pid"
    mkdir -p "${run_file%/*}" 2>/dev/null && printf '%s\n' "$$" > "$run_file" 2>/dev/null \
        && trap 'rm -f "$run_file"' EXIT
fi

[ -n "$backup_dir" ] || die "BACKUP_DIR is not set."
case "$retention_days" in
    ''|*[!0-9]*) die "RETENTION_DAYS must be a whole number of days: '${retention_days}'." ;;
esac
# A window under a day arrives in seconds, because days cannot say it: unset, a two-hour lab
# retention ran on the 14-day default and no chain was ever dropped (2026-10-02). The sweep below
# is the same one - whole chains, judged on the UTC stamp in their names - with a nearer cutoff.
retention_window="${retention_days} days ago"
if [ -n "$retention_seconds" ]; then
    case "$retention_seconds" in
        *[!0-9]*) die "RETENTION_SECONDS must be a whole number of seconds: '${retention_seconds}'." ;;
    esac
    retention_window="${retention_seconds} seconds ago"
fi
# Per file type (0.35.0, 1.229): the base chains by FULL_BASE's own window, when the job states it.
# keep = no sweep. An INCR goes with its chain, so there is no INCR window to read.
sweep_bases=1
if [ -n "${RETENTION_FULL_BASE_SECONDS:-}" ]; then
    case "$RETENTION_FULL_BASE_SECONDS" in
        keep) sweep_bases="" ;;
        *[!0-9]*) die "RETENTION_FULL_BASE_SECONDS must be seconds (at least 60) or keep: '${RETENTION_FULL_BASE_SECONDS}'." ;;
        *) [ "$RETENTION_FULL_BASE_SECONDS" -ge 60 ] \
               || die "RETENTION_FULL_BASE_SECONDS must be at least 60, or keep: '${RETENTION_FULL_BASE_SECONDS}'."
           retention_window="${RETENTION_FULL_BASE_SECONDS} seconds ago" ;;
    esac
fi
# Unset is the normal case and means the image's default. A non-numeric value is not: it would be
# pasted into the psql argument list and produce a connection error naming the port, four calls in.
case "$pg_port" in
    ''|*[!0-9]*) [ -z "$pg_port" ] || die "PG_PORT must be a port number: '${pg_port}'." ;;
esac

# One function, two definitions, chosen once here. Everything below calls `run_db` and neither knows
# nor cares which of the two it got - which is the point: a container-only step added later would
# work in testing and fail on a host install, and the reverse.
if [ -n "$container" ]; then
    # docker CLI: plain first, fall back to sudo (host user may not be in the docker group).
    DOCKER="docker"
    $DOCKER info >/dev/null 2>&1 || DOCKER="sudo docker"
    $DOCKER inspect "$container" >/dev/null 2>&1 \
        || die "container '${container}' not found or docker unavailable on host."
    # Present is not running. A stopped container used to fail further on, as whatever the first
    # exec could not do - "no sqlcmd found (container X); install mssql-tools" on 2026-09-24 -
    # which sends the reader to install tools that are there.
    [ "$($DOCKER inspect -f '{{.State.Running}}' "$container" 2>/dev/null)" = "true" ] \
        || die "container '${container}' is not running - start it (docker start ${container}) and run the backup again."

    # -u $pg_os_user, not root. `docker exec` on the postgres image lands as root, and a backup
    # taken that way is written root-owned with 0700 - unreadable to the postgres user, so
    # pg_combinebackup and pg_verifybackup fail and PostgreSQL refuses to start on the restored
    # directory. The backup would look present and be unrestorable.
    #
    # No -i, and stdin closed: this script is fed to `bash -s` over SSH, so a `docker exec -i`
    # without its own input would read the rest of this file as the container's stdin and the shell
    # would silently run out of script (exit 0, no output, nothing backed up).
    run_db() { $DOCKER exec -u "$pg_os_user" "$container" bash -lc "$1" </dev/null; }
    where="container ${container}"
else
    # Host-native. The same requirement as the -u above and for the same reason: the files must
    # belong to the database user, or the restore cannot read them. Already being that user is the
    # ordinary case when dbabrain connects as it; `su` covers the case where it connects as root.
    command -v pg_basebackup >/dev/null 2>&1 \
        || die "pg_basebackup is not on PATH and DOCKER_CONTAINER is not set, so there is no way to reach a cluster. Set DOCKER_CONTAINER for a containerised cluster, or install the PostgreSQL client tools on this host."
    if [ "$(id -un 2>/dev/null)" = "$pg_os_user" ]; then
        run_db() { bash -lc "$1" </dev/null; }
        where="host as ${pg_os_user}"
    else
        id "$pg_os_user" >/dev/null 2>&1 \
            || die "PG_OS_USER '${pg_os_user}' does not exist on this host."
        command -v su >/dev/null 2>&1 || die "su is not available, so this cannot drop to '${pg_os_user}'."
        run_db() { su -s /bin/bash "$pg_os_user" -c "$1" </dev/null; }
        where="host as ${pg_os_user} via su"
    fi
fi
printf 'reaching the cluster: %s\n' "$where"

base_dir="${backup_dir}/base"
# Root where the database runs, for the one thing its own user cannot do: take over a backup
# folder someone else made (see "writable by the database user" below). On a host it never
# prompts - a sudo that wants a password fails, and the folder is named.
if [ -n "$container" ]; then
    run_root() { $DOCKER exec -u 0 "$container" bash -lc "$1" </dev/null; }
else
    run_root() { sudo -n bash -lc "$1" </dev/null; }
fi

# Writable by the database user, not only by whoever made the folder above it. A lab's bind mount
# belongs to the SSH user, and on 2026-09-24 every backup into it failed "mkdir: Permission
# denied" until the folder was handed over by hand. Made as root where the database runs and
# given to the user it runs as (numeric ids: the su path cannot carry quotes) - the whole folder,
# because what is checked is the folder this job writes, which may sit one level below it.
if ! run_db "mkdir -p '${base_dir}' && test -w '${base_dir}'" 2>/dev/null; then
    engine_uid="$(run_db 'id -u' 2>/dev/null)"; engine_gid="$(run_db 'id -g' 2>/dev/null)"
    [ -n "$engine_uid" ] && run_root "mkdir -p '${backup_dir}' && chown -R ${engine_uid}:${engine_gid} '${backup_dir}'" >/dev/null 2>&1
fi

run_db "mkdir -p '${base_dir}'" || die "could not create '${base_dir}' inside '${container}'."

# Refuse to back up a standby: an incremental chain taken from a replica silently diverges from
# the primary's timeline after a failover.
in_recovery="$(run_db "psql ${pg_conn} -tAc 'select pg_is_in_recovery()'" 2>/dev/null | tr -d '[:space:]')"
[ "$in_recovery" = "f" ] || die "target is not a primary (pg_is_in_recovery=${in_recovery:-unknown})."

# TAR, compressed on the server, from PostgreSQL 15 (server-side compression's first release); the
# plain directory below it. Read from the server, not from this client: it is the server that
# compresses.
server_version_num="$(run_db "psql ${pg_conn} -tAc 'show server_version_num'" 2>/dev/null | tr -d '[:space:]')"
case "$server_version_num" in ''|*[!0-9]*) server_version_num=0 ;; esac
if [ "$server_version_num" -ge 150000 ]; then
    bb_format="-Ft --compress=server-zstd"
    format_name="tar, compressed on the server (zstd)"
else
    bb_format=""
    format_name="plain (PostgreSQL ${server_version_num} has no server-side compression)"
fi
printf 'backup format: %s\n' "$format_name"

# The newest existing backup is what an incremental chains onto.
latest="$(run_db "ls -1d ${base_dir}/*_FULL ${base_dir}/*_INCR 2>/dev/null | sort | tail -1" | tr -d '\r')"

# The level is the SCHEDULER's decision and arrives in $BACKUP_LEVEL. It used to be made here,
# with `[ "${DBABRAIN_WEEKDAY:-$(date +%u)}" = "7" ]`, and that was the bug: dbabrain only began
# passing $DBABRAIN_WEEKDAY in 0.20.0, so every older node fell through to `date +%u` on the
# CONTAINER HOST. On a host set to UTC that is a different day from the one the scheduler used for
# the hour window - one backup, two clocks. Measured on 2026-09-19: the host said Saturday, the
# node's own +07 said Sunday, so five incrementals ran and failed where the weekly full was due.
#
# Now the weekday is config (`time_window.weekdays`, ISO 1-7) and the window rule reads it on the
# node's configured clock, the same clock as `from_hour`. A run with no $BACKUP_LEVEL is a manual
# one; it takes the incremental, and the baseline rule below still makes the first ever run a FULL.
if [ -n "$level_override" ]; then
    case "$level_override" in
        full|FULL|0) level="FULL" ;;
        incr|INCR|1) level="INCR" ;;
        *) die "BACKUP_LEVEL must be full or incr: '${level_override}'." ;;
    esac
else
    level="INCR"
fi

# An incremental with nothing to chain onto is not an error to fail on - it is the first ever
# run (or the first after a retention sweep). Take the baseline instead of failing every day
# until the next Sunday.
if [ "$level" = "INCR" ] && [ -z "$latest" ]; then
    printf 'no prior backup found; taking a FULL baseline instead of an incremental\n'
    level="FULL"
fi

# An incremental needs WAL summaries, and summaries only exist while summarize_wal is on. Asking
# first turns a certain refusal - and the checkpoint it forces, and the "aborting backup due to
# backend exiting before pg_backup_stop was called" it leaves in the server log - into a baseline
# taken on purpose.
if [ "$level" = "INCR" ]; then
    summarize="$(run_db "psql ${pg_conn} -tAc 'show summarize_wal'" 2>/dev/null | tr -d '[:space:]')"
    if [ "$summarize" != "on" ]; then
        printf 'summarize_wal=%s, so an incremental is impossible; taking a FULL baseline instead\n' \
            "${summarize:-unknown}"
        level="FULL"
    fi
fi

# An incremental must chain onto a backup of the SAME cluster. A cluster that has been
# restored, re-initdb'd or replaced gets a new system identifier, and the server then refuses
# the parent manifest outright - permanently, because no retry changes which cluster the parent
# came from. Without this the refusal lands in the generic failure path below and the job
# answers "error" every night, leaving base/ untouched, exactly as the WAL-summaries case did
# for five nights. Reading the identifier off the parent first turns a dead chain into one
# deliberate baseline.
if [ "$level" = "INCR" ]; then
    parent_sysid="$(run_db "grep -o '\"System-Identifier\": *[0-9]*' '${latest}/backup_manifest' 2>/dev/null | head -1 | grep -o '[0-9]*$'" | tr -d '[:space:]')"
    live_sysid="$(run_db "psql ${pg_conn} -tAc 'select system_identifier from pg_control_system()'" 2>/dev/null | tr -d '[:space:]')"
    if [ -z "$parent_sysid" ]; then
        # Manifests only carry the identifier from PostgreSQL 17, the release that added
        # --incremental. A parent without one predates it and cannot be chained to.
        printf 'parent %s carries no System-Identifier, so the chain cannot be verified; taking a FULL baseline instead\n' \
            "$latest"
        level="FULL"
    elif [ -n "$live_sysid" ] && [ "$parent_sysid" != "$live_sysid" ]; then
        printf 'parent %s belongs to cluster %s but this cluster is %s; the chain is dead, taking a FULL baseline instead\n' \
            "$latest" "$parent_sysid" "$live_sysid"
        level="FULL"
    fi
fi

# One attempt. The output is captured so a refusal can be classified below, and printed either
# way: dbabrain stores this as stdout_tail, and pg_basebackup's own message is the only thing that
# says WHY a backup did not happen.
take_backup() {
    level="$1"
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    target="${base_dir}/${stamp}_${level}"
    # -X stream: ship the WAL generated during the copy inside the backup, so each backup is
    # restorable on its own without reaching into the WAL archive.
    # -c fast: checkpoint immediately instead of waiting for the next scheduled one.
    # An incremental chains onto its parent's backup_manifest, which sits beside the tars as it sat
    # in the plain directory - so a tar incremental chains onto a plain parent as well.
    if [ "$level" = "FULL" ]; then
        bb_args="-D '${target}' ${bb_format} -X stream -c fast --manifest-checksums=CRC32C"
    else
        printf 'incremental_parent=%s\n' "$latest"
        bb_args="-D '${target}' ${bb_format} -X stream -c fast --manifest-checksums=CRC32C --incremental='${latest}/backup_manifest'"
    fi
    printf 'backup_level=%s where=%s target=%s retention_days=%s\n' \
        "$level" "$where" "$target" "$retention_days"
    # When the backup began (1.254), UTC - for the receipt.
    started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    bb_output="$(run_db "pg_basebackup ${pg_conn} ${bb_args} 2>&1")"
    bb_status=$?
    [ -n "$bb_output" ] && printf '%s\n' "$bb_output"
    return $bb_status
}

if ! take_backup "$level"; then
    run_db "rm -rf '${target}'" || true
    # An incremental the server refuses for want of WAL summaries CANNOT be retried into success:
    # summaries are only written while summarize_wal is on, and nothing can produce them for WAL
    # that has already been recycled. This estate met it on five consecutive nights (2026-09-19)
    # because the baseline predated summarize_wal being enabled - a job that answered "error" every
    # night and left base/ without a single new directory, so the night had no backup at all.
    # Taking the baseline now costs one full copy and gives tomorrow's incremental something it can
    # chain onto. Narrow on purpose: any OTHER failure still fails, because answering a full disk
    # by writing more is not a recovery.
    case "${level}:${bb_output}" in
        INCR:*"WAL summaries"*|INCR:*"system identifier"*)
            printf 'the server refused the incremental as unchainable; taking a FULL baseline instead\n'
            if ! take_backup FULL; then
                run_db "rm -rf '${target}'" || true
                die "pg_basebackup failed (level=FULL, taken after the incremental was refused); partial target removed."
            fi
            ;;
        *)
            die "pg_basebackup failed (level=${level}); partial target removed."
            ;;
    esac
fi

run_db "test -f '${target}/backup_manifest'" \
    || die "pg_basebackup left no backup_manifest in '${target}'."

# The file's path on this server, not inside the container (0.34.0): through the container's
# mount that holds it, the longest one first. Empty for a file in no mount - it then lives only in
# the container's own layer, gone with the container, which is worth the catalog saying.
_dbabrain_mounts=""
if [ -n "$container" ]; then
    _dbabrain_mounts="$($DOCKER inspect -f '{{range .Mounts}}{{.Destination}}|{{.Source}}{{"\n"}}{{end}}' "$container" 2>/dev/null)"
fi
host_path_of() {
    if [ -z "$container" ]; then printf '%s' "$1"; return 0; fi
    _best_dst=""; _best_src=""
    while IFS='|' read -r _dst _src; do
        [ -n "$_dst" ] || continue
        case "$1" in
            "$_dst"|"$_dst"/*)
                if [ "${#_dst}" -gt "${#_best_dst}" ]; then _best_dst="$_dst"; _best_src="$_src"; fi ;;
        esac
    done <<HOSTPATHS
$_dbabrain_mounts
HOSTPATHS
    if [ -n "$_best_dst" ]; then printf '%s%s' "$_best_src" "${1#"$_best_dst"}"; fi
    return 0
}

# --- The receipt (0.34.0): one FILE line for this backup ---------------------------------------
# A base backup is a directory - a few tars (or, in the plain format, the whole data directory) -
# and its backup_manifest carries a checksum of every data file. So the catalog records the backup
# as one row: its directory, the sha256 of its manifest, and pg_verifybackup's verdict, which
# checks every file against that manifest.
taken="${target##*_}"                       # FULL or INCR - the fallback above may have changed it
case "$taken" in FULL) file_type=FULL_BASE ;; *) file_type=INCR_BASE ;; esac
verify=not_run
verify_note=""
is_tar=no
run_db "ls '${target}'/base.tar* >/dev/null 2>&1" && is_tar=yes
# Found where it is, not only on PATH: the Debian/PGDG packages put a /usr/bin wrapper in front of
# pg_basebackup but none in front of pg_verifybackup, and a login shell (`bash -lc`) resets PATH -
# so on the store's container (2026-11-10) it was never on PATH, and every backup since the
# receipt was added had read "pg_verifybackup is not installed" while it sat in
# /usr/lib/postgresql/18/bin. The server's own major first, then any.
server_major=$(( server_version_num / 10000 ))
vb_bin="$(run_db "command -v pg_verifybackup || ls -1 /usr/lib/postgresql/${server_major}/bin/pg_verifybackup /usr/pgsql-${server_major}/bin/pg_verifybackup 2>/dev/null | head -1 || true" | head -1 | tr -d '\r')"
[ -n "$vb_bin" ] || vb_bin="$(run_db "ls -1 /usr/lib/postgresql/*/bin/pg_verifybackup /usr/pgsql-*/bin/pg_verifybackup 2>/dev/null | sort -V | tail -1" | tr -d '\r')"
vb_major=""
[ -n "$vb_bin" ] && vb_major="$(run_db "'${vb_bin}' --version 2>/dev/null" | grep -o '[0-9][0-9]*' | head -1)"
if [ -z "$vb_bin" ]; then
    verify_note="pg_verifybackup is not installed where the cluster runs"
elif [ "$is_tar" = "yes" ] && [ "${vb_major:-0}" -lt 18 ]; then
    # pg_verifybackup reads a tar backup from PostgreSQL 18 on; older ones only a plain directory.
    verify_note="tar backup: pg_verifybackup ${vb_major:-?} reads tar only from PostgreSQL 18 - not verified here"
else
    if [ "$is_tar" = "yes" ]; then
        # The WAL is inside pg_wal.tar, which pg_waldump cannot read in place: every data file in the
        # tars is checked against the manifest, the WAL is not parsed (-n) - said, not hidden.
        vb_out="$(run_db "'${vb_bin}' -n '${target}' 2>&1")"
        vb_rc=$?
        verify_note="tar backup: every file checked against the manifest; the WAL in pg_wal.tar not parsed"
    else
        vb_out="$(run_db "'${vb_bin}' '${target}' 2>&1")"
        vb_rc=$?
    fi
    # The WAL check runs pg_waldump. Where it is missing, every file is still checked against the
    # manifest (-n skips only the WAL) - a missing tool must not read as a broken backup.
    if [ "$is_tar" = "no" ] && [ "$vb_rc" -ne 0 ] && printf '%s' "$vb_out" | grep -q 'pg_waldump'; then
        vb_out="$(run_db "'${vb_bin}' -n '${target}' 2>&1")"
        vb_rc=$?
        verify_note="files checked against the manifest; WAL not parsed (no pg_waldump)"
    fi
    if [ "$vb_rc" -eq 0 ]; then
        verify=passed
    elif printf '%s' "$vb_out" | grep -qi 'incremental'; then
        # An incremental backup holds only the changed blocks; pg_verifybackup cannot judge it on
        # its own, and saying "failed" about a good backup would be a lie in the other direction.
        verify_note="pg_verifybackup does not verify an incremental backup on its own"
    else
        verify=failed
        verify_note="$(printf '%s' "$vb_out" | tr '\t\r\n' '   ' | cut -c1-300)"
        printf 'ERROR pg_verifybackup failed for %s: %s\n' "$target" "$vb_out" >&2
    fi
fi
manifest_sha="$(run_db "sha256sum '${target}/backup_manifest'" 2>/dev/null | cut -d' ' -f1 | tr -d '[:space:]')"
[ "${#manifest_sha}" -eq 64 ] || { printf 'warning: could not hash %s/backup_manifest\n' "$target" >&2; manifest_sha=""; }
target_bytes="$(run_db "du -sb '${target}' 2>/dev/null | cut -f1" | tr -d '[:space:]')"
# pg_basebackup has no encryption of its own: the backup is written unencrypted - the repository
# encrypts it (offload_encrypt).
printf 'FILE\tdb=%s\ttype=%s\tsize=%s\tsha256=%s\tverify=%s\tencrypted=no\tstart=%s\tnote=%s\thost_path=%s\tcontainer=%s\tpath=%s\n' \
    "cluster" "$file_type" "${target_bytes:-}" "$manifest_sha" "$verify" "${started:-}" "$verify_note" \
    "$(host_path_of "$target")" "$container" "$target"
[ "$verify" = "failed" ] && die "pg_verifybackup found the backup in '${target}' does not match its manifest."

# --- Retention: drop whole chains, newest-member-first ---------------------------------------
# A chain runs from a _FULL up to (not including) the next _FULL. Keep the chain if any member
# is within the window; the directory names sort chronologically, so one pass is enough.
cleanup=$(cat <<CLEANUP
set -u
cutoff=\$(date -u -d "${retention_window}" +%Y%m%dT%H%M%SZ 2>/dev/null) || exit 0
chain=""
newest=""
drop_chain() {
    [ -n "\$chain" ] || return 0
    if [ "\$newest" \< "\$cutoff" ]; then
        for d in \$chain; do
            echo "retention: removing \$d"
            rm -rf "\$d"
        done
    fi
}
for d in \$(ls -1d ${base_dir}/*_FULL ${base_dir}/*_INCR 2>/dev/null | sort); do
    case "\$d" in
        *_FULL) drop_chain; chain="\$d"; newest=\$(basename "\$d" | cut -d_ -f1) ;;
        *_INCR) chain="\$chain \$d"; newest=\$(basename "\$d" | cut -d_ -f1) ;;
    esac
done
drop_chain
CLEANUP
)
if [ -n "$sweep_bases" ]; then
    run_db "$cleanup" || printf 'warning: retention sweep reported an error\n' >&2
else
    printf 'retention: base chains kept (keep)\n'
fi

# Make the pieces readable to the host's SSH user. They are written 0600 by the database user,
# which is right for a local backup but blocks the cross-machine restore: the transfer reads the
# source over SFTP as an ordinary account that is neither the owner nor in its group, so a
# freshly written file is unreadable and the copy fails part-way with "Permission denied".
# Group ownership cannot fix it (the group is the database user's own), so this opens read to
# others. That is a deliberate trade: these are lab backups on a private host, and the
# alternative is a transfer that breaks every time the job writes a new file.
run_db "chmod -R a+rX '${backup_dir}'"     || printf 'warning: could not relax permissions on %s
' "${backup_dir}" >&2

size="$(run_db "du -sh '${target}' 2>/dev/null | cut -f1" | tr -d '\r')"
printf 'RESULT=ok backup_level=%s target=%s size=%s retention_days=%s\n' \
    "$level" "$target" "${size:-unknown}" "$retention_days"
