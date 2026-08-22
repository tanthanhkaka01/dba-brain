#!/usr/bin/env bash
# Entrypoint for the DBA Ops Assistant container.
#
#   daemon [args...]  run the app-command scheduler (default CMD). Extra args are
#                     forwarded to the daemon, e.g. the decryption passphrase:
#                       docker compose run -d --name db_ops_daemon db_ops \
#                         daemon --key "<passphrase>"
#   shell             drop into an interactive bash shell
#   <command...>      exec verbatim, e.g.:
#                       docker run --rm db_ops \
#                         python -m db_ops.metrics.cli --config config.json collect --dry-run --key "<passphrase>"
#
# DB_OPS_CONFIG       config path passed to the daemon (default: config.json)
# DB_OPS_DAEMON_DELAY daemon scan delay in seconds (default: 2)
#
# The decryption passphrase is supplied at runtime via --key and is never stored
# in the image, compose file, or any env file.
set -euo pipefail

CONFIG="${DB_OPS_CONFIG:-config.json}"

# The image runs as a non-root user (uid 10001). `data/`, `logs/` and `runtime/` are bind mounts
# owned by whoever created them on the host, so an unwritable one is the first thing that goes
# wrong — and it surfaces as a traceback from whichever component wrote first, which reads as a
# broken toolkit rather than as a permission. Said once, here, with the fix in it.
for required in logs runtime; do
  if [ -d "$required" ] && [ ! -w "$required" ]; then
    cat >&2 <<UNWRITABLE
$required/ is not writable by uid $(id -u), which is what this image runs as.

It is a bind mount, so the host owns it. Either:
  chown -R $(id -u):$(id -g) <host path>      # give this user the directory
  docker run --user 0:0 ...                   # or run as root, as older images did
UNWRITABLE
    exit 1
  fi
done

case "${1:-daemon}" in
  daemon)
    shift || true
    exec python -m db_ops.jobs.daemon --config "$CONFIG" --delay-seconds "${DB_OPS_DAEMON_DELAY:-2}" "$@"
    ;;
  shell)
    exec /bin/bash
    ;;
  *)
    exec "$@"
    ;;
esac
