#!/bin/bash
# Stop one running backup on this Linux host (0.34.0, 1.238) - common.cli backup-stop runs it.
#
# Order matters. On a production SQL Server (2026-10-29) the engine session doing the backup was killed first;
# the script driving it reconnected a second later and backed up the next database, 548 GB, for
# another hour. So:
#   1. the run's own process tree on this host - the backup script, under the pid it wrote to
#      ${TMPDIR:-/tmp}/dbabrain_runs/<tag>.pid - so nothing is left to start the next database;
#   2. then, for SQL Server, every session the run opened: its sqlcmd named each one
#      host_name = dbabrain:<tag>, so a native job's backup on the same instance is never touched;
#   3. then what is left, said.
#
# A pid is ended only when that process started before the pid file was written: a file left behind
# by a run that died must not end whatever process has that number now.
#
# Env: DBABRAIN_RUN_TAG (required), DB_TYPE, and for sqlserver DOCKER_CONTAINER (optional),
# MSSQL_USER, MSSQL_PASSWORD - the run's own login.
# Prints STOP<TAB>key=value lines, then RESULT=ok.

set -u

die() { printf 'RESULT=error reason=%s\n' "$1" >&2; exit 2; }

tag="${DBABRAIN_RUN_TAG:-}"
case "$tag" in
    ''|*[!A-Za-z0-9_.-]*) die "DBABRAIN_RUN_TAG is missing or holds a character outside A-Z a-z 0-9 _ . -" ;;
esac

# Children first, then the parent: a parent ended first would leave its children to init.
tree() {
    local child
    for child in $(pgrep -P "$1" 2>/dev/null); do tree "$child"; done
    printf '%s\n' "$1"
}

run_file="${TMPDIR:-/tmp}/dbabrain_runs/${tag}.pid"
if [ -f "$run_file" ]; then
    pid="$(tr -dc '0-9' < "$run_file")"
    printf 'STOP\tpid=%s\n' "$pid"
    written="$(stat -c %Y "$run_file" 2>/dev/null || echo 0)"
    started=""
    if [ -n "$pid" ] && [ "$pid" != "$$" ]; then
        started="$(date -d "$(ps -o lstart= -p "$pid" 2>/dev/null)" +%s 2>/dev/null || true)"
    fi
    if [ -n "$started" ] && [ "$started" -le "$written" ]; then
        procs="$(tree "$pid")"
        for p in $procs; do
            kill -TERM "$p" 2>/dev/null && printf 'STOP\tkilled=%s\n' "$p"
        done
        sleep 3
        for p in $procs; do
            kill -0 "$p" 2>/dev/null && kill -KILL "$p" 2>/dev/null
        done
        sleep 1
        if kill -0 "$pid" 2>/dev/null; then printf 'STOP\talive=1\n'; else printf 'STOP\talive=0\n'; fi
    else
        # Not running, or the number belongs to another process now.
        printf 'STOP\talive=0\n'
    fi
    rm -f "$run_file"
else
    printf 'STOP\tpid_file=missing\n'
fi

if [ "${DB_TYPE:-}" = "sqlserver" ]; then
    container="${DOCKER_CONTAINER:-}"
    mssql_user="${MSSQL_USER:-}"
    [ -n "$mssql_user" ] || die "MSSQL_USER is not set: the run's sessions cannot be asked for."
    export SQLCMDPASSWORD="${MSSQL_PASSWORD:-}"
    if [ -n "$container" ]; then
        DOCKER="docker"
        $DOCKER info >/dev/null 2>&1 || DOCKER="sudo docker"
        # A stopped container holds no session: nothing to end there, and an exec would only fail.
        if [ "$($DOCKER inspect -f '{{.State.Running}}' "$container" 2>/dev/null)" != "true" ]; then
            printf 'STOP\tcontainer_running=0\n'
            printf 'RESULT=ok\n'
            exit 0
        fi
        exec_here() { $DOCKER exec -i -e SQLCMDPASSWORD "$container" "$@" < /dev/null; }
        probe_here() { $DOCKER exec "$container" sh -c "$1" >/dev/null 2>&1; }
    else
        exec_here() { "$@" < /dev/null; }
        probe_here() { sh -c "$1" >/dev/null 2>&1; }
    fi
    sqlcmd_bin=""
    for candidate in /opt/mssql-tools18/bin/sqlcmd /opt/mssql-tools/bin/sqlcmd sqlcmd; do
        if probe_here "test -x '$candidate'" || probe_here "command -v $candidate"; then
            sqlcmd_bin="$candidate"; break
        fi
    done
    [ -n "$sqlcmd_bin" ] || die "no sqlcmd found to end the run's sessions."
    rows() {
        exec_here "$sqlcmd_bin" -C -b -h -1 -W -S localhost -U "$mssql_user" -Q "SET NOCOUNT ON;
SELECT CAST(s.session_id AS varchar(10)) + '|' + ISNULL(DB_NAME(r.database_id), '')
FROM sys.dm_exec_sessions s LEFT JOIN sys.dm_exec_requests r ON r.session_id = s.session_id
WHERE s.host_name = 'dbabrain:${tag}' AND s.session_id <> @@SPID;"
    }
    for row in $(rows); do
        spid="${row%%|*}"
        case "$spid" in ''|*[!0-9]*) continue ;; esac
        exec_here "$sqlcmd_bin" -C -b -S localhost -U "$mssql_user" -Q "KILL ${spid};" >/dev/null 2>&1 \
            && printf 'STOP\tsession_killed=%s\tdb=%s\n' "$spid" "${row#*|}"
    done
    sleep 3
    for row in $(rows); do
        spid="${row%%|*}"
        case "$spid" in ''|*[!0-9]*) continue ;; esac
        printf 'STOP\tsession_left=%s\tdb=%s\n' "$spid" "${row#*|}"
    done
fi

printf 'RESULT=ok\n'
